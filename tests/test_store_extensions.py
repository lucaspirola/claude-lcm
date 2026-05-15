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
    # Insert parent "A" first — required now that parent_session_id has a FK constraint
    store.open_session(
        session_id="A",
        agent_kind="claude-code",
        project_key="-home-lucas-ai-x",
    )
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


# ──────────────────────────────────────────────────────────────────────────────
# Explorer tests
# ──────────────────────────────────────────────────────────────────────────────

def test_explorer_py_extracts_functions_and_classes():
    from claude_lcm.explorer import explore
    blob = b"def foo():\n    pass\n\nasync def bar():\n    pass\n\nclass Baz:\n    pass\n"
    result = explore("/fake/file.py", blob)
    assert result is not None
    assert "foo" in result
    assert "bar" in result
    assert "Baz" in result


def test_explorer_json_extracts_top_level_keys():
    import json as _json
    from claude_lcm.explorer import explore
    blob = _json.dumps({"name": "alice", "items": [1, 2], "meta": {}}).encode()
    result = explore("/fake/data.json", blob)
    assert result is not None
    assert "name" in result
    assert "items" in result
    assert "meta" in result


def test_explorer_sql_extracts_table_and_view_names():
    from claude_lcm.explorer import explore
    blob = b"CREATE TABLE users (id INT);\nCREATE VIEW active_users AS SELECT * FROM users;\n"
    result = explore("/fake/schema.sql", blob)
    assert result is not None
    assert "tables:" in result
    assert "users" in result
    assert "views:" in result
    assert "active_users" in result


def test_explorer_text_fallback_shows_first_and_last_lines():
    from claude_lcm.explorer import explore
    lines = [f"line {i}" for i in range(40)]
    blob = "\n".join(lines).encode()
    result = explore("/fake/notes.txt", blob)
    assert result is not None
    assert "line 0" in result
    assert "line 39" in result
    assert "line 20" not in result  # middle line excluded by first-20 + last-10 strategy


def test_explorer_none_blob_returns_none():
    from claude_lcm.explorer import explore
    assert explore("/fake/file.py", None) is None


def test_explorer_binary_blob_returns_none_without_raising():
    from claude_lcm.explorer import explore
    result = explore("/fake/file.py", b"\x00\xff\xfe")
    assert result is None  # UTF-8 decode fails → except → None


def test_explorer_caps_output_at_2000_chars():
    from claude_lcm.explorer import explore
    lines = [f"def func_{i}(): pass" for i in range(200)]
    blob = "\n".join(lines).encode()
    result = explore("/fake/big.py", blob)
    assert result is not None
    assert len(result) <= 2000


# ──────────────────────────────────────────────────────────────────────────────
# exploration_summary column + store read methods
# ──────────────────────────────────────────────────────────────────────────────

def test_file_snapshots_has_exploration_summary_column(store: MessageStore):
    cols = {r[1] for r in store._conn.execute(
        "PRAGMA table_info(file_snapshots)"
    ).fetchall()}
    assert "exploration_summary" in cols


def test_append_file_snapshot_stores_exploration_summary(store: MessageStore):
    store.open_session("s_sum", "claude-code")
    sid = store.append_file_snapshot(
        "s_sum", file_path="/tmp/foo.py", op="write",
        content=b"def foo(): pass",
        exploration_summary="functions: foo",
    )
    row = store._conn.execute(
        "SELECT exploration_summary FROM file_snapshots WHERE snapshot_id=?", (sid,)
    ).fetchone()
    assert row[0] == "functions: foo"


def test_append_file_snapshot_exploration_summary_defaults_none(store: MessageStore):
    store.open_session("s_sum2", "claude-code")
    sid = store.append_file_snapshot(
        "s_sum2", file_path="/tmp/bar.py", op="read", content=b"x=1",
    )
    row = store._conn.execute(
        "SELECT exploration_summary FROM file_snapshots WHERE snapshot_id=?", (sid,)
    ).fetchone()
    assert row[0] is None


def test_get_file_snapshot_by_id(store: MessageStore):
    store.open_session("s_gfs", "claude-code")
    sid = store.append_file_snapshot(
        "s_gfs", file_path="/tmp/a.py", op="read",
        content=b"print('hi')",
        exploration_summary="functions: main",
    )
    snap = store.get_file_snapshot(sid)
    assert snap is not None
    assert snap["snapshot_id"] == sid
    assert snap["file_path"] == "/tmp/a.py"
    assert snap["op"] == "read"
    assert snap["exploration_summary"] == "functions: main"
    assert snap["size_bytes"] == len(b"print('hi')")


def test_get_file_snapshot_returns_none_for_missing_id(store: MessageStore):
    assert store.get_file_snapshot(999999) is None


def test_get_latest_snapshot_for_path_by_session(tmp_path: Path):
    s = MessageStore(tmp_path / "v.sqlite")
    s.open_session("A", "claude-code", project_key="-pk")
    s.open_session("B", "claude-code", project_key="-pk", parent_session_id="A")
    # Use explicit captured_at values to avoid relying on wall-clock ordering.
    s._conn.execute(
        """INSERT INTO file_snapshots
           (session_id, file_path, content_hash, content_blob, captured_at, op, exploration_summary)
           VALUES ('A', '/tmp/x.py', 'hash1', 'old', 1000.0, 'read', 'old')"""
    )
    s._conn.execute(
        """INSERT INTO file_snapshots
           (session_id, file_path, content_hash, content_blob, captured_at, op, exploration_summary)
           VALUES ('B', '/tmp/x.py', 'hash2', 'new', 2000.0, 'write', 'new')"""
    )
    s._conn.commit()
    lineage = s.walk_lineage("B")
    snap = s.get_latest_snapshot_for_path("/tmp/x.py", session_ids=lineage)
    assert snap is not None
    assert snap["exploration_summary"] == "new"
    s.close()


def test_get_latest_snapshot_for_path_vault_global(tmp_path: Path):
    s = MessageStore(tmp_path / "v.sqlite")
    s.open_session("X", "claude-code", project_key="-pk")
    s.append_file_snapshot("X", file_path="/tmp/y.py", op="read",
                           content=b"code", exploration_summary="global")
    snap = s.get_latest_snapshot_for_path("/tmp/y.py", session_ids=None)
    assert snap is not None
    assert snap["exploration_summary"] == "global"
    s.close()


def test_get_latest_snapshot_for_path_returns_none_when_missing(store: MessageStore):
    assert store.get_latest_snapshot_for_path("/nonexistent/path.py") is None


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
