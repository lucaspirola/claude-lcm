#!/usr/bin/env python3
"""Stop hook — end-of-assistant-turn marker."""

from __future__ import annotations

from typing import Any, Dict

from adapter.hooks._common import engine_for, safe_main, write_response


def handle(payload: Dict[str, Any]) -> None:
    session_id = payload.get("session_id") or payload.get("sessionId")
    if not session_id:
        return
    transcript_path = payload.get("transcript_path") or payload.get("transcriptPath")
    # Record a lightweight end-of-turn marker so the transcript has a
    # clear boundary between assistant turns, then pull the assistant's
    # actual reply text (and thinking, and any subagent turns) out of
    # Claude Code's own transcript file via the byte-offset cursor in
    # `sessions.transcript_offset` — the Stop hook payload itself doesn't
    # carry the reply text directly, but it does carry transcript_path.
    with engine_for(session_id) as eng:
        eng.ingest_message({
            "role": "system",
            "content": "[stop: assistant turn ended]",
        })
        eng.sync_transcript(session_id, transcript_path)
    write_response({"continue": True})


if __name__ == "__main__":
    safe_main(handle)
