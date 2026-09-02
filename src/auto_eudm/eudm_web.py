#!/usr/bin/env python3
"""Local AutoEUDM web interface and request queue server."""

from __future__ import annotations

import argparse
import socket
import threading
import webbrowser

from .bootstrap import ensure_runtime
from .eudm_config import AppConfig
from . import eudm_request as eudm
from . import run_reporting
from .web_runtime import Application, open_existing_server
from .web_server import AutoEUDMServer



def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the local AutoEUDM request workspace.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""The server binds to this computer only and does not expose EUDM cookies.

Examples:
  python3 eudm_web.py
  python3 eudm_web.py --port 8787
  EUDM_SIMULATE=true python3 eudm_web.py
""",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        choices=("127.0.0.1", "localhost"),
        help="Local bind address (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port", type=int, default=8765, help="Local port (default: 8765)."
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Start the server without opening the web interface.",
    )
    args = parser.parse_args()
    if args.port < 1024 or args.port > 65535:
        raise eudm.EUDMError("--port must be between 1024 and 65535.")

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
    url = f"http://127.0.0.1:{args.port}/"
    try:
        server = AutoEUDMServer((args.host, args.port), app)
    except OSError as exc:
        if exc.errno in {48, 98}:
            if not args.no_open and open_existing_server(url):
                return 0
            raise eudm.EUDMError(
                f"Port {args.port} is already in use. The web UI may already be open, "
                "or choose another port with --port."
            ) from exc
        raise
    print(f"AutoEUDM is ready at {url}", flush=True)
    print(
        "Keep this window open while using the web interface. Press Control-C to stop.",
        flush=True,
    )
    if not args.no_open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nAutoEUDM stopped.")
    finally:
        app.flush_pending_state()
        server.server_close()
    return 0


def cli() -> None:
    """Run the web server with stable, user-facing startup errors."""
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nAutoEUDM stopped.")
        raise SystemExit(130)
    except eudm.EUDMError as exc:
        print(f"Error: {exc}")
        raise SystemExit(2)
    except (socket.error, OSError) as exc:
        print(f"Error: Could not start the local web server: {exc}")
        raise SystemExit(2)


if __name__ == "__main__":
    cli()
