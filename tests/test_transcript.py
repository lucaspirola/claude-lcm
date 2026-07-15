"""Unit tests for claude_lcm/transcript.py — the pure JSONL transcript parser."""

from __future__ import annotations

import json
from pathlib import Path

from claude_lcm.transcript import extract_messages, read_new_lines


def _line(entry_type: str, role: str, content) -> str:
    return json.dumps({
        "type": entry_type,
        "message": {"role": role, "content": content},
        "timestamp": "2026-07-15T10:00:00.000Z",
    }) + "\n"


def test_read_new_lines_missing_file_returns_empty(tmp_path: Path):
    entries, offset = read_new_lines(tmp_path / "nope.jsonl", 0)
    assert entries == []
    assert offset == 0


def test_read_new_lines_full_file(tmp_path: Path):
    p = tmp_path / "t.jsonl"
    p.write_text(_line("assistant", "assistant", [{"type": "text", "text": "hi"}]))
    entries, offset = read_new_lines(p, 0)
    assert len(entries) == 1
    assert offset == p.stat().st_size


def test_read_new_lines_incremental(tmp_path: Path):
    p = tmp_path / "t.jsonl"
    p.write_text(_line("assistant", "assistant", [{"type": "text", "text": "one"}]))
    entries1, offset1 = read_new_lines(p, 0)
    assert len(entries1) == 1

    with p.open("a") as f:
        f.write(_line("assistant", "assistant", [{"type": "text", "text": "two"}]))
    entries2, offset2 = read_new_lines(p, offset1)
    assert len(entries2) == 1
    assert extract_messages(entries2)[0]["content"] == "two"
    assert offset2 > offset1


def test_read_new_lines_holds_back_partial_trailing_line(tmp_path: Path):
    p = tmp_path / "t.jsonl"
    complete = _line("assistant", "assistant", [{"type": "text", "text": "done"}])
    partial = json.dumps({"type": "assistant", "message": {"role": "assistant",
                          "content": [{"type": "text", "text": "unfinished"}]}})
    # Write bytes, not text: the parser tracks raw on-disk byte offsets, and
    # this assertion pins one to a Python-computed length. Path.write_text opens
    # in text mode, which on Windows translates '\n' -> '\r\n' and shifts every
    # offset by a byte. Claude Code writes LF-delimited JSONL, so feed the parser
    # exactly those bytes on every platform.
    p.write_bytes((complete + partial).encode())  # no trailing newline on partial

    entries, offset = read_new_lines(p, 0)
    assert len(entries) == 1
    assert extract_messages(entries)[0]["content"] == "done"
    assert offset == len(complete.encode())

    # Completing the line on a later write is picked up from the held-back offset.
    with p.open("ab") as f:
        f.write(b"\n")
    entries2, offset2 = read_new_lines(p, offset)
    assert len(entries2) == 1
    assert extract_messages(entries2)[0]["content"] == "unfinished"
    assert offset2 == p.stat().st_size


def test_read_new_lines_skips_malformed_json(tmp_path: Path):
    p = tmp_path / "t.jsonl"
    good = _line("assistant", "assistant", [{"type": "text", "text": "ok"}])
    p.write_text("{not valid json\n" + good)
    entries, offset = read_new_lines(p, 0)
    assert len(entries) == 1
    assert extract_messages(entries)[0]["content"] == "ok"


def test_extract_messages_text_and_thinking_get_distinct_roles():
    entries = [json.loads(_line("assistant", "assistant", [
        {"type": "thinking", "thinking": "reasoning..."},
        {"type": "text", "text": "the answer"},
    ]))]
    messages = extract_messages(entries)
    assert messages == [
        {"role": "assistant_thinking", "content": "reasoning...",
         "timestamp": messages[0]["timestamp"], "agent_id": None},
        {"role": "assistant", "content": "the answer",
         "timestamp": messages[1]["timestamp"], "agent_id": None},
    ]


def test_extract_messages_skips_tool_use_blocks():
    entries = [json.loads(_line("assistant", "assistant", [
        {"type": "tool_use", "name": "Read", "input": {"file_path": "/x"}},
    ]))]
    assert extract_messages(entries) == []


def test_extract_messages_skips_empty_blocks():
    entries = [json.loads(_line("assistant", "assistant", [
        {"type": "text", "text": "   "},
        {"type": "thinking", "thinking": ""},
    ]))]
    assert extract_messages(entries) == []


def test_extract_messages_main_thread_user_entries_are_skipped():
    entries = [json.loads(_line("user", "user", "hello"))]
    assert extract_messages(entries, agent_id=None) == []


def test_extract_messages_subagent_user_entries_are_kept():
    entries = [json.loads(_line("user", "user", "do the thing"))]
    messages = extract_messages(entries, agent_id="agent-x1")
    assert messages == [{
        "role": "user", "content": "do the thing",
        "timestamp": messages[0]["timestamp"], "agent_id": "agent-x1",
    }]


def test_extract_messages_subagent_user_tool_result_blocks_are_skipped():
    entries = [json.loads(_line("user", "user", [
        {"type": "tool_result", "content": "some tool output"},
    ]))]
    assert extract_messages(entries, agent_id="agent-x1") == []


def test_extract_messages_tags_agent_id_on_text_and_thinking():
    entries = [json.loads(_line("assistant", "assistant", [
        {"type": "text", "text": "sub reply"},
    ]))]
    messages = extract_messages(entries, agent_id="agent-x1")
    assert messages[0]["agent_id"] == "agent-x1"
