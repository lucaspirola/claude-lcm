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
