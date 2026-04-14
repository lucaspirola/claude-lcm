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
