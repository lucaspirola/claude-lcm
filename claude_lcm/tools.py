"""Tool handlers — the code that runs when an MCP client invokes a tool.

---
Adapted from hermes-lcm
(https://github.com/stephenschoettler/hermes-lcm) tools.py at v0.2.0,
Copyright (c) Stephen Schoettler, MIT License. Changes: dropped
`_synthesize_expansion_answer` and `lcm_expand_query` (both require an
LLM for synthesis, deferred to v2); `lcm_status` / `lcm_doctor` rewritten
for v1 (no compaction) so they report only what's meaningful today —
vault paths, session metadata, FTS sync — instead of compaction stats.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, TYPE_CHECKING

if TYPE_CHECKING:
    from .engine import ClaudeLcmEngine

logger = logging.getLogger(__name__)


def _require_engine(kwargs: Dict[str, Any]) -> "ClaudeLcmEngine | None":
    engine = kwargs.get("engine")
    return engine if engine is not None else None


def _get_session_node(engine: "ClaudeLcmEngine", node_id: int):
    node = engine._dag.get_node(node_id)
    if node is None or node.session_id != engine._session_id:
        return None
    return node


def _expand_message_sources(engine: "ClaudeLcmEngine", node, max_tokens: int) -> list[dict[str, Any]]:
    from .tokens import count_tokens

    messages = []
    budget_used = 0
    for store_id in node.source_ids:
        stored = engine._store.get(store_id)
        if not stored or stored.get("session_id") != engine._session_id:
            continue
        content = stored.get("content", "")
        msg_tokens = count_tokens(content)
        if budget_used + msg_tokens > max_tokens and messages:
            messages.append(
                {
                    "note": f"Truncated — {len(node.source_ids) - len(messages)} more messages available",
                }
            )
            break
        messages.append(
            {
                "store_id": stored["store_id"],
                "role": stored["role"],
                "content": content[:2000] if len(content) > 2000 else content,
            }
        )
        budget_used += msg_tokens
    return messages


def _expand_child_nodes(engine: "ClaudeLcmEngine", node) -> list[dict[str, Any]]:
    children = [child for child in engine._dag.get_source_nodes(node) if child.session_id == engine._session_id]
    return [
        {
            "node_id": child.node_id,
            "depth": child.depth,
            "summary": child.summary[:1000],
            "token_count": child.token_count,
            "expand_hint": child.expand_hint,
        }
        for child in children
    ]


def lcm_grep(args: Dict[str, Any], **kwargs) -> str:
    """Search raw messages (and DAG nodes, if any) for the current session."""
    engine = _require_engine(kwargs)
    if engine is None:
        return json.dumps({"error": "claude-lcm engine not initialized"})

    query = args.get("query", "").strip()
    if not query:
        return json.dumps({"error": "No query provided"})

    limit = args.get("limit", 10)
    session_id = engine._session_id
    results = []

    try:
        msg_hits = engine._store.search(query, session_id=session_id, limit=limit)
        for hit in msg_hits:
            results.append(
                {
                    "type": "message",
                    "depth": "raw",
                    "store_id": hit["store_id"],
                    "role": hit["role"],
                    "snippet": hit.get("snippet", hit.get("content", "")[:200]),
                }
            )
    except Exception as exc:
        logger.debug("Message search failed: %s", exc)

    try:
        node_hits = engine._dag.search(query, session_id=session_id, limit=limit)
        for node in node_hits:
            results.append(
                {
                    "type": "summary",
                    "depth": f"d{node.depth}",
                    "node_id": node.node_id,
                    "snippet": node.summary[:300],
                    "token_count": node.token_count,
                    "expand_hint": node.expand_hint,
                }
            )
    except Exception as exc:
        logger.debug("Node search failed: %s", exc)

    results.sort(key=lambda result: (0 if result["type"] == "message" else 1, result.get("depth", "")))
    return json.dumps({"query": query, "total_results": len(results), "results": results[:limit]})


def lcm_describe(args: Dict[str, Any], **kwargs) -> str:
    """Inspect a node's subtree or get session overview."""
    engine = _require_engine(kwargs)
    if engine is None:
        return json.dumps({"error": "claude-lcm engine not initialized"})

    node_id = args.get("node_id")
    session_id = engine._session_id

    if node_id is not None:
        node = _get_session_node(engine, node_id)
        if node is None:
            return json.dumps({"error": f"Node {node_id} not found in current session"})
        info = engine._dag.describe_subtree(node_id)
        return json.dumps(info)

    all_nodes = engine._dag.get_session_nodes(session_id)
    overview = {
        "session_id": session_id,
        "store_message_count": engine._store.get_session_count(session_id),
        "dag_node_count": len(all_nodes),
        "depths": {},
    }

    if not all_nodes:
        overview["note"] = (
            "No DAG nodes in this session (v1 has no compaction). "
            "Use lcm_grep to search raw messages."
        )

    for depth in sorted({node.depth for node in all_nodes}):
        nodes = [node for node in all_nodes if node.depth == depth]
        overview["depths"][f"d{depth}"] = {
            "count": len(nodes),
            "total_tokens": sum(node.token_count for node in nodes),
            "total_source_tokens": sum(node.source_token_count for node in nodes),
            "nodes": [
                {
                    "node_id": node.node_id,
                    "token_count": node.token_count,
                    "expand_hint": node.expand_hint,
                }
                for node in nodes[:20]
            ],
        }

    return json.dumps(overview)


def lcm_expand(args: Dict[str, Any], **kwargs) -> str:
    """Expand a summary node to its source content. No-op in v1 (empty DAG)."""
    engine = _require_engine(kwargs)
    if engine is None:
        return json.dumps({"error": "claude-lcm engine not initialized"})

    node_id = args.get("node_id")
    if node_id is None:
        return json.dumps({"error": "node_id is required"})

    node = _get_session_node(engine, node_id)
    if node is None:
        return json.dumps({
            "error": f"Node {node_id} not found in current session",
            "hint": (
                "v1 has no compaction — the DAG is empty. "
                "Use lcm_grep to search raw messages instead."
            ),
        })

    max_tokens = args.get("max_tokens", 4000)

    if node.source_type == "messages":
        messages = _expand_message_sources(engine, node, max_tokens=max_tokens)
        return json.dumps(
            {
                "node_id": node_id,
                "depth": node.depth,
                "source_type": "messages",
                "expanded": messages,
            }
        )

    if node.source_type == "nodes":
        children = _expand_child_nodes(engine, node)
        return json.dumps(
            {
                "node_id": node_id,
                "depth": node.depth,
                "source_type": "nodes",
                "expanded": children,
            }
        )

    return json.dumps({"error": f"Unknown source_type: {node.source_type}"})


def lcm_status(args: Dict[str, Any], **kwargs) -> str:
    """Quick health overview of the vault for the current session."""
    engine = _require_engine(kwargs)
    if engine is None:
        return json.dumps({"error": "claude-lcm engine not initialized"})

    session_id = engine._session_id
    if not session_id:
        return json.dumps({"error": "No active session"})

    store_messages = engine._store.get_session_count(session_id)
    store_tokens = engine._store.get_session_token_total(session_id)
    all_nodes = engine._dag.get_session_nodes(session_id)

    depths: dict[int, dict] = {}
    for node in all_nodes:
        d = depths.setdefault(node.depth, {"count": 0, "tokens": 0, "source_tokens": 0})
        d["count"] += 1
        d["tokens"] += node.token_count
        d["source_tokens"] += node.source_token_count

    session_meta = engine._store.get_session(session_id) or {}

    return json.dumps({
        "session_id": session_id,
        "agent_kind": session_meta.get("agent_kind"),
        "workspace_path": session_meta.get("workspace_path"),
        "workspace_fingerprint": session_meta.get("workspace_fingerprint"),
        "started_at": session_meta.get("started_at"),
        "store": {
            "messages": store_messages,
            "estimated_tokens": store_tokens,
        },
        "dag": {
            "total_nodes": len(all_nodes),
            "depths": {
                f"d{depth}": info for depth, info in sorted(depths.items())
            },
        },
        "vault_path": str(engine._store.db_path),
        "version": "v1 (no compaction)",
    })


def lcm_doctor(args: Dict[str, Any], **kwargs) -> str:
    """Run diagnostics on the vault."""
    engine = _require_engine(kwargs)
    if engine is None:
        return json.dumps({"error": "claude-lcm engine not initialized"})

    checks: list[dict] = []
    session_id = engine._session_id

    try:
        result = engine._store._conn.execute("PRAGMA integrity_check").fetchone()
        ok = result and result[0] == "ok"
        checks.append({
            "check": "database_integrity",
            "status": "pass" if ok else "fail",
            "detail": result[0] if result else "no response",
        })
    except Exception as e:
        checks.append({
            "check": "database_integrity",
            "status": "fail",
            "detail": str(e),
        })

    try:
        msg_count = engine._store._conn.execute(
            "SELECT COUNT(*) FROM messages"
        ).fetchone()[0]
        fts_count = engine._store._conn.execute(
            "SELECT COUNT(*) FROM messages_fts"
        ).fetchone()[0]
        checks.append({
            "check": "fts_index_sync",
            "status": "pass" if fts_count >= msg_count else "warn",
            "detail": f"{fts_count} FTS rows, {msg_count} total messages",
        })
    except Exception as e:
        checks.append({
            "check": "fts_index_sync",
            "status": "fail",
            "detail": str(e),
        })

    try:
        all_nodes = engine._dag.get_session_nodes(session_id) if session_id else []
        orphaned = 0
        for node in all_nodes:
            if node.source_type == "messages":
                for sid in node.source_ids:
                    stored = engine._store.get(sid)
                    if stored is None:
                        orphaned += 1
                        break
        checks.append({
            "check": "orphaned_dag_nodes",
            "status": "pass" if orphaned == 0 else "warn",
            "detail": (
                f"{orphaned} nodes reference missing store messages"
                if orphaned else "all nodes have valid sources"
            ),
        })
    except Exception as e:
        checks.append({
            "check": "orphaned_dag_nodes",
            "status": "fail",
            "detail": str(e),
        })

    try:
        row = engine._store._conn.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()
        version = row[0] if row else None
        checks.append({
            "check": "schema_version",
            "status": "pass" if version == "1" else "warn",
            "detail": f"schema_version={version!r}",
        })
    except Exception as e:
        checks.append({
            "check": "schema_version",
            "status": "fail",
            "detail": str(e),
        })

    overall = "healthy"
    if any(ch["status"] == "fail" for ch in checks):
        overall = "unhealthy"
    elif any(ch["status"] == "warn" for ch in checks):
        overall = "warnings"

    return json.dumps({
        "overall": overall,
        "checks": checks,
    })
