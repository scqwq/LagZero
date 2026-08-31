from __future__ import annotations

import os
import sys
from pathlib import Path


APP_NAME = "LagZero"


def _resolve_data_dir() -> Path:
    if getattr(sys, "frozen", False):
        local_app_data = os.environ.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
        return base / APP_NAME / "data"
    return Path(__file__).resolve().parent.parent / "data"


DATA_DIR = _resolve_data_dir()
