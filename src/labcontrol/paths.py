from __future__ import annotations

import sys
from pathlib import Path


def project_root() -> Path:
    """Return the source checkout or frozen application directory."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def default_config_path() -> Path:
    return project_root() / "configs" / "default.toml"
