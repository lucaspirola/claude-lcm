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
import os
from typing import Any, Dict, TYPE_CHECKING

if TYPE_CHECKING:
    from .engine import ClaudeLcmEngine

logger = logging.getLogger(__name__)

_VALID_SCOPES = ("lineage", "workspace", "session", "auto")


def _fts5_quote(query: str) -> str:
    """Escape a query as a single FTS5 string literal (a quoted phrase).

    Wraps the whole query in double quotes, doubling any embedded ones, so
    punctuation like ':' '[' '-' is treated as literal tokens instead of FTS5
    operators. Tokenization still applies, but no operator parsing happens —
    which is what callers asking for a 'literal' match want.
    """
    return '"' + query.replace('"', '""') + '"'


def _resolve_scope_session_ids(engine: "ClaudeLcmEngine",
                                scope: str) -> list[str] | None:
    """Return the list of session ids to filter by for a given scope.

    'lineage'  — walk parent_session_id chain from current session
    'workspace' — all sessions with the same project_key as current session
    'session'  — only the current session
    """
    current = getattr(engine, "_session_id", None)
    if not current:
        return []
    if scope == "session":
        return [current]
    if scope == "auto":
        # Deterministic: stay in the current session when it has rows of its
        # own (point-in-time audit), otherwise widen to the full lineage.
        if engine._store.session_has_rows(current):
            return [current]
        return engine._store.walk_lineage(current)
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


def _require_engine(kwargs: Dict[str, Any]) -> "ClaudeLcmEngine | None":
    return kwargs.get("engine")


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
    scope = args.get("scope", "lineage")
    if scope not in _VALID_SCOPES:
        scope = "lineage"
    match_mode = args.get("match_mode", "fts5")
    if match_mode not in ("fts5", "literal"):
        match_mode = "fts5"
    include_thinking = bool(args.get("include_thinking", False))
    include_subagents = bool(args.get("include_subagents", False))

    session_id = engine._session_id
    session_ids = _resolve_scope_session_ids(engine, scope)
    results = []

    effective_query = _fts5_quote(query) if match_mode == "literal" else query
    try:
        msg_hits = engine._store.search(
            effective_query, session_ids=session_ids, limit=limit,
            include_thinking=include_thinking, include_subagents=include_subagents,
        )
    except Exception as exc:
        # An FTS5 parse error must never masquerade as an empty result (a false
        # negative that reads as "no matches"). Retry once as a quoted literal;
        # if that also fails, surface the error explicitly.
        logger.debug("FTS5 search failed (%s); retrying as literal", exc)
        try:
            effective_query = _fts5_quote(query)
            msg_hits = engine._store.search(
                effective_query, session_ids=session_ids, limit=limit,
                include_thinking=include_thinking, include_subagents=include_subagents,
            )
            match_mode = "literal"
        except Exception as exc2:
            return json.dumps({
                "error": "fts5_parse_error",
                "detail": str(exc2),
                "query": query,
                "hint": "Retry with match_mode='literal', or simplify the FTS5 query.",
            })
    for hit in msg_hits:
        result = {
            "type": "message",
            "depth": "raw",
            "store_id": hit["store_id"],
            "role": hit["role"],
            "snippet": hit.get("snippet", hit.get("content", "")[:200]),
        }
        if hit.get("agent_id"):
            result["agent_id"] = hit["agent_id"]
        results.append(result)

    try:
        # effective_query reflects the literal-mode fallback above, keeping the
        # DAG search consistent with the message search.
        node_hits = engine._dag.search(effective_query, session_id=session_id, limit=limit)
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
    return json.dumps({
        "query": query,
        "scope": scope,
        "match_mode": match_mode,
        "total_results": len(results),
        "results": results[:limit],
    })


def lcm_recent(args: Dict[str, Any], **kwargs) -> str:
    """Return the most recent N messages from the vault, newest first."""
    engine = _require_engine(kwargs)
    if engine is None:
        return json.dumps({"error": "claude-lcm engine not initialized"})

    limit = args.get("limit", args.get("n", 10))  # accept `n` as an alias
    scope = args.get("scope", "lineage")
    if scope not in _VALID_SCOPES:
        scope = "lineage"
    include_thinking = bool(args.get("include_thinking", False))
    include_subagents = bool(args.get("include_subagents", False))
    include_tool_calls = bool(args.get("include_tool_calls", False))

    session_ids = _resolve_scope_session_ids(engine, scope)
    messages = engine._store.recent_messages(
        session_ids, limit=limit,
        include_thinking=include_thinking, include_subagents=include_subagents,
        include_tool_calls=include_tool_calls,
    )
    return json.dumps({
        "scope": scope,
        "total_results": len(messages),
        "messages": messages,
    })


def lcm_describe(args: Dict[str, Any], **kwargs) -> str:
    """Describe a file snapshot (by id or path) or return a session overview."""
    from datetime import datetime, timezone

    engine = _require_engine(kwargs)
    if engine is None:
        return json.dumps({"error": "claude-lcm engine not initialized"})

    id_arg = args.get("id")
    session_id = engine._session_id

    # ── file snapshot lookup ────────────────────────────────────────────────
    if id_arg is not None:
        if isinstance(id_arg, int):
            snap = engine._store.get_file_snapshot(id_arg)
        else:
            caller_session = args.get("session_id") or session_id
            lineage = engine._store.walk_lineage(caller_session) if caller_session else None
            snap = engine._store.get_latest_snapshot_for_path(str(id_arg), session_ids=lineage)

        if snap is None:
            return json.dumps({"error": "not found", "id": id_arg})

        captured_ts = snap.get("captured_at")
        captured_iso = (
            datetime.fromtimestamp(captured_ts, tz=timezone.utc).isoformat()
            if captured_ts else None
        )
        return json.dumps({
            "snapshot_id": snap["snapshot_id"],
            "path": snap["file_path"],
            "extension": os.path.splitext(snap["file_path"])[1],
            "size_bytes": snap["size_bytes"],
            "captured_at": captured_iso,
            "session_id": snap["session_id"],
            "op": snap["op"],
            "exploration_summary": snap.get("exploration_summary"),
        })

    # ── session overview (backward compat, no id provided) ──────────────────
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


def _vault_size_bytes(db_path: Any) -> int:
    """On-disk footprint of the vault: the SQLite file plus its WAL/SHM
    sidecars (WAL mode keeps a `-wal` that can dwarf the main file)."""
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += os.path.getsize(f"{db_path}{suffix}")
        except OSError:
            pass
    return total


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
    role_counts = engine._store.get_role_counts(session_id)
    subagent_count = engine._store.get_subagent_count(session_id)
    all_nodes = engine._dag.get_session_nodes(session_id)

    depths: dict[int, dict] = {}
    for node in all_nodes:
        d = depths.setdefault(node.depth, {"count": 0, "tokens": 0, "source_tokens": 0})
        d["count"] += 1
        d["tokens"] += node.token_count
        d["source_tokens"] += node.source_token_count

    session_meta = engine._store.get_session(session_id) or {}
    vault_stats = engine._store.get_vault_stats()

    transcript_path, transcript_offset = engine._store.get_transcript_offset(session_id)
    transcript_sync: Dict[str, Any] = {
        "path": transcript_path,
        "synced_bytes": transcript_offset,
    }
    if transcript_path:
        try:
            transcript_sync["file_size_bytes"] = os.path.getsize(transcript_path)
        except OSError:
            transcript_sync["file_size_bytes"] = None

    return json.dumps({
        "session_id": session_id,
        "agent_kind": session_meta.get("agent_kind"),
        "workspace_path": session_meta.get("workspace_path"),
        "workspace_fingerprint": session_meta.get("workspace_fingerprint"),
        "started_at": session_meta.get("started_at"),
        "store": {
            "messages": store_messages,
            "estimated_tokens": store_tokens,
            "role_counts": role_counts,
            "subagent_transcripts_ingested": subagent_count,
        },
        "transcript_sync": transcript_sync,
        "dag": {
            "total_nodes": len(all_nodes),
            "depths": {
                f"d{depth}": info for depth, info in sorted(depths.items())
            },
        },
        "vault": {
            "path": str(engine._store.db_path),
            "size_bytes": _vault_size_bytes(engine._store.db_path),
            "total_sessions": vault_stats["total_sessions"],
            "total_messages": vault_stats["total_messages"],
        },
        "vault_path": str(engine._store.db_path),  # kept for back-compat
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

    if session_id:
        try:
            transcript_path, offset = engine._store.get_transcript_offset(session_id)
            if not transcript_path:
                # Not yet applicable (no Stop/SessionEnd has fired for this
                # session) rather than unhealthy — a brand-new session
                # legitimately has nothing to report here.
                checks.append({
                    "check": "transcript_sync",
                    "status": "pass",
                    "detail": "no transcript synced yet for this session "
                              "(no Stop/SessionEnd hook has fired)",
                })
            elif not os.path.exists(transcript_path):
                checks.append({
                    "check": "transcript_sync",
                    "status": "warn",
                    "detail": f"transcript_path {transcript_path!r} no longer exists",
                })
            else:
                size = os.path.getsize(transcript_path)
                # A gap is expected transiently mid-turn (the writer is still
                # flushing); only flag it as a real problem once it's larger
                # than a single line could plausibly be.
                behind = size - offset
                checks.append({
                    "check": "transcript_sync",
                    "status": "pass" if behind < 65536 else "warn",
                    "detail": f"synced {offset}/{size} bytes"
                              + ("" if behind < 65536 else f" ({behind} bytes behind)"),
                })
        except Exception as e:
            checks.append({
                "check": "transcript_sync",
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


# ----------------------------------------------------------------------------
# Structured tool-call access, identity, and markers (claude-lcm extensions)
# ----------------------------------------------------------------------------

def _normalize_tool_call(call: Any) -> Dict[str, Any]:
    """Best-effort normalization of a stored tool_call into {name, args, id}.

    Handles the adapter's native shape ({id, name, arguments}) plus OpenAI-style
    ({function:{name, arguments}}) and Anthropic-style ({input}) defensively.
    """
    if not isinstance(call, dict):
        return {"name": None, "args": None, "id": None}
    fn = call.get("function") if isinstance(call.get("function"), dict) else {}
    name = call.get("name") or fn.get("name")
    args = call.get("arguments")
    if args is None:
        args = fn.get("arguments")
    if args is None:
        args = call.get("input")
    if args is None:
        args = call.get("args")
    return {"name": name, "args": args, "id": call.get("id") or call.get("tool_call_id")}


def _truncate(text: Any, limit: int) -> Any:
    if not isinstance(text, str):
        return text
    return text if len(text) <= limit else text[:limit] + "…"


def lcm_tool_calls(args: Dict[str, Any], **kwargs) -> str:
    """Structured view of tool calls — each tool_use paired with its tool_result.

    Defaults to scope='session' (point-in-time audits). group_by='call' returns a
    flat list of calls newest-first; group_by='turn' groups calls under the
    assistant turn that issued them.

    Pairing uses tool_call_id when present, else falls back to FIFO same-tool-name
    adjacency; because Claude Code hook payloads frequently omit tool_call_id,
    results from interleaved same-name calls may be paired in call order and can
    mispair if results arrive out of order — treat `result` as best-effort.
    """
    engine = _require_engine(kwargs)
    if engine is None:
        return json.dumps({"error": "claude-lcm engine not initialized"})

    scope = args.get("scope", "session")
    if scope not in _VALID_SCOPES:
        scope = "session"
    limit = args.get("limit", 20)
    tool_name = args.get("tool_name")
    group_by = args.get("group_by", "call")
    if group_by not in ("call", "turn"):
        group_by = "call"
    result_chars = args.get("result_chars", 2000)

    session_ids = _resolve_scope_session_ids(engine, scope)
    if not session_ids:
        return json.dumps({
            "error": "No active session",
            "hint": "pass session_id (from the SessionStart context)",
        })

    rows = engine._store.tool_call_rows(
        session_ids, limit=max(limit * 8, 200), tool_name=tool_name
    )

    turns: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []  # call records awaiting a result
    for row in rows:
        tcs = row.get("tool_calls")
        if tcs:
            turn = {
                "message_id": row.get("store_id"),
                "text": row.get("content"),
                "timestamp": row.get("timestamp"),
                "session_id": row.get("session_id"),
                "tool_calls": [],
            }
            for raw in tcs:
                norm = _normalize_tool_call(raw)
                rec = {
                    "store_id": row.get("store_id"),
                    "tool_name": norm["name"] or row.get("tool_name"),
                    "args": norm["args"],
                    "tool_call_id": norm["id"],
                    "result": None,
                    "result_store_id": None,
                    "timestamp": row.get("timestamp"),
                    "session_id": row.get("session_id"),
                }
                turn["tool_calls"].append(rec)
                pending.append(rec)
            turns.append(turn)
        else:
            # tool_result row — pair to a pending call (exact id first, then by
            # name adjacency, since CC hook payloads frequently omit tool_call_id).
            rid = row.get("tool_call_id")
            rname = row.get("tool_name")
            match = None
            if rid:
                for rec in pending:
                    if rec["result"] is None and rec["tool_call_id"] and rec["tool_call_id"] == rid:
                        match = rec
                        break
            if match is None:
                for rec in pending:
                    if rec["result"] is None and (rname is None or rec["tool_name"] == rname):
                        match = rec
                        break
            if match is not None:
                match["result"] = _truncate(row.get("content"), result_chars)
                match["result_store_id"] = row.get("store_id")
                pending.remove(match)

    if group_by == "turn":
        out_turns = []
        for turn in turns:
            calls = turn["tool_calls"]
            if tool_name:
                calls = [c for c in calls if c["tool_name"] == tool_name]
            if not calls:
                continue
            out_turns.append({**turn, "tool_calls": calls})
        out_turns.sort(key=lambda t: (t["message_id"] or 0), reverse=True)
        out_turns = out_turns[:limit]
        return json.dumps({
            "scope": scope,
            "group_by": "turn",
            "total_results": len(out_turns),
            "turns": out_turns,
        })

    calls = [c for turn in turns for c in turn["tool_calls"]]
    if tool_name:
        # redundant for call-mode after the SQL filter; still needed for turn-mode aggregation
        calls = [c for c in calls if c["tool_name"] == tool_name]
    calls.sort(key=lambda c: (c["store_id"] or 0), reverse=True)
    calls = calls[:limit]
    return json.dumps({
        "scope": scope,
        "group_by": "call",
        "total_results": len(calls),
        "tool_calls": calls,
    })


def lcm_whoami(args: Dict[str, Any], **kwargs) -> str:
    """Return the calling session's identity + lineage.

    With session_id (the canonical path) returns exact identity. Without it,
    best-effort: resolve via CLAUDE_PROJECT_DIR to the most recent session in
    this workspace — flagged so callers know it may be wrong under concurrency.
    """
    engine = _require_engine(kwargs)
    if engine is None:
        return json.dumps({"error": "claude-lcm engine not initialized"})

    # The MCP server normally pops session_id into the engine; honor an explicit
    # args["session_id"] as a fallback (engine value takes precedence).
    session_id = engine._session_id or args.get("session_id")
    resolved_via = "session_id"
    warning = None
    if not session_id:
        proj = os.environ.get("CLAUDE_PROJECT_DIR")
        if proj:
            from claude_lcm.workspace import sanitize_path
            pk = sanitize_path(proj)
            session_id = engine._store.latest_session_for_project(pk)
            resolved_via = "project_dir_latest"
            warning = (
                "session_id not provided; resolved to the most recently started "
                "session for CLAUDE_PROJECT_DIR. May be wrong with concurrent sessions."
            )
    if not session_id:
        return json.dumps({
            "error": "could not determine session_id",
            "hint": "pass session_id explicitly (it is in the SessionStart context block)",
        })

    meta = engine._store.get_session(session_id) or {}
    lineage = engine._store.walk_lineage(session_id)
    parent = lineage[1] if len(lineage) > 1 else None
    out = {
        "session_id": session_id,
        "parent_session_id": parent,
        "lineage": lineage,
        "started_at": meta.get("started_at"),
        "workspace_path": meta.get("workspace_path"),
        "agent_kind": meta.get("agent_kind"),
        "message_count": engine._store.get_session_count(session_id),
        "resolved_via": resolved_via,
    }
    if warning:
        out["warning"] = warning
    return json.dumps(out)


def lcm_mark(args: Dict[str, Any], **kwargs) -> str:
    """Record a named mark (bookmark / protocol marker) for the current session."""
    engine = _require_engine(kwargs)
    if engine is None:
        return json.dumps({"error": "claude-lcm engine not initialized"})

    session_id = engine._session_id
    if not session_id:
        return json.dumps({"error": "No active session", "hint": "pass session_id"})
    name = (args.get("name") or "").strip()
    if not name:
        return json.dumps({"error": "name is required"})
    store_id = args.get("store_id")
    metadata = args.get("metadata")
    if store_id is not None and engine._store.get(store_id) is None:
        return json.dumps({"error": "store_id not found", "store_id": store_id})
    try:
        mark_id = engine._store.add_mark(
            session_id, name, store_id=store_id, metadata=metadata
        )
    except Exception as exc:
        return json.dumps({"error": f"add_mark failed: {exc}"})
    return json.dumps({
        "mark_id": mark_id,
        "session_id": session_id,
        "name": name,
        "store_id": store_id,
        "pinned": store_id is not None,
    })


def lcm_marks(args: Dict[str, Any], **kwargs) -> str:
    """List marks for the current session (or scope), optionally filtered by name."""
    engine = _require_engine(kwargs)
    if engine is None:
        return json.dumps({"error": "claude-lcm engine not initialized"})

    scope = args.get("scope", "lineage")
    if scope not in _VALID_SCOPES:
        scope = "lineage"
    name = args.get("name")
    limit = args.get("limit", 50)
    session_ids = _resolve_scope_session_ids(engine, scope)
    if not session_ids:
        return json.dumps({
            "error": "No active session",
            "hint": "pass session_id (from the SessionStart context)",
        })
    marks = engine._store.get_marks(session_ids, name=name, limit=limit)
    return json.dumps({
        "scope": scope,
        "total_results": len(marks),
        "marks": marks,
    })
