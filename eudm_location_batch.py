#!/usr/bin/env python3
"""Compatibility wrapper for guided batch location deployments."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR / "src"))
from auto_eudm.eudm_location_batch import main

if __name__ == "__main__":
    raise SystemExit(main())
