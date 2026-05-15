# Session Backup — claude-lcm development

If `lcm_recent` / `lcm_grep` are not working, read this file to restore context.

## What this project is

`claude-lcm` is a lossless transcript vault for Claude Code. It captures every
message, tool call, and assistant turn into a local SQLite database at
`~/.local/share/claude-lcm/vault.sqlite`, and exposes the archive via an MCP
server so future sessions can recall prior context — including across `/clear`.

Repo: `/home/lucas/ai/claude-lcm`
Install: `.venv/bin/python -m adapter.install` then restart CC.

## What we built in this session (2026-04-14)

### Feature: /clear lineage (`feature/clear-lineage`, merged to `main`)

When the user types `/clear`, CC mints a new `session_id`. Previously the vault
lost the link. Now:

- `SessionEnd(source='clear')` writes `clear_handoff[project_key] = old_sid`
- `SessionStart(source='clear')` reads it, stamps `parent_session_id` on the new row
- `project_key = sanitize_path(cwd)` — matches CC's `~/.claude/projects/<sanitized>/`

`lcm_grep` gained a `scope` param (`lineage` | `workspace` | `session`, default `lineage`).
`lcm_recent` was added — returns last N messages newest-first, same scope support.

### Key commits (all on `main`)

```
94496fa feat(tools): add lcm_recent
43a40d9 fix(store): backfill migration + parent_session_id FK
43dbb19 feat(tools): lcm_grep scope parameter
4b95bf9 feat(store): search accepts session_ids list filter
def03b9 test(hooks): end-to-end /clear chain
49e20e4 feat(hooks): session_start stamps project_key + consumes handoff
d1a430c feat(hooks): session_end writes clear_handoff on source=clear
c2bbad9 feat(store): walk_lineage recursive CTE
bb4aefb feat(store): clear_handoff upsert/take + set_end_reason
70696cc feat(store): open_session accepts project_key and parent_session_id
5901eaf feat(store): additive schema migration
46986bb fix(workspace): import + parity test
1609748 feat(workspace): port CC's sanitizePath
```

### Files changed

| File | What changed |
|---|---|
| `claude_lcm/workspace.py` | `sanitize_path()` + djb2 hash |
| `claude_lcm/store.py` | Schema migration; 7 new methods; `search` + `recent_messages` |
| `claude_lcm/engine.py` | Passthroughs for all new store methods |
| `claude_lcm/schemas.py` | `_SCOPE_PARAM` on all tools; new `LCM_RECENT` |
| `claude_lcm/tools.py` | `_resolve_scope_session_ids`; `lcm_grep` scope; `lcm_recent` |
| `adapter/hooks/session_end.py` | Writes handoff on source=clear |
| `adapter/hooks/session_start.py` | Stamps project_key; consumes handoff |
| `adapter/mcp_server.py` | Registered `lcm_recent` |

### Tests

48 tests pass. Run: `PYTHONPATH= PYTHONNOUSERSITE=1 .venv/bin/python -m pytest tests/ -v`

## Design spec & plan

- Spec: `docs/superpowers/specs/2026-04-14-clear-lineage-design.md`
- Plan: `docs/superpowers/plans/2026-04-14-clear-lineage.md`

## What was NOT done (explicitly out of scope)

- Storage location change (vault stays at XDG `~/.local/share/claude-lcm/`)
- Git worktree collapsing — `project_key` is always raw cwd
- Compaction / summary DAG (v2)
- `lcm adopt` recovery tool for renamed folders

## Latest change (2026-04-14, session f43cf894)

### Recall intent: hook pre-fetches messages, no tool call needed

When the user's prompt contains a recall phrase ("remember our last N messages",
"catch me up", "what were we doing", etc.), `UserPromptSubmit` now detects it,
fetches the messages from the vault directly, and injects them as `additionalContext`.
Claude receives the data without needing to call any tool.

Patterns detected: remember, recall, catch me up, what were we, what did we,
restore context, prior context, recent messages/context/history, past/last N messages.

Numeric limit is parsed from the prompt; defaults to 20 if no number found.

Files changed: `adapter/hooks/user_prompt_submit.py`, `claude_lcm/engine.py` (new `recent_messages_lineage`).

### Fresh `claude` start now auto-links to prior session

Previously, killing CC and restarting with plain `claude` (no `--resume`, no `/clear`)
left the new session unlinked — `lcm_recent scope=lineage` only saw the empty new session.

Fix: `SessionStart` now calls `latest_session_for_project(project_key, exclude=self)`
when there is no `/clear` handoff. If a prior session exists for the same workspace,
it is stamped as `parent_session_id` automatically.

Files changed:
- `claude_lcm/store.py` — `latest_session_for_project()`
- `claude_lcm/engine.py` — passthrough
- `adapter/hooks/session_start.py` — auto-link on fresh start; unified "has_prior" context

53 tests pass.
