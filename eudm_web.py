#!/usr/bin/env python3
"""Compatibility-free entry point for the AutoEUDM localhost web interface."""

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from auto_eudm.eudm_web import cli  # noqa: E402


if __name__ == "__main__":
    cli()
