"""Immutable message store — the source of truth.

Every message is persisted verbatim and never modified. The store is
append-only with optional pruning of very old messages (configurable).

Each message gets a monotonic store_id used as a stable reference.

---
Adapted from hermes-lcm (https://github.com/stephenschoettler/hermes-lcm)
store.py at v0.2.0, Copyright (c) Stephen Schoettler, MIT License.
Extensions for claude-lcm: `sessions`, `skill_loads`, `file_snapshots`
tables and their append methods. Base schema unchanged to preserve
cross-agent vault compatibility with hermes-lcm.
"""

import hashlib
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class MessageStore:
    """SQLite-backed immutable message store."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _init_db(self):
        self._conn = sqlite3.connect(
            str(self.db_path), timeout=5.0, check_same_thread=False
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                store_id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                tool_call_id TEXT,
                tool_calls TEXT,
                tool_name TEXT,
                timestamp REAL NOT NULL,
                token_estimate INTEGER DEFAULT 0,
                pinned INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_msg_session
                ON messages(session_id, store_id);
            CREATE INDEX IF NOT EXISTS idx_msg_session_ts
                ON messages(session_id, timestamp);

            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                content,
                content=messages,
                content_rowid=store_id
            );

            CREATE TRIGGER IF NOT EXISTS msg_fts_insert
                AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts(rowid, content)
                    VALUES (new.store_id, new.content);
            END;

            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            -- claude-lcm extensions ----------------------------------------

            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                agent_kind TEXT NOT NULL,
                workspace_fingerprint TEXT,
                workspace_path TEXT,
                started_at REAL NOT NULL,
                ended_at REAL,
                metadata TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_fingerprint
                ON sessions(workspace_fingerprint);
            CREATE INDEX IF NOT EXISTS idx_sessions_started
                ON sessions(started_at);

            CREATE TABLE IF NOT EXISTS skill_loads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                message_id INTEGER,
                skill_name TEXT NOT NULL,
                skill_path TEXT,
                content_hash TEXT,
                loaded_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_skill_loads_session
                ON skill_loads(session_id, loaded_at);

            CREATE TABLE IF NOT EXISTS file_snapshots (
                snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                message_id INTEGER,
                file_path TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                content_blob BLOB,
                external_uri TEXT,
                captured_at REAL NOT NULL,
                op TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_snapshots_session
                ON file_snapshots(session_id, captured_at);
            CREATE INDEX IF NOT EXISTS idx_snapshots_path
                ON file_snapshots(file_path, captured_at);
            CREATE INDEX IF NOT EXISTS idx_snapshots_hash
                ON file_snapshots(content_hash);
        """)
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
                "ALTER TABLE sessions ADD COLUMN parent_session_id TEXT "
                "REFERENCES sessions(session_id)"
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
        # Backfill project_key from workspace_path for vaults created before this migration
        rows = self._conn.execute(
            "SELECT session_id, workspace_path FROM sessions "
            "WHERE project_key IS NULL AND workspace_path IS NOT NULL"
        ).fetchall()
        if rows:
            from claude_lcm.workspace import sanitize_path
            for sid, wp in rows:
                self._conn.execute(
                    "UPDATE sessions SET project_key = ? WHERE session_id = ?",
                    (sanitize_path(wp), sid),
                )
            self._conn.commit()
        self._conn.execute(
            "INSERT OR IGNORE INTO metadata(key, value) VALUES (?, ?)",
            ("schema_version", "1"),
        )
        self._conn.commit()

    # -- Write operations ---------------------------------------------------

    def append(self, session_id: str, msg: Dict[str, Any],
               token_estimate: int = 0) -> int:
        """Persist a message and return its store_id."""
        tool_calls = msg.get("tool_calls")
        tc_json = json.dumps(tool_calls) if tool_calls else None

        cur = self._conn.execute(
            """INSERT INTO messages
               (session_id, role, content, tool_call_id, tool_calls,
                tool_name, timestamp, token_estimate, pinned)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                msg.get("role", "unknown"),
                msg.get("content"),
                msg.get("tool_call_id"),
                tc_json,
                msg.get("tool_name"),
                msg.get("timestamp", time.time()),
                token_estimate,
                0,
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def append_batch(self, session_id: str,
                     messages: List[Dict[str, Any]],
                     token_estimates: List[int] | None = None) -> List[int]:
        """Persist multiple messages in one transaction. Returns store_ids."""
        if token_estimates is None:
            token_estimates = [0] * len(messages)

        ids = []
        ts = time.time()
        with self._conn:
            for msg, est in zip(messages, token_estimates):
                tc = msg.get("tool_calls")
                tc_json = json.dumps(tc) if tc else None
                cur = self._conn.execute(
                    """INSERT INTO messages
                       (session_id, role, content, tool_call_id, tool_calls,
                        tool_name, timestamp, token_estimate, pinned)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        session_id,
                        msg.get("role", "unknown"),
                        msg.get("content"),
                        msg.get("tool_call_id"),
                        tc_json,
                        msg.get("tool_name"),
                        msg.get("timestamp", ts),
                        est,
                        0,
                    ),
                )
                ids.append(cur.lastrowid)
        return ids

    def pin(self, store_id: int) -> None:
        self._conn.execute(
            "UPDATE messages SET pinned = 1 WHERE store_id = ?", (store_id,)
        )
        self._conn.commit()

    def unpin(self, store_id: int) -> None:
        self._conn.execute(
            "UPDATE messages SET pinned = 0 WHERE store_id = ?", (store_id,)
        )
        self._conn.commit()

    # -- Session registry (claude-lcm extension) -------------------------

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

    def set_parent_session(self, session_id: str, parent_session_id: str) -> None:
        """Stamp parent_session_id on an existing session row."""
        self._conn.execute(
            "UPDATE sessions SET parent_session_id = ? WHERE session_id = ?",
            (parent_session_id, session_id),
        )
        self._conn.commit()

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

    def close_session(self, session_id: str) -> None:
        self._conn.execute(
            "UPDATE sessions SET ended_at = ? WHERE session_id = ? AND ended_at IS NULL",
            (time.time(), session_id),
        )
        self._conn.commit()

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

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            """SELECT session_id, agent_kind, workspace_fingerprint,
                      workspace_path, started_at, ended_at, metadata
               FROM sessions WHERE session_id = ?""",
            (session_id,),
        ).fetchone()
        if not row:
            return None
        cols = ["session_id", "agent_kind", "workspace_fingerprint",
                "workspace_path", "started_at", "ended_at", "metadata"]
        d = dict(zip(cols, row))
        if d.get("metadata"):
            try:
                d["metadata"] = json.loads(d["metadata"])
            except (json.JSONDecodeError, TypeError):
                pass
        return d

    # -- Skill loads (claude-lcm extension) -------------------------------

    def append_skill_load(self, session_id: str, skill_name: str,
                          skill_path: str | None = None,
                          content_hash: str | None = None,
                          message_id: int | None = None) -> int:
        cur = self._conn.execute(
            """INSERT INTO skill_loads
               (session_id, message_id, skill_name, skill_path,
                content_hash, loaded_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (session_id, message_id, skill_name, skill_path,
             content_hash, time.time()),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_loaded_skill_names(self, session_id: str) -> set[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT skill_name FROM skill_loads WHERE session_id = ?",
            (session_id,),
        ).fetchall()
        return {r[0] for r in rows}

    # -- File snapshots (claude-lcm extension) ----------------------------

    def append_file_snapshot(self, session_id: str, file_path: str, op: str,
                             content: bytes | None = None,
                             external_uri: str | None = None,
                             message_id: int | None = None) -> int:
        """Append a file snapshot. One of `content` or `external_uri` must be set.

        In v1 we populate `content_blob` inline. v3 will flip to `external_uri`
        pointing at an agentfs-backed revision; the column already exists so
        no schema migration is needed.
        """
        if content is None and external_uri is None:
            raise ValueError("append_file_snapshot requires content or external_uri")
        content_hash = (
            hashlib.sha256(content).hexdigest() if content is not None
            else hashlib.sha256(external_uri.encode("utf-8")).hexdigest()
        )
        cur = self._conn.execute(
            """INSERT INTO file_snapshots
               (session_id, message_id, file_path, content_hash,
                content_blob, external_uri, captured_at, op)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, message_id, file_path, content_hash,
             content, external_uri, time.time(), op),
        )
        self._conn.commit()
        return cur.lastrowid

    # -- Read operations ----------------------------------------------------

    def get(self, store_id: int) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM messages WHERE store_id = ?", (store_id,)
        ).fetchone()
        return self._row_to_dict(row) if row else None

    def get_range(self, session_id: str, start_id: int = 0,
                  end_id: int | None = None,
                  limit: int = 1000) -> List[Dict[str, Any]]:
        if end_id is not None:
            rows = self._conn.execute(
                """SELECT * FROM messages
                   WHERE session_id = ? AND store_id >= ? AND store_id <= ?
                   ORDER BY store_id LIMIT ?""",
                (session_id, start_id, end_id, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """SELECT * FROM messages
                   WHERE session_id = ? AND store_id >= ?
                   ORDER BY store_id LIMIT ?""",
                (session_id, start_id, limit),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_session_messages(self, session_id: str,
                             limit: int = 10000) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT * FROM messages
               WHERE session_id = ?
               ORDER BY store_id LIMIT ?""",
            (session_id, limit),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_session_count(self, session_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return row[0] if row else 0

    def get_session_token_total(self, session_id: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(token_estimate), 0) FROM messages WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return row[0] if row else 0

    # -- Search -------------------------------------------------------------

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
            rows = self._conn.execute(
                f"""SELECT m.*, snippet(messages_fts, 0, '>>>', '<<<', '...', 40) as snippet
                   FROM messages_fts fts
                   JOIN messages m ON m.store_id = fts.rowid
                   WHERE messages_fts MATCH ?
                     AND m.session_id IN ({placeholders})
                   ORDER BY rank LIMIT ?""",
                (query, *session_ids, limit),
            ).fetchall()
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
        results = []
        for r in rows:
            d = self._row_to_dict(r)
            d["snippet"] = r[-1] if len(r) > 10 else ""
            results.append(d)
        return results

    # -- Helpers ------------------------------------------------------------

    def _row_to_dict(self, row) -> Dict[str, Any]:
        if row is None:
            return {}
        cols = [
            "store_id", "session_id", "role", "content", "tool_call_id",
            "tool_calls", "tool_name", "timestamp", "token_estimate", "pinned",
        ]
        d = dict(zip(cols, row[:len(cols)]))
        if d.get("tool_calls"):
            try:
                d["tool_calls"] = json.loads(d["tool_calls"])
            except (json.JSONDecodeError, TypeError):
                pass
        return d

    def to_openai_msg(self, stored: Dict[str, Any]) -> Dict[str, Any]:
        msg: Dict[str, Any] = {"role": stored["role"]}
        if stored.get("content") is not None:
            msg["content"] = stored["content"]
        if stored.get("tool_calls"):
            msg["tool_calls"] = stored["tool_calls"]
        if stored.get("tool_call_id"):
            msg["tool_call_id"] = stored["tool_call_id"]
        if stored.get("tool_name"):
            msg["name"] = stored["tool_name"]
        return msg

    # -- Lifecycle ----------------------------------------------------------

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None
