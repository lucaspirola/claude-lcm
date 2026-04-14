#!/usr/bin/env python3
"""PreToolUse hook — record the tool call and snapshot files for Read/Edit."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from adapter.hooks._common import engine_for, safe_main, write_response

SNAPSHOT_TOOLS = {"Read", "Edit", "Write", "NotebookEdit"}


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
    if not session_id or not tool_name:
        return

    with engine_for(session_id) as eng:
        tool_call = {
            "id": payload.get("tool_call_id") or "",
            "name": tool_name,
            "arguments": tool_input,
        }
        mid = eng.ingest_message({
            "role": "assistant",
            "content": None,
            "tool_calls": [tool_call],
            "tool_name": tool_name,
        })

        if tool_name in SNAPSHOT_TOOLS:
            file_path = (
                tool_input.get("file_path")
                or tool_input.get("filePath")
                or tool_input.get("path")
            )
            if file_path:
                content = _read_file_bytes(str(file_path))
                if content is not None:
                    eng.ingest_file_snapshot(
                        file_path=str(file_path),
                        op="pre_" + tool_name.lower(),
                        content=content,
                        message_id=mid,
                    )
                else:
                    eng.ingest_file_snapshot(
                        file_path=str(file_path),
                        op="pre_" + tool_name.lower(),
                        content=None,
                        external_uri=f"missing-or-oversize://{file_path}",
                        message_id=mid,
                    )
    write_response({"continue": True})


if __name__ == "__main__":
    safe_main(handle)
