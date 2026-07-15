"""Tests for MCP tool handlers — invoked directly, bypassing the stdio transport."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_lcm.config import ClaudeLcmConfig
from claude_lcm.engine import ClaudeLcmEngine
from claude_lcm.tools import (
    lcm_describe,
    lcm_doctor,
    lcm_expand,
    lcm_grep,
    lcm_recent,
    lcm_status,
)


@pytest.fixture
def engine(tmp_path: Path):
    cfg = ClaudeLcmConfig(vault_path=tmp_path / "v.sqlite")
    e = ClaudeLcmEngine(config=cfg)
    e.open_session("s", agent_kind="claude-code",
                   workspace_fingerprint="fp",
                   workspace_path=str(tmp_path))
    e.ingest_message({"role": "user", "content": "please grep for banana"})
    e.ingest_message({"role": "assistant", "content": "I'll look for banana now"})
    yield e
    e.close()


def test_grep_finds_message(engine):
    out = json.loads(lcm_grep({"query": "banana"}, engine=engine))
    assert out["total_results"] >= 1
    assert any("banana" in r.get("snippet", "").lower() for r in out["results"])


def test_grep_empty_query(engine):
    out = json.loads(lcm_grep({"query": "   "}, engine=engine))
    assert "error" in out


def test_describe_session_overview(engine):
    out = json.loads(lcm_describe({}, engine=engine))
    assert out["session_id"] == "s"
    assert out["store_message_count"] == 2
    assert out["dag_node_count"] == 0
    assert "note" in out  # v1 no-compaction hint


def test_expand_degrades_gracefully(engine):
    out = json.loads(lcm_expand({"node_id": 9999}, engine=engine))
    assert "error" in out
    assert "v1 has no compaction" in out["hint"]


def test_status_reports_session(engine):
    out = json.loads(lcm_status({}, engine=engine))
    assert out["session_id"] == "s"
    assert out["agent_kind"] == "claude-code"
    assert out["store"]["messages"] == 2
    assert out["version"] == "v1 (no compaction)"


def test_doctor_healthy(engine):
    out = json.loads(lcm_doctor({}, engine=engine))
    assert out["overall"] == "healthy"
    names = {c["check"] for c in out["checks"]}
    assert {"database_integrity", "fts_index_sync",
            "orphaned_dag_nodes", "schema_version", "transcript_sync"} <= names


def test_status_reports_role_counts_and_subagent_count(tmp_path):
    cfg = ClaudeLcmConfig(vault_path=tmp_path / "v.sqlite")
    eng = ClaudeLcmEngine(config=cfg, session_id="s")
    eng.open_session("s", agent_kind="claude-code", workspace_path=str(tmp_path))
    eng.ingest_message({"role": "user", "content": "hi"})
    eng.ingest_message({"role": "assistant", "content": "reply"})
    eng.ingest_message({"role": "assistant_thinking", "content": "reasoning"})
    eng.ingest_message({"role": "assistant", "content": "sub reply", "agent_id": "agent-a1"})

    out = json.loads(lcm_status({}, engine=eng))
    assert out["store"]["role_counts"] == {
        "user": 1, "assistant": 2, "assistant_thinking": 1,
    }
    assert out["store"]["subagent_transcripts_ingested"] == 1
    assert out["transcript_sync"] == {"path": None, "synced_bytes": 0}
    eng.close()


def test_status_reports_transcript_sync_state(tmp_path):
    cfg = ClaudeLcmConfig(vault_path=tmp_path / "v.sqlite")
    eng = ClaudeLcmEngine(config=cfg, session_id="s")
    eng.open_session("s", agent_kind="claude-code", workspace_path=str(tmp_path))

    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
    }) + "\n")
    eng.sync_transcript("s", str(transcript))

    out = json.loads(lcm_status({}, engine=eng))
    assert out["transcript_sync"]["path"] == str(transcript)
    assert out["transcript_sync"]["synced_bytes"] == transcript.stat().st_size
    assert out["transcript_sync"]["file_size_bytes"] == transcript.stat().st_size
    eng.close()


def test_doctor_transcript_sync_pass_when_caught_up(tmp_path):
    cfg = ClaudeLcmConfig(vault_path=tmp_path / "v.sqlite")
    eng = ClaudeLcmEngine(config=cfg, session_id="s")
    eng.open_session("s", agent_kind="claude-code", workspace_path=str(tmp_path))

    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
    }) + "\n")
    eng.sync_transcript("s", str(transcript))

    out = json.loads(lcm_doctor({}, engine=eng))
    check = next(c for c in out["checks"] if c["check"] == "transcript_sync")
    assert check["status"] == "pass"
    assert out["overall"] == "healthy"
    eng.close()


def test_doctor_transcript_sync_warns_when_behind(tmp_path):
    cfg = ClaudeLcmConfig(vault_path=tmp_path / "v.sqlite")
    eng = ClaudeLcmEngine(config=cfg, session_id="s")
    eng.open_session("s", agent_kind="claude-code", workspace_path=str(tmp_path))

    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
    }) + "\n")
    eng.sync_transcript("s", str(transcript))
    # Simulate a large gap accumulating without another sync.
    with transcript.open("a") as f:
        f.write(("x" * 70000) + "\n")

    out = json.loads(lcm_doctor({}, engine=eng))
    check = next(c for c in out["checks"] if c["check"] == "transcript_sync")
    assert check["status"] == "warn"
    assert out["overall"] == "warnings"
    eng.close()


def test_lcm_grep_scope_lineage_walks_parents(tmp_path):
    import json
    from claude_lcm.config import ClaudeLcmConfig
    from claude_lcm.engine import ClaudeLcmEngine
    from claude_lcm.tools import lcm_grep

    cfg = ClaudeLcmConfig(vault_path=tmp_path / "v.sqlite")
    eng = ClaudeLcmEngine(config=cfg, session_id="B")
    eng.open_session("A", project_key="-pk")
    eng.open_session("B", project_key="-pk", parent_session_id="A")
    eng.ingest_message({"role": "user", "content": "alpha in A"}, session_id="A")
    eng.ingest_message({"role": "user", "content": "beta in B"}, session_id="B")

    out = json.loads(lcm_grep({"query": "alpha", "scope": "lineage"},
                               engine=eng))
    snippets = [r.get("snippet", "") for r in out["results"]]
    assert any("alpha" in s for s in snippets), out
    eng.close()


def test_lcm_grep_scope_session_excludes_parent(tmp_path):
    import json
    from claude_lcm.config import ClaudeLcmConfig
    from claude_lcm.engine import ClaudeLcmEngine
    from claude_lcm.tools import lcm_grep

    cfg = ClaudeLcmConfig(vault_path=tmp_path / "v.sqlite")
    eng = ClaudeLcmEngine(config=cfg, session_id="B")
    eng.open_session("A", project_key="-pk")
    eng.open_session("B", project_key="-pk", parent_session_id="A")
    eng.ingest_message({"role": "user", "content": "alpha in A"}, session_id="A")
    eng.ingest_message({"role": "user", "content": "alpha in B"}, session_id="B")

    out = json.loads(lcm_grep({"query": "alpha", "scope": "session"},
                               engine=eng))
    # Only B's message, not A's
    assert out["total_results"] == 1
    eng.close()


def test_lcm_grep_scope_workspace_crosses_siblings(tmp_path):
    import json
    from claude_lcm.config import ClaudeLcmConfig
    from claude_lcm.engine import ClaudeLcmEngine
    from claude_lcm.tools import lcm_grep

    cfg = ClaudeLcmConfig(vault_path=tmp_path / "v.sqlite")
    eng = ClaudeLcmEngine(config=cfg, session_id="B")
    eng.open_session("A", project_key="-pk")
    eng.open_session("B", project_key="-pk")  # sibling, no parent link
    eng.open_session("Z", project_key="-other")
    eng.ingest_message({"role": "user", "content": "alpha in A"}, session_id="A")
    eng.ingest_message({"role": "user", "content": "alpha in B"}, session_id="B")
    eng.ingest_message({"role": "user", "content": "alpha in Z"}, session_id="Z")

    out = json.loads(lcm_grep({"query": "alpha", "scope": "workspace"},
                               engine=eng))
    assert out["total_results"] == 2  # A and B, not Z
    eng.close()


def test_lcm_recent_returns_messages_newest_first(tmp_path):
    cfg = ClaudeLcmConfig(vault_path=tmp_path / "v.sqlite")
    eng = ClaudeLcmEngine(config=cfg, session_id="A")
    eng.open_session("A", project_key="-pk")
    eng.ingest_message({"role": "user", "content": "first message", "timestamp": 1000.0})
    eng.ingest_message({"role": "assistant", "content": "second message", "timestamp": 2000.0})
    eng.ingest_message({"role": "user", "content": "third message", "timestamp": 3000.0})

    out = json.loads(lcm_recent({}, engine=eng))
    assert out["total_results"] == 3
    assert out["messages"][0]["content"] == "third message"
    assert out["messages"][-1]["content"] == "first message"
    eng.close()


def test_lcm_recent_limit(tmp_path):
    cfg = ClaudeLcmConfig(vault_path=tmp_path / "v.sqlite")
    eng = ClaudeLcmEngine(config=cfg, session_id="A")
    eng.open_session("A", project_key="-pk")
    for i in range(5):
        eng.ingest_message({"role": "user", "content": f"msg {i}", "timestamp": float(i)})

    out = json.loads(lcm_recent({"limit": 2}, engine=eng))
    assert len(out["messages"]) == 2
    eng.close()


def test_lcm_recent_excludes_thinking_and_subagents_by_default(tmp_path):
    cfg = ClaudeLcmConfig(vault_path=tmp_path / "v.sqlite")
    eng = ClaudeLcmEngine(config=cfg, session_id="A")
    eng.open_session("A", project_key="-pk")
    eng.ingest_message({"role": "assistant", "content": "main reply", "timestamp": 1.0})
    eng.ingest_message({"role": "assistant_thinking", "content": "reasoning", "timestamp": 2.0})
    eng.ingest_message({"role": "assistant", "content": "sub reply", "timestamp": 3.0,
                        "agent_id": "agent-a1"})

    out = json.loads(lcm_recent({}, engine=eng))
    assert [m["content"] for m in out["messages"]] == ["main reply"]

    out_thinking = json.loads(lcm_recent({"include_thinking": True}, engine=eng))
    contents = {m["content"] for m in out_thinking["messages"]}
    assert contents == {"main reply", "reasoning"}

    out_subagents = json.loads(lcm_recent({"include_subagents": True}, engine=eng))
    contents = {m["content"] for m in out_subagents["messages"]}
    assert contents == {"main reply", "sub reply"}
    eng.close()


def test_lcm_grep_excludes_thinking_and_subagents_by_default(tmp_path):
    cfg = ClaudeLcmConfig(vault_path=tmp_path / "v.sqlite")
    eng = ClaudeLcmEngine(config=cfg, session_id="A")
    eng.open_session("A", project_key="-pk")
    eng.ingest_message({"role": "assistant", "content": "kumquat in the reply"})
    eng.ingest_message({"role": "assistant_thinking", "content": "kumquat in my reasoning"})
    eng.ingest_message({"role": "assistant", "content": "kumquat from a subagent",
                        "agent_id": "agent-a1"})

    out = json.loads(lcm_grep({"query": "kumquat"}, engine=eng))
    assert out["total_results"] == 1

    out_all = json.loads(lcm_grep(
        {"query": "kumquat", "include_thinking": True, "include_subagents": True},
        engine=eng,
    ))
    assert out_all["total_results"] == 3
    eng.close()


def test_lcm_describe_by_snapshot_id(tmp_path):
    cfg = ClaudeLcmConfig(vault_path=tmp_path / "v.sqlite")
    eng = ClaudeLcmEngine(config=cfg, session_id="s")
    eng.open_session("s", project_key="-pk")
    sid = eng.ingest_file_snapshot(
        file_path="/tmp/foo.py", op="read",
        content=b"def foo(): pass",
        exploration_summary="functions: foo",
    )
    out = json.loads(lcm_describe({"id": sid}, engine=eng))
    assert "error" not in out
    assert out["snapshot_id"] == sid
    assert out["path"] == "/tmp/foo.py"
    assert out["extension"] == ".py"
    assert out["op"] == "read"
    assert out["exploration_summary"] == "functions: foo"
    eng.close()


def test_lcm_describe_by_path(tmp_path):
    cfg = ClaudeLcmConfig(vault_path=tmp_path / "v.sqlite")
    eng = ClaudeLcmEngine(config=cfg, session_id="s")
    eng.open_session("s", project_key="-pk")
    eng.ingest_file_snapshot(
        file_path="/tmp/bar.py", op="read", content=b"x=1",
    )
    out = json.loads(lcm_describe({"id": "/tmp/bar.py", "session_id": "s"}, engine=eng))
    assert "error" not in out
    assert out["path"] == "/tmp/bar.py"
    assert out["extension"] == ".py"
    eng.close()


def test_lcm_describe_not_found(engine):
    out = json.loads(lcm_describe({"id": 99999}, engine=engine))
    assert "error" in out
    out2 = json.loads(lcm_describe({"id": "/nonexistent/path.py"}, engine=engine))
    assert "error" in out2


def test_lcm_describe_session_overview_unchanged(engine):
    """No id → session overview still works (backward compat)."""
    out = json.loads(lcm_describe({}, engine=engine))
    assert out["session_id"] == "s"
    assert "store_message_count" in out


def test_lcm_recent_scope_lineage_crosses_clear(tmp_path):
    cfg = ClaudeLcmConfig(vault_path=tmp_path / "v.sqlite")
    eng = ClaudeLcmEngine(config=cfg, session_id="B")
    eng.open_session("A", project_key="-pk")
    eng.open_session("B", project_key="-pk", parent_session_id="A")
    eng.ingest_message({"role": "user", "content": "from A"}, session_id="A")
    eng.ingest_message({"role": "user", "content": "from B"}, session_id="B")

    out = json.loads(lcm_recent({"scope": "lineage"}, engine=eng))
    contents = [m["content"] for m in out["messages"]]
    assert "from A" in contents
    assert "from B" in contents
    eng.close()
