#!/usr/bin/env python3
"""Local AutoEUDM web interface and request queue server."""

from __future__ import annotations

import argparse
import ipaddress
import os
import re
import socket
import sys
import threading
import webbrowser
from pathlib import Path

from .bootstrap import ensure_runtime

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the Deployments request workspace.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""The default is loopback-only. A private LAN bind also requires an explicit
allowed-subnet setting; use this only for a trusted local network.

Examples:
  python3 eudm_web.py
  python3 eudm_web.py --port 8787
  EUDM_SIMULATE=true python3 eudm_web.py
""",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind to localhost or an RFC1918 private IPv4 address.",
    )
    parser.add_argument(
        "--port", type=int, default=8765, help="Local port (default: 8765)."
    )
    parser.add_argument(
        "--instance-id",
        default=os.environ.get("AUTO_EUDM_INSTANCE_ID", "default"),
        help="Independent local data instance (default: default).",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Start the server without opening the web interface.",
    )
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", args.instance_id):
        raise ValueError("--instance-id must contain 1–48 letters, numbers, hyphens, or underscores")
    os.environ["AUTO_EUDM_INSTANCE_ID"] = args.instance_id

    # Import path-dependent modules only after instance selection, so every
    # store and logger resolves to that instance's own data directory.
    from .eudm_config import AppConfig
    from . import eudm_request as eudm
    from . import run_reporting
    from .web_runtime import Application, open_existing_server
    from .web_server import AutoEUDMServer

    if args.port < 1024 or args.port > 65535:
        raise eudm.EUDMError("--port must be between 1024 and 65535.")
    if args.host not in {"localhost", "127.0.0.1"}:
        try:
            address = ipaddress.ip_address(args.host)
            allowed_network = ipaddress.ip_network(
                os.environ.get("AUTO_EUDM_ALLOWED_NETWORK", ""), strict=False
            )
        except ValueError as exc:
            raise eudm.EUDMError(
                "--host must be localhost or an address in AUTO_EUDM_ALLOWED_NETWORK."
            ) from exc
        private_ranges = (
            ipaddress.ip_network("10.0.0.0/8"),
            ipaddress.ip_network("172.16.0.0/12"),
            ipaddress.ip_network("192.168.0.0/16"),
        )
        if (
            address.version != 4
            or not any(address in network for network in private_ranges)
            or address not in allowed_network
        ):
            raise eudm.EUDMError(
                "LAN binding is limited to an RFC1918 address inside the explicit allowed subnet."
            )

    ensure_runtime(
        requirement_file="requirements-sheet.txt", import_name="openpyxl"
    )
    try:
        config = AppConfig.load()
    except ValueError as exc:
        raise eudm.EUDMError(
            f"Could not load shared configuration: {exc}"
        ) from exc
    if not config.simulate or config.spreadsheet_import_enabled:
        ensure_runtime(
            requirement_file="requirements-browser.txt",
            import_name="playwright",
        )

    run_reporting.configure_logging(
        enabled=config.logging, command="eudm-web"
    )
    app = Application(config)
    url = f"http://{args.host}:{args.port}/"
    try:
        server = AutoEUDMServer((args.host, args.port), app)
    except OSError as exc:
        if exc.errno in {48, 98}:
            if not args.no_open and open_existing_server(url, args.instance_id):
                return 0
            raise eudm.EUDMError(
                f"Port {args.port} is already in use. The web UI may already be open, "
                "or choose another port with --port."
            ) from exc
        raise
    app.start_auth_monitor()
    print(f"Deployments is ready at {url}", flush=True)
    if os.environ.get("AUTO_EUDM_SERVICE_CONTROL"):
        print("Deployments is running under its background service manager.", flush=True)
    else:
        print("Keep this window open while using the web interface. Press Control-C to stop.", flush=True)
    if not args.no_open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nDeployments stopped.")
    finally:
        app.flush_pending_state()
        server.server_close()
    if server.restart_requested:
        os.execv(
            sys.executable,
            [sys.executable, str(ROOT / "eudm_web.py"), *sys.argv[1:]],
        )
    return 0


def cli() -> None:
    """Run the web server with stable, user-facing startup errors."""
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nDeployments stopped.")
        raise SystemExit(130)
    except (socket.error, OSError) as exc:
        print(f"Error: Could not start the local web server: {exc}")
        raise SystemExit(2)
    except Exception as exc:
        from .eudm_request import EUDMError

        if isinstance(exc, EUDMError):
            print(f"Error: {exc}")
            raise SystemExit(2)
        raise


if __name__ == "__main__":
    cli()
