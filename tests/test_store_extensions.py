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


def test_store_init_adds_project_key_and_handoff(tmp_path):
    from claude_lcm.store import MessageStore

    db = tmp_path / "v.sqlite"
    store = MessageStore(db)

    cols = {r[1] for r in store._conn.execute("PRAGMA table_info(sessions)").fetchall()}
    assert {"project_key", "parent_session_id", "end_reason"} <= cols

    tables = {r[0] for r in store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "clear_handoff" in tables

    # Index exists
    idx = {r[0] for r in store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    ).fetchall()}
    assert "idx_sessions_project_key_ended" in idx

    store.close()


def test_store_init_is_idempotent_on_existing_vault(tmp_path):
    from claude_lcm.store import MessageStore

    db = tmp_path / "v.sqlite"
    MessageStore(db).close()
    # Re-open — must not raise "duplicate column" or similar.
    store = MessageStore(db)
    store.close()


def test_open_session_persists_project_key_and_parent(tmp_path):
    from claude_lcm.store import MessageStore

    store = MessageStore(tmp_path / "v.sqlite")
    store.open_session(
        session_id="B",
        agent_kind="claude-code",
        project_key="-home-lucas-ai-x",
        parent_session_id="A",
    )
    row = store._conn.execute(
        "SELECT project_key, parent_session_id FROM sessions WHERE session_id='B'"
    ).fetchone()
    assert row == ("-home-lucas-ai-x", "A")
    store.close()


def test_set_end_reason_updates_row(tmp_path):
    from claude_lcm.store import MessageStore

    store = MessageStore(tmp_path / "v.sqlite")
    store.open_session(session_id="A", agent_kind="claude-code",
                       project_key="-pk")
    store.close_session("A")
    store.set_end_reason("A", "clear")
    row = store._conn.execute(
        "SELECT end_reason FROM sessions WHERE session_id='A'"
    ).fetchone()
    assert row[0] == "clear"
    store.close()


def test_upsert_and_take_clear_handoff_roundtrip(tmp_path):
    from claude_lcm.store import MessageStore

    store = MessageStore(tmp_path / "v.sqlite")
    store.upsert_clear_handoff(project_key="-pk", ending_session_id="A")
    assert store.take_clear_handoff("-pk") == "A"
    # Second take returns None (row was deleted)
    assert store.take_clear_handoff("-pk") is None
    store.close()


def test_upsert_clear_handoff_overwrites_orphan(tmp_path):
    from claude_lcm.store import MessageStore

    store = MessageStore(tmp_path / "v.sqlite")
    store.upsert_clear_handoff(project_key="-pk", ending_session_id="A")
    store.upsert_clear_handoff(project_key="-pk", ending_session_id="B")
    assert store.take_clear_handoff("-pk") == "B"
    store.close()


def test_clear_handoff_is_scoped_by_project_key(tmp_path):
    from claude_lcm.store import MessageStore

    store = MessageStore(tmp_path / "v.sqlite")
    store.upsert_clear_handoff(project_key="-pkX", ending_session_id="A")
    store.upsert_clear_handoff(project_key="-pkY", ending_session_id="B")
    assert store.take_clear_handoff("-pkX") == "A"
    assert store.take_clear_handoff("-pkY") == "B"
    store.close()


def test_walk_lineage_single_session(tmp_path):
    from claude_lcm.store import MessageStore

    store = MessageStore(tmp_path / "v.sqlite")
    store.open_session(session_id="A", agent_kind="claude-code",
                       project_key="-pk")
    assert store.walk_lineage("A") == ["A"]
    store.close()


def test_walk_lineage_chain(tmp_path):
    """A -> B -> C -> D: walk_lineage(D) returns [D, C, B, A]."""
    from claude_lcm.store import MessageStore

    store = MessageStore(tmp_path / "v.sqlite")
    store.open_session("A", "claude-code", project_key="-pk")
    store.open_session("B", "claude-code", project_key="-pk",
                       parent_session_id="A")
    store.open_session("C", "claude-code", project_key="-pk",
                       parent_session_id="B")
    store.open_session("D", "claude-code", project_key="-pk",
                       parent_session_id="C")
    assert store.walk_lineage("D") == ["D", "C", "B", "A"]
    store.close()


def test_walk_lineage_fork_via_resume(tmp_path):
    """A -> B -> C (one chain), A -> D (resume then clear)."""
    from claude_lcm.store import MessageStore

    store = MessageStore(tmp_path / "v.sqlite")
    store.open_session("A", "claude-code", project_key="-pk")
    store.open_session("B", "claude-code", project_key="-pk",
                       parent_session_id="A")
    store.open_session("C", "claude-code", project_key="-pk",
                       parent_session_id="B")
    # User --resumes A, then /clears to D -- D's parent is A, not C
    store.open_session("D", "claude-code", project_key="-pk",
                       parent_session_id="A")
    assert store.walk_lineage("D") == ["D", "A"]
    assert store.walk_lineage("C") == ["C", "B", "A"]
    store.close()


def test_project_key_for_session(tmp_path):
    from claude_lcm.store import MessageStore

    store = MessageStore(tmp_path / "v.sqlite")
    store.open_session("A", "claude-code", project_key="-pkA")
    store.open_session("B", "claude-code", project_key="-pkB")
    assert store.project_key_for_session("A") == "-pkA"
    assert store.project_key_for_session("B") == "-pkB"
    assert store.project_key_for_session("Z") is None
    store.close()


def test_search_filters_by_session_ids(tmp_path):
    from claude_lcm.store import MessageStore

    store = MessageStore(tmp_path / "v.sqlite")
    for sid in ("A", "B", "C"):
        store.open_session(sid, "claude-code", project_key="-pk")
        store.append(sid, {"role": "user",
                           "content": f"hello from {sid}",
                           "timestamp": 0})

    results = store.search("hello", session_ids=["A", "B"], limit=10)
    contents = sorted(r["content"] for r in results)
    assert contents == ["hello from A", "hello from B"]
    store.close()
