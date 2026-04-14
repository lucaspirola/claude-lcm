#!/usr/bin/env python3
"""SessionEnd hook — close the session row and, on /clear, hand off lineage."""

from __future__ import annotations

from typing import Any, Dict

from adapter.hooks._common import engine_for, safe_main, write_response
from claude_lcm.workspace import sanitize_path


def handle(payload: Dict[str, Any]) -> None:
    session_id = payload.get("session_id") or payload.get("sessionId")
    if not session_id:
        return
    source = payload.get("source")
    cwd = payload.get("cwd")
    with engine_for(session_id) as eng:
        eng.close_session(session_id)
        if source == "clear":
            eng.set_end_reason(session_id, "clear")
            if cwd:
                eng.upsert_clear_handoff(
                    project_key=sanitize_path(cwd),
                    ending_session_id=session_id,
                )
        else:
            eng.set_end_reason(session_id, "normal")
    write_response({"continue": True})


if __name__ == "__main__":
    safe_main(handle)
