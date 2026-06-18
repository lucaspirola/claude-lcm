# tests/test_workspace.py
"""Tests for sanitize_path parity with CC's sessionStoragePortable.sanitizePath."""

from __future__ import annotations

import os
import re

from pathlib import Path

from claude_lcm.workspace import MAX_SANITIZED_LENGTH, sanitize_path

# sanitize_path absolutizes its input before collapsing non-alnum chars, so
# its output is platform-dependent: a POSIX-style input like "/home/lucas"
# stays as-is on POSIX but is resolved against the current drive on Windows
# (e.g. "C:\\home\\lucas" -> "C--home-lucas"). Tests that assert exact
# byte-for-byte vectors are therefore guarded to POSIX; cross-platform tests
# derive their expectation from the same absolutization the implementation
# uses, so they verify behavior (collapse, expand, truncate+hash) on any OS.
POSIX = os.name == "posix"

_NON_ALNUM_RE = re.compile(r"[^a-zA-Z0-9]")


def _expected(name: str) -> str:
    """The platform-correct sanitized form for ``name`` (no truncation)."""
    abs_path = os.path.abspath(os.path.expanduser(name))
    return _NON_ALNUM_RE.sub("-", abs_path)


def test_sanitize_path_simple_absolute():
    # Cross-platform: matches the collapsed, absolutized path.
    assert sanitize_path("/home/lucas/ai/claude-lcm") == _expected("/home/lucas/ai/claude-lcm")
    if POSIX:
        assert sanitize_path("/home/lucas/ai/claude-lcm") == "-home-lucas-ai-claude-lcm"


def test_sanitize_path_collapses_all_non_alnum():
    # Matches CC: /[^a-zA-Z0-9]/g -> '-'. Dots, slashes, underscores, colons all collapse.
    out = sanitize_path("/tmp/a_b.c:d")
    assert _NON_ALNUM_RE.sub("", out) == out.replace("-", "")  # only [a-zA-Z0-9-]
    assert out == _expected("/tmp/a_b.c:d")
    if POSIX:
        assert out == "-tmp-a-b-c-d"


def test_sanitize_path_resolves_relative(tmp_path: Path):
    # Relative paths are absolutized against cwd, not left raw — so "./foo"
    # and its absolute form must agree (the documented invariant).
    out = sanitize_path("./foo")
    assert "foo" in out
    assert out == sanitize_path(os.path.abspath("foo"))


def test_sanitize_path_expands_home():
    out = sanitize_path("~/ai/claude-lcm")
    assert "claude-lcm" in out
    assert "~" not in out


def test_sanitize_path_long_path_truncates_with_hash():
    long_dir = "/a" + ("/very-long-segment" * 30)  # > 200 alnum chars
    out = sanitize_path(long_dir)
    assert len(out) > MAX_SANITIZED_LENGTH  # has hash suffix
    # Compare against the absolutized expectation so the leading drive prefix
    # (Windows) or leading slash (POSIX) is accounted for either way.
    plain = _expected(long_dir)
    assert out.startswith(plain[:MAX_SANITIZED_LENGTH] + "-")


def test_sanitize_path_parity_with_live_claude_projects_dir():
    """Verify our sanitize_path output matches the actual ~/.claude/projects/ directory name.

    Directly compares sanitize_path(known_path) against the on-disk directory name
    that CC created. Skips if the known path's projects directory doesn't exist.
    """
    import pytest

    known_path = "/home/lucas/ai/claude-lcm"
    expected_dir = Path.home() / ".claude" / "projects" / "-home-lucas-ai-claude-lcm"
    if not expected_dir.is_dir():
        pytest.skip(f"~/.claude/projects/-home-lucas-ai-claude-lcm does not exist on this machine")
    assert sanitize_path(known_path) == "-home-lucas-ai-claude-lcm"
