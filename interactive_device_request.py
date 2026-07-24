#!/usr/bin/env python3
"""Compatibility wrapper for the packaged interactive DWP frontend."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from dwp_device_request.interactive_device_request import main


if __name__ == "__main__":
    raise SystemExit(main())
