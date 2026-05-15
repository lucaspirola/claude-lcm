#!/usr/bin/env python3
"""UserPromptSubmit hook — append the user's message to the vault.

If the prompt expresses recall intent ("remember our last N messages",
"what were we doing", etc.) the hook pre-fetches recent messages from the
vault and injects them as additionalContext so Claude never needs to call
any tool.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List

from adapter.hooks._common import engine_for, safe_main, write_response

# Phrases that signal the user wants to recall prior context.
_RECALL_RE = re.compile(
    r"\b("
    r"remember"
    r"|recall"
    r"|catch me up"
    r"|what were we"
    r"|what did we"
    r"|restore context"
    r"|prior context"
    r"|recent (messages?|context|history)"
    r"|past \d+ messages?"
    r"|last \d+ messages?"
    r")\b",
    re.IGNORECASE,
)

_LIMIT_RE = re.compile(r"\b(\d+)\s+messages?\b", re.IGNORECASE)

_DEFAULT_RECALL_LIMIT = 20


def _is_recall_intent(prompt: str) -> bool:
    return bool(_RECALL_RE.search(prompt))


def _extract_limit(prompt: str) -> int:
    m = _LIMIT_RE.search(prompt)
    return int(m.group(1)) if m else _DEFAULT_RECALL_LIMIT


def _format_messages(messages: List[Dict[str, Any]]) -> str:
    lines = [f"[lcm: {len(messages)} recent messages from vault, newest first]"]
    for msg in messages:
        role = msg.get("role", "?")
        content = msg.get("content") or ""
        ts = msg.get("timestamp", "")
        sid = msg.get("session_id", "")
        lines.append(f"\n--- {role} | session={sid[:8]} | ts={ts:.0f} ---")
        lines.append(content[:400] + ("…" if len(content) > 400 else ""))
    return "\n".join(lines)


def handle(payload: Dict[str, Any]) -> None:
    session_id = payload.get("session_id") or payload.get("sessionId")
    prompt = payload.get("prompt") or payload.get("user_prompt") or ""
    if not session_id or not prompt:
        return

    with engine_for(session_id) as eng:
        eng.ingest_message({"role": "user", "content": prompt})

        extra = ""
        if _is_recall_intent(prompt):
            limit = _extract_limit(prompt)
            messages = eng.recent_messages_lineage(session_id, limit)
            if messages:
                extra = "\n\n" + _format_messages(messages)

    context = (
        f"claude-lcm: this Claude Code session_id is {session_id}. "
        f"Pass it as the `session_id` argument on every lcm_* tool call."
        + extra
    )
    write_response({
        "continue": True,
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        },
    })


if __name__ == "__main__":
    safe_main(handle)
