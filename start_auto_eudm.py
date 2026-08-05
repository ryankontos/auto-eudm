#!/usr/bin/env python3
"""One-command, cross-platform first-run launcher for the AutoEUDM web UI."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
VENV = Path(os.environ.get("EUDM_VENV_DIR", str(ROOT / ".venv"))).expanduser()
REQUIREMENTS = ROOT / "requirements"


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def say(message: str) -> None:
    print(f"AutoEUDM  ·  {message}", flush=True)


def fail(message: str) -> int:
    print(f"\nAutoEUDM could not start: {message}", file=sys.stderr)
    return 1


def copy_environment_file() -> None:
    target = ROOT / ".env"
    template = ROOT / ".env.example"
    if target.exists() or not template.exists():
        return
    shutil.copyfile(template, target)
    say("Created .env from the safe simulation template.")
    say("Edit .env before making a real EUDM connection.")


def configured_simulation() -> bool:
    sys.path.insert(0, str(SRC))
    from auto_eudm.eudm_config import AppConfig  # noqa: WPS433

    return AppConfig.load().simulate


def open_web_url(url: str) -> None:
    """Open the launcher URL using the saved web preference when available."""
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    from auto_eudm.browser_launch import open_url, preference_enabled  # noqa: WPS433
    from auto_eudm.eudm_config import AppConfig  # noqa: WPS433

    config = None
    try:
        config = AppConfig.load()
        profile = config.browser_profile
    except (OSError, ValueError):
        profile = None
    open_url(
        url,
        profile=profile,
        use_profile=preference_enabled(),
        debug_port=config.browser_debug_port if config else 9222,
    )


def package_available(python: Path, package: str) -> bool:
    return subprocess.run(
        [str(python), "-c", f"import {package}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0


def web_target(arguments: list[str]) -> tuple[str, int] | None:
    """Read the web command's host/port without importing the web app."""
    if "--help" in arguments or "-h" in arguments or "--no-open" in arguments:
        return None
    host = "127.0.0.1"
    port = 8765
    for index, argument in enumerate(arguments):
        if argument == "--host" and index + 1 < len(arguments):
            host = arguments[index + 1]
        elif argument.startswith("--host="):
            host = argument.split("=", 1)[1]
        elif argument == "--port" and index + 1 < len(arguments):
            try:
                port = int(arguments[index + 1])
            except ValueError:
                return None
        elif argument.startswith("--port="):
            try:
                port = int(argument.split("=", 1)[1])
            except ValueError:
                return None
    if host == "localhost":
        host = "127.0.0.1"
    if not 1024 <= port <= 65535:
        return None
    return host, port


def open_existing_web_ui(arguments: list[str]) -> bool:
    """Open an already-running AutoEUDM server instead of starting a duplicate."""
    target = web_target(arguments)
    if target is None:
        return False
    host, port = target
    url = f"http://{host}:{port}/"
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "AutoEUDM launcher"})
        with urllib.request.urlopen(request, timeout=0.8) as response:
            body = response.read(4096).decode("utf-8", errors="ignore")
        if response.status >= 400 or "AutoEUDM" not in body:
            return False
    except (OSError, urllib.error.URLError):
        return False
    open_web_url(url)
    say(f"The web workspace is already running; opening {url}")
    return True


def ensure_environment() -> Path:
    python = venv_python()
    if not python.exists():
        say("Creating the project environment (first run only)…")
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)

    requirements = [("openpyxl", REQUIREMENTS / "requirements-sheet.txt")]
    if not configured_simulation():
        requirements.append(("playwright", REQUIREMENTS / "requirements-browser.txt"))

    for package, requirement_file in requirements:
        if package_available(python, package):
            continue
        say(f"Installing {package} (first run only)…")
        subprocess.run(
            [str(python), "-m", "pip", "install", "-r", str(requirement_file)],
            check=True,
        )
    return python


def main() -> int:
    try:
        copy_environment_file()
        if open_existing_web_ui(sys.argv[1:]):
            return 0
        if importlib.util.find_spec("venv") is None:
            return fail("Python was installed without the venv module. Reinstall Python 3 from python.org.")
        python = ensure_environment()
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SRC) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        say("Opening the local request workspace…")
        completed = subprocess.run(
            [str(python), "-m", "auto_eudm.eudm_web", *sys.argv[1:]],
            cwd=ROOT,
            env=env,
            check=False,
        )
        return completed.returncode
    except FileNotFoundError as exc:
        return fail(f"Python 3 could not be found. Install Python 3.10 or newer, then try again. ({exc})")
    except subprocess.CalledProcessError as exc:
        return fail(f"A setup command failed with exit code {exc.returncode}.")
    except KeyboardInterrupt:
        return 130
    except ValueError as exc:
        return fail(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
