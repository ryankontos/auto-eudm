#!/usr/bin/env python3
"""One-command, cross-platform first-run launcher for the AutoEUDM web UI."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser


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


def package_available(python: Path, package: str) -> bool:
    return subprocess.run(
        [str(python), "-c", f"import {package}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0


def current_commit_id() -> str | None:
    """Read the checkout commit used to launch the server."""
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            check=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = result.stdout.strip()
    return commit or None


def web_target(arguments: list[str]) -> tuple[str, int] | None:
    """Read the web command's host/port without importing the web app."""
    if "--help" in arguments or "-h" in arguments:
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


def web_ui_is_running(url: str) -> bool:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "AutoEUDM launcher"})
        with urllib.request.urlopen(request, timeout=0.8) as response:
            body = response.read(4096).decode("utf-8", errors="ignore")
        return response.status < 400 and "AutoEUDM" in body
    except (OSError, urllib.error.URLError):
        return False


def request_json(url: str, *, method: str = "GET") -> dict[str, object] | None:
    try:
        request = urllib.request.Request(
            url,
            method=method,
            headers={"User-Agent": "AutoEUDM launcher"},
        )
        with urllib.request.urlopen(request, timeout=0.8) as response:
            if response.status >= 400:
                return None
            payload = json.loads(response.read(4096).decode("utf-8", errors="ignore"))
        return payload if isinstance(payload, dict) else None
    except (OSError, urllib.error.URLError, ValueError):
        return None


def wait_for_web_server_stop(url: str) -> bool:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not web_ui_is_running(url):
            return True
        time.sleep(0.1)
    return False


def stop_server_process(port: int, pid: object = None) -> None:
    """Stop a stale local server, including servers predating /api/runtime."""
    try:
        process_id = int(pid) if pid is not None else None
    except (TypeError, ValueError):
        process_id = None
    if process_id and process_id != os.getpid():
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process_id), "/T", "/F"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                os.kill(process_id, signal.SIGTERM)
            return
        except (OSError, ValueError):
            pass
    if os.name == "nt":
        result = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            check=False,
        )
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) >= 5 and fields[1].rsplit(":", 1)[-1] == str(port) and fields[3] == "LISTENING":
                subprocess.run(
                    ["taskkill", "/PID", fields[4], "/T", "/F"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
    else:
        result = subprocess.run(
            ["lsof", "-tiTCP:" + str(port), "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            check=False,
        )
        for value in result.stdout.split():
            try:
                process_id = int(value)
                if process_id != os.getpid():
                    os.kill(process_id, signal.SIGTERM)
            except (OSError, ValueError):
                continue


def open_existing_web_ui(arguments: list[str]) -> bool:
    """Open a matching server or replace one launched from an older commit."""
    target = web_target(arguments)
    if target is None:
        return False
    host, port = target
    url = f"http://{host}:{port}/"
    if not web_ui_is_running(url):
        return False
    current = current_commit_id()
    runtime = request_json(f"{url.rstrip('/')}/api/runtime")
    running = runtime.get("commit_id") if runtime else None
    open_ui = "--no-open" not in arguments
    if current is None:
        if open_ui:
            webbrowser.open(url)
        say(f"The web workspace is already running; opening {url}" if open_ui else "The web workspace is already running.")
        return True
    if current and running == current:
        if open_ui:
            webbrowser.open(url)
        say(f"The web workspace is already running; opening {url}" if open_ui else "The web workspace is already running.")
        return True
    say("The running web workspace is from an older commit; restarting it…")
    shutdown = request_json(f"{url.rstrip('/')}/api/shutdown", method="POST")
    if shutdown is None:
        stop_server_process(port, runtime.get("pid") if runtime else None)
    if not wait_for_web_server_stop(url):
        stop_server_process(port, runtime.get("pid") if runtime else None)
        wait_for_web_server_stop(url)
    return False


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
