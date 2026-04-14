# `/clear` Lineage & Workspace Identity — Implementation Plan

> **For agentic workers:** Use `superpowers:subagent-driven-development` or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make `lcm_grep` recall context across Claude Code `/clear` boundaries by persisting a `parent_session_id` chain in the vault, keyed by a sanitized-cwd `project_key` that matches CC's own project-directory naming.

**Architecture:** `SessionEnd(source='clear')` writes a one-row handoff keyed on `project_key`; the immediately-following `SessionStart(source='clear')` reads and deletes it and stamps `parent_session_id` on the new session row. `lcm_grep` gains a `scope` parameter (`'lineage' | 'workspace' | 'session'`, default `'lineage'`) that walks the parent chain via a recursive CTE.

**Tech Stack:** Python 3.11+, sqlite3, FTS5, stdio MCP. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-04-14-clear-lineage-design.md`

---

## File structure

| File | Change | Purpose |
|---|---|---|
| `claude_lcm/workspace.py` | modify | Add `sanitize_path()` + `MAX_SANITIZED_LENGTH` + djb2 fallback |
| `claude_lcm/store.py` | modify | Schema migration: `project_key`, `parent_session_id`, `end_reason` columns on `sessions`; new `clear_handoff` table; new methods (`set_end_reason`, `upsert_clear_handoff`, `take_clear_handoff`, `walk_lineage`, `project_key_for_session`); `open_session` gains `project_key` + `parent_session_id` args; `search` gains `session_ids` list filter |
| `claude_lcm/engine.py` | modify | Thin pass-throughs for the new store methods so hooks and tool handlers don't reach into `_store` directly |
| `claude_lcm/schemas.py` | modify | Add `scope` enum param to `LCM_GREP` (and the other `lcm_*` tool schemas) |
| `claude_lcm/tools.py` | modify | `_resolve_scope_session_ids()` helper; thread through `lcm_grep` (and, for consistency, the other `lcm_*` handlers) |
| `adapter/hooks/session_start.py` | modify | Compute `project_key`; on `source == 'clear'` read+consume handoff and set parent |
| `adapter/hooks/session_end.py` | modify | On `source == 'clear'` set `end_reason='clear'` and upsert handoff |
| `tests/test_workspace.py` | create | Unit tests for `sanitize_path` including parity against a real `~/.claude/projects/` directory name |
| `tests/test_store_extensions.py` | modify | Tests for new store methods (schema + lineage + handoff + scope filter) |
| `tests/test_hooks.py` | modify | Add end-to-end `/clear` chain subprocess test |
| `tests/test_mcp_server.py` | modify | Scope parameter coverage for `lcm_grep` |

---

## Task 1: Port CC's `sanitizePath` to Python

**Files:**
- Create: `tests/test_workspace.py`
- Modify: `claude_lcm/workspace.py`

Reference: `/home/lucas/ai/hermes/claude-code/src/utils/sessionStoragePortable.ts:311-319`. CC's rule is `name.replace(/[^a-zA-Z0-9]/g, '-')` with a truncate+hash fallback past 200 chars. For paths under 200 chars the output is bit-identical to CC's Node fallback branch. `~/.claude/projects/` directories are proof; use one as a parity fixture.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_workspace.py
"""Tests for sanitize_path parity with CC's sessionStoragePortable.sanitizePath."""

from __future__ import annotations

from pathlib import Path

from claude_lcm.workspace import MAX_SANITIZED_LENGTH, sanitize_path


def test_sanitize_path_simple_absolute():
    assert sanitize_path("/home/lucas/ai/claude-lcm") == "-home-lucas-ai-claude-lcm"


def test_sanitize_path_collapses_all_non_alnum():
    # Matches CC: /[^a-zA-Z0-9]/g -> '-'. Dots, slashes, underscores, colons all collapse.
    assert sanitize_path("/tmp/a_b.c:d") == "-tmp-a-b-c-d"


def test_sanitize_path_resolves_relative(tmp_path: Path):
    # Relative paths are absolutized against cwd, not left raw.
    raw = "./foo"
    out = sanitize_path(raw)
    assert out.startswith("-")
    assert "foo" in out


def test_sanitize_path_expands_home():
    out = sanitize_path("~/ai/claude-lcm")
    assert "claude-lcm" in out
    assert "~" not in out


def test_sanitize_path_long_path_truncates_with_hash():
    long_dir = "/a" + ("/very-long-segment" * 30)  # > 200 alnum chars
    out = sanitize_path(long_dir)
    assert len(out) > MAX_SANITIZED_LENGTH  # has hash suffix
    # First MAX_SANITIZED_LENGTH chars are the plain replacement truncated
    plain = long_dir.replace("/", "-").replace(".", "-")
    # replace all non-alnum with '-' in plain
    import re
    plain = re.sub(r"[^a-zA-Z0-9]", "-", long_dir)
    assert out.startswith(plain[:MAX_SANITIZED_LENGTH] + "-")


def test_sanitize_path_parity_with_live_claude_projects_dir():
    """Smoke-test against a real ~/.claude/projects/* directory.

    Picks any subdirectory of ~/.claude/projects and reconstructs the
    original path by replacing hyphens with slashes. Not strictly sound
    (ambiguous for paths containing hyphens), but for the common case of
    a path like /home/lucas/ai/claude-lcm it works as a parity probe.
    If ~/.claude/projects does not exist, the test skips.
    """
    import os
    import pytest

    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.is_dir():
        pytest.skip("no ~/.claude/projects on this machine")
    for child in projects_dir.iterdir():
        if not child.is_dir():
            continue
        name = child.name
        # Reconstruct a plausible absolute path from the sanitized name.
        candidate = "/" + name.lstrip("-").replace("-", "/")
        if os.path.isabs(candidate) and Path(candidate).exists():
            assert sanitize_path(candidate) == name
            return
    pytest.skip("no reconstructible projects dir found")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
PYTHONPATH= PYTHONNOUSERSITE=1 .venv/bin/python -m pytest tests/test_workspace.py -v
```

Expected: `ImportError` or `AttributeError: module 'claude_lcm.workspace' has no attribute 'sanitize_path'`.

- [ ] **Step 3: Implement `sanitize_path` in `claude_lcm/workspace.py`**

Append to the end of `claude_lcm/workspace.py`:

```python
import re

MAX_SANITIZED_LENGTH = 200
_NON_ALNUM_RE = re.compile(r"[^a-zA-Z0-9]")


def _djb2_hash(s: str) -> str:
    """Port of the Node-fallback simpleHash in CC's sessionStoragePortable.

    CC itself uses Bun.hash under the CLI; its SDK fallback uses djb2. We
    match the djb2 fallback so our output is reproducible on pure-Python
    installs. Paths under MAX_SANITIZED_LENGTH are unaffected and stay
    bit-perfect with CC.
    """
    h = 5381
    for ch in s:
        h = ((h * 33) + ord(ch)) & 0xFFFFFFFF
    # JS Math.abs(djb2Hash(str)).toString(36)
    return _int_to_base36(h)


def _int_to_base36(n: int) -> str:
    if n == 0:
        return "0"
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = []
    while n > 0:
        out.append(digits[n % 36])
        n //= 36
    return "".join(reversed(out))


def sanitize_path(name: str) -> str:
    """Python port of CC's `sanitizePath` (sessionStoragePortable.ts).

    Input is an arbitrary path; it is expanded (`~`) and absolutized
    before sanitizing so that `sanitize_path("./foo")` and
    `sanitize_path(os.path.abspath("./foo"))` agree. Non-alphanumeric
    characters are replaced with `-`. Over-long outputs are truncated
    and suffixed with a djb2-based hash, matching the CC Node fallback.
    """
    abs_path = os.path.abspath(os.path.expanduser(name))
    sanitized = _NON_ALNUM_RE.sub("-", abs_path)
    if len(sanitized) <= MAX_SANITIZED_LENGTH:
        return sanitized
    return f"{sanitized[:MAX_SANITIZED_LENGTH]}-{_djb2_hash(abs_path)}"
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
PYTHONPATH= PYTHONNOUSERSITE=1 .venv/bin/python -m pytest tests/test_workspace.py -v
```

Expected: all tests green (parity test may skip if `~/.claude/projects` is unreconstructible).

- [ ] **Step 5: Commit**

```bash
git add claude_lcm/workspace.py tests/test_workspace.py
git commit -m "feat(workspace): port CC's sanitizePath for project_key identity"
```

---

## Task 2: Schema migration — new columns + `clear_handoff` table

**Files:**
- Modify: `claude_lcm/store.py:43-128` (the `_init_db` executescript block)
- Modify: `tests/test_store_extensions.py`

SQLite `ALTER TABLE ADD COLUMN` is idempotent only via try/except or a pragma check. Use `PRAGMA table_info(sessions)` to detect existing columns and add only the missing ones. `CREATE TABLE IF NOT EXISTS` handles `clear_handoff`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_store_extensions.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
PYTHONPATH= PYTHONNOUSERSITE=1 .venv/bin/python -m pytest tests/test_store_extensions.py::test_store_init_adds_project_key_and_handoff -v
```

Expected: assertion error — columns/table not present yet.

- [ ] **Step 3: Extend `_init_db` in `claude_lcm/store.py`**

After the existing `executescript` block (around line 123), before `self._conn.execute("INSERT OR IGNORE INTO metadata ...")`, add:

```python
        # --- /clear lineage migration (additive, idempotent) ---
        existing = {
            r[1] for r in self._conn.execute(
                "PRAGMA table_info(sessions)"
            ).fetchall()
        }
        if "project_key" not in existing:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN project_key TEXT"
            )
        if "parent_session_id" not in existing:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN parent_session_id TEXT"
            )
        if "end_reason" not in existing:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN end_reason TEXT"
            )
        self._conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_sessions_project_key_ended
               ON sessions(project_key, ended_at DESC)"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS clear_handoff (
                project_key       TEXT PRIMARY KEY,
                ending_session_id TEXT NOT NULL,
                ts                REAL NOT NULL
            )"""
        )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
PYTHONPATH= PYTHONNOUSERSITE=1 .venv/bin/python -m pytest tests/test_store_extensions.py -v
```

Expected: new tests pass; existing tests still pass.

- [ ] **Step 5: Commit**

```bash
git add claude_lcm/store.py tests/test_store_extensions.py
git commit -m "feat(store): additive /clear lineage schema migration"
```

---

## Task 3: Extend `open_session` with `project_key` and `parent_session_id`

**Files:**
- Modify: `claude_lcm/store.py:205-224` (the `open_session` method)
- Modify: `claude_lcm/engine.py:35-46` (the engine `open_session` wrapper)
- Modify: `tests/test_store_extensions.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_store_extensions.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
PYTHONPATH= PYTHONNOUSERSITE=1 .venv/bin/python -m pytest tests/test_store_extensions.py::test_open_session_persists_project_key_and_parent -v
```

Expected: `TypeError: open_session() got an unexpected keyword argument 'project_key'`.

- [ ] **Step 3: Update `open_session` in `claude_lcm/store.py`**

Replace the body of `open_session` (around line 205) with:

```python
    def open_session(self, session_id: str, agent_kind: str,
                     workspace_fingerprint: str | None = None,
                     workspace_path: str | None = None,
                     project_key: str | None = None,
                     parent_session_id: str | None = None,
                     metadata: Dict[str, Any] | None = None) -> None:
        """Insert a sessions row. Idempotent via INSERT OR IGNORE."""
        self._conn.execute(
            """INSERT OR IGNORE INTO sessions
               (session_id, agent_kind, workspace_fingerprint,
                workspace_path, project_key, parent_session_id,
                started_at, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                agent_kind,
                workspace_fingerprint,
                workspace_path,
                project_key,
                parent_session_id,
                time.time(),
                json.dumps(metadata) if metadata else None,
            ),
        )
        self._conn.commit()
```

Also add a setter used later by SessionStart when the parent is discovered *after* the row already exists:

```python
    def set_parent_session(self, session_id: str, parent_session_id: str) -> None:
        self._conn.execute(
            "UPDATE sessions SET parent_session_id = ? WHERE session_id = ?",
            (parent_session_id, session_id),
        )
        self._conn.commit()
```

Update `get_session` to include the new columns in its SELECT + column list.

- [ ] **Step 4: Update `ClaudeLcmEngine.open_session` in `claude_lcm/engine.py`**

Replace the `open_session` method (around line 35) with:

```python
    def open_session(self, session_id: str, agent_kind: str = "claude-code",
                     workspace_fingerprint: str | None = None,
                     workspace_path: str | None = None,
                     project_key: str | None = None,
                     parent_session_id: str | None = None,
                     metadata: Dict[str, Any] | None = None) -> None:
        self._session_id = session_id
        self._store.open_session(
            session_id=session_id,
            agent_kind=agent_kind,
            workspace_fingerprint=workspace_fingerprint,
            workspace_path=workspace_path,
            project_key=project_key,
            parent_session_id=parent_session_id,
            metadata=metadata,
        )

    def set_parent_session(self, session_id: str, parent_session_id: str) -> None:
        self._store.set_parent_session(session_id, parent_session_id)
```

- [ ] **Step 5: Run tests and commit**

```bash
PYTHONPATH= PYTHONNOUSERSITE=1 .venv/bin/python -m pytest tests/ -v
git add claude_lcm/store.py claude_lcm/engine.py tests/test_store_extensions.py
git commit -m "feat(store): open_session accepts project_key and parent_session_id"
```

---

## Task 4: `set_end_reason`, `upsert_clear_handoff`, `take_clear_handoff`

**Files:**
- Modify: `claude_lcm/store.py` (add three methods near `close_session`)
- Modify: `claude_lcm/engine.py` (thin wrappers)
- Modify: `tests/test_store_extensions.py`

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
PYTHONPATH= PYTHONNOUSERSITE=1 .venv/bin/python -m pytest tests/test_store_extensions.py -v -k "end_reason or handoff"
```

Expected: `AttributeError` on the three methods.

- [ ] **Step 3: Implement the three methods in `claude_lcm/store.py`**

Add near the existing `close_session` method:

```python
    def set_end_reason(self, session_id: str, end_reason: str) -> None:
        self._conn.execute(
            "UPDATE sessions SET end_reason = ? WHERE session_id = ?",
            (end_reason, session_id),
        )
        self._conn.commit()

    def upsert_clear_handoff(self, project_key: str,
                             ending_session_id: str) -> None:
        """Record that a /clear just ended session `ending_session_id`.

        Keyed on project_key — the immediately-following SessionStart
        hook in the same CC process will pick this up via
        take_clear_handoff. Safe to call repeatedly: later writes
        overwrite orphan rows left by a previous /clear whose
        SessionStart never fired (user quit between the two hooks).
        """
        self._conn.execute(
            """INSERT INTO clear_handoff (project_key, ending_session_id, ts)
               VALUES (?, ?, ?)
               ON CONFLICT(project_key) DO UPDATE SET
                   ending_session_id = excluded.ending_session_id,
                   ts = excluded.ts""",
            (project_key, ending_session_id, time.time()),
        )
        self._conn.commit()

    def take_clear_handoff(self, project_key: str) -> str | None:
        """Atomically consume a pending handoff for `project_key`.

        Returns the ending session id if present and deletes the row,
        or None if no handoff is pending.
        """
        with self._conn:
            row = self._conn.execute(
                "SELECT ending_session_id FROM clear_handoff WHERE project_key = ?",
                (project_key,),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "DELETE FROM clear_handoff WHERE project_key = ?",
                (project_key,),
            )
            return row[0]
```

- [ ] **Step 4: Add engine passthroughs in `claude_lcm/engine.py`**

```python
    def set_end_reason(self, session_id: str, end_reason: str) -> None:
        self._store.set_end_reason(session_id, end_reason)

    def upsert_clear_handoff(self, project_key: str,
                             ending_session_id: str) -> None:
        self._store.upsert_clear_handoff(project_key, ending_session_id)

    def take_clear_handoff(self, project_key: str) -> str | None:
        return self._store.take_clear_handoff(project_key)
```

- [ ] **Step 5: Run tests and commit**

```bash
PYTHONPATH= PYTHONNOUSERSITE=1 .venv/bin/python -m pytest tests/ -v
git add claude_lcm/store.py claude_lcm/engine.py tests/test_store_extensions.py
git commit -m "feat(store): clear_handoff upsert/take + set_end_reason"
```

---

## Task 5: `walk_lineage` (recursive CTE) + `project_key_for_session`

**Files:**
- Modify: `claude_lcm/store.py`
- Modify: `claude_lcm/engine.py`
- Modify: `tests/test_store_extensions.py`

- [ ] **Step 1: Write the failing tests**

```python
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
    """A -> B -> C (sibling), A -> D (resumed then cleared)."""
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
PYTHONPATH= PYTHONNOUSERSITE=1 .venv/bin/python -m pytest tests/test_store_extensions.py -v -k "lineage or project_key_for"
```

Expected: `AttributeError`.

- [ ] **Step 3: Implement the two methods in `claude_lcm/store.py`**

Add near the other session helpers:

```python
    def walk_lineage(self, session_id: str) -> List[str]:
        """Return [session_id, parent, grandparent, ...] via recursive CTE.

        The walk stops when parent_session_id is NULL or when a cycle is
        detected (defensive: the recursive CTE would otherwise loop).
        """
        rows = self._conn.execute(
            """WITH RECURSIVE lineage(sid, depth) AS (
                   SELECT ?, 0
                 UNION ALL
                   SELECT s.parent_session_id, l.depth + 1
                     FROM sessions s
                     JOIN lineage l ON s.session_id = l.sid
                    WHERE s.parent_session_id IS NOT NULL
                      AND l.depth < 1000
               )
               SELECT sid FROM lineage""",
            (session_id,),
        ).fetchall()
        return [r[0] for r in rows]

    def project_key_for_session(self, session_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT project_key FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return row[0] if row else None
```

- [ ] **Step 4: Engine passthroughs**

In `claude_lcm/engine.py`:

```python
    def walk_lineage(self, session_id: str) -> list[str]:
        return self._store.walk_lineage(session_id)

    def project_key_for_session(self, session_id: str) -> str | None:
        return self._store.project_key_for_session(session_id)
```

- [ ] **Step 5: Run tests and commit**

```bash
PYTHONPATH= PYTHONNOUSERSITE=1 .venv/bin/python -m pytest tests/ -v
git add claude_lcm/store.py claude_lcm/engine.py tests/test_store_extensions.py
git commit -m "feat(store): walk_lineage recursive CTE + project_key_for_session"
```

---

## Task 6: `session_end.py` — write handoff on `source='clear'`

**Files:**
- Modify: `adapter/hooks/session_end.py`
- Modify: `tests/test_hooks.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_hooks.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
PYTHONPATH= PYTHONNOUSERSITE=1 .venv/bin/python -m pytest tests/test_hooks.py -v -k "source_clear or source_normal"
```

Expected: assertion failures — `end_reason` is NULL and no handoff row exists.

- [ ] **Step 3: Rewrite `adapter/hooks/session_end.py`**

```python
#!/usr/bin/env python3
"""SessionEnd hook — close the session row and, on /clear, hand off lineage."""

from __future__ import annotations

from typing import Any, Dict

from adapter.hooks._common import engine_for, safe_main, write_response
from claude_lcm.workspace import sanitize_path


def handle(payload: Dict[str, Any]) -> None:
    session_id = payload.get("session_id") or payload.get("sessionId")
    if not session_id:
        return
    source = payload.get("source")
    cwd = payload.get("cwd")
    with engine_for(session_id) as eng:
        eng.close_session(session_id)
        if source == "clear":
            eng.set_end_reason(session_id, "clear")
            if cwd:
                eng.upsert_clear_handoff(
                    project_key=sanitize_path(cwd),
                    ending_session_id=session_id,
                )
        else:
            eng.set_end_reason(session_id, "normal")
    write_response({"continue": True})


if __name__ == "__main__":
    safe_main(handle)
```

- [ ] **Step 4: Run tests**

```bash
PYTHONPATH= PYTHONNOUSERSITE=1 .venv/bin/python -m pytest tests/test_hooks.py -v
```

Expected: new tests pass; existing pass.

- [ ] **Step 5: Commit**

```bash
git add adapter/hooks/session_end.py tests/test_hooks.py
git commit -m "feat(hooks): session_end writes clear_handoff on source=clear"
```

---

## Task 7: `session_start.py` — compute `project_key` and consume handoff

**Files:**
- Modify: `adapter/hooks/session_start.py`
- Modify: `tests/test_hooks.py`

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
PYTHONPATH= PYTHONNOUSERSITE=1 .venv/bin/python -m pytest tests/test_hooks.py -v -k "project_key or source_clear_links or source_clear_no_handoff"
```

Expected: `project_key` column NULL; `parent_session_id` NULL.

- [ ] **Step 3: Rewrite `adapter/hooks/session_start.py`**

```python
#!/usr/bin/env python3
"""SessionStart hook — register a session row in the vault."""

from __future__ import annotations

from typing import Any, Dict

from adapter.hooks._common import engine_for, safe_main, write_response
from claude_lcm.workspace import fingerprint, sanitize_path


def handle(payload: Dict[str, Any]) -> None:
    session_id = payload.get("session_id") or payload.get("sessionId")
    if not session_id:
        return
    cwd = payload.get("cwd")
    source = payload.get("source")
    project_key = sanitize_path(cwd) if cwd else None
    fp, path = fingerprint(cwd)

    with engine_for(session_id) as eng:
        eng.open_session(
            session_id=session_id,
            agent_kind="claude-code",
            workspace_fingerprint=fp,
            workspace_path=path,
            project_key=project_key,
            metadata={
                "source": source,
                "transcript_path": payload.get("transcript_path"),
            },
        )
        if source == "clear" and project_key:
            parent_sid = eng.take_clear_handoff(project_key)
            if parent_sid:
                eng.set_parent_session(session_id, parent_sid)

    write_response({
        "continue": True,
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": (
                f"claude-lcm: this Claude Code session_id is {session_id}. "
                f"Pass it as the `session_id` argument on every lcm_* tool call "
                f"so the vault scopes results to this session."
            ),
        },
    })


if __name__ == "__main__":
    safe_main(handle)
```

Also add `set_parent_session` passthrough to `claude_lcm/engine.py` if not already present (Task 3 should have covered it).

- [ ] **Step 4: Run tests**

```bash
PYTHONPATH= PYTHONNOUSERSITE=1 .venv/bin/python -m pytest tests/ -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add adapter/hooks/session_start.py claude_lcm/engine.py tests/test_hooks.py
git commit -m "feat(hooks): session_start stamps project_key and consumes clear handoff"
```

---

## Task 8: End-to-end `/clear` chain subprocess test

**Files:**
- Modify: `tests/test_hooks.py`

Verifies Task 6 + Task 7 work together through the real subprocess pipeline (catching any import or serialization regressions the unit tests miss) and that lineage walks correctly over multiple `/clear`s.

- [ ] **Step 1: Write the failing test**

```python
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
    assert store.take_clear_handoff("-" + cwd.lstrip("/").replace("/", "-")) is None
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
```

- [ ] **Step 2: Run tests**

```bash
PYTHONPATH= PYTHONNOUSERSITE=1 .venv/bin/python -m pytest tests/test_hooks.py::test_clear_chain_end_to_end tests/test_hooks.py::test_clear_chains_do_not_cross_projects -v
```

Expected: both pass.

- [ ] **Step 3: Commit**

```bash
git add tests/test_hooks.py
git commit -m "test(hooks): end-to-end /clear chain + multi-project isolation"
```

---

## Task 9: Extend `store.search` with a `session_ids` filter

**Files:**
- Modify: `claude_lcm/store.py:358-400` (the `search` method)
- Modify: `tests/test_store_extensions.py`

Current `search` accepts `session_id: str | None`. Add an optional `session_ids: list[str] | None` parameter for scope='lineage' and scope='workspace'. Keep `session_id` for backward compat.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
PYTHONPATH= PYTHONNOUSERSITE=1 .venv/bin/python -m pytest tests/test_store_extensions.py::test_search_filters_by_session_ids -v
```

Expected: `TypeError: search() got an unexpected keyword argument 'session_ids'`.

- [ ] **Step 3: Update `search` in `claude_lcm/store.py`**

```python
    def search(self, query: str, session_id: str | None = None,
               session_ids: list[str] | None = None,
               limit: int = 20) -> List[Dict[str, Any]]:
        """FTS5 search across messages.

        At most one of `session_id` or `session_ids` should be provided.
        If both are None, searches all sessions in the vault.
        """
        if session_ids is not None:
            if not session_ids:
                return []
            placeholders = ",".join("?" * len(session_ids))
            sql = f"""
                SELECT m.*, snippet(messages_fts, 0, '>>>', '<<<', '...', 40) as snippet
                  FROM messages_fts fts
                  JOIN messages m ON m.store_id = fts.rowid
                 WHERE messages_fts MATCH ?
                   AND m.session_id IN ({placeholders})
                 ORDER BY rank LIMIT ?
            """
            params = (query, *session_ids, limit)
            rows = self._conn.execute(sql, params).fetchall()
        elif session_id:
            rows = self._conn.execute(
                """SELECT m.*, snippet(messages_fts, 0, '>>>', '<<<', '...', 40) as snippet
                   FROM messages_fts fts
                   JOIN messages m ON m.store_id = fts.rowid
                   WHERE messages_fts MATCH ? AND m.session_id = ?
                   ORDER BY rank LIMIT ?""",
                (query, session_id, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """SELECT m.*, snippet(messages_fts, 0, '>>>', '<<<', '...', 40) as snippet
                   FROM messages_fts fts
                   JOIN messages m ON m.store_id = fts.rowid
                   WHERE messages_fts MATCH ?
                   ORDER BY rank LIMIT ?""",
                (query, limit),
            ).fetchall()
        # ... existing result assembly loop unchanged ...
```

(Keep the existing snippet-assembly / row-to-dict code after the `rows = ...` assignment.)

- [ ] **Step 4: Run tests**

```bash
PYTHONPATH= PYTHONNOUSERSITE=1 .venv/bin/python -m pytest tests/ -v
```

- [ ] **Step 5: Commit**

```bash
git add claude_lcm/store.py tests/test_store_extensions.py
git commit -m "feat(store): search accepts session_ids list filter"
```

---

## Task 10: Add `scope` parameter to `lcm_grep` schema and handler

**Files:**
- Modify: `claude_lcm/schemas.py` (LCM_GREP and, for consistency, LCM_DESCRIBE / LCM_EXPAND / LCM_STATUS / LCM_DOCTOR)
- Modify: `claude_lcm/tools.py` (add `_resolve_scope_session_ids` helper; wire into `lcm_grep`)
- Modify: `tests/test_mcp_server.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_mcp_server.py`:

```python
def test_lcm_grep_scope_lineage_walks_parents(tmp_path, monkeypatch):
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
PYTHONPATH= PYTHONNOUSERSITE=1 .venv/bin/python -m pytest tests/test_mcp_server.py -v -k "scope"
```

Expected: failure — `scope` is currently ignored.

- [ ] **Step 3: Update `LCM_GREP` schema in `claude_lcm/schemas.py`**

Add a `scope` property to the `LCM_GREP` parameters and (identically) to `LCM_DESCRIBE`, `LCM_EXPAND`, `LCM_STATUS`, `LCM_DOCTOR`:

```python
_SCOPE_PARAM = {
    "type": "string",
    "enum": ["lineage", "workspace", "session"],
    "description": (
        "Search scope. 'lineage' (default) walks parent_session_id from "
        "the current session transitively — includes prior sessions "
        "chained by /clear. 'workspace' widens to every session in the "
        "same project_key (sanitized cwd). 'session' limits to the "
        "current session_id only."
    ),
    "default": "lineage",
}
```

and add `"scope": _SCOPE_PARAM` inside each tool's `parameters.properties`.

- [ ] **Step 4: Implement `_resolve_scope_session_ids` + wire into `lcm_grep` in `claude_lcm/tools.py`**

Add near the top of `tools.py`:

```python
def _resolve_scope_session_ids(engine: "ClaudeLcmEngine",
                                scope: str) -> list[str] | None:
    """Return the list of session ids to filter by for a given scope.

    Returns None to mean "do not filter by session ids" (scope='workspace'
    uses a project_key filter instead — the caller should handle that).
    """
    current = engine._session_id
    if not current:
        return []
    if scope == "session":
        return [current]
    if scope == "workspace":
        pk = engine._store.project_key_for_session(current)
        if pk is None:
            return [current]
        rows = engine._store._conn.execute(
            "SELECT session_id FROM sessions WHERE project_key = ?",
            (pk,),
        ).fetchall()
        return [r[0] for r in rows]
    # default: lineage
    return engine._store.walk_lineage(current)
```

Then update the `lcm_grep` body to use it:

```python
def lcm_grep(args: Dict[str, Any], **kwargs) -> str:
    engine = _require_engine(kwargs)
    if engine is None:
        return json.dumps({"error": "claude-lcm engine not initialized"})

    query = args.get("query", "").strip()
    if not query:
        return json.dumps({"error": "No query provided"})

    limit = args.get("limit", 10)
    scope = args.get("scope", "lineage")
    if scope not in ("lineage", "workspace", "session"):
        scope = "lineage"

    session_ids = _resolve_scope_session_ids(engine, scope)
    results = []
    try:
        msg_hits = engine._store.search(
            query, session_ids=session_ids, limit=limit,
        )
        for hit in msg_hits:
            results.append({
                "type": "message",
                "depth": "raw",
                "store_id": hit["store_id"],
                "role": hit["role"],
                "snippet": hit.get("snippet", hit.get("content", "")[:200]),
            })
    except Exception as exc:
        logger.debug("Message search failed: %s", exc)

    # DAG search unchanged — empty in v1
    try:
        node_hits = engine._dag.search(query, session_id=engine._session_id,
                                        limit=limit)
        for node in node_hits:
            results.append({
                "type": "summary",
                "depth": f"d{node.depth}",
                "node_id": node.node_id,
                "snippet": node.summary[:300],
                "token_count": node.token_count,
                "expand_hint": node.expand_hint,
            })
    except Exception as exc:
        logger.debug("Node search failed: %s", exc)

    results.sort(key=lambda r: (0 if r["type"] == "message" else 1,
                                 r.get("depth", "")))
    return json.dumps({
        "query": query,
        "scope": scope,
        "total_results": len(results),
        "results": results[:limit],
    })
```

- [ ] **Step 5: Run full test suite**

```bash
PYTHONPATH= PYTHONNOUSERSITE=1 .venv/bin/python -m pytest tests/ -v
```

Expected: all tests pass (new scope tests + all prior tests).

- [ ] **Step 6: Commit**

```bash
git add claude_lcm/schemas.py claude_lcm/tools.py tests/test_mcp_server.py
git commit -m "feat(tools): lcm_grep scope parameter (lineage|workspace|session)"
```

---

## Task 11: Spec trace — document the `/clear` flow in `CLAUDE.md` (project-local, not committed)

Not required for v1 merge, and `CLAUDE.md` is `.gitignore`'d. Skip unless you want a local memo for future sessions.

---

## Verification pass (run at the end of the plan)

```bash
PYTHONPATH= PYTHONNOUSERSITE=1 .venv/bin/python -m pytest tests/ -v
```

All existing tests plus the new ones in `test_workspace.py`, `test_store_extensions.py`, `test_hooks.py`, `test_mcp_server.py` must pass. No tests should have been removed.

Manual smoke test (optional — requires live CC restart):

```bash
.venv/bin/python -m adapter.install
# restart Claude Code
# in a CC session: send a message; /clear; send another message; ask
#   "Use lcm_grep with scope=lineage to find my first message"
# Expected: the pre-/clear message is returned.
```

## Out-of-scope guardrails

- **Do not** move the vault path. `LCM_VAULT_PATH` default stays XDG.
- **Do not** change `workspace_fingerprint` semantics. Leave that column populated from git remote for metadata; it is no longer load-bearing.
- **Do not** add timestamp TTLs on `clear_handoff`; the upsert overwrite is the cleanup mechanism.
- **Do not** touch `claude_lcm/dag.py` — compaction is v2.
- **Do not** change `hermes-lcm` base schema (the original `messages`, `metadata`, `messages_fts` tables). All new columns live on the already-claude-lcm-owned `sessions` table, and `clear_handoff` is a new claude-lcm-only table.
- **Do not** invoke plan-document-reviewer or any other subagent without an explicit user request.
