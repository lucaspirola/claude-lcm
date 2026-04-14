#!/usr/bin/env python3
"""SessionEnd hook — mark the session as ended."""

from __future__ import annotations

from typing import Any, Dict

from adapter.hooks._common import engine_for, safe_main, write_response


def handle(payload: Dict[str, Any]) -> None:
    session_id = payload.get("session_id") or payload.get("sessionId")
    if not session_id:
        return
    with engine_for(session_id) as eng:
        eng.close_session(session_id)
    write_response({"continue": True})


if __name__ == "__main__":
    safe_main(handle)
