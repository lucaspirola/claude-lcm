#!/usr/bin/env python3
"""UserPromptSubmit hook — append the user's message to the vault."""

from __future__ import annotations

from typing import Any, Dict

from adapter.hooks._common import engine_for, safe_main, write_response


def handle(payload: Dict[str, Any]) -> None:
    session_id = payload.get("session_id") or payload.get("sessionId")
    prompt = payload.get("prompt") or payload.get("user_prompt") or ""
    if not session_id or not prompt:
        return
    with engine_for(session_id) as eng:
        eng.ingest_message({"role": "user", "content": prompt})
    write_response({
        "continue": True,
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": (
                f"claude-lcm: this Claude Code session_id is {session_id}. "
                f"Pass it as the `session_id` argument on every lcm_* tool call."
            ),
        },
    })


if __name__ == "__main__":
    safe_main(handle)
