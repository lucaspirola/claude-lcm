# tests/test_workspace.py
"""Tests for sanitize_path parity with CC's sessionStoragePortable.sanitizePath."""

from __future__ import annotations

from pathlib import Path

from claude_lcm.workspace import MAX_SANITIZED_LENGTH, sanitize_path


def test_sanitize_path_simple_absolute():
    assert sanitize_path("/home/lucas/ai/claude-lcm") == "-home-lucas-ai-claude-lcm"


def test_sanitize_path_collapses_all_non_alnum():
    # Matches CC: /[^a-zA-Z0-9]/g -> '-'. Dots, slashes, underscores, colons all collapse.
    assert sanitize_path("/tmp/a_b.c:d") == "-tmp-a-b-c-d"


def test_sanitize_path_resolves_relative(tmp_path: Path):
    # Relative paths are absolutized against cwd, not left raw.
    raw = "./foo"
    out = sanitize_path(raw)
    assert out.startswith("-")
    assert "foo" in out


def test_sanitize_path_expands_home():
    out = sanitize_path("~/ai/claude-lcm")
    assert "claude-lcm" in out
    assert "~" not in out


def test_sanitize_path_long_path_truncates_with_hash():
    long_dir = "/a" + ("/very-long-segment" * 30)  # > 200 alnum chars
    out = sanitize_path(long_dir)
    assert len(out) > MAX_SANITIZED_LENGTH  # has hash suffix
    import re
    plain = re.sub(r"[^a-zA-Z0-9]", "-", long_dir)
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
