"""Workspace fingerprinting.

A workspace fingerprint is a stable identifier derived from a directory,
used to group sessions that operate on the same codebase regardless of
which agent produced them. Strategy: sha256 of the git remote origin URL
when available, falling back to sha256 of the absolute cwd. This lets
multiple clones of the same repo on disk share a fingerprint if they
share a remote.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path


def _git_remote(cwd: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=1.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    url = result.stdout.strip()
    return url or None


def fingerprint(cwd: str | os.PathLike | None = None) -> tuple[str, str]:
    """Return (fingerprint, workspace_path) for the given directory.

    fingerprint is a hex sha256 string. workspace_path is the absolute
    cwd used for the derivation.
    """
    path = Path(cwd or os.getcwd()).resolve()
    remote = _git_remote(path)
    if remote:
        digest = hashlib.sha256(remote.encode("utf-8")).hexdigest()
    else:
        digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()
    return digest, str(path)
