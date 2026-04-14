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
        "Inspect a summary node's subtree metadata WITHOUT loading full "
        "content. Returns token counts, child manifest, and expand hints. "
        "Use this to plan retrieval strategy before spending tokens on "
        "lcm_expand. If called with no node_id, returns the top-level "
        "overview for the current session. In v1 (no compaction) this "
        "returns a session message summary rather than a DAG tree."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "node_id": {
                "type": "integer",
                "description": "Summary node ID to inspect. Omit for session overview.",
            },
            "session_id": _SESSION_ID_PARAM,
            "scope": _SCOPE_PARAM,
        },
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
