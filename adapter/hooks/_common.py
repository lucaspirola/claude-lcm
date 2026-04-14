"""Shared utilities for hook stubs.

Hooks are short-lived CLI processes: read JSON from stdin, open the vault,
append one or two rows, exit. They must NEVER raise to Claude Code — a
broken hook is a logged warning, not a blocked tool call.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator

from claude_lcm.config import ClaudeLcmConfig
from claude_lcm.engine import ClaudeLcmEngine

logger = logging.getLogger("claude_lcm.hook")


def _log_path() -> Path:
    env = os.environ.get("LCM_HOOK_LOG")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".local" / "share" / "claude-lcm" / "hook.log"


def _log(msg: str) -> None:
    try:
        p = _log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def read_payload() -> Dict[str, Any]:
    """Read the JSON payload from stdin. Returns {} on failure."""
    try:
        raw = sys.stdin.read()
        if not raw:
            return {}
        return json.loads(raw)
    except Exception as e:
        _log(f"read_payload failed: {e}")
        return {}


def write_response(data: Dict[str, Any] | None = None) -> None:
    """Write a JSON response to stdout for hooks that need to respond."""
    if data is None:
        return
    try:
        json.dump(data, sys.stdout)
        sys.stdout.write("\n")
        sys.stdout.flush()
    except Exception as e:
        _log(f"write_response failed: {e}")


@contextmanager
def engine_for(session_id: str | None = None) -> Iterator[ClaudeLcmEngine]:
    """Open an engine, yield it, close it. Swallows all exceptions."""
    eng: ClaudeLcmEngine | None = None
    try:
        eng = ClaudeLcmEngine(
            config=ClaudeLcmConfig.from_env(),
            session_id=session_id,
        )
        yield eng
    except Exception:
        _log("engine open/use failed:\n" + traceback.format_exc())
        # Yield a no-op stand-in if we never managed to create the engine.
        if eng is None:
            class _NoOp:
                def __getattr__(self, _name):
                    def _noop(*_a, **_kw):
                        return None
                    return _noop
            yield _NoOp()  # type: ignore[misc]
    finally:
        if eng is not None:
            try:
                eng.close()
            except Exception:
                pass


def safe_main(handler) -> None:
    """Run a hook handler, swallowing all errors so Claude Code is never blocked."""
    try:
        payload = read_payload()
        handler(payload)
    except SystemExit:
        raise
    except Exception:
        _log("hook crashed:\n" + traceback.format_exc())
        # Emit a permissive response so Claude Code proceeds.
        try:
            write_response({"continue": True})
        except Exception:
            pass
    sys.exit(0)
