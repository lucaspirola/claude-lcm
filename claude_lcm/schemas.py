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
            "session_id": _SESSION_ID_PARAM,
            "scope": _SCOPE_PARAM,
        },
        "required": ["query"],
    },
}

LCM_DESCRIBE = {
    "name": "lcm_describe",
    "description": (
        "Return metadata for a file snapshot or summary node. "
        "Pass a snapshot_id (int) or a file path (string) as `id`. "
        "For paths, pass session_id to scope to a lineage; omit for vault-global latest. "
        "Omit `id` entirely to get a session overview (message count, DAG status). "
        "In v1 the DAG is empty; integer IDs resolve to file snapshots only."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "id": {
                "type": ["string", "integer"],
                "description": "snapshot_id (int) or file path (string). Omit for session overview.",
            },
            "session_id": _SESSION_ID_PARAM,
            "scope": _SCOPE_PARAM,
        },
        # `id` is intentionally optional: omitting it triggers the session
        # overview (backward compat). When present it performs a snapshot lookup.
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
        "Get a quick health overview of the claude-lcm vault for the "
        "current session. Shows total session count, message count, vault "
        "size on disk, and active configuration."
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
                "description": "Number of messages to return (default 10)",
                "default": 10,
            },
            "scope": _SCOPE_PARAM,
            "session_id": _SESSION_ID_PARAM,
        },
        "required": [],
    },
}
