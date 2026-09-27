#!/usr/bin/env python3
"""Run a private, simulator-only Deployments server under a macOS LaunchAgent."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import plistlib


ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "bin" / "python"
INSTANCE_ID = "lan-test"
PORT = 8766
LABEL = "com.ryankontos.deployments.lan-test"
AGENT_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG_DIR = Path.home() / "Library" / "Logs" / "Deployments"
LOG_PATH = LOG_DIR / "lan-test-server.log"
RESTART_DELAY_SECONDS = 1.5
WATCH_INTERVAL_SECONDS = 1.0

_STOP = threading.Event()


def say(message: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {message}"
    print(line, flush=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")


def private_ipv4(value: str) -> ipaddress.IPv4Address:
    address = ipaddress.ip_address(value)
    if address.version != 4 or not address.is_private or address.is_loopback:
        raise ValueError("The LAN test server requires an RFC1918 private IPv4 address.")
    if not any(
        address in network
        for network in (
            ipaddress.ip_network("10.0.0.0/8"),
            ipaddress.ip_network("172.16.0.0/12"),
            ipaddress.ip_network("192.168.0.0/16"),
        )
    ):
        raise ValueError("The LAN test server requires an RFC1918 private IPv4 address.")
    return address


def active_route_address() -> str:
    """Ask the OS routing table for the source address without sending traffic."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))
        return str(private_ipv4(probe.getsockname()[0]))
    finally:
        probe.close()


def watch_signature() -> tuple[tuple[str, int, int], ...]:
    paths: list[Path] = []
    for directory in (ROOT / "src", ROOT / "web", ROOT / "scripts", ROOT / "requirements"):
        if directory.is_dir():
            paths.extend(directory.rglob("*"))
    paths.extend(
        path for path in ROOT.glob("*.py") if path.is_file()
    )
    signature: list[tuple[str, int, int]] = []
    for path in paths:
        if not path.is_file() or path.is_symlink():
            continue
        if any(part in {"__pycache__", ".pytest_cache"} for part in path.parts):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        signature.append((str(path.relative_to(ROOT)), stat.st_mtime_ns, stat.st_size))
    return tuple(sorted(signature))


def stop_child(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        process.send_signal(signal.SIGINT)
        process.wait(timeout=12)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def server_environment(network: ipaddress.IPv4Network) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("EUDM_") and not key.startswith("AUTO_EUDM_")
    }
    environment.update(
        {
            "AUTO_EUDM_ALLOWED_NETWORK": str(network),
            "AUTO_EUDM_FILE_WATCHER": "1",
            "AUTO_EUDM_INSTANCE_ID": INSTANCE_ID,
            "EUDM_BASE": "http://127.0.0.1:9",
            "EUDM_ENABLE_SPREADSHEET_IMPORT": "true",
            "EUDM_ENV_FILE": "/dev/null",
            "EUDM_LOGGING": "true",
            "EUDM_SIMULATE": "true",
            "EUDM_SKIP_AUTO_INSTALL": "1",
            "PYTHONPATH": str(ROOT / "src"),
        }
    )
    return environment


def serve(network_text: str) -> int:
    network = ipaddress.ip_network(network_text, strict=False)
    if network.version != 4 or not any(
        network.subnet_of(private_network)
        for private_network in (
            ipaddress.ip_network("10.0.0.0/8"),
            ipaddress.ip_network("172.16.0.0/12"),
            ipaddress.ip_network("192.168.0.0/16"),
        )
    ):
        raise ValueError("The allowed subnet must be within a private RFC1918 IPv4 range.")
    if not PYTHON.is_file():
        raise FileNotFoundError(f"Project Python environment is missing: {PYTHON}")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    baseline = watch_signature()
    child: subprocess.Popen[bytes] | None = None
    current_address = ""
    say(f"Supervisor ready; watching local code. Instance={INSTANCE_ID}; port={PORT}.")
    try:
        while not _STOP.is_set():
            try:
                address = active_route_address()
            except (OSError, ValueError) as exc:
                if child is not None:
                    say(f"LAN address unavailable; pausing server: {exc}")
                    stop_child(child)
                    child = None
                    current_address = ""
                _STOP.wait(5)
                continue

            if ipaddress.ip_address(address) not in network:
                if child is not None:
                    say(f"LAN address {address} is outside {network}; pausing server.")
                    stop_child(child)
                    child = None
                    current_address = ""
                _STOP.wait(5)
                continue

            changed = watch_signature() != baseline
            address_changed = bool(current_address and address != current_address)
            if child is not None and (changed or address_changed):
                reason = "source files changed" if changed else "LAN address changed"
                say(f"{reason}; restarting the simulator server.")
                stop_child(child)
                child = None
            if changed:
                time.sleep(0.5)
                baseline = watch_signature()

            if child is None:
                current_address = address
                command = [
                    str(PYTHON),
                    "-m",
                    "auto_eudm.eudm_web",
                    "--host",
                    address,
                    "--port",
                    str(PORT),
                    "--instance-id",
                    INSTANCE_ID,
                    "--no-open",
                ]
                log = LOG_PATH.open("ab")
                try:
                    child = subprocess.Popen(
                        command,
                        cwd=ROOT,
                        env=server_environment(network),
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                    )
                finally:
                    log.close()
                say(f"Started simulator at http://{address}:{PORT}/")
            elif child.poll() is not None:
                code = child.returncode
                say(f"Simulator stopped (exit {code}); retrying shortly.")
                child = None
                _STOP.wait(RESTART_DELAY_SECONDS)

            if _STOP.wait(WATCH_INTERVAL_SECONDS):
                break
    finally:
        stop_child(child)
        say("Supervisor stopped.")
    return 0


def launch_agent_payload(network: ipaddress.IPv4Network) -> dict[str, object]:
    return {
        "Label": LABEL,
        "ProgramArguments": [str(PYTHON), str(Path(__file__).resolve()), "serve", str(network)],
        "WorkingDirectory": str(ROOT),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "ProcessType": "Background",
        "EnvironmentVariables": {
            "AUTO_EUDM_ALLOWED_NETWORK": str(network),
            "AUTO_EUDM_INSTANCE_ID": INSTANCE_ID,
            "EUDM_BASE": "http://127.0.0.1:9",
            "EUDM_ENABLE_SPREADSHEET_IMPORT": "true",
            "EUDM_ENV_FILE": "/dev/null",
            "EUDM_LOGGING": "true",
            "EUDM_SIMULATE": "true",
            "EUDM_SKIP_AUTO_INSTALL": "1",
            "PYTHONPATH": str(ROOT / "src"),
        },
        "StandardOutPath": str(LOG_PATH),
        "StandardErrorPath": str(LOG_PATH),
    }


def install(network_text: str) -> None:
    network = ipaddress.ip_network(network_text, strict=False)
    current = ipaddress.ip_address(active_route_address())
    if current not in network:
        raise ValueError(f"This Mac's current address {current} is not in {network}.")
    AGENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    temporary = AGENT_PATH.with_suffix(".plist.tmp")
    temporary.write_bytes(plistlib.dumps(launch_agent_payload(network)))
    temporary.replace(AGENT_PATH)
    domain = f"gui/{os.getuid()}"
    subprocess.run(
        ["launchctl", "bootout", domain, str(AGENT_PATH)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    subprocess.run(["launchctl", "bootstrap", domain, str(AGENT_PATH)], check=True)
    subprocess.run(["launchctl", "kickstart", "-k", f"{domain}/{LABEL}"], check=True)
    say(f"Installed login service for {network}; the simulator data is isolated in instance '{INSTANCE_ID}'.")


def uninstall() -> None:
    domain = f"gui/{os.getuid()}"
    subprocess.run(
        ["launchctl", "bootout", domain, str(AGENT_PATH)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    AGENT_PATH.unlink(missing_ok=True)
    say("Removed the LAN test server login service. Its data and logs were kept.")


def status() -> int:
    try:
        address = active_route_address()
    except (OSError, ValueError):
        address = "unavailable"
    print(f"LaunchAgent: {'installed' if AGENT_PATH.is_file() else 'not installed'}")
    print(f"Current LAN address: {address}")
    print(f"URL: http://{address}:{PORT}/" if address != "unavailable" else "URL: unavailable")
    try:
        with urllib.request.urlopen(f"http://{address}:{PORT}/api/runtime", timeout=1) as response:
            runtime = json.loads(response.read(4096).decode("utf-8"))
        print(f"Server: running ({runtime.get('instance_id', 'unknown')})")
    except (OSError, urllib.error.URLError, ValueError):
        print("Server: not responding")
    print(f"Log: {LOG_PATH}")
    return 0


def _handle_stop(_signum: int, _frame: object) -> None:
    _STOP.set()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    install_parser = subparsers.add_parser("install", help="install and start the login service")
    install_parser.add_argument("--network", required=True, help="trusted RFC1918 subnet, e.g. 192.168.1.0/24")
    serve_parser = subparsers.add_parser("serve", help=argparse.SUPPRESS)
    serve_parser.add_argument("network")
    subparsers.add_parser("uninstall", help="stop and remove the login service")
    subparsers.add_parser("status", help="show server and login service status")
    args = parser.parse_args()
    if args.action == "install":
        install(args.network)
        return 0
    if args.action == "serve":
        signal.signal(signal.SIGTERM, _handle_stop)
        signal.signal(signal.SIGINT, _handle_stop)
        return serve(args.network)
    if args.action == "uninstall":
        uninstall()
        return 0
    return status()


if __name__ == "__main__":
    raise SystemExit(main())
