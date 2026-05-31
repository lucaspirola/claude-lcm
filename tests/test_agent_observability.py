"""Tests for the agent-observability tooling batch:

lcm_tool_calls, lcm_whoami, lcm_mark/lcm_marks, scope='auto', the lcm_grep
match_mode/silent-error fix, and the lcm_recent `n` alias.
"""

import json

import pytest

from claude_lcm import tools
from claude_lcm.config import ClaudeLcmConfig
from claude_lcm.engine import ClaudeLcmEngine
from claude_lcm.workspace import sanitize_path


@pytest.fixture
def vault_path(tmp_path, monkeypatch):
    path = tmp_path / "vault.sqlite"
    monkeypatch.setenv("LCM_VAULT_PATH", str(path))
    # make sure a stray env session id never leaks into direct-constructed engines
    monkeypatch.delenv("LCM_SESSION_ID", raising=False)
    return path


def _engine(session_id):
    return ClaudeLcmEngine(config=ClaudeLcmConfig.from_env(), session_id=session_id)


def _open(engine, session_id, project_key=None, workspace_path=None,
          parent_session_id=None):
    engine.open_session(
        session_id=session_id,
        agent_kind="claude-code",
        workspace_path=workspace_path,
        project_key=project_key,
        parent_session_id=parent_session_id,
    )


def _ingest_tool_call(engine, call_id, name, args, result, session_id=None):
    """Mirror the pre/post tool-use hooks: a tool_use row then a tool_result row."""
    engine.ingest_message({
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": call_id, "name": name, "arguments": args}],
        "tool_name": name,
    }, session_id=session_id)
    engine.ingest_message({
        "role": "tool",
        "content": result,
        "tool_call_id": call_id,
        "tool_name": name,
    }, session_id=session_id)


# --------------------------------------------------------------------------
# lcm_tool_calls
# --------------------------------------------------------------------------

def test_tool_calls_pairs_use_with_result(vault_path):
    eng = _engine("S1")
    _open(eng, "S1")
    _ingest_tool_call(eng, "c1", "Read", {"file_path": "/a.py"}, "contents of a")
    _ingest_tool_call(eng, "c2", "Bash", {"command": "ls"}, "file listing")

    out = json.loads(tools.lcm_tool_calls({}, engine=eng))
    assert out["group_by"] == "call"
    assert out["scope"] == "session"  # audit tools default to session
    assert out["total_results"] == 2
    by_name = {c["tool_name"]: c for c in out["tool_calls"]}
    assert by_name["Read"]["args"] == {"file_path": "/a.py"}
    assert by_name["Read"]["result"] == "contents of a"
    assert by_name["Bash"]["result"] == "file listing"
    eng.close()


def test_tool_calls_filter_and_turn_grouping(vault_path):
    eng = _engine("S1")
    _open(eng, "S1")
    _ingest_tool_call(eng, "c1", "Read", {"file_path": "/a.py"}, "A")
    _ingest_tool_call(eng, "c2", "Read", {"file_path": "/b.py"}, "B")
    _ingest_tool_call(eng, "c3", "Bash", {"command": "ls"}, "L")

    flat = json.loads(tools.lcm_tool_calls({"tool_name": "Read"}, engine=eng))
    assert flat["total_results"] == 2
    assert all(c["tool_name"] == "Read" for c in flat["tool_calls"])

    turns = json.loads(tools.lcm_tool_calls({"group_by": "turn"}, engine=eng))
    assert turns["group_by"] == "turn"
    # each tool_use row is its own assistant turn in this adapter
    assert turns["total_results"] == 3
    assert all("tool_calls" in t and "message_id" in t for t in turns["turns"])
    eng.close()


def test_tool_calls_pairs_without_tool_call_id(vault_path):
    """CC hook payloads frequently omit tool_call_id; pairing must fall back to
    name adjacency."""
    eng = _engine("S1")
    _open(eng, "S1")
    _ingest_tool_call(eng, "", "Read", {"file_path": "/a.py"}, "adjacent result")

    out = json.loads(tools.lcm_tool_calls({}, engine=eng))
    assert out["total_results"] == 1
    assert out["tool_calls"][0]["result"] == "adjacent result"
    eng.close()


# --------------------------------------------------------------------------
# lcm_whoami
# --------------------------------------------------------------------------

def test_whoami_returns_lineage_and_parent(vault_path):
    eng = _engine("CHILD")
    _open(eng, "PARENT")
    _open(eng, "CHILD", parent_session_id="PARENT")

    out = json.loads(tools.lcm_whoami({}, engine=eng))
    assert out["session_id"] == "CHILD"
    assert out["parent_session_id"] == "PARENT"
    assert out["lineage"] == ["CHILD", "PARENT"]
    assert out["resolved_via"] == "session_id"
    eng.close()


def test_whoami_best_effort_via_project_dir(vault_path, tmp_path, monkeypatch):
    proj = str(tmp_path / "proj")
    pk = sanitize_path(proj)
    setup = _engine("ONLY")
    _open(setup, "ONLY", project_key=pk, workspace_path=proj)
    setup.close()

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", proj)
    eng = _engine(None)  # no session id — must fall back
    out = json.loads(tools.lcm_whoami({}, engine=eng))
    assert out["session_id"] == "ONLY"
    assert out["resolved_via"] == "project_dir_latest"
    assert "warning" in out
    eng.close()


# --------------------------------------------------------------------------
# marks
# --------------------------------------------------------------------------

def test_mark_and_marks_roundtrip_and_pin(vault_path):
    eng = _engine("S1")
    _open(eng, "S1")
    sid_msg = eng.ingest_message({"role": "user", "content": "hello"})

    marked = json.loads(tools.lcm_mark(
        {"name": "ml-intern:active", "store_id": sid_msg, "metadata": {"k": "v"}},
        engine=eng,
    ))
    assert marked["pinned"] is True
    assert marked["name"] == "ml-intern:active"

    listed = json.loads(tools.lcm_marks({"scope": "session"}, engine=eng))
    assert listed["total_results"] == 1
    mark = listed["marks"][0]
    assert mark["name"] == "ml-intern:active"
    assert mark["store_id"] == sid_msg
    assert mark["metadata"] == {"k": "v"}

    # the referenced message is pinned
    assert eng._store.get(sid_msg)["pinned"] == 1

    # filter by name
    none = json.loads(tools.lcm_marks({"name": "nope", "scope": "session"}, engine=eng))
    assert none["total_results"] == 0
    eng.close()


def test_mark_requires_name(vault_path):
    eng = _engine("S1")
    _open(eng, "S1")
    out = json.loads(tools.lcm_mark({}, engine=eng))
    assert "error" in out
    eng.close()


# --------------------------------------------------------------------------
# lcm_grep match_mode + silent-error fix
# --------------------------------------------------------------------------

def test_grep_literal_matches_json_needle(vault_path):
    eng = _engine("S1")
    _open(eng, "S1")
    eng.ingest_message({"role": "user", "content": 'the marker is "tool_name":"Read" here'})

    out = json.loads(tools.lcm_grep(
        {"query": '"tool_name":"Read"', "match_mode": "literal", "scope": "session"},
        engine=eng,
    ))
    assert "error" not in out
    assert out["match_mode"] == "literal"
    assert out["total_results"] >= 1
    eng.close()


def test_grep_malformed_query_recovers_not_silent(vault_path):
    """A raw FTS5 parse error must not masquerade as an empty success — the
    server recovers in literal mode (or returns an explicit error)."""
    eng = _engine("S1")
    _open(eng, "S1")
    eng.ingest_message({"role": "user", "content": "some content"})

    out = json.loads(tools.lcm_grep({"query": '"', "scope": "session"}, engine=eng))
    # Either an explicit parse error, or recovery flagged as literal mode —
    # never a plain fts5 empty-success.
    assert ("error" in out) or (out.get("match_mode") == "literal")
    eng.close()


# --------------------------------------------------------------------------
# scope='auto'
# --------------------------------------------------------------------------

def test_scope_auto_prefers_session_when_rows_exist(vault_path):
    eng = _engine("CHILD")
    _open(eng, "PARENT")
    _open(eng, "CHILD", parent_session_id="PARENT")
    # rows in both sessions, distinguishable single-token content
    eng.ingest_message({"role": "user", "content": "parentonly"},
                       session_id="PARENT")
    eng.ingest_message({"role": "user", "content": "childonly"})

    ids = tools._resolve_scope_session_ids(eng, "auto")
    assert ids == ["CHILD"]  # current session has rows → session scope

    # grep with auto should not see the parent-only token
    out = json.loads(tools.lcm_grep({"query": "parentonly", "scope": "auto"}, engine=eng))
    assert out["total_results"] == 0
    eng.close()


def test_scope_auto_widens_to_lineage_when_session_empty(vault_path):
    eng = _engine("CHILD")
    _open(eng, "PARENT")
    _open(eng, "CHILD", parent_session_id="PARENT")
    eng.ingest_message({"role": "user", "content": "parent token"},
                       session_id="PARENT")
    # CHILD has no rows of its own

    ids = tools._resolve_scope_session_ids(eng, "auto")
    assert ids == ["CHILD", "PARENT"]  # empty session → widen to lineage
    eng.close()


# --------------------------------------------------------------------------
# lcm_recent `n` alias
# --------------------------------------------------------------------------

def test_recent_n_alias(vault_path):
    eng = _engine("S1")
    _open(eng, "S1")
    for i in range(5):
        eng.ingest_message({"role": "user", "content": f"m{i}"})

    out = json.loads(tools.lcm_recent({"n": 3, "scope": "session"}, engine=eng))
    assert out["total_results"] == 3
    eng.close()


# --------------------------------------------------------------------------
# regression tests
# --------------------------------------------------------------------------

def test_tool_calls_no_cross_session_leak(vault_path):
    """H1: the session filter must apply to the whole OR group, not just the
    last branch — other sessions' tool rows must not leak under scope='session'."""
    eng = _engine("CHILD")
    _open(eng, "PARENT")
    _open(eng, "CHILD", parent_session_id="PARENT")
    _ingest_tool_call(eng, "p1", "Read", {"file_path": "/parent.py"}, "parent result",
                      session_id="PARENT")
    _ingest_tool_call(eng, "c1", "Bash", {"command": "ls"}, "child result")

    out = json.loads(tools.lcm_tool_calls({"scope": "session"}, engine=eng))
    sessions = {c["session_id"] for c in out["tool_calls"]}
    assert sessions == {"CHILD"}
    assert all(c["tool_name"] != "Read" for c in out["tool_calls"])
    eng.close()


def test_mark_bogus_store_id_not_pinned(vault_path):
    """L2: a non-existent store_id must return an error and create no mark."""
    eng = _engine("S1")
    _open(eng, "S1")

    out = json.loads(tools.lcm_mark({"name": "bogus", "store_id": 99999}, engine=eng))
    assert "error" in out
    assert out["store_id"] == 99999

    listed = json.loads(tools.lcm_marks({"scope": "session"}, engine=eng))
    assert listed["total_results"] == 0
    eng.close()


def test_tool_calls_tool_name_sql_filter(vault_path):
    """M3: tool_name filtering is pushed into SQL, so a call older than the fetch
    window is still found instead of being truncated away.

    The lone Task pair is ingested first (oldest), then 205 Read pairs push it far
    past the newest-window floor (200 rows). With SQL-level `AND tool_name = ?` only
    the Task rows are fetched, so it is found; without it the newest-window fetch
    would be all Read rows and the Task call would be truncated → 0 results."""
    eng = _engine("S1")
    _open(eng, "S1")
    _ingest_tool_call(eng, "t0", "Task", {"prompt": "do it"}, "T0")
    for i in range(205):
        _ingest_tool_call(eng, f"r{i}", "Read", {"file_path": f"/r{i}.py"}, f"R{i}")

    out = json.loads(tools.lcm_tool_calls(
        {"tool_name": "Task", "limit": 2, "scope": "session"}, engine=eng))
    assert out["total_results"] == 1
    assert all(c["tool_name"] == "Task" for c in out["tool_calls"])
    eng.close()


def test_tool_calls_returns_newest_window(vault_path):
    """tool_call_rows fetches the NEWEST window: with more rows than the fetch
    floor, lcm_tool_calls must surface the most recent call, not an old one."""
    eng = _engine("S1")
    _open(eng, "S1")
    for i in range(205):
        _ingest_tool_call(eng, f"c{i}", "Read", {"file_path": f"/f{i}"}, f"r{i}")

    out = json.loads(tools.lcm_tool_calls({"limit": 1, "scope": "session"}, engine=eng))
    assert out["total_results"] == 1
    assert out["tool_calls"][0]["args"] == {"file_path": "/f204"}
    eng.close()
