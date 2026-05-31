# claude-lcm

A **lossless transcript vault** for Claude Code (and other local agents). It
captures every user message, assistant turn, tool call + result, skill load,
and file snapshot into a local SQLite database, and exposes the archive over an
MCP server so future sessions can recall prior context — even messages that
have scrolled out of Claude Code's live context window.

Runs entirely locally. No cloud, no required API keys. The base schema is
lifted verbatim from [`hermes-lcm`](https://github.com/stephenschoettler/hermes-lcm)
so vaults stay cross-compatible between agents.

---

## What it captures

Every Claude Code event, via six lifecycle hooks, is appended to the vault:

| Hook | Captured |
|---|---|
| `SessionStart` | session row + lineage link; injects the session-id context block |
| `UserPromptSubmit` | user message; detects recall intent and pre-injects recent context |
| `PreToolUse` | tool call (`tool_use`) + file snapshot & structure summary |
| `PostToolUse` | tool result (`tool_result`) + file snapshot & structure summary |
| `Stop` | assistant turn text |
| `SessionEnd` | session close |

Additional capture detail:

- **File snapshots** — file content read/written by tools is stored inline
  (blob), capped at `LCM_MAX_SNAPSHOT_BYTES` (oversize falls back to an
  `oversize://` URI placeholder).
- **Deterministic structure summaries** — at snapshot time, `explorer.py`
  produces a short, stdlib-only summary per file: `.py` → defs/classes,
  `.json` → top-level keys + types, `.sql` → tables/views, else a text
  head/tail preview. No LLM involved.
- **Recall-intent injection** — when a prompt matches phrases like "remember",
  "catch me up", or "last N messages", the `UserPromptSubmit` hook pre-fetches
  recent lineage messages and injects them as `additionalContext`, so Claude
  can answer without spending a tool call.
- **Crash-safe** — hooks never block Claude Code. Any exception is logged to
  `~/.local/share/claude-lcm/hook.log` and a permissive `{"continue": true}`
  is emitted; a no-op stand-in engine is used if the vault can't open.

---

## MCP tools

Ten `lcm_*` tools are exposed over a stdio MCP server. All are **deterministic
— no LLM calls**.

### Recall

- **`lcm_grep`** — full-history search over raw messages. FTS5 syntax
  (keywords, `"quoted phrases"`, `OR`, `NOT`). `match_mode="fts5"` (default) or
  `"literal"` (escapes punctuation like `:` `[` `-`). On an FTS5 parse error it
  auto-retries as a quoted literal and surfaces an explicit `fts5_parse_error`
  rather than returning a false "no matches".
- **`lcm_recent`** — the most recent N messages, newest-first. Ideal after
  `/clear` to recall what was being discussed. Accepts `n` as an alias for
  `limit`.

### Audit / observability

- **`lcm_tool_calls`** — structured tool-call audit: each `tool_use` paired
  with its `tool_result`, with parsed `args` and a truncated `result`. Pairs by
  `tool_call_id` when present, else by same-tool-name call order.
  `group_by="call"` (flat, newest-first) or `"turn"` (grouped under the
  assistant turn). Defaults to `scope="session"`.
- **`lcm_whoami`** — the calling session's identity + lineage: `session_id`,
  `parent_session_id`, the full lineage chain, `started_at`, workspace. Falls
  back to a best-effort `CLAUDE_PROJECT_DIR` → latest-session guess when
  `session_id` is omitted (flagged via `resolved_via`).
- **`lcm_describe`** — metadata for a file snapshot or a session overview. Pass
  a `snapshot_id` (int) or a file path (string) as `id`; omit `id` for a
  session overview. For paths, pass `session_id` to scope to a lineage.

### Marks (write tools)

- **`lcm_mark`** — record a named, first-class bookmark / protocol marker
  (e.g. `name="ml-intern:active"`). Optionally pass `store_id` to bookmark and
  pin a specific message. Prefer this over embedding magic marker strings in
  the transcript.
- **`lcm_marks`** — list marks, optionally filtered by name.

### Vault status

- **`lcm_status`** — quick health overview: session count, message count, vault
  size on disk, active config.
- **`lcm_doctor`** — diagnostics: database integrity, FTS5 sync, orphaned DAG
  nodes, config validation.

### Deferred to v2 (degrades gracefully)

- **`lcm_expand`** — recover original detail behind a summary node. The DAG is
  empty in v1, so this is a no-op that redirects you to `lcm_grep`.

---

## Scope model

Recall and audit tools accept a shared `scope` parameter:

| Scope | Meaning |
|---|---|
| `lineage` | walk `parent_session_id` transitively — includes sessions chained by `/clear` (**default for recall tools**) |
| `workspace` | every session in the same project (sanitized cwd) |
| `session` | the current `session_id` only — point-in-time audits (**default for audit tools**) |
| `auto` | deterministically `session` if the current session has rows of its own, else `lineage` |

### Session identity

Claude Code does **not** pass the session id to MCP servers (only
`CLAUDE_PROJECT_DIR`). The `SessionStart` hook injects a context block telling
Claude its `session_id` and to pass it on every `lcm_*` call. This is the only
identity channel; `lcm_whoami`'s `CLAUDE_PROJECT_DIR` fallback is the safety net
when it's missing. **Subagents** share the parent's `session_id` but do not
inherit the injected text — pass it into the subagent prompt explicitly.

---

## Install

Requires Python 3.12+.

```bash
# One-time setup
python3 -m venv .venv && .venv/bin/pip install mcp pytest

# Install the adapter into ~/.claude (hooks + MCP server)
.venv/bin/python -m adapter.install --dry-run   # preview
.venv/bin/python -m adapter.install              # install
.venv/bin/python -m adapter.install --uninstall  # remove
```

The installer is idempotent: hook commands carry a `# claude-lcm` sentinel for
re-entrant detection, backups are written with a `.clcm.bak` suffix, hook
commands are pinned to `.venv/bin/python` (else `sys.executable`), and
`PYTHONPATH=<repo_root>` is prepended so neither the hooks nor the MCP server
need a pip install.

### Tests

```bash
PYTHONPATH= PYTHONNOUSERSITE=1 .venv/bin/python -m pytest tests/
```

(`PYTHONPATH=` + `PYTHONNOUSERSITE=1` disable ROS 2 launch_testing plugins
dragged in via user site-packages on some machines.)

---

## Configuration

| Var | Purpose | Default |
|---|---|---|
| `LCM_VAULT_PATH` | SQLite vault file path | `~/.local/share/claude-lcm/vault.sqlite` (XDG) |
| `LCM_MAX_SNAPSHOT_BYTES` | Per-snapshot blob cap; oversize → `oversize://` URI | 2 MiB |
| `LCM_HOOK_LOG` | Hook crash log path | `~/.local/share/claude-lcm/hook.log` |
| `LCM_SESSION_ID` | Override session id (hook testing) | unset |
| `LCM_LOG_LEVEL` | Python log level | `WARNING` |

---

## Architecture

Three layers; `claude_lcm/` knows nothing about Claude Code.

### `claude_lcm/` — host-agnostic engine

- `store.py` — SQLite `MessageStore`, WAL mode, FTS5 virtual table mirrored off
  `messages` via triggers. Base schema lifted from hermes-lcm; extensions
  (`sessions`, `skill_loads`, `file_snapshots`, `marks`) are additive. New
  columns arrive via idempotent `PRAGMA table_info`-guarded migrations on
  `open()` — never destructive ALTERs.
- `engine.py` — `ClaudeLcmEngine` owns one `MessageStore` + one `SummaryDAG`
  bound to a single session: `open_session`, `ingest_*`, `grep`, `close`.
- `tools.py` / `schemas.py` — the `lcm_*` handlers and their JSON schemas.
- `explorer.py` — deterministic, stdlib-only file-structure summarizer. Every
  failure path returns `None`, so it's always safe to call from a hook.
- `dag.py` — `SummaryDAG` for hierarchical compaction. **Empty in v1**; the
  table exists but nothing writes to it (compaction is v2).
- `config.py`, `workspace.py`, `session_patterns.py`, `tokens.py` — helpers
  (config dataclass, git-remote fingerprinting, turn grouping, optional
  tiktoken token counting).

### `adapter/` — the Claude Code bridge

- `adapter/hooks/*.py` — one short-lived process per hook event.
- `adapter/hooks/_common.py` — crash-safe engine context manager + `safe_main`.
- `adapter/mcp_server.py` — stdio MCP server exposing the ten tools.
- `adapter/install.py` — idempotent installer.

### `vendor/` — reference copy of upstream

Raw `hermes-lcm` files kept for diff reference. **Not installed, not imported.**
Diff against these before changing any lifted module — preserving the base
schema is what keeps vaults cross-agent compatible.

---

## Status & roadmap

v1 is **lossless transcripts only** — message store + FTS5, no compaction, no
local LLM. Deliberately out of scope until re-planned:

- **Compaction / summary nodes** — v2 (`dag.py` stays empty; `lcm_expand`
  degrades gracefully).
- **Local LLM** — v2.
- **agentfs-backed external snapshots** — v3 (the `file_snapshots.external_uri`
  column already exists; flipping from inline blob to URI is the
  schema-compatible path).

The base schema must not change — breaking cross-agent vault compatibility is
the one-way door this project is designed to avoid.

See [`CONTEXT.md`](CONTEXT.md) for the full design history and rationale.
