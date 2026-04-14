"""Tests for hook stubs — including a concurrent-writer stress test."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_hook(module: str, payload: dict, vault_path: Path) -> tuple[int, str, str]:
    env = os.environ.copy()
    env["LCM_VAULT_PATH"] = str(vault_path)
    env["PYTHONPATH"] = str(REPO_ROOT)
    proc = subprocess.run(
        [sys.executable, "-m", module],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_session_start_creates_session_row(tmp_path: Path):
    vault = tmp_path / "v.sqlite"
    rc, _, _ = _run_hook(
        "adapter.hooks.session_start",
        {"session_id": "sess-A", "cwd": str(tmp_path), "source": "startup"},
        vault,
    )
    assert rc == 0
    rows = sqlite3.connect(vault).execute(
        "SELECT session_id, agent_kind FROM sessions"
    ).fetchall()
    assert rows == [("sess-A", "claude-code")]


def test_user_prompt_submit_appends_message(tmp_path: Path):
    vault = tmp_path / "v.sqlite"
    _run_hook("adapter.hooks.session_start",
              {"session_id": "sess-B", "cwd": str(tmp_path)}, vault)
    _run_hook("adapter.hooks.user_prompt_submit",
              {"session_id": "sess-B", "prompt": "hello alpha bravo"}, vault)
    rows = sqlite3.connect(vault).execute(
        "SELECT role, content FROM messages WHERE session_id='sess-B'"
    ).fetchall()
    assert rows == [("user", "hello alpha bravo")]


def test_pre_tool_use_captures_file_snapshot(tmp_path: Path):
    vault = tmp_path / "v.sqlite"
    target = tmp_path / "hello.txt"
    target.write_text("contents-v1")
    _run_hook("adapter.hooks.session_start",
              {"session_id": "sess-C", "cwd": str(tmp_path)}, vault)
    _run_hook(
        "adapter.hooks.pre_tool_use",
        {
            "session_id": "sess-C",
            "tool_name": "Read",
            "tool_input": {"file_path": str(target)},
        },
        vault,
    )
    rows = sqlite3.connect(vault).execute(
        "SELECT file_path, op, content_blob FROM file_snapshots WHERE session_id='sess-C'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == str(target)
    assert rows[0][1] == "pre_read"
    assert rows[0][2] == b"contents-v1"


def test_post_tool_use_captures_tool_result(tmp_path: Path):
    vault = tmp_path / "v.sqlite"
    _run_hook("adapter.hooks.session_start",
              {"session_id": "sess-D", "cwd": str(tmp_path)}, vault)
    _run_hook(
        "adapter.hooks.post_tool_use",
        {
            "session_id": "sess-D",
            "tool_name": "Bash",
            "tool_input": {"command": "echo hi"},
            "tool_response": {"stdout": "hi\n", "exit_code": 0},
        },
        vault,
    )
    rows = sqlite3.connect(vault).execute(
        "SELECT role, tool_name, content FROM messages WHERE session_id='sess-D'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "tool"
    assert rows[0][1] == "Bash"
    assert "hi" in rows[0][2]


def test_session_end_sets_ended_at(tmp_path: Path):
    vault = tmp_path / "v.sqlite"
    _run_hook("adapter.hooks.session_start",
              {"session_id": "sess-E", "cwd": str(tmp_path)}, vault)
    _run_hook("adapter.hooks.session_end", {"session_id": "sess-E"}, vault)
    ended = sqlite3.connect(vault).execute(
        "SELECT ended_at FROM sessions WHERE session_id='sess-E'"
    ).fetchone()[0]
    assert ended is not None


def test_session_end_source_clear_writes_handoff_and_end_reason(tmp_path: Path):
    vault = tmp_path / "v.sqlite"
    cwd = str(tmp_path)
    # Arrange: open session A in project_key = sanitize_path(cwd)
    _run_hook(
        "adapter.hooks.session_start",
        {"session_id": "A", "cwd": cwd, "source": "startup"},
        vault,
    )
    # Act: SessionEnd with source=clear
    rc, _, _ = _run_hook(
        "adapter.hooks.session_end",
        {"session_id": "A", "cwd": cwd, "source": "clear"},
        vault,
    )
    assert rc == 0

    conn = sqlite3.connect(vault)
    # end_reason stamped
    assert conn.execute(
        "SELECT end_reason FROM sessions WHERE session_id='A'"
    ).fetchone() == ("clear",)
    # clear_handoff row present, keyed on project_key = sanitize_path(cwd)
    from claude_lcm.workspace import sanitize_path
    pk = sanitize_path(cwd)
    row = conn.execute(
        "SELECT ending_session_id FROM clear_handoff WHERE project_key=?",
        (pk,),
    ).fetchone()
    assert row == ("A",)


def test_session_end_source_normal_sets_end_reason_normal(tmp_path: Path):
    vault = tmp_path / "v.sqlite"
    _run_hook(
        "adapter.hooks.session_start",
        {"session_id": "A", "cwd": str(tmp_path), "source": "startup"},
        vault,
    )
    _run_hook(
        "adapter.hooks.session_end",
        {"session_id": "A", "cwd": str(tmp_path), "source": "exit"},
        vault,
    )
    row = sqlite3.connect(vault).execute(
        "SELECT end_reason FROM sessions WHERE session_id='A'"
    ).fetchone()
    assert row == ("normal",)
    # No handoff row
    cnt = sqlite3.connect(vault).execute(
        "SELECT COUNT(*) FROM clear_handoff"
    ).fetchone()[0]
    assert cnt == 0


def test_session_start_stamps_project_key(tmp_path: Path):
    vault = tmp_path / "v.sqlite"
    _run_hook(
        "adapter.hooks.session_start",
        {"session_id": "A", "cwd": str(tmp_path), "source": "startup"},
        vault,
    )
    from claude_lcm.workspace import sanitize_path
    row = sqlite3.connect(vault).execute(
        "SELECT project_key FROM sessions WHERE session_id='A'"
    ).fetchone()
    assert row == (sanitize_path(str(tmp_path)),)


def test_session_start_source_clear_links_parent_and_drains_handoff(tmp_path: Path):
    vault = tmp_path / "v.sqlite"
    cwd = str(tmp_path)
    _run_hook("adapter.hooks.session_start",
              {"session_id": "A", "cwd": cwd, "source": "startup"}, vault)
    _run_hook("adapter.hooks.session_end",
              {"session_id": "A", "cwd": cwd, "source": "clear"}, vault)
    _run_hook("adapter.hooks.session_start",
              {"session_id": "B", "cwd": cwd, "source": "clear"}, vault)

    conn = sqlite3.connect(vault)
    row = conn.execute(
        "SELECT parent_session_id FROM sessions WHERE session_id='B'"
    ).fetchone()
    assert row == ("A",)
    # handoff consumed
    cnt = conn.execute("SELECT COUNT(*) FROM clear_handoff").fetchone()[0]
    assert cnt == 0


def test_session_start_source_clear_no_handoff_is_no_op(tmp_path: Path):
    vault = tmp_path / "v.sqlite"
    # Fresh vault, SessionStart(source=clear) with no prior SessionEnd.
    _run_hook("adapter.hooks.session_start",
              {"session_id": "B", "cwd": str(tmp_path), "source": "clear"},
              vault)
    row = sqlite3.connect(vault).execute(
        "SELECT parent_session_id FROM sessions WHERE session_id='B'"
    ).fetchone()
    assert row == (None,)


def test_concurrent_writers(tmp_path: Path):
    """Spawn 5 parallel hook processes against the same vault.

    Verifies WAL mode + busy_timeout handle concurrent inserts without
    raising "database is locked".
    """
    vault = tmp_path / "v.sqlite"
    # open the session first so every writer has a parent row
    _run_hook("adapter.hooks.session_start",
              {"session_id": "stress-1", "cwd": str(tmp_path)}, vault)

    procs = []
    env = os.environ.copy()
    env["LCM_VAULT_PATH"] = str(vault)
    env["PYTHONPATH"] = str(REPO_ROOT)
    for i in range(5):
        p = subprocess.Popen(
            [sys.executable, "-m", "adapter.hooks.user_prompt_submit"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
        )
        procs.append((p, i))
    for p, i in procs:
        out, err = p.communicate(
            input=json.dumps({"session_id": "stress-1", "prompt": f"message-{i}"}),
            timeout=15,
        )
        assert p.returncode == 0, f"worker {i} failed: {err}"

    rows = sqlite3.connect(vault).execute(
        "SELECT COUNT(*) FROM messages WHERE session_id='stress-1'"
    ).fetchone()
    assert rows[0] == 5
