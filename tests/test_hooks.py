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
