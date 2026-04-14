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
            "orphaned_dag_nodes", "schema_version"} <= names


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
