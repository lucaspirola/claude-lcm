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

    cleared = source == "clear"
    context = (
        f"claude-lcm: this Claude Code session_id is {session_id}. "
        f"Pass it as the `session_id` argument on every lcm_* tool call. "
        f"Tools available: lcm_grep (keyword search), lcm_recent (last N messages, newest-first), "
        f"lcm_status, lcm_doctor. All tools default to scope='lineage', which automatically "
        f"includes messages from sessions chained by /clear. "
    )
    if cleared:
        context += (
            f"The user just ran /clear — this is a continuation of a prior conversation. "
            f"When the user asks you to recall, remember, or summarize recent context "
            f"(e.g. 'what were we doing?', 'remember our last N messages', 'catch me up'), "
            f"call lcm_recent(session_id='{session_id}', limit=<N or 20>) immediately "
            f"without asking for clarification."
        )
    else:
        context += (
            f"When the user asks to recall recent context or past messages, "
            f"use lcm_recent(session_id='{session_id}')."
        )
    write_response({
        "continue": True,
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        },
    })


if __name__ == "__main__":
    safe_main(handle)
