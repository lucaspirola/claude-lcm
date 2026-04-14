"""Tests for the claude-lcm extensions to the lifted MessageStore."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_lcm.store import MessageStore


@pytest.fixture
def store(tmp_path: Path) -> MessageStore:
    s = MessageStore(tmp_path / "vault.sqlite")
    yield s
    s.close()


def test_schema_version_set(store: MessageStore):
    row = store._conn.execute(
        "SELECT value FROM metadata WHERE key='schema_version'"
    ).fetchone()
    assert row == ("1",)


def test_open_session_idempotent(store: MessageStore):
    store.open_session("s1", "claude-code", workspace_path="/a", workspace_fingerprint="fp")
    store.open_session("s1", "claude-code", workspace_path="/different", workspace_fingerprint="fp2")
    row = store.get_session("s1")
    assert row["agent_kind"] == "claude-code"
    # first insert wins (INSERT OR IGNORE) — the second call is a no-op
    assert row["workspace_path"] == "/a"


def test_close_session_sets_ended_at(store: MessageStore):
    store.open_session("s2", "claude-code")
    row = store.get_session("s2")
    assert row["ended_at"] is None
    store.close_session("s2")
    row = store.get_session("s2")
    assert row["ended_at"] is not None


def test_append_skill_load(store: MessageStore):
    store.open_session("s3", "claude-code")
    sid = store.append_skill_load("s3", "commit", skill_path="/fake", content_hash="abc")
    assert sid > 0
    names = store.get_loaded_skill_names("s3")
    assert "commit" in names


def test_append_file_snapshot_inline(store: MessageStore):
    store.open_session("s4", "claude-code")
    content = b"print('hi')\n"
    sid = store.append_file_snapshot(
        "s4", file_path="/tmp/foo.py", op="write", content=content
    )
    row = store._conn.execute(
        "SELECT file_path, op, content_blob, external_uri, content_hash FROM file_snapshots WHERE snapshot_id=?",
        (sid,),
    ).fetchone()
    assert row[0] == "/tmp/foo.py"
    assert row[1] == "write"
    assert row[2] == content
    assert row[3] is None
    assert len(row[4]) == 64  # sha256 hex


def test_append_file_snapshot_external_uri(store: MessageStore):
    store.open_session("s5", "claude-code")
    sid = store.append_file_snapshot(
        "s5", file_path="/tmp/big.bin", op="read",
        content=None, external_uri="agentfs://rev/7"
    )
    row = store._conn.execute(
        "SELECT content_blob, external_uri FROM file_snapshots WHERE snapshot_id=?", (sid,)
    ).fetchone()
    assert row[0] is None
    assert row[1] == "agentfs://rev/7"


def test_append_file_snapshot_requires_content_or_uri(store: MessageStore):
    store.open_session("s6", "claude-code")
    with pytest.raises(ValueError):
        store.append_file_snapshot("s6", file_path="/x", op="write")


def test_message_round_trip_and_fts_search(store: MessageStore):
    store.open_session("s7", "claude-code")
    store.append("s7", {"role": "user", "content": "refactor the payment service"})
    store.append("s7", {"role": "assistant", "content": "ok, which file?"})
    hits = store.search("payment", session_id="s7")
    assert len(hits) == 1
    assert hits[0]["role"] == "user"
    assert "payment" in hits[0]["snippet"].lower()
