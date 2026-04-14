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


# Port of CC's sanitizePath for project_key identity.
import re as _re

MAX_SANITIZED_LENGTH = 200
_NON_ALNUM_RE = _re.compile(r"[^a-zA-Z0-9]")


def _djb2_hash(s: str) -> str:
    """Port of the Node-fallback simpleHash in CC's sessionStoragePortable.

    CC itself uses Bun.hash under the CLI; its SDK fallback uses djb2. We
    match the djb2 fallback so our output is reproducible on pure-Python
    installs. Paths under MAX_SANITIZED_LENGTH are unaffected and stay
    bit-perfect with CC.
    """
    h = 5381
    for ch in s:
        h = ((h * 33) + ord(ch)) & 0xFFFFFFFF
    return _int_to_base36(h)


def _int_to_base36(n: int) -> str:
    if n == 0:
        return "0"
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = []
    while n > 0:
        out.append(digits[n % 36])
        n //= 36
    return "".join(reversed(out))


def sanitize_path(name: str) -> str:
    """Python port of CC's `sanitizePath` (sessionStoragePortable.ts).

    Input is an arbitrary path; it is expanded (`~`) and absolutized
    before sanitizing so that `sanitize_path("./foo")` and
    `sanitize_path(os.path.abspath("./foo"))` agree. Non-alphanumeric
    characters are replaced with `-`. Over-long outputs are truncated
    and suffixed with a djb2-based hash, matching the CC Node fallback.
    """
    abs_path = os.path.abspath(os.path.expanduser(name))
    sanitized = _NON_ALNUM_RE.sub("-", abs_path)
    if len(sanitized) <= MAX_SANITIZED_LENGTH:
        return sanitized
    return f"{sanitized[:MAX_SANITIZED_LENGTH]}-{_djb2_hash(abs_path)}"
