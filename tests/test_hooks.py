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


def test_clear_chain_end_to_end(tmp_path: Path):
    """Simulate A -> /clear -> B -> /clear -> C via subprocess hooks."""
    vault = tmp_path / "v.sqlite"
    cwd = str(tmp_path)

    # A starts and ends via /clear
    _run_hook("adapter.hooks.session_start",
              {"session_id": "A", "cwd": cwd, "source": "startup"}, vault)
    _run_hook("adapter.hooks.user_prompt_submit",
              {"session_id": "A", "prompt": "first message in A"}, vault)
    _run_hook("adapter.hooks.session_end",
              {"session_id": "A", "cwd": cwd, "source": "clear"}, vault)

    # B starts via /clear, ends via /clear
    _run_hook("adapter.hooks.session_start",
              {"session_id": "B", "cwd": cwd, "source": "clear"}, vault)
    _run_hook("adapter.hooks.user_prompt_submit",
              {"session_id": "B", "prompt": "first message in B"}, vault)
    _run_hook("adapter.hooks.session_end",
              {"session_id": "B", "cwd": cwd, "source": "clear"}, vault)

    # C starts via /clear
    _run_hook("adapter.hooks.session_start",
              {"session_id": "C", "cwd": cwd, "source": "clear"}, vault)

    # Lineage walk from C
    from claude_lcm.store import MessageStore
    store = MessageStore(vault)
    assert store.walk_lineage("C") == ["C", "B", "A"]
    # Handoff drained
    from claude_lcm.workspace import sanitize_path
    assert store.take_clear_handoff(sanitize_path(cwd)) is None
    store.close()


def test_clear_chains_do_not_cross_projects(tmp_path: Path):
    vault = tmp_path / "v.sqlite"
    proj_x = tmp_path / "x"; proj_x.mkdir()
    proj_y = tmp_path / "y"; proj_y.mkdir()

    _run_hook("adapter.hooks.session_start",
              {"session_id": "A", "cwd": str(proj_x), "source": "startup"}, vault)
    _run_hook("adapter.hooks.session_end",
              {"session_id": "A", "cwd": str(proj_x), "source": "clear"}, vault)

    # A /clear in project Y should see no handoff from X
    _run_hook("adapter.hooks.session_start",
              {"session_id": "B", "cwd": str(proj_y), "source": "clear"}, vault)
    row = sqlite3.connect(vault).execute(
        "SELECT parent_session_id FROM sessions WHERE session_id='B'"
    ).fetchone()
    assert row == (None,)

    # And the original X handoff is still intact for X's own next session
    _run_hook("adapter.hooks.session_start",
              {"session_id": "A2", "cwd": str(proj_x), "source": "clear"}, vault)
    row = sqlite3.connect(vault).execute(
        "SELECT parent_session_id FROM sessions WHERE session_id='A2'"
    ).fetchone()
    assert row == ("A",)


def test_fresh_start_auto_links_to_prior_session(tmp_path: Path):
    """A plain `claude` start (no /clear) auto-links to the most recent prior session.

    This covers the case where the user kills CC and restarts without --resume.
    The new session should be linked so that lcm_recent scope=lineage works.
    """
    vault = tmp_path / "v.sqlite"
    cwd = str(tmp_path)

    # Session A: a normal session that ended without /clear
    _run_hook("adapter.hooks.session_start",
              {"session_id": "A", "cwd": cwd, "source": "startup"}, vault)
    _run_hook("adapter.hooks.user_prompt_submit",
              {"session_id": "A", "prompt": "hello from A"}, vault)
    # No SessionEnd / no clear handoff — simulates killing the process

    # Session B: fresh `claude` start (source omitted / None)
    _run_hook("adapter.hooks.session_start",
              {"session_id": "B", "cwd": cwd}, vault)

    row = sqlite3.connect(vault).execute(
        "SELECT parent_session_id FROM sessions WHERE session_id='B'"
    ).fetchone()
    assert row == ("A",), f"expected parent=A, got {row}"


def test_fresh_start_does_not_cross_projects(tmp_path: Path):
    """Auto-link must not pick up sessions from a different workspace."""
    vault = tmp_path / "v.sqlite"
    proj_x = tmp_path / "x"; proj_x.mkdir()
    proj_y = tmp_path / "y"; proj_y.mkdir()

    _run_hook("adapter.hooks.session_start",
              {"session_id": "A", "cwd": str(proj_x), "source": "startup"}, vault)

    # B starts fresh in project Y — must not link to A (different project)
    _run_hook("adapter.hooks.session_start",
              {"session_id": "B", "cwd": str(proj_y)}, vault)
    row = sqlite3.connect(vault).execute(
        "SELECT parent_session_id FROM sessions WHERE session_id='B'"
    ).fetchone()
    assert row == (None,)


# ---------------------------------------------------------------------------
# Recall-intent injection tests
# ---------------------------------------------------------------------------

def test_recall_intent_injects_messages(tmp_path: Path):
    """When the prompt contains a recall phrase, additionalContext includes vault messages."""
    vault = tmp_path / "v.sqlite"
    cwd = str(tmp_path)

    # Seed a prior session with a message
    _run_hook("adapter.hooks.session_start",
              {"session_id": "A", "cwd": cwd, "source": "startup"}, vault)
    _run_hook("adapter.hooks.user_prompt_submit",
              {"session_id": "A", "prompt": "hello from session A"}, vault)

    # New session auto-linked to A
    _run_hook("adapter.hooks.session_start",
              {"session_id": "B", "cwd": cwd}, vault)

    # Recall-intent prompt in session B
    _, stdout, _ = _run_hook("adapter.hooks.user_prompt_submit",
                              {"session_id": "B", "prompt": "remember our last 5 messages"}, vault)

    out = json.loads(stdout)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "lcm:" in ctx, "should include lcm header"
    assert "hello from session A" in ctx, "should include prior message content"


def test_non_recall_prompt_does_not_inject(tmp_path: Path):
    """A regular prompt must not inject message history."""
    vault = tmp_path / "v.sqlite"
    cwd = str(tmp_path)

    _run_hook("adapter.hooks.session_start",
              {"session_id": "A", "cwd": cwd}, vault)
    _run_hook("adapter.hooks.user_prompt_submit",
              {"session_id": "A", "prompt": "what is 2+2?"}, vault)

    _, stdout, _ = _run_hook("adapter.hooks.user_prompt_submit",
                              {"session_id": "A", "prompt": "explain this function"}, vault)
    out = json.loads(stdout)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "lcm:" not in ctx or "recent messages" not in ctx


@pytest.mark.parametrize("prompt", [
    "remember our past 10 messages",
    "recall what we were doing",
    "catch me up",
    "what were we working on?",
    "what did we decide about the schema?",
    "restore context please",
    "show me the last 20 messages",
])
def test_recall_patterns_detected(prompt: str):
    """All recall-intent phrases should be detected."""
    from adapter.hooks.user_prompt_submit import _is_recall_intent
    assert _is_recall_intent(prompt), f"should detect recall in: {prompt!r}"


def test_limit_extraction():
    """Numeric limit should be parsed from the prompt."""
    from adapter.hooks.user_prompt_submit import _extract_limit
    assert _extract_limit("remember our last 30 messages") == 30
    assert _extract_limit("past 7 messages please") == 7
    assert _extract_limit("catch me up") == 20  # default


# ---------------------------------------------------------------------------
# Transcript sync (assistant text/thinking + subagents) — Stop / SessionEnd
# ---------------------------------------------------------------------------

def _transcript_line(entry_type: str, role: str, content) -> str:
    return json.dumps({
        "type": entry_type,
        "message": {"role": role, "content": content},
        "timestamp": "2026-07-15T10:00:00.000Z",
    }) + "\n"


def test_stop_ingests_assistant_text_and_thinking(tmp_path: Path):
    vault = tmp_path / "v.sqlite"
    transcript = tmp_path / "sess-T.jsonl"
    transcript.write_text(
        _transcript_line("assistant", "assistant", [
            {"type": "thinking", "thinking": "pondering the approach"},
        ])
        + _transcript_line("assistant", "assistant", [
            {"type": "text", "text": "Here is my reply"},
        ])
    )

    _run_hook("adapter.hooks.session_start",
              {"session_id": "sess-T", "cwd": str(tmp_path)}, vault)
    _run_hook("adapter.hooks.stop",
              {"session_id": "sess-T", "transcript_path": str(transcript)}, vault)

    rows = sqlite3.connect(vault).execute(
        "SELECT role, content FROM messages WHERE session_id='sess-T' "
        "AND role IN ('assistant', 'assistant_thinking') ORDER BY store_id"
    ).fetchall()
    assert rows == [
        ("assistant_thinking", "pondering the approach"),
        ("assistant", "Here is my reply"),
    ]

    # Re-firing Stop with an unchanged transcript must not duplicate rows.
    _run_hook("adapter.hooks.stop",
              {"session_id": "sess-T", "transcript_path": str(transcript)}, vault)
    rows_again = sqlite3.connect(vault).execute(
        "SELECT role, content FROM messages WHERE session_id='sess-T' "
        "AND role IN ('assistant', 'assistant_thinking') ORDER BY store_id"
    ).fetchall()
    assert rows_again == rows

    # Appending a new line and firing Stop again ingests only the delta.
    with transcript.open("a") as f:
        f.write(_transcript_line("assistant", "assistant", [
            {"type": "text", "text": "Second reply"},
        ]))
    _run_hook("adapter.hooks.stop",
              {"session_id": "sess-T", "transcript_path": str(transcript)}, vault)
    rows_final = sqlite3.connect(vault).execute(
        "SELECT role, content FROM messages WHERE session_id='sess-T' "
        "AND role IN ('assistant', 'assistant_thinking') ORDER BY store_id"
    ).fetchall()
    assert rows_final == rows + [("assistant", "Second reply")]


def test_stop_ingests_subagent_transcript(tmp_path: Path):
    vault = tmp_path / "v.sqlite"
    transcript = tmp_path / "sess-S.jsonl"
    transcript.write_text("")  # main thread has nothing new this turn

    subagents_dir = tmp_path / "sess-S" / "subagents"
    subagents_dir.mkdir(parents=True)
    agent_file = subagents_dir / "agent-a1.jsonl"
    agent_file.write_text(
        _transcript_line("user", "user", "Investigate the bug")
        + _transcript_line("assistant", "assistant", [
            {"type": "text", "text": "Found it in foo.py"},
        ])
    )

    _run_hook("adapter.hooks.session_start",
              {"session_id": "sess-S", "cwd": str(tmp_path)}, vault)
    _run_hook("adapter.hooks.stop",
              {"session_id": "sess-S", "transcript_path": str(transcript)}, vault)

    rows = sqlite3.connect(vault).execute(
        "SELECT role, content, agent_id FROM messages WHERE session_id='sess-S' "
        "AND agent_id IS NOT NULL ORDER BY store_id"
    ).fetchall()
    assert rows == [
        ("user", "Investigate the bug", "agent-a1"),
        ("assistant", "Found it in foo.py", "agent-a1"),
    ]


def test_session_end_syncs_transcript_as_catchup(tmp_path: Path):
    """SessionEnd should ingest any turn Stop never got to (e.g. killed process)."""
    vault = tmp_path / "v.sqlite"
    transcript = tmp_path / "sess-U.jsonl"
    transcript.write_text(
        _transcript_line("assistant", "assistant", [
            {"type": "text", "text": "Final words"},
        ])
    )

    _run_hook("adapter.hooks.session_start",
              {"session_id": "sess-U", "cwd": str(tmp_path)}, vault)
    _run_hook("adapter.hooks.session_end",
              {"session_id": "sess-U", "transcript_path": str(transcript)}, vault)

    row = sqlite3.connect(vault).execute(
        "SELECT role, content FROM messages WHERE session_id='sess-U' AND role='assistant'"
    ).fetchone()
    assert row == ("assistant", "Final words")
