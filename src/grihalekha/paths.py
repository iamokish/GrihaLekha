"""Portable user-data locations; installation files never contain live accounts."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def default_data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "GrihaLekha"
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "grihalekha"


def asset_dir() -> Path:
    return Path(__file__).resolve().parent / "assets"


def application_icon_path() -> Path:
    """Resolve the bundled icon, with a fallback for a source checkout."""
    filename = "grihalekha.ico" if sys.platform == "win32" else "grihalekha-icon-512.png"
    packaged = asset_dir() / "icons" / filename
    if packaged.is_file():
        return packaged
    source = Path(__file__).resolve().parents[2] / "icons" / filename
    return source if source.is_file() else packaged

