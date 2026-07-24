"""Opt-out runtime bootstrap for the command-line entry points."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys


def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def ensure_runtime(*, requirement_file: str, import_name: str) -> None:
    """Create/use the repository .venv and install an optional dependency set."""
    if os.getenv("DWP_SKIP_AUTO_INSTALL", "").casefold() in {"1", "true", "yes", "on"}:
        return
    if os.getenv("DWP_BOOTSTRAPPED") or any(arg in {"-h", "--help"} for arg in sys.argv[1:]):
        return
    if importlib.util.find_spec(import_name) is not None:
        return

    root = Path(__file__).resolve().parents[2]
    venv = Path(os.getenv("DWP_VENV_DIR", str(root / ".venv"))).expanduser()
    python = _venv_python(venv)
    requirements = root / "requirements" / requirement_file
    try:
        if not python.exists():
            print("Setting up the project environment (one time)...", file=sys.stderr)
            subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        print(f"Installing {import_name} (one time)...", file=sys.stderr)
        subprocess.run([str(python), "-m", "pip", "install", "-r", str(requirements)], check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(
            f"Could not prepare the local Python environment. Install {requirements} manually, "
            f"or set DWP_SKIP_AUTO_INSTALL=1. ({exc})"
        ) from exc

    env = os.environ.copy()
    env["DWP_BOOTSTRAPPED"] = "1"
    os.execvpe(str(python), [str(python), *sys.argv], env)
