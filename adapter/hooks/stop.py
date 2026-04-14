#!/usr/bin/env python3
"""Stop hook — end-of-assistant-turn marker."""

from __future__ import annotations

from typing import Any, Dict

from adapter.hooks._common import engine_for, safe_main, write_response


def handle(payload: Dict[str, Any]) -> None:
    session_id = payload.get("session_id") or payload.get("sessionId")
    if not session_id:
        return
    # Record a lightweight end-of-turn marker so the transcript has a
    # clear boundary between assistant turns. Actual assistant text flows
    # through tool calls; the final free-text reply (if any) is not
    # directly exposed in the Stop hook payload.
    with engine_for(session_id) as eng:
        eng.ingest_message({
            "role": "system",
            "content": "[stop: assistant turn ended]",
        })
    write_response({"continue": True})


if __name__ == "__main__":
    safe_main(handle)
