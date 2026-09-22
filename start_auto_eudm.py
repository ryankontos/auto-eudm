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
from typing import Callable, TextIO
import urllib.error
import urllib.request
import webbrowser


ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
VENV = Path(os.environ.get("EUDM_VENV_DIR", str(ROOT / ".venv"))).expanduser()
REQUIREMENTS = ROOT / "requirements"
SERVICE_LOG = ROOT / "results" / "auto-eudm-service.log"


def service_control_module(port: int) -> tuple[Path, Callable[[Path | None], str]]:
    sys.path.insert(0, str(SRC))
    from auto_eudm.local_service import consume_control_action, control_file_for_port

    return control_file_for_port(port), consume_control_action


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


def stop_server_process(port: int, pid: object = None) -> bool:
    """Stop a stale local server, including servers predating /api/runtime."""
    try:
        process_id = int(pid) if pid is not None else None
    except (TypeError, ValueError):
        process_id = None
    if process_id and process_id != os.getpid():
        try:
            if os.name == "nt":
                completed = subprocess.run(
                    ["taskkill", "/PID", str(process_id), "/T", "/F"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if completed.returncode == 0:
                    return True
            else:
                os.kill(process_id, signal.SIGTERM)
                return True
        except (OSError, ValueError):
            pass
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return False
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) >= 5 and fields[1].rsplit(":", 1)[-1] == str(port) and fields[3] == "LISTENING":
                completed = subprocess.run(
                    ["taskkill", "/PID", fields[4], "/T", "/F"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if completed.returncode == 0:
                    return True
    else:
        try:
            result = subprocess.run(
                ["lsof", "-tiTCP:" + str(port), "-sTCP:LISTEN"],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return False
        stopped = False
        for value in result.stdout.split():
            try:
                process_id = int(value)
                if process_id != os.getpid():
                    os.kill(process_id, signal.SIGTERM)
                    stopped = True
            except (OSError, ValueError):
                continue
        return stopped
    return False


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
    expected_background = "--foreground" not in arguments
    if current and running == current and runtime and runtime.get("background") is expected_background:
        if open_ui:
            webbrowser.open(url)
        say(f"The web workspace is already running; opening {url}" if open_ui else "The web workspace is already running.")
        return True
    if current and running == current:
        target_mode = "background service" if expected_background else "foreground server"
        say(f"Switching the web workspace to its {target_mode}…")
    else:
        say("The running web workspace is from an older commit; restarting it…")
    shutdown = request_json(f"{url.rstrip('/')}/api/shutdown", method="POST")
    if shutdown is None and not stop_server_process(port, runtime.get("pid") if runtime else None):
        raise ValueError("Could not stop the older AutoEUDM server. Close its launcher window, then try again.")
    if not wait_for_web_server_stop(url):
        stopped = stop_server_process(port, runtime.get("pid") if runtime else None)
        if not stopped or not wait_for_web_server_stop(url):
            raise ValueError("Could not stop the older AutoEUDM server. Close its launcher window, then try again.")
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


def service_arguments(arguments: list[str]) -> tuple[bool, bool, list[str]]:
    service = "--service" in arguments
    foreground = "--foreground" in arguments
    forwarded = [value for value in arguments if value not in {"--service", "--foreground"}]
    return service, foreground, forwarded


def prepare_service_environment() -> tuple[Path, dict[str, str]]:
    python = ensure_environment()
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return python, env


def _service_log() -> TextIO:
    SERVICE_LOG.parent.mkdir(parents=True, exist_ok=True)
    if SERVICE_LOG.exists() and SERVICE_LOG.stat().st_size > 2_000_000:
        previous = SERVICE_LOG.with_suffix(".log.1")
        try:
            previous.unlink(missing_ok=True)
            SERVICE_LOG.replace(previous)
        except OSError:
            pass
    return SERVICE_LOG.open("a", encoding="utf-8")


def supervise_service(arguments: list[str]) -> int:
    host, port = web_target(arguments) or ("127.0.0.1", 8765)
    control_file, consume_control_action = service_control_module(port)
    control_file.parent.mkdir(parents=True, exist_ok=True)
    control_file.unlink(missing_ok=True)
    url = f"http://{host}:{port}/"
    if web_ui_is_running(url):
        say("The local web workspace is already running.")
        return 0

    while True:
        python, env = prepare_service_environment()
        env["AUTO_EUDM_SERVICE_CONTROL"] = str(control_file)
        command = [
            str(python), "-m", "auto_eudm.eudm_web",
            *[value for value in arguments if value != "--no-open"],
            "--no-open",
        ]
        say("Starting the background web service…")
        with _service_log() as log:
            process = subprocess.Popen(
                command,
                cwd=ROOT,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        restart = False
        should_quit = False
        while True:
            action = consume_control_action(control_file)
            if action:
                if action == "quit":
                    should_quit = True
                elif action == "restart":
                    restart = True
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                break
            return_code = process.poll()
            if return_code is not None:
                if return_code == 0:
                    return 0
                if web_ui_is_running(url):
                    say("Another AutoEUDM service already owns this local port.")
                    return 0
                say(f"The web process stopped with exit code {return_code}; restarting it…")
                restart = True
                break
            time.sleep(0.25)
        if should_quit:
            say("AutoEUDM has stopped.")
            return 0
        if restart:
            time.sleep(0.2)


def start_background_service(arguments: list[str], python: Path, env: dict[str, str]) -> int:
    target = web_target(arguments)
    if target is None:
        return fail("The local web server settings were invalid.")
    host, port = target
    url = f"http://{host}:{port}/"
    command = [str(python), str(ROOT / "start_auto_eudm.py"), "--service", *arguments]
    command = [value for value in command if value != "--foreground"]
    log = _service_log()
    try:
        creation_flags = 0
        popen_options: dict[str, object] = {}
        if os.name == "nt":
            creation_flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        else:
            popen_options["start_new_session"] = True
        subprocess.Popen(
            command,
            cwd=ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=creation_flags,
            **popen_options,
        )
    finally:
        log.close()
    if "--no-open" in arguments:
        say("AutoEUDM is starting in the background.")
        return 0
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        if web_ui_is_running(url):
            webbrowser.open(url)
            say("AutoEUDM is running in the background.")
            return 0
        time.sleep(0.25)
    return fail("The background web service did not become ready. Check results/auto-eudm-service.log.")


def main() -> int:
    try:
        is_service, foreground, arguments = service_arguments(sys.argv[1:])
        copy_environment_file()
        if is_service:
            return supervise_service(arguments)
        if open_existing_web_ui(arguments):
            return 0
        if importlib.util.find_spec("venv") is None:
            return fail("Python was installed without the venv module. Reinstall Python 3 from python.org.")
        python, env = prepare_service_environment()
        if not foreground and "--help" not in arguments and "-h" not in arguments:
            return start_background_service(arguments, python, env)
        say("Opening the local request workspace…")
        completed = subprocess.run(
            [str(python), "-m", "auto_eudm.eudm_web", *arguments],
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
