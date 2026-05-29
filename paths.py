"""Centralised path resolution that works in both source-tree dev mode AND
inside a py2app bundle (where __file__ lives in a read-only zip).

  RESOURCE_DIR — read-only static assets (dashboard.html, sounds/, hooks/).
                In dev: the source dir. In bundle: Contents/Resources/.
  USER_DATA_DIR — writable per-user state (logs, voice recordings, cached
                  generated sound assets). ~/Library/Application Support/Dispatch.
  LOG_DIR — ~/Library/Logs/Dispatch.
"""
from __future__ import annotations

import sys
from pathlib import Path


def _resource_dir() -> Path:
    # py2app sets sys.frozen = "macosx_app"
    if getattr(sys, "frozen", False):
        # sys.executable is .../Contents/MacOS/Dispatch
        return Path(sys.executable).resolve().parent.parent / "Resources"
    # source layout
    return Path(__file__).resolve().parent


RESOURCE_DIR: Path = _resource_dir()
USER_DATA_DIR: Path = Path.home() / "Library" / "Application Support" / "Dispatch"
LOG_DIR: Path = Path.home() / "Library" / "Logs" / "Dispatch"

# Ensure writable dirs exist on first import
for _d in (USER_DATA_DIR, LOG_DIR):
    try:
        _d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
