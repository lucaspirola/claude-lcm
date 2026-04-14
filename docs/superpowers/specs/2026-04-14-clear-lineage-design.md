# claude-lcm: `/clear` Lineage & Workspace Identity

- **Status:** Approved (brainstorm complete, ready for implementation plan)
- **Date:** 2026-04-14
- **Brainstorm session:** `d7759f16-0897-4a23-9a1c-7f91ffd9f309`

## Problem

When the user types `/clear` in Claude Code, CC calls `regenerateSessionId()` and mints a fresh `session_id`. The previous session's id becomes a `parentSessionId` in bootstrap state, but that pointer is **in-memory only and never exposed in hook payloads**. Result: claude-lcm's hooks see a new `session_id` with no link back to the conversation the user just "cleared." Any `lcm_*` query scoped by `session_id` loses everything prior, even though the rows are still in the vault.

## Non-problems ruled out during research

After tracing CC source (`/home/lucas/ai/hermes/claude-code`), the following were confirmed to **not** create lineage gaps and need no special handling:

| Event | `session_id` behavior | Hook visibility |
|---|---|---|
| `/compact` / auto-compact | unchanged | fires `SessionStart(source='compact')` as a *notification only*; `session_id` is the same before and after |
| `/rewind` | unchanged | **no hook fires at all**; it's an in-memory message-array truncate + UI conversation-id reroll for the logo |
| `--resume <sid>` | `switchSession(<sid>)` adopts a known id | fires `SessionStart(source='resume')`; the id is one the vault already knows about |
| normal message | unchanged | — |

`regenerateSessionId()` is called from exactly one site in the entire CC codebase: `src/commands/clear/conversation.ts:203`. **`/clear` is the only event that breaks session-id lineage.**

## Key design constraint

`clearConversation` runs its hook sequence synchronously in a single process tick:

```
SessionEnd(source='clear')  →  regenerateSessionId()  →  SessionStart(source='clear')
```

This gives a deterministic two-hook handoff window within one CC process. No async delay, no timestamp budget, no cross-process state, no user-quittable gap.

## Identity model

```python
project_key = sanitizePath(cwd)
```

Matches CC's own project-directory naming (`~/.claude/projects/-home-lucas-ai-claude-lcm/`).

**Invariant: where the user starts CC == what context they see.** No git detection, no worktree collapsing, no sentinel files in the project folder, no home-directory rename of the vault. Accepted side effects:

- Running CC from a subdirectory yields a different `project_key` than running from the repo root. Matches CC.
- Git worktrees get independent histories. Matches CC.
- Renaming / moving a folder makes history unreachable from the new path; rows remain in the vault, recoverable via a future `lcm adopt` tool *only if* anyone asks for one.

## Scope policy (Option C)

`lcm_grep` and the other `lcm_*` query tools gain a `scope` parameter:

| `scope` | Query |
|---|---|
| `'lineage'` **(default)** | Walk `parent_session_id` recursively from the current session; UNION-filter on the resulting set of session ids |
| `'workspace'` | `project_key = <current project_key>` |
| `'session'` | `session_id = <current session_id>` |

`'lineage'` is the default because it matches "what came before in this conversation thread" — the most common recall need right after a `/clear`.

## Schema delta

All additions are new columns and a new table. `hermes-lcm` base schema stays unchanged, preserving cross-agent vault compatibility stated in CONTEXT.md.

```sql
-- sessions: new columns
ALTER TABLE sessions ADD COLUMN project_key TEXT;
ALTER TABLE sessions ADD COLUMN parent_session_id TEXT
    REFERENCES sessions(session_id);
ALTER TABLE sessions ADD COLUMN end_reason TEXT;   -- 'clear' | 'normal' | NULL

CREATE INDEX IF NOT EXISTS idx_sessions_project_key_ended
    ON sessions(project_key, ended_at DESC);

-- new: synchronous handoff table; single row per project_key at any moment
CREATE TABLE IF NOT EXISTS clear_handoff (
    project_key       TEXT PRIMARY KEY,
    ending_session_id TEXT NOT NULL,
    ts                REAL NOT NULL
);
```

`workspace_fingerprint` (git-remote-sha256) stays in the schema as a nullable metadata column. It is no longer load-bearing for scoping and may be removed or left orphaned in a later cleanup.

## Hook changes

### `adapter/hooks/session_end.py`

On `payload.source == 'clear'`:

1. `project_key = sanitize_path(payload['cwd'])`
2. `UPDATE sessions SET ended_at = now, end_reason = 'clear' WHERE session_id = :sid`
3. `INSERT OR REPLACE INTO clear_handoff (project_key, ending_session_id, ts) VALUES (:pk, :sid, now)`

On any other `source`: set `end_reason = 'normal'`, no handoff row.

### `adapter/hooks/session_start.py`

Always:

1. `project_key = sanitize_path(payload['cwd'])`
2. `INSERT INTO sessions (session_id, project_key, started_at, …) VALUES (…)`

Additionally, on `payload.source == 'clear'`:

3. `row = SELECT * FROM clear_handoff WHERE project_key = :pk`
4. If row found: `UPDATE sessions SET parent_session_id = row.ending_session_id WHERE session_id = :new_sid`
5. `DELETE FROM clear_handoff WHERE project_key = :pk`

**No timestamp check, no TTL.** The handoff row always belongs to *this* `/clear` because it was written nanoseconds earlier in the same CC process tick.

### Other hooks

`pre_tool_use`, `post_tool_use`, `user_prompt_submit`, `stop` are unchanged. They continue to write message / tool rows keyed on `session_id`.

## MCP tool change

`claude_lcm/schemas.py` — add `scope` to the parameter schemas of `lcm_grep` (and, for consistency, the other `lcm_*` tools) with an enum `'session' | 'workspace' | 'lineage'`, default `'lineage'`.

`claude_lcm/tools.py` handler resolution:

```python
def _resolve_scope(store, session_id: str, scope: str) -> tuple[str, dict]:
    if scope == 'session':
        return "session_id = :sid", {"sid": session_id}
    if scope == 'workspace':
        pk = store.project_key_for_session(session_id)
        return "project_key = :pk", {"pk": pk}
    # default: 'lineage'
    ids = store.walk_lineage(session_id)
    placeholders = ",".join(f":id{i}" for i in range(len(ids)))
    return (
        f"session_id IN ({placeholders})",
        {f"id{i}": sid for i, sid in enumerate(ids)},
    )
```

`store.walk_lineage(session_id)` uses a recursive CTE:

```sql
WITH RECURSIVE lineage(sid) AS (
    SELECT :start_sid
    UNION ALL
    SELECT s.parent_session_id
      FROM sessions s
      JOIN lineage l ON s.session_id = l.sid
     WHERE s.parent_session_id IS NOT NULL
)
SELECT sid FROM lineage;
```

## `sanitize_path` implementation

Mirror CC's behavior exactly so that `project_key` values match the directory names CC already writes under `~/.claude/projects/`. A straightforward port:

```python
def sanitize_path(cwd: str) -> str:
    abs_path = os.path.abspath(os.path.expanduser(cwd))
    return abs_path.replace("/", "-")
```

The implementation must be validated against CC's `utils/path.ts` via a unit test that spot-checks real `~/.claude/projects/*` directory names on the same machine.

## Migration

On `store.py` init, run once (idempotent):

```sql
UPDATE sessions
   SET project_key = <sanitized_from_workspace_path>
 WHERE project_key IS NULL
   AND workspace_path IS NOT NULL;
```

Rows where `workspace_path` is also NULL keep `project_key = NULL` and are only reachable via `scope='session'`. No data loss, no destructive migration.

## Testing plan

New tests in `tests/`:

1. **Single `/clear` handoff** — fire `SessionEnd(source='clear', session_id=A, cwd=X)`, then `SessionStart(source='clear', session_id=B, cwd=X)`. Assert `B.parent_session_id == A` and `clear_handoff` is empty.
2. **Chain walk** — simulate `A → /clear → B → /clear → C → /clear → D`. Assert `walk_lineage(D) == [D, C, B, A]`.
3. **Fork via `--resume`** — `A → /clear → B → /clear → C` (active), then resume `A`, then `/clear → D`. Assert `walk_lineage(D) == [D, A]` and `walk_lineage(C) == [C, B, A]`. The two sub-chains walk independently.
4. **Multi-project isolation** — `/clear` in project `X` never touches `clear_handoff` for project `Y`. Concurrent two-project test with tmp `project_key`s.
5. **Orphan handoff overwrite** — if `SessionStart(source='clear')` never fires after a `SessionEnd(source='clear')` (user quit immediately), the next `SessionEnd(source='clear')` in the same `project_key` overwrites the orphan via `INSERT OR REPLACE`. No row buildup.
6. **`lcm_grep` scope** — seeded fixture vault; assert `scope='session'`, `scope='workspace'`, `scope='lineage'` return the documented subsets.
7. **`sanitize_path` parity** — assert our implementation produces the same string as CC on at least one known-good example against the live `~/.claude/projects/` listing.

## Explicitly out of scope

- Storage location change — vault stays at `~/.local/share/claude-lcm/vault.sqlite`.
- Sentinel file inside project folders.
- PPID-based handoff.
- Git-worktree collapsing to main-repo identity.
- Timestamp TTLs on `clear_handoff`.
- `lcm adopt` / manual project reassociation command (deferred until a user asks).
- Changes to the `hermes-lcm` base schema (cross-agent compatibility preserved).
- v2 compaction / summary DAG (separate, future work).

## Open questions (deferred)

- **Subdirectory runs.** If users routinely run CC from a subdir and expect repo-root context, they will be surprised. If that complaint appears, add `scope='repo'` (computed via `git rev-parse --git-common-dir`) as a new scope *without* touching the identity column.
- **`lcm adopt`.** Recovery tool for renamed or moved folders. Deferred until asked.
- **Simultaneous `/clear` across two CC windows on the same `project_key`.** Accepted rare-case failure: the later writer's handoff wins via `INSERT OR REPLACE`, the earlier `/clear`'s new session gets no `parent_session_id`. Documented as a known limitation.

## Brainstorm provenance

This design was converged through a sequence of narrowing and rejections:

1. First proposal (timestamp-bounded handoff + PPID key + git-remote-sha256 workspace) was rejected by the user as over-engineered and failing to cover rewind/resume/compact.
2. CC source research refuted the assumption that `/compact`, `/rewind`, and `--resume` regenerate `session_id`. Only `/clear` does.
3. User's "delete folder → history gone" observation reframed identity as path-based rather than git-based.
4. User rejected bundling a storage-location move into the identity change; storage stays where it is.
5. User rejected git-worktree collapsing in favor of a strict "cwd == project" invariant matching CC's own behavior.

The final design is strictly smaller than the first proposal: one schema delta, one new table, two hook-file edits, one parameter added to the query tools. No new dependencies, no new processes, no configuration surface.
