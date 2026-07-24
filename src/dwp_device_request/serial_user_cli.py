#!/usr/bin/env python3
"""Deploy newline-separated SERIAL USERNAME pairs to users.

Pass the text as one argument, read it from --file, pipe it on stdin, or run
without input and paste lines interactively. Every line must contain exactly
one serial number, whitespace, and one username.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from .bootstrap import ensure_runtime
from . import automate_device_request as dwp
from .cli_common import add_runtime_arguments, console, open_client, request_for, validate_runtime_args
from .dwp_config import AppConfig
from .user_deployments import (
    USER_STATUSES,
    UserDeployment,
    UserDeploymentRunner,
    print_grouped_results,
)


class SerialUserCLI:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(
            description=__doc__,
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog="""Input examples:
  python3 serial_user_cli.py $'ABC123 user.one\\nDEF456 user.two'
  python3 serial_user_cli.py --file assignments.txt
  pbpaste | python3 serial_user_cli.py -

Status option 1 is Used stock (the DWP value Deployed - Existing Stock).
All values are previewed before authentication. Use --dry-run for no API work
or --simulate for a complete local rehearsal.
""",
        )
        parser.add_argument(
            "pairs",
            nargs="?",
            help="Literal newline-separated SERIAL USERNAME text, or '-' to read stdin.",
        )
        parser.add_argument("--file", type=Path, help="Read SERIAL USERNAME lines from a text file.")
        parser.add_argument(
            "--request-for",
            default=self.config.request_for,
            help="Requesting login ID (default: DWP_REQUEST_FOR).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Parse and preview only; no browser or DWP requests.",
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Accept the batch preview without the confirmation prompt.",
        )
        add_runtime_arguments(parser, self.config)
        return parser

    @staticmethod
    def read_interactive() -> str:
        print("Paste SERIAL USERNAME lines. Enter a blank line when finished:")
        lines: list[str] = []
        while True:
            try:
                line = input()
            except EOFError:
                break
            if not line.strip():
                break
            lines.append(line)
        return "\n".join(lines)

    @staticmethod
    def read_input(args: argparse.Namespace) -> str:
        if args.file and args.pairs is not None:
            raise dwp.DWPError("Use either PAIRS or --file, not both.")
        if args.file:
            try:
                return args.file.expanduser().read_text(encoding="utf-8")
            except OSError as exc:
                raise dwp.DWPError(f"Could not read input file: {args.file}") from exc
        if args.pairs == "-" or (args.pairs is None and not sys.stdin.isatty()):
            return sys.stdin.read()
        if args.pairs is not None:
            return args.pairs
        return SerialUserCLI.read_interactive()

    @staticmethod
    def parse_pairs(raw: str) -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        errors: list[str] = []
        for line_number, line in enumerate(raw.splitlines(), 1):
            if not line.strip():
                continue
            fields = line.split()
            if len(fields) != 2:
                errors.append(f"line {line_number} must contain exactly SERIAL USERNAME")
                continue
            serial, username = fields
            pairs.append((serial, username))
        if errors:
            raise dwp.DWPError("Invalid input: " + "; ".join(errors))
        if not pairs:
            raise dwp.DWPError("No SERIAL USERNAME pairs were supplied.")
        serials = [serial.casefold() for serial, _ in pairs]
        duplicates = sorted({serial for serial in serials if serials.count(serial) > 1})
        if duplicates:
            raise dwp.DWPError("Duplicate serial numbers: " + ", ".join(duplicates))
        return pairs

    def choose_status(self) -> tuple[str, str]:
        print("\nDeployment status for every device")
        for index, (label, value) in enumerate(USER_STATUSES, 1):
            detail = " (Deployed - Existing Stock)" if index == 1 else ""
            print(f"  {index}. {label}{detail}")
        default = next(
            (index for index, (_, value) in enumerate(USER_STATUSES, 1) if value == self.config.default_user_status),
            1,
        )
        while True:
            raw = input(f"Choose 1-{len(USER_STATUSES)} [{default}]: ").strip()
            if not raw:
                return USER_STATUSES[default - 1]
            if raw.isdigit() and 1 <= int(raw) <= len(USER_STATUSES):
                return USER_STATUSES[int(raw) - 1]
            print("Enter one of the listed numbers.")

    @staticmethod
    def preview(deployments: list[UserDeployment], status_label: str) -> None:
        print("\nPreview")
        print(f"  Status for all: {status_label}")
        for index, deployment in enumerate(deployments, 1):
            print(f"  {index}. {deployment.serial} → {deployment.username}")
        print(f"  Total: {len(deployments)}")

    def run(self) -> int:
        args = self.parser().parse_args()
        validate_runtime_args(args)
        pairs = self.parse_pairs(self.read_input(args))
        status_label, status_value = self.choose_status()
        deployments = [
            UserDeployment(serial, username, status_value) for serial, username in pairs
        ]
        self.preview(deployments, status_label)
        if args.dry_run:
            print("\nDry run complete. No browser was opened and no DWP requests were made.")
            return 0
        requester = request_for(args, self.config)
        if not args.yes and not console.yes_no(f"Submit all {len(deployments)} requests now?"):
            print("Cancelled before authentication. No DWP requests were made.")
            return 0
        client = open_client(args)
        outcomes = UserDeploymentRunner(
            client, requester, manual_review=args.manual_review
        ).run(deployments)
        print_grouped_results(outcomes)
        return 1 if any(outcome.error for outcome in outcomes) else 0


def main() -> int:
    if not any(arg in {"--simulate", "--no-simulate"} for arg in sys.argv[1:]):
        ensure_runtime(requirement_file="requirements-browser.txt", import_name="playwright")
    return SerialUserCLI(AppConfig.load()).run()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        raise SystemExit(130)
    except EOFError:
        print("\nInput ended before the batch was complete.", file=sys.stderr)
        raise SystemExit(2)
    except (dwp.DWPError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2)
    except Exception:
        print("Error: An unexpected problem occurred. Re-run with --verbose.", file=sys.stderr)
        raise SystemExit(2)
