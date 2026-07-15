"""Tool schemas — what the LLM sees.

---
Adapted from hermes-lcm
(https://github.com/stephenschoettler/hermes-lcm) schemas.py at v0.2.0,
Copyright (c) Stephen Schoettler, MIT License. Changes: `LCM_EXPAND_QUERY`
dropped (it requires an LLM for synthesis, deferred to v2); descriptions
updated to reflect that v1 has no compaction — the DAG table is empty
until v2 and `lcm_expand` / `lcm_describe` degrade gracefully. Tool
names kept at `lcm_*` matching upstream — Claude Code namespaces MCP
tools by server id (`mcp__claude-lcm-mcp__lcm_grep`), so this does not
collide with hermes-lcm if both are mounted.
"""

_SESSION_ID_PARAM = {
    "type": "string",
    "description": (
        "Claude Code session_id to scope results to. The SessionStart "
        "hook injects this into your context — pass it on every call."
    ),
}

_SCOPE_PARAM = {
    "type": "string",
    "enum": ["lineage", "workspace", "session", "auto"],
    "description": (
        "Search scope. 'lineage' (default for recall tools) walks "
        "parent_session_id from the current session transitively — includes "
        "prior sessions chained by /clear. 'workspace' widens to every session "
        "in the same project_key (sanitized cwd). 'session' limits to the "
        "current session_id only — use this for point-in-time audits. 'auto' "
        "resolves deterministically to 'session' when the current session has "
        "rows of its own, otherwise widens to 'lineage'."
    ),
    "default": "lineage",
}

# Same enum and semantics as _SCOPE_PARAM, but defaulting to 'session' — used by
# the point-in-time audit tools (lcm_tool_calls) where the recall-oriented
# 'lineage' default would be wrong. Keeping the declared default in sync with the
# handler's actual default is the contract MCP clients rely on.
_SCOPE_PARAM_SESSION_DEFAULT = {**_SCOPE_PARAM, "default": "session"}

_INCLUDE_THINKING_PARAM = {
    "type": "boolean",
    "description": (
        "Include the assistant's internal `thinking` blocks (role "
        "'assistant_thinking'), not just its final reply text. Default "
        "false — thinking is captured in the vault but hidden from recall "
        "unless explicitly requested."
    ),
    "default": False,
}

_INCLUDE_SUBAGENTS_PARAM = {
    "type": "boolean",
    "description": (
        "Include turns from subagent (Task/Agent tool) transcripts, not "
        "just the main conversation thread. Default false — a single Task "
        "call can be dozens of turns and would drown out the main thread."
    ),
    "default": False,
}

LCM_GREP = {
    "name": "lcm_grep",
    "description": (
        "Search the full conversation history — raw messages and (once "
        "compaction is enabled in v2) summary nodes at every depth. Use "
        "this to find specific topics, decisions, file paths, or error "
        "messages from earlier sessions, even ones that have scrolled "
        "out of Claude Code's current context window. FTS5 syntax: "
        "keywords, \"quoted phrases\", OR, NOT."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query (FTS5 syntax)",
            },
            "limit": {
                "type": "integer",
                "description": "Max results to return (default 10)",
                "default": 10,
            },
            "match_mode": {
                "type": "string",
                "enum": ["fts5", "literal"],
                "description": (
                    "'fts5' (default) interprets the query as FTS5 syntax "
                    "(keywords, \"phrases\", OR, NOT). 'literal' escapes the "
                    "whole query as one quoted phrase so punctuation like ':' "
                    "'[' '-' is matched literally instead of as operators. "
                    "On an FTS5 parse error the server auto-retries in 'literal' "
                    "mode and reports an explicit error if that also fails — it "
                    "never returns an empty result as a false 'no matches'."
                ),
                "default": "fts5",
            },
            "session_id": _SESSION_ID_PARAM,
            "scope": _SCOPE_PARAM,
            "include_thinking": _INCLUDE_THINKING_PARAM,
            "include_subagents": _INCLUDE_SUBAGENTS_PARAM,
        },
        "required": ["query"],
    },
}

LCM_DESCRIBE = {
    "name": "lcm_describe",
    "description": (
        "Return metadata for a file snapshot or summary node.\n"
        "• `id` = file path (string): returns the latest snapshot for that path. "
        "Pass session_id to scope the lookup to your session lineage; omit "
        "session_id for the vault-global latest.\n"
        "• `id` = snapshot_id (int): an exact by-id lookup. Integer ids are "
        "vault-global primary keys — they IGNORE session_id/scope and may return "
        "a snapshot from any session or workspace in the vault. (This is "
        "intentional: the vault is shared across sessions.)\n"
        "• omit `id` entirely: returns a session overview (message count, DAG "
        "status) for the current session.\n"
        "In v1 the DAG is empty; integer IDs resolve to file snapshots only."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "id": {
                "type": ["string", "integer"],
                "description": (
                    "snapshot_id (int, vault-global) or file path (string, "
                    "scoped by session_id). Omit for session overview."
                ),
            },
            # session_id scopes PATH lookups to a lineage; it has no effect on
            # integer-id lookups (vault-global) or the session overview.
            "session_id": _SESSION_ID_PARAM,
        },
        # `id` is intentionally optional: omitting it triggers the session
        # overview (backward compat). When present it performs a snapshot lookup.
        # No `scope` param: the handler never consulted it — path lookups always
        # use the caller's lineage, so advertising `scope` here was misleading.
        "required": [],
    },
}

LCM_EXPAND = {
    "name": "lcm_expand",
    "description": (
        "Recover the original detail behind a summary node. Given a "
        "node_id, returns the source messages or lower-depth summaries "
        "that were compacted into that node. In v1 the DAG is empty, so "
        "this degrades to a no-op and directs the caller to lcm_grep."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "node_id": {
                "type": "integer",
                "description": "Summary node ID to expand",
            },
            "max_tokens": {
                "type": "integer",
                "description": "Token budget for returned content (default 4000)",
                "default": 4000,
            },
            "session_id": _SESSION_ID_PARAM,
            "scope": _SCOPE_PARAM,
        },
        "required": ["node_id"],
    },
}

LCM_STATUS = {
    "name": "lcm_status",
    "description": (
        "Health overview of the claude-lcm vault. Returns current-session "
        "stats (message count, per-role counts, estimated tokens, subagent "
        "transcripts ingested, and the transcript-sync cursor) under `store`/"
        "`transcript_sync`, plus a vault-wide `vault` block (total session "
        "count, total message count, and on-disk size in bytes including the "
        "WAL) and the schema `version`. Session-scoped counts describe the "
        "current session only; the `vault` block is global. For deeper "
        "integrity checks (FTS sync, orphans, sync freshness) use lcm_doctor."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "session_id": _SESSION_ID_PARAM,
            "scope": _SCOPE_PARAM,
        },
        "required": [],
    },
}

LCM_DOCTOR = {
    "name": "lcm_doctor",
    "description": (
        "Run diagnostics on the claude-lcm vault. Checks database "
        "integrity, FTS5 sync, orphaned DAG nodes, and validates "
        "configuration. Use this to troubleshoot problems or verify a "
        "healthy setup."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "session_id": _SESSION_ID_PARAM,
            "scope": _SCOPE_PARAM,
        },
        "required": [],
    },
}

LCM_RECENT = {
    "name": "lcm_recent",
    "description": (
        "Return the most recent N messages from the current session or its "
        "lineage, newest first. Use this after /clear to recall what was "
        "being discussed, or to orient yourself at the start of a session. "
        "For keyword search, use lcm_grep instead."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Number of messages to return (default 10). `n` is accepted as an alias.",
                "default": 10,
            },
            "n": {
                "type": "integer",
                "description": (
                    "Alias for `limit` (number of messages to return). When both "
                    "`limit` and `n` are supplied, `limit` takes precedence."
                ),
            },
            "scope": _SCOPE_PARAM,
            "session_id": _SESSION_ID_PARAM,
            "include_thinking": _INCLUDE_THINKING_PARAM,
            "include_subagents": _INCLUDE_SUBAGENTS_PARAM,
        },
        "required": [],
    },
}

LCM_TOOL_CALLS = {
    "name": "lcm_tool_calls",
    "description": (
        "Structured view of tool calls — each tool_use paired with its "
        "tool_result — instead of grepping raw JSON. Use this to audit what "
        "tools ran, with parsed arguments and (truncated) results. Defaults to "
        "scope='session' for point-in-time audits. group_by='call' returns a "
        "flat list newest-first; group_by='turn' groups calls under the "
        "assistant turn that issued them. Pairing uses tool_call_id when present, "
        "else falls back to same-tool-name call order; since Claude Code hook "
        "payloads often omit tool_call_id, `result` is best-effort and may "
        "mispair interleaved same-name calls whose results arrive out of order."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "tool_name": {
                "type": "string",
                "description": "Filter to a single tool name (e.g. 'Read', 'Task').",
            },
            "limit": {
                "type": "integer",
                "description": "Max calls (or turns) to return, newest-first (default 20).",
                "default": 20,
            },
            "group_by": {
                "type": "string",
                "enum": ["call", "turn"],
                "description": "Granularity: individual calls or assistant turns.",
                "default": "call",
            },
            "result_chars": {
                "type": "integer",
                "description": "Truncate each tool result to this many chars (default 2000).",
                "default": 2000,
            },
            "session_id": _SESSION_ID_PARAM,
            "scope": _SCOPE_PARAM_SESSION_DEFAULT,
        },
        "required": [],
    },
}

LCM_WHOAMI = {
    "name": "lcm_whoami",
    "description": (
        "Return the calling session's identity and lineage: session_id, "
        "parent_session_id, the full lineage chain, started_at, and workspace. "
        "Pass session_id (it is in the SessionStart context block). If omitted, "
        "the server makes a best-effort guess from CLAUDE_PROJECT_DIR (the most "
        "recent session in this workspace) and flags it via 'resolved_via'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "session_id": _SESSION_ID_PARAM,
        },
        "required": [],
    },
}

LCM_MARK = {
    "name": "lcm_mark",
    "description": (
        "Record a named mark for the current session — a first-class bookmark "
        "or protocol marker (e.g. name='ml-intern:active'). Pass store_id to "
        "bookmark a specific message (which also pins it). Use this instead of "
        "embedding magic marker strings in the transcript for later FTS grep."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Marker name / label (e.g. 'ml-intern:active').",
            },
            "store_id": {
                "type": "integer",
                "description": "Optional message store_id to bookmark (also pins it).",
            },
            "metadata": {
                "type": "object",
                "description": "Optional JSON metadata to attach to the mark.",
            },
            "session_id": _SESSION_ID_PARAM,
        },
        "required": ["name"],
    },
}

LCM_MARKS = {
    "name": "lcm_marks",
    "description": (
        "List marks (bookmarks / protocol markers), optionally filtered by "
        "name. Defaults to scope='lineage'. Use this to check whether a "
        "protocol marker was ever set, without FTS-grepping transcript strings."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Filter to a single marker name.",
            },
            "limit": {
                "type": "integer",
                "description": "Max marks to return, newest-first (default 50).",
                "default": 50,
            },
            "session_id": _SESSION_ID_PARAM,
            "scope": _SCOPE_PARAM,
        },
        "required": [],
    },
}
