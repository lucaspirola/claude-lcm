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
    """Smoke-test against a real ~/.claude/projects/* directory.

    Picks any subdirectory of ~/.claude/projects and reconstructs the
    original path by replacing hyphens with slashes. Not strictly sound
    (ambiguous for paths containing hyphens), but for the common case of
    a path like /home/lucas/ai/claude-lcm it works as a parity probe.
    If ~/.claude/projects does not exist, the test skips.
    """
    import os
    import pytest

    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.is_dir():
        pytest.skip("no ~/.claude/projects on this machine")
    for child in projects_dir.iterdir():
        if not child.is_dir():
            continue
        name = child.name
        # Reconstruct a plausible absolute path from the sanitized name.
        candidate = "/" + name.lstrip("-").replace("-", "/")
        if os.path.isabs(candidate) and Path(candidate).exists():
            assert sanitize_path(candidate) == name
            return
    pytest.skip("no reconstructible projects dir found")
