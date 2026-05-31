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
        parent_sid = None
        if project_key:
            if source == "clear":
                parent_sid = eng.take_clear_handoff(project_key)
            if parent_sid is None:
                # Fresh `claude` start (no /clear handoff) — auto-link to the
                # most recent prior session in this workspace so lineage scope works.
                parent_sid = eng.latest_session_for_project(project_key, exclude_session_id=session_id)
            if parent_sid:
                eng.set_parent_session(session_id, parent_sid)

    has_prior = parent_sid is not None
    context = (
        f"claude-lcm: this Claude Code session_id is {session_id}. "
        f"Pass it as the `session_id` argument on every lcm_* tool call. "
        f"Tools available: lcm_grep (keyword search), lcm_recent (last N messages, "
        f"newest-first), lcm_tool_calls (structured tool-call audit), lcm_whoami "
        f"(this session's id + lineage), lcm_mark / lcm_marks (set/list named "
        f"markers), lcm_describe, lcm_expand, lcm_status, lcm_doctor. "
        f"scope: session|workspace|lineage|auto: recall tools default to "
        f"'lineage' (includes sessions chained by /clear); use scope='session' for "
        f"point-in-time audits. Full parameter schemas are in each tool's MCP "
        f"definition. "
    )
    if has_prior:
        context += (
            f"This session is linked to a prior conversation in the same workspace. "
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
