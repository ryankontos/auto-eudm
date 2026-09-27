"""Per-user, per-instance paths for Deployments runtime data."""

from __future__ import annotations

import os
from pathlib import Path
import re
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
INSTANCE_ID = os.environ.get("AUTO_EUDM_INSTANCE_ID", "default").strip() or "default"
if not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", INSTANCE_ID):
    raise ValueError("AUTO_EUDM_INSTANCE_ID must contain 1–48 letters, numbers, hyphens, or underscores")


def _application_support_root() -> Path:
    configured = os.environ.get("AUTO_EUDM_DATA_HOME", "").strip()
    if configured:
        return Path(configured).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Deployments"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "Deployments"
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "Deployments"


_EXPLICIT_INSTANCE_DIR = os.environ.get("AUTO_EUDM_DATA_DIR", "").strip()
_EXPLICIT_LEGACY_RESULTS_DIR = os.environ.get("AUTO_EUDM_LEGACY_DATA_DIR", "").strip()
INSTANCE_DIR = (
    Path(_EXPLICIT_INSTANCE_DIR).expanduser()
    if _EXPLICIT_INSTANCE_DIR
    else _application_support_root() / "instances" / INSTANCE_ID
)
DATABASE_PATH = INSTANCE_DIR / "deployments.sqlite3"
LOG_DIR = INSTANCE_DIR / "logs"
if _EXPLICIT_LEGACY_RESULTS_DIR:
    LEGACY_RESULTS_DIR = Path(_EXPLICIT_LEGACY_RESULTS_DIR).expanduser()
elif INSTANCE_ID == "default" and not _EXPLICIT_INSTANCE_DIR:
    LEGACY_RESULTS_DIR = PROJECT_ROOT / "results"
else:
    LEGACY_RESULTS_DIR = None

# Retain the name used by existing diagnostic writers; it now points outside
# the checkout unless an explicit test-instance directory was provided.
DATA_DIR = INSTANCE_DIR
