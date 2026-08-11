"""Guided batch deployment of serial numbers to one location, with no user."""

from __future__ import annotations

import argparse
from collections import Counter
import os
from pathlib import Path
import subprocess
import sys

from . import eudm_request as eudm
from . import run_reporting
from .cli_common import (
    add_runtime_arguments,
    console,
    request_for,
    start_run,
    validate_runtime_args,
)
from .eudm_config import AppConfig
from .identifiers import is_serial


LOCATION_STATUSES = ("New Stock", "Used Stock", "Pending Pickup")


def parse_serials(raw: str) -> list[str]:
    serials = [value.strip() for value in raw.replace(",", "\n").splitlines() if value.strip()]
    if not serials:
        raise eudm.EUDMError("Enter at least one serial number.")
    if any(not is_serial(serial) for serial in serials):
        raise eudm.EUDMError(
            "Serial numbers must be at least 6 characters and contain only letters, numbers, "
            "periods, underscores, or hyphens."
        )
    serial_counts = Counter(serial.casefold() for serial in serials)
    duplicates = sorted(serial for serial, count in serial_counts.items() if count > 1)
    if duplicates:
        raise eudm.EUDMError("Duplicate serial numbers: " + ", ".join(duplicates))
    return serials


def gather_serials(initial: str | None) -> list[str]:
    if initial:
        return parse_serials(initial)
    _, mode = console.choose(
        "How would you like to enter serial numbers?",
        [
            ("Paste a comma- or newline-separated list", "paste"),
            ("Enter one serial at a time", "one-by-one"),
        ],
    )
    if mode == "paste":
        print("Paste serial numbers, then enter a blank line when finished:")
        lines: list[str] = []
        while True:
            line = input()
            if not line.strip():
                break
            lines.append(line)
        return parse_serials("\n".join(lines))
    print("Enter serial numbers. Press Enter on a blank line when finished:")
    entries: list[str] = []
    while True:
        value = input("Serial number: ").strip()
        if not value:
            break
        entries.append(value)
    return parse_serials("\n".join(entries))


def main() -> int:
    config = AppConfig.load()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serials", help="Comma- or newline-separated serial numbers.")
    parser.add_argument("--dry-run", action="store_true", help="Preview only.")
    parser.add_argument("--request-for", default=config.request_for)
    add_runtime_arguments(parser, config)
    args = parser.parse_args()
    validate_runtime_args(args)
    start_run(args, "eudm-location-batch")
    default = LOCATION_STATUSES.index(config.default_location_status) + 1 if config.default_location_status in LOCATION_STATUSES else 1
    print("\nLocation deployment type for every serial")
    for index, status in enumerate(LOCATION_STATUSES, 1):
        print(f"  {index}. {status}")
    print("  4. Enter another exact EUDM status")
    while True:
        choice = input(f"Choose 1-4 [{default}]: ").strip() or str(default)
        if choice in {"1", "2", "3"}:
            status = LOCATION_STATUSES[int(choice) - 1]
            break
        if choice == "4":
            status = console.text("Exact EUDM location status")
            break
        print("Enter a number from 1 to 4.")
    serials = gather_serials(args.serials)
    location = " → ".join(value for value in (config.city, config.building, config.floor, config.room, config.cabinet) if value)
    print("\nPreview")
    print(f"  Status: {status}")
    print(f"  Location: {location or 'will be requested by EUDM'}")
    print(f"  Serial numbers ({len(serials)}): {', '.join(serials)}")
    if args.dry_run:
        run_reporting.write_result_file("eudm-location-batch", [f"DRY RUN | status={status} | serials={','.join(serials)} | location={location}"])
        print("Dry run complete. No browser was opened and no EUDM requests were made.")
        return 0
    args.request_for = request_for(args, config)
    if not console.yes_no("Create and submit this one batch request?"):
        print("Cancelled before authentication. No EUDM requests were made.")
        return 0

    root = Path(__file__).resolve().parents[2]
    command = [
        sys.executable, str(root / "eudm_request.py"), "--batch",
        "--serials", ",".join(serials), "--target", "location", "--status", status,
        "--submit", "--request-for", args.request_for,
        "--base", args.base,
        "--simulate" if args.simulate else "--no-simulate",
        "--manual-review" if args.manual_review else "--no-manual-review",
        "--verbose" if args.verbose else "--no-verbose",
        "--logging" if args.logging else "--no-logging",
        "--headless" if args.headless else "--no-headless",
    ]
    if args.cookie_mode:
        command.append("--cookie-mode")
    elif args.browser_profile:
        command.extend(("--browser-profile", args.browser_profile))
    run_reporting.event("Starting batch-location child command for %d serials", len(serials))
    completed = subprocess.run(command, env=os.environ.copy(), check=False)
    return completed.returncode


def cli() -> None:
    """Run the command with stable, user-facing error handling."""
    try:
        raise SystemExit(main())
    except (eudm.EUDMError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2)
    except KeyboardInterrupt:
        print("\nCancelled.")
        raise SystemExit(130)


if __name__ == "__main__":
    cli()
