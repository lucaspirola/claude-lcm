"""Standalone engine — owns the vault and exposes ingest + query methods.

Unlike hermes-lcm's LCMEngine (which inherits a Hermes ABC), this engine
is pure Python and has no host coupling. It exposes `_store`, `_dag`,
and `_session_id` as attributes because the lifted tool handlers in
`tools.py` access them directly.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import ClaudeLcmConfig
from .dag import SummaryDAG
from .store import MessageStore
from .tokens import count_tokens

logger = logging.getLogger(__name__)


class ClaudeLcmEngine:
    """Owns a single vault file and provides append/query operations."""

    def __init__(self, config: ClaudeLcmConfig | None = None,
                 session_id: str | None = None):
        self._config = config or ClaudeLcmConfig.from_env()
        self._store = MessageStore(self._config.vault_path)
        self._dag = SummaryDAG(self._config.vault_path)
        self._session_id: str | None = session_id

    # -- Session lifecycle --------------------------------------------------

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

    def set_end_reason(self, session_id: str, end_reason: str) -> None:
        self._store.set_end_reason(session_id, end_reason)

    def upsert_clear_handoff(self, project_key: str,
                             ending_session_id: str) -> None:
        self._store.upsert_clear_handoff(project_key, ending_session_id)

    def take_clear_handoff(self, project_key: str) -> str | None:
        return self._store.take_clear_handoff(project_key)

    def close_session(self, session_id: str | None = None) -> None:
        sid = session_id or self._session_id
        if sid:
            self._store.close_session(sid)

    # -- Ingest -------------------------------------------------------------

    def ingest_message(self, msg: Dict[str, Any],
                       session_id: str | None = None) -> int:
        sid = session_id or self._session_id
        if not sid:
            raise RuntimeError("no active session — call open_session first")
        tokens = count_tokens(msg.get("content") or "")
        return self._store.append(sid, msg, token_estimate=tokens)

    def ingest_messages(self, messages: List[Dict[str, Any]],
                        session_id: str | None = None) -> List[int]:
        sid = session_id or self._session_id
        if not sid:
            raise RuntimeError("no active session — call open_session first")
        estimates = [count_tokens(m.get("content") or "") for m in messages]
        return self._store.append_batch(sid, messages, token_estimates=estimates)

    def ingest_skill_load(self, skill_name: str,
                          skill_path: str | None = None,
                          content_hash: str | None = None,
                          message_id: int | None = None,
                          session_id: str | None = None) -> int:
        sid = session_id or self._session_id
        if not sid:
            raise RuntimeError("no active session — call open_session first")
        return self._store.append_skill_load(
            session_id=sid,
            skill_name=skill_name,
            skill_path=skill_path,
            content_hash=content_hash,
            message_id=message_id,
        )

    def ingest_file_snapshot(self, file_path: str, op: str,
                             content: bytes | None = None,
                             external_uri: str | None = None,
                             message_id: int | None = None,
                             session_id: str | None = None) -> int:
        sid = session_id or self._session_id
        if not sid:
            raise RuntimeError("no active session — call open_session first")
        if (content is not None
                and len(content) > self._config.max_snapshot_bytes):
            # fall back to storing a hash-only reference so the vault
            # doesn't balloon on accidentally-huge files
            content = None
            external_uri = external_uri or f"oversize://{file_path}"
        return self._store.append_file_snapshot(
            session_id=sid,
            file_path=file_path,
            op=op,
            content=content,
            external_uri=external_uri,
            message_id=message_id,
        )

    # -- Query --------------------------------------------------------------

    def grep(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        return self._store.search(
            query, session_id=self._session_id, limit=limit
        )

    def vault_path(self) -> Path:
        return Path(self._config.vault_path)

    # -- Lifecycle ----------------------------------------------------------

    def close(self) -> None:
        try:
            self._store.close()
        except Exception:
            pass
        try:
            self._dag.close()
        except Exception:
            pass
