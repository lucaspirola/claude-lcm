"""Incremental reader for Claude Code's own JSONL transcript files.

Claude Code writes one JSON object per line to `<transcript_path>` for the
main conversation thread, and to a sibling `<transcript_dir>/subagents/
agent-<id>.jsonl` per subagent (Task/Agent tool) invocation. Each assistant
turn is split across one line per content block (`thinking` / `text` /
`tool_use`), tagged with a top-level `type` of `"assistant"` or `"user"`.

This module never mutates or re-reads more than it has to: callers track a
byte offset per file (see `store.get_transcript_offset` /
`get_subagent_offset`) and pass it back in, so re-running costs only the
bytes appended since the last call.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple


def read_new_lines(path: str | Path, offset: int = 0) -> Tuple[List[Dict[str, Any]], int]:
    """Read complete JSON lines appended to `path` since `offset`.

    Returns (parsed_entries, new_offset). A trailing line with no final
    newline (the writer may still be mid-flush) is left unconsumed —
    `new_offset` stops right before it, so the next call picks it up once
    it's complete. Malformed JSON lines are skipped, never fatal. A missing
    file returns ([], offset) unchanged.
    """
    p = Path(path)
    if not p.exists():
        return [], offset
    try:
        with p.open("rb") as f:
            f.seek(offset)
            chunk = f.read()
    except OSError:
        return [], offset

    if not chunk:
        return [], offset

    last_newline = chunk.rfind(b"\n")
    if last_newline == -1:
        # No complete line since offset yet.
        return [], offset
    complete, _partial = chunk[:last_newline], chunk[last_newline + 1:]
    new_offset = offset + last_newline + 1

    entries: List[Dict[str, Any]] = []
    for raw_line in complete.split(b"\n"):
        if not raw_line.strip():
            continue
        try:
            entries.append(json.loads(raw_line))
        except json.JSONDecodeError:
            continue
    return entries, new_offset


def _parse_timestamp(entry: Dict[str, Any]) -> float:
    ts = entry.get("timestamp")
    if not ts:
        return time.time()
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return time.time()


def _text_from_user_content(content: Any) -> str | None:
    """Extract literal text from a transcript user entry's content field.

    `content` is either a plain string, or a list of blocks — only `text`
    blocks are literal user input; `tool_result` blocks are the tool's own
    output already captured elsewhere and are skipped.
    """
    if isinstance(content, str):
        return content or None
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        joined = "\n".join(part for part in parts if part)
        return joined or None
    return None


def extract_messages(entries: List[Dict[str, Any]],
                     agent_id: str | None = None) -> List[Dict[str, Any]]:
    """Turn raw transcript entries into vault-ready message dicts.

    `agent_id` is None for the main thread, or the subagent id when parsing
    a `subagents/agent-<id>.jsonl` file. Main-thread `user` entries are
    skipped (already captured via UserPromptSubmit / PostToolUse); subagent
    `user` entries are kept since nothing else sees a subagent's own prompt
    or tool-result turns. `tool_use` blocks are always skipped (already
    captured via PreToolUse's tool_input).
    """
    messages: List[Dict[str, Any]] = []
    for entry in entries:
        entry_type = entry.get("type")
        message = entry.get("message") or {}

        if entry_type == "assistant":
            ts = _parse_timestamp(entry)
            for block in message.get("content") or []:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "text":
                    text = block.get("text", "").strip()
                    if text:
                        messages.append({
                            "role": "assistant",
                            "content": text,
                            "timestamp": ts,
                            "agent_id": agent_id,
                        })
                elif block_type == "thinking":
                    thinking = block.get("thinking", "").strip()
                    if thinking:
                        messages.append({
                            "role": "assistant_thinking",
                            "content": thinking,
                            "timestamp": ts,
                            "agent_id": agent_id,
                        })
                # tool_use: skipped, already captured via PreToolUse.

        elif entry_type == "user" and agent_id is not None:
            text = _text_from_user_content(message.get("content"))
            if text:
                messages.append({
                    "role": "user",
                    "content": text,
                    "timestamp": _parse_timestamp(entry),
                    "agent_id": agent_id,
                })
            # main-thread user entries (agent_id is None): skipped, already
            # captured via UserPromptSubmit or PostToolUse.

    return messages
