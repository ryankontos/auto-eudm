"""Opt-out runtime bootstrap for the command-line entry points."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys


def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _restart_in(python: Path) -> None:
    env = os.environ.copy()
    # ``python -m package.module`` sets argv[0] to the module source path.
    # Preserve the module invocation so package-relative imports still work.
    main_spec = getattr(sys.modules.get("__main__"), "__spec__", None)
    if main_spec is not None and getattr(main_spec, "name", None):
        command = [str(python), "-m", main_spec.name, *sys.argv[1:]]
    else:
        command = [str(python), *sys.argv]
    os.execvpe(str(python), command, env)


def ensure_runtime(*, requirement_file: str, import_name: str) -> None:
    """Create/use the repository .venv and install an optional dependency set."""
    if os.getenv("EUDM_SKIP_AUTO_INSTALL", "").casefold() in {"1", "true", "yes", "on"}:
        return
    if any(arg in {"-h", "--help"} for arg in sys.argv[1:]):
        return
    if importlib.util.find_spec(import_name) is not None:
        return

    root = Path(__file__).resolve().parents[2]
    venv = Path(os.getenv("EUDM_VENV_DIR", str(root / ".venv"))).expanduser()
    python = _venv_python(venv)
    requirements = root / "requirements" / requirement_file
    try:
        if python.exists():
            available = subprocess.run(
                [str(python), "-c", f"import {import_name}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            ).returncode == 0
            if available:
                _restart_in(python)
        if not python.exists():
            print("Setting up the project environment (one time)...", file=sys.stderr)
            subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        print(f"Installing {import_name} (one time)...", file=sys.stderr)
        subprocess.run([str(python), "-m", "pip", "install", "-r", str(requirements)], check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(
            f"Could not prepare the local Python environment. Install {requirements} manually, "
            f"or set EUDM_SKIP_AUTO_INSTALL=1. ({exc})"
        ) from exc

    _restart_in(python)
