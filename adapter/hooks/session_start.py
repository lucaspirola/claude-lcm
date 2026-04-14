#!/usr/bin/env python3
"""SessionStart hook — register a session row in the vault."""

from __future__ import annotations

from typing import Any, Dict

from adapter.hooks._common import engine_for, safe_main, write_response
from claude_lcm.workspace import fingerprint


def handle(payload: Dict[str, Any]) -> None:
    session_id = payload.get("session_id") or payload.get("sessionId")
    if not session_id:
        return
    cwd = payload.get("cwd")
    fp, path = fingerprint(cwd)
    with engine_for(session_id) as eng:
        eng.open_session(
            session_id=session_id,
            agent_kind="claude-code",
            workspace_fingerprint=fp,
            workspace_path=path,
            metadata={
                "source": payload.get("source"),
                "transcript_path": payload.get("transcript_path"),
            },
        )
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
