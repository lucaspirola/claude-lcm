"""Deterministic file explorer — generates structural summaries without LLM calls.

Called at file-snapshot capture time. Returns a short, human-readable string
describing the file's structure, or None on any failure. Always safe to call:
all errors are swallowed and produce None rather than raising.
"""

from __future__ import annotations

import json
import os
import re

_MAX_OUTPUT = 2000
_JSON_PEEK = 50 * 1024  # bytes
_PREVIEW_HEAD = 20
_PREVIEW_TAIL = 10


def explore(path: str, blob: bytes | None) -> str | None:
    """Return a short structural summary of the file, or None on failure.

    Args:
        path: File path — used for extension dispatch.
        blob: Raw file bytes, or None for oversize/missing files.
              Content-dependent strategies return None when blob is None.
    """
    if blob is None:
        return None
    try:
        ext = os.path.splitext(path)[1].lower()
        if ext == ".py":
            return _explore_python(blob)
        if ext == ".json":
            return _explore_json(blob)
        if ext == ".sql":
            return _explore_sql(blob)
        return _explore_text(blob)
    except Exception:
        return None


# ── per-type explorers ─────────────────────────────────────────────────────────

def _explore_python(blob: bytes) -> str | None:
    text = blob.decode("utf-8")
    functions = re.findall(r"^(?:async )?def (\w+)", text, re.MULTILINE)
    classes = re.findall(r"^class (\w+)", text, re.MULTILINE)
    parts = []
    if functions:
        parts.append("functions: " + ", ".join(functions))
    if classes:
        parts.append("classes: " + ", ".join(classes))
    result = "\n".join(parts) if parts else None
    return _cap(result)


def _explore_json(blob: bytes) -> str | None:
    # Decode first, then truncate by character — slicing raw bytes can cut a
    # multi-byte sequence or a JSON token, producing spurious decode/parse errors.
    text = blob.decode("utf-8", errors="replace")[:_JSON_PEEK]
    data = json.loads(text)
    if not isinstance(data, dict):
        return None
    type_name = {
        dict: "dict", list: "list", str: "str",
        int: "int", float: "float", bool: "bool", type(None): "null",
    }
    pairs = [
        f"{k}({type_name.get(type(v), type(v).__name__)})"
        for k, v in data.items()
    ]
    return _cap("keys: " + ", ".join(pairs)) if pairs else None


def _explore_sql(blob: bytes) -> str | None:
    text = blob.decode("utf-8")
    tables = re.findall(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)", text, re.IGNORECASE
    )
    views = re.findall(
        r"CREATE\s+VIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)", text, re.IGNORECASE
    )
    parts = []
    if tables:
        parts.append("tables: " + ", ".join(tables))
    if views:
        parts.append("views: " + ", ".join(views))
    return _cap("\n".join(parts)) if parts else None


def _explore_text(blob: bytes) -> str | None:
    text = blob.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if len(lines) <= _PREVIEW_HEAD + _PREVIEW_TAIL:
        return _cap("\n".join(lines))
    head = lines[:_PREVIEW_HEAD]
    tail = lines[-_PREVIEW_TAIL:]
    return _cap("\n".join(head) + "\n...\n" + "\n".join(tail))


def _cap(s: str | None) -> str | None:
    if s is None:
        return None
    return s[:_MAX_OUTPUT] if len(s) > _MAX_OUTPUT else s
