"""Configuration for claude-lcm.

v1 is a lossless transcript vault with no compaction, so this config is
small. Compaction tunables (leaf_chunk_tokens, context_threshold, etc.)
are deliberately absent — they return as v2 adds the compaction layer.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _xdg_data_home() -> Path:
    env = os.environ.get("XDG_DATA_HOME")
    if env:
        return Path(env)
    return Path.home() / ".local" / "share"


def default_vault_path() -> Path:
    env = os.environ.get("LCM_VAULT_PATH")
    if env:
        return Path(env).expanduser()
    return _xdg_data_home() / "claude-lcm" / "vault.sqlite"


@dataclass
class ClaudeLcmConfig:
    vault_path: Path
    max_snapshot_bytes: int = 2 * 1024 * 1024  # 2 MiB per file snapshot blob

    @classmethod
    def from_env(cls) -> "ClaudeLcmConfig":
        return cls(
            vault_path=default_vault_path(),
            max_snapshot_bytes=int(
                os.environ.get("LCM_MAX_SNAPSHOT_BYTES", 2 * 1024 * 1024)
            ),
        )
