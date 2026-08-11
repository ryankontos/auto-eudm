#!/usr/bin/env python3
"""Compatibility wrapper for the packaged inventory spreadsheet CLI."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from auto_eudm.eudm_inventory_import import cli


if __name__ == "__main__":
    cli()
