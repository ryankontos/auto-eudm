#!/usr/bin/env python3
"""Compatibility-free entry point for the AutoEUDM localhost web interface."""

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from auto_eudm.eudm_web import main  # noqa: E402
from auto_eudm import eudm_request as eudm  # noqa: E402


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nAutoEUDM stopped.")
        raise SystemExit(130)
    except eudm.EUDMError as exc:
        print(f"Error: {exc}")
        raise SystemExit(2)
