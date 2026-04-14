"""Idempotent installer for claude-lcm Claude Code adapter.

Writes hook entries into ~/.claude/settings.json and registers the MCP
server. Re-running is safe: existing entries with matching commands are
left alone; new entries are appended.

Usage:
    python -m adapter.install           # install
    python -m adapter.install --uninstall
    python -m adapter.install --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[1]
SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
MCP_SETTINGS_PATH = Path.home() / ".claude.json"  # Claude Code MCP config location
VAULT_DEFAULT = Path.home() / ".local" / "share" / "claude-lcm"

HOOKS: list[tuple[str, str]] = [
    ("SessionStart", "adapter.hooks.session_start"),
    ("UserPromptSubmit", "adapter.hooks.user_prompt_submit"),
    ("PreToolUse", "adapter.hooks.pre_tool_use"),
    ("PostToolUse", "adapter.hooks.post_tool_use"),
    ("Stop", "adapter.hooks.stop"),
    ("SessionEnd", "adapter.hooks.session_end"),
]

MCP_TAG = "claude-lcm-mcp"
HOOK_TAG = "# claude-lcm"  # sentinel substring used for idempotency


def _python_exe() -> str:
    """Pick the python interpreter to invoke hooks with.

    Prefer the project venv if present; otherwise whatever is running us.
    """
    venv_py = REPO_ROOT / ".venv" / "bin" / "python"
    if venv_py.exists():
        return str(venv_py)
    return sys.executable


def _hook_command(module: str) -> str:
    py = _python_exe()
    # PYTHONPATH prepend so hook processes can import the repo without install
    return (
        f'env PYTHONPATH="{REPO_ROOT}:$PYTHONPATH" "{py}" -m {module} '
        f'{HOOK_TAG}'
    )


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        raise SystemExit(f"error: {path} is not valid JSON — refusing to clobber")


def _save_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".clcm.bak"))
    path.write_text(json.dumps(data, indent=2) + "\n")


def _ensure_hook_entry(settings: Dict[str, Any], event: str, module: str) -> bool:
    """Add a hook entry for `event` if one with our sentinel isn't present.

    Returns True if the settings dict was modified.
    """
    hooks = settings.setdefault("hooks", {})
    event_entries: List[Dict[str, Any]] = hooks.setdefault(event, [])

    new_cmd = _hook_command(module)

    for entry in event_entries:
        for h in entry.get("hooks", []):
            cmd = h.get("command", "")
            if HOOK_TAG in cmd and module in cmd:
                # already installed — refresh the command in case the
                # interpreter path changed
                if cmd != new_cmd:
                    h["command"] = new_cmd
                    return True
                return False

    event_entries.append({
        "matcher": "",
        "hooks": [
            {
                "type": "command",
                "command": new_cmd,
                "timeout": 10,
            }
        ],
    })
    return True


def _remove_hook_entries(settings: Dict[str, Any]) -> int:
    """Remove every entry containing our sentinel. Returns count removed."""
    hooks = settings.get("hooks", {})
    removed = 0
    for event in list(hooks.keys()):
        entries = hooks[event]
        kept: List[Dict[str, Any]] = []
        for entry in entries:
            sub = entry.get("hooks", [])
            sub_kept = [h for h in sub if HOOK_TAG not in h.get("command", "")]
            if sub_kept:
                entry["hooks"] = sub_kept
                kept.append(entry)
            else:
                removed += 1
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    return removed


def _ensure_mcp_entry(mcp_settings: Dict[str, Any]) -> bool:
    """Register the MCP server in ~/.claude.json if not already present."""
    mcp_servers = mcp_settings.setdefault("mcpServers", {})
    py = _python_exe()
    command = py
    args = ["-m", "adapter.mcp_server"]
    env = {"PYTHONPATH": str(REPO_ROOT)}
    entry = {
        "command": command,
        "args": args,
        "env": env,
    }
    existing = mcp_servers.get(MCP_TAG)
    if existing == entry:
        return False
    mcp_servers[MCP_TAG] = entry
    return True


def _remove_mcp_entry(mcp_settings: Dict[str, Any]) -> bool:
    mcp_servers = mcp_settings.get("mcpServers", {})
    if MCP_TAG in mcp_servers:
        del mcp_servers[MCP_TAG]
        return True
    return False


def install(dry_run: bool = False) -> None:
    VAULT_DEFAULT.mkdir(parents=True, exist_ok=True)

    settings = _load_json(SETTINGS_PATH)
    changed_hooks = False
    for event, module in HOOKS:
        if _ensure_hook_entry(settings, event, module):
            changed_hooks = True

    mcp_settings = _load_json(MCP_SETTINGS_PATH)
    changed_mcp = _ensure_mcp_entry(mcp_settings)

    print(f"repo_root: {REPO_ROOT}")
    print(f"python:    {_python_exe()}")
    print(f"vault dir: {VAULT_DEFAULT}")
    print(f"hooks changed: {changed_hooks}")
    print(f"mcp changed:   {changed_mcp}")

    if dry_run:
        print("(dry-run: not writing files)")
        return

    if changed_hooks:
        _save_json(SETTINGS_PATH, settings)
        print(f"wrote {SETTINGS_PATH}")
    if changed_mcp:
        _save_json(MCP_SETTINGS_PATH, mcp_settings)
        print(f"wrote {MCP_SETTINGS_PATH}")


def uninstall(dry_run: bool = False) -> None:
    settings = _load_json(SETTINGS_PATH)
    removed_hooks = _remove_hook_entries(settings)

    mcp_settings = _load_json(MCP_SETTINGS_PATH)
    removed_mcp = _remove_mcp_entry(mcp_settings)

    print(f"hook entries removed: {removed_hooks}")
    print(f"mcp entry removed:    {removed_mcp}")

    if dry_run:
        print("(dry-run: not writing files)")
        return

    if removed_hooks:
        _save_json(SETTINGS_PATH, settings)
        print(f"wrote {SETTINGS_PATH}")
    if removed_mcp:
        _save_json(MCP_SETTINGS_PATH, mcp_settings)
        print(f"wrote {MCP_SETTINGS_PATH}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--uninstall", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    if args.uninstall:
        uninstall(dry_run=args.dry_run)
    else:
        install(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
