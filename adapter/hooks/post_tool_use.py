#!/usr/bin/env python3
"""PostToolUse hook — record the tool result and post-edit snapshots."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from adapter.hooks._common import engine_for, safe_main, write_response
from claude_lcm.explorer import explore

POST_SNAPSHOT_TOOLS = {"Edit", "Write", "NotebookEdit"}


def _stringify(obj: Any) -> str:
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    try:
        return json.dumps(obj, default=str)
    except Exception:
        return str(obj)


def _read_file_bytes(path_str: str, max_bytes: int = 2 * 1024 * 1024) -> bytes | None:
    try:
        p = Path(path_str)
        if not p.exists() or not p.is_file():
            return None
        size = p.stat().st_size
        if size > max_bytes:
            return None
        return p.read_bytes()
    except Exception:
        return None


def handle(payload: Dict[str, Any]) -> None:
    session_id = payload.get("session_id") or payload.get("sessionId")
    tool_name = payload.get("tool_name") or payload.get("toolName") or ""
    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
    tool_response = (
        payload.get("tool_response")
        or payload.get("toolResponse")
        or payload.get("tool_output")
        or {}
    )
    if not session_id or not tool_name:
        return

    with engine_for(session_id) as eng:
        mid = eng.ingest_message({
            "role": "tool",
            "content": _stringify(tool_response),
            "tool_call_id": payload.get("tool_call_id") or "",
            "tool_name": tool_name,
        })

        if tool_name in POST_SNAPSHOT_TOOLS:
            file_path = (
                tool_input.get("file_path")
                or tool_input.get("filePath")
                or tool_input.get("path")
            )
            if file_path:
                content = _read_file_bytes(str(file_path))
                summary = explore(str(file_path), content)
                if content is not None:
                    eng.ingest_file_snapshot(
                        file_path=str(file_path),
                        op="post_" + tool_name.lower(),
                        content=content,
                        message_id=mid,
                        exploration_summary=summary,
                    )
                else:
                    eng.ingest_file_snapshot(
                        file_path=str(file_path),
                        op="post_" + tool_name.lower(),
                        content=None,
                        external_uri=f"missing-or-oversize://{file_path}",
                        message_id=mid,
                        exploration_summary=summary,
                    )
    write_response({"continue": True})


if __name__ == "__main__":
    safe_main(handle)
