---
title: lcm_describe + Deterministic Exploration Summaries
date: 2026-04-14
status: approved
---

## Summary

Implement two missing v1 features identified by comparing claude-lcm against
the LCM paper (Ehrlich & Blackman, Voltropy PBC, arXiv Feb 2026):

1. **`lcm_describe(id)`** — extend the existing (stub) MCP tool to actually
   handle file snapshot lookups. The schema and handler already exist in
   `schemas.py` / `tools.py` / `mcp_server.py` but the handler only returns
   an empty-DAG message. This replaces that stub with a real implementation.
2. **Deterministic Exploration Summaries** — type-aware, LLM-free file
   summaries stored alongside each file snapshot, making `lcm_describe`
   genuinely informative and aligning with the paper's §2.2 design.

Plus a full-codebase code review pass (all Python files, not just touched ones).

No compaction. No LLM calls. All schema changes are additive.

---

## Architecture

### Schema change — `file_snapshots`

One new nullable column:

```sql
ALTER TABLE file_snapshots ADD COLUMN exploration_summary TEXT;
```

Added via the existing additive migration pattern in `store.py` (run on
`open()`; idempotent via `PRAGMA table_info`). Existing rows get `NULL`.
New snapshots get a summary when the explorer succeeds, `NULL` on any failure.

### New module — `claude_lcm/explorer.py`

Deterministic, synchronous, no external dependencies beyond the stdlib.
Single public function:

```python
def explore(path: str, blob: bytes | None) -> str | None:
    """Return a short structural summary of the file, or None on failure."""
```

Type dispatch on file extension:

| Extension(s) | Strategy | Output shape |
|---|---|---|
| `.py` | regex over blob for `^(async )?def ` and `^class ` at col 0 | `"functions: foo, bar\nclasses: MyClass"` |
| `.json` | `json.loads` first 50 KB; enumerate top-level keys + value types | `"keys: name(str), items(list), meta(dict)"` |
| `.sql` | regex for `CREATE (TABLE\|VIEW\|INDEX)` names | `"tables: users, posts\nviews: active_users"` |
| `.md`, `.txt`, other text | first 20 lines + `...` + last 10 lines | truncated text preview |
| binary / undecodable | `None` | silent |

Rules:
- Always wrapped in a bare `except Exception: return None` — explorer failure
  must never raise to the hook.
- Output capped at 2000 characters.
- If `blob` is None (e.g. oversize snapshot), `path` alone is used to infer
  extension; content-dependent strategies return `None`.

### Hook changes — `pre_tool_use.py` and `post_tool_use.py`

The hooks call `eng.ingest_file_snapshot(...)` (the engine method). The
full change chain is:

1. In the hook, call `explore(file_path, content)` before calling
   `eng.ingest_file_snapshot(...)`, then pass `exploration_summary=summary`
   to it.
2. `engine.ingest_file_snapshot` gains an optional `exploration_summary`
   keyword arg (default `None`) and threads it through to the store call.
3. `store.append_file_snapshot` gains the same optional `exploration_summary`
   keyword arg and writes it to the new column.

Hook pseudocode (applies to **both** branches in each hook — the normal path
where `content` is a bytes value and the `content is None` path for oversize
files stored with `external_uri`):
```python
summary = explore(file_path, content)   # content may be None; explorer handles it
eng.ingest_file_snapshot(
    file_path=str(file_path),
    op="pre_" + tool_name.lower(),
    content=content,
    message_id=mid,
    exploration_summary=summary,   # new; will be None when content is None
)
```
When `content` is `None`, `explore()` returns `None` for all content-dependent
strategies (`.py`, `.json`, `.sql`); only path-extension-based fallbacks could
still run, but since they also need the blob they return `None` too. The result
is `NULL` in the column — correct and expected.

### `lcm_describe` schema update (`claude_lcm/schemas.py`)

The existing `LCM_DESCRIBE` schema uses `node_id: int (optional)` — a
DAG-only interface. This is replaced with a generalized `id` parameter that
handles both file snapshots and DAG nodes:

```python
LCM_DESCRIBE = {
    "name": "lcm_describe",
    "description": (
        "Return metadata for a file snapshot or summary node. "
        "Pass a snapshot_id (int) or a file path (string). "
        "For paths, pass session_id to scope to a lineage; omit for vault-global latest. "
        "In v1 the DAG is empty; integer IDs resolve to file snapshots only."
    ),
    "parameters": {           # must be "parameters" — mcp_server.py reads schema["parameters"]
        "type": "object",
        "properties": {
            "id": {
                "type": ["string", "integer"],
                "description": "snapshot_id (int) or file path (string)",
            },
            "session_id": {
                "type": "string",
                "description": "Optional. Used for lineage scoping on path-based lookups.",
            },
        },
        "required": ["id"],   # session_id optional — int IDs are vault-global; path lookup
                              # falls back to most-recent-across-vault if omitted
    },
}
```

This is a **breaking change to the existing schema** (replaces `node_id` with
`id`). Because the DAG is empty in v1 and the old handler returned only an
empty-DAG stub regardless of `node_id`, there is no functional regression.

### `lcm_describe` handler update (`claude_lcm/tools.py`)

The existing handler body is replaced. Resolution logic (all reads go through two new store methods added to
`MessageStore` in `store.py`, keeping `tools.py` free of raw SQL):

- `id` is an integer → call `store.get_file_snapshot(snapshot_id: int) → dict | None`.
  Looks up `file_snapshots` by `snapshot_id` directly; vault-global, no session scoping.
- `id` is a string → interpret as a file path; call
  `store.get_latest_snapshot_for_path(path: str, session_ids: list[str] | None) → dict | None`.
  If `session_id` was passed, walk lineage (same `walk_lineage` helper used by `lcm_grep`)
  and pass all session IDs in the lineage. If `session_id` is absent, pass `None` and the
  method returns the most-recent snapshot across the entire vault for that path.
- No match → return `{"error": "not found", "id": id}`.

Response shape:
```json
{
  "snapshot_id": 42,
  "path": "/home/lucas/ai/claude-lcm/store.py",
  "extension": ".py",
  "size_bytes": 8192,
  "captured_at": "2026-04-14T10:00:00Z",
  "session_id": "f16f80d1-...",
  "op": "pre_read",
  "exploration_summary": "functions: open, append_message, search\nclasses: MessageStore"
}
```

`extension` is derived at query time via `os.path.splitext(row["file_path"])[1]`
— it is not a stored column. `size_bytes` is `len(content_blob)` when the blob
is present, else `0`.

Note: `token_count` is **omitted** from the response in v1. The column does
not exist on `file_snapshots` and computing it on-the-fly is out of scope.

**Registration** (`adapter/mcp_server.py`): `lcm_describe` is already
registered. No change needed to `mcp_server.py` for registration — only
`schemas.py` and `tools.py` need updating.

---

## Code Review Scope

A dedicated review pass (separate agent, `superpowers:requesting-code-review`)
covers **all** Python source files — not just those modified here:

```
claude_lcm/
  store.py, dag.py, engine.py, tools.py, schemas.py,
  config.py, workspace.py, session_patterns.py, tokens.py,
  explorer.py  ← new
adapter/
  hooks/_common.py, session_start.py, user_prompt_submit.py,
  pre_tool_use.py, post_tool_use.py, stop.py, session_end.py
  mcp_server.py, install.py
tests/
  test_store_extensions.py, test_hooks.py, test_mcp_server.py
```

Any issues found are addressed before the branch is closed.

---

## Testing

**`test_store_extensions.py`** additions:
- `test_exploration_summary_stored` — write a `.py` snapshot with known
  content, assert `exploration_summary` contains expected function names.
- `test_exploration_summary_null_on_failure` — pass garbage bytes, assert
  `exploration_summary` is `None` without raising.
- `test_explorer_json` — known JSON blob → correct key listing.
- `test_explorer_sql` — known SQL → correct table listing.
- `test_explorer_fallback` — `.txt` file → preview truncation.

**`test_mcp_server.py`** additions:
- `test_lcm_describe_by_snapshot_id` — insert snapshot, call handler with
  int id, assert all response fields present.
- `test_lcm_describe_by_path` — insert snapshot, call with path string,
  assert returns latest snapshot.
- `test_lcm_describe_not_found` — call with unknown int id and with unknown
  path string; both assert `{"error": "not found", "id": <id>}`.

---

## Files Changed

| File | Change |
|---|---|
| `claude_lcm/store.py` | Add `exploration_summary` column migration; add kwarg to `append_file_snapshot`; add `get_file_snapshot(snapshot_id)` and `get_latest_snapshot_for_path(path, session_ids)` read methods |
| `claude_lcm/engine.py` | Add `exploration_summary` kwarg to `ingest_file_snapshot`; thread through to store |
| `claude_lcm/explorer.py` | **New** — deterministic file explorer |
| `claude_lcm/schemas.py` | Replace `LCM_DESCRIBE` schema: `node_id` → `id` (string\|integer) |
| `claude_lcm/tools.py` | Replace `lcm_describe` stub handler with real implementation |
| `adapter/hooks/pre_tool_use.py` | Call `explore()`, pass `exploration_summary` to `ingest_file_snapshot` |
| `adapter/hooks/post_tool_use.py` | Call `explore()`, pass `exploration_summary` to `ingest_file_snapshot` |
| `tests/test_store_extensions.py` | Explorer + column tests |
| `tests/test_mcp_server.py` | `lcm_describe` handler tests |

`adapter/mcp_server.py` — no change needed (`lcm_describe` already registered).

All other files reviewed but not modified (unless code review finds issues).

---

## Out of Scope

- Token count persistence (`token_count` column on `messages` or `file_snapshots`) — v2
- LLM-generated exploration summaries — v2
- `lcm_expand` implementation — v2
- Summary node compaction — v2
- `llm_map` / `agentic_map` / `Task` operator tools — Volt-specific, not v1
