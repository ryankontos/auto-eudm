#!/usr/bin/env python3
"""Guided importer for Sydney inventory tracking workbooks.

The importer reads a dated sheet, previews the resulting device changes, then
either stops (--dry-run), submits real EUDM requests, or submits fully local
simulation requests (--simulate). It never submits before showing the preview
and receiving a final confirmation.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
import re
import sys
from typing import Any, Iterable

from .bootstrap import ensure_runtime
from . import eudm_request as eudm
from .cli_common import (
    add_runtime_arguments,
    console,
    open_client,
    request_for as resolve_request_for,
    start_run,
    validate_runtime_args,
)
from .eudm_config import AppConfig
from .user_assignments import (
    DeploymentOutcome,
    UserDeployment,
    UserDeploymentRunner,
    print_grouped_results,
)


NEW_STOCK = "Deployed - New Stock"
EXISTING_STOCK = "Deployed - Existing Stock"
PENDING_RETURN = "Pending Return"
FILE_PREFIX = "Inventory Tracking - Sydney"


@dataclass(frozen=True)
class SheetRow:
    row_number: int
    deployment_date: date
    username: str | None
    new_serial: str | None
    old_serial: str | None
    marked_red: bool


@dataclass(frozen=True)
class Action:
    group: str
    row_number: int
    username: str
    serial: str
    status: str


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.startswith("#"):
        return None
    if isinstance(value, float) and value.is_integer():
        text = str(int(value))
    return text


def looks_like_serial(value: str | None) -> bool:
    """Reject blanks and obvious sheet markers such as 1-5 without overfitting vendors."""
    return bool(
        value
        and len(value) >= 6
        and not any(character.isspace() for character in value)
        and re.fullmatch(r"[A-Za-z0-9._-]+", value)
    )


def cell_is_red(cell: Any) -> bool:
    colors = [getattr(cell.font, "color", None)]
    if getattr(cell.fill, "fill_type", None):
        colors.extend((getattr(cell.fill, "fgColor", None), getattr(cell.fill, "bgColor", None)))
    for color in colors:
        if color is None or getattr(color, "type", None) != "rgb":
            continue
        rgb = str(getattr(color, "rgb", "") or "").upper()
        if rgb.endswith("FF0000"):
            return True
    return False


def normalize_date(value: Any, epoch: datetime) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        try:
            from openpyxl.utils.datetime import from_excel

            converted = from_excel(value, epoch)
            return converted.date() if isinstance(converted, datetime) else converted
        except (TypeError, ValueError, OverflowError):
            return None
    if isinstance(value, str):
        text = value.strip()
        for pattern in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%y"):
            try:
                return datetime.strptime(text, pattern).date()
            except ValueError:
                pass
    return None


def username_for(row: tuple[Any, ...]) -> str | None:
    """Read the EUDM username from column D only; column F is never a fallback."""
    return clean_text(row[3].value)


def select_workbook_sheet(workbook: Any) -> Any:
    if "Bookings 2026" in workbook.sheetnames:
        return workbook["Bookings 2026"]
    selected = choose_number(
        "The workbook has no sheet named 'Bookings 2026'. Select a sheet",
        list(workbook.sheetnames),
    )
    return workbook[workbook.sheetnames[selected]]


def load_sheet(path: Path) -> tuple[str, list[SheetRow]]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise eudm.EUDMError(
            "Spreadsheet support is not installed. Run: "
            "python3 -m pip install -r requirements-sheet.txt"
        ) from exc
    try:
        workbook = load_workbook(path, data_only=True, read_only=False)
    except FileNotFoundError as exc:
        raise eudm.EUDMError(f"Spreadsheet not found: {path}") from exc
    except Exception as exc:
        raise eudm.EUDMError(
            f"Could not read {path.name}. Use an unencrypted .xlsx or .xlsm workbook."
        ) from exc
    try:
        sheet = select_workbook_sheet(workbook)
        rows: list[SheetRow] = []
        for values in sheet.iter_rows(min_row=2, min_col=1, max_col=12):
            deployment_date = normalize_date(values[0].value, workbook.epoch)
            if deployment_date is None:
                continue
            username = username_for(values)
            rows.append(
                SheetRow(
                    row_number=values[0].row,
                    deployment_date=deployment_date,
                    username=username,
                    new_serial=clean_text(values[9].value),
                    old_serial=clean_text(values[11].value),
                    marked_red=any(cell_is_red(cell) for cell in values),
                )
            )
        if not rows:
            raise eudm.EUDMError(
                f"No dated data rows were found in columns A-L of sheet {sheet.title!r}."
            )
        return sheet.title, rows
    finally:
        workbook.close()


def latest_download() -> Path | None:
    downloads = Path.home() / "Downloads"
    candidates = [
        path
        for pattern in (f"{FILE_PREFIX}*.xlsx", f"{FILE_PREFIX}*.xlsm")
        for path in downloads.glob(pattern)
        if not path.name.startswith("~$")
    ]
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def choose_file(argument: str | None) -> Path:
    if argument:
        return Path(argument).expanduser().resolve()
    latest = latest_download()
    if latest:
        raw = input(f"Spreadsheet [{latest}]: ").strip()
        return Path(raw).expanduser().resolve() if raw else latest.resolve()
    raw = input("Spreadsheet path: ").strip()
    if not raw:
        raise eudm.EUDMError(
            f"No {FILE_PREFIX!r} workbook was found in Downloads and no file was entered."
        )
    return Path(raw).expanduser().resolve()


def choose_number(label: str, options: list[str]) -> int:
    return console.choose_index(label, options)


def yes_no(label: str, *, default: bool = False) -> bool:
    return console.yes_no(label, default=default)


def parse_number_selection(raw: str, maximum: int) -> set[int]:
    selected: set[int] = set()
    if not raw.strip():
        return selected
    for part in raw.split(","):
        token = part.strip()
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", token)
        if not match:
            raise ValueError(f"{token!r} is not a number or range")
        first = int(match.group(1))
        last = int(match.group(2) or first)
        if first > last or first < 1 or last > maximum:
            raise ValueError(f"{token!r} is outside 1-{maximum}")
        selected.update(range(first - 1, last))
    return selected


def eligible_counts(rows: Iterable[SheetRow]) -> tuple[int, int]:
    eligible = [row for row in rows if not row.marked_red and row.username]
    return (
        sum(looks_like_serial(row.new_serial) for row in eligible),
        sum(looks_like_serial(row.old_serial) for row in eligible),
    )


def build_actions(
    rows: list[SheetRow], selected_date: date, mode: str
) -> tuple[list[Action], Counter[str]]:
    actions: list[Action] = []
    ignored: Counter[str] = Counter()
    for row in rows:
        if row.deployment_date != selected_date:
            continue
        if row.marked_red:
            ignored["marked red"] += 1
            continue
        if not row.username:
            if mode in ("new", "both") and looks_like_serial(row.new_serial):
                ignored["new serial has no username in column D"] += 1
            if mode in ("returns", "both") and looks_like_serial(row.old_serial):
                ignored["return serial has no username in column D"] += 1
            if (
                not looks_like_serial(row.new_serial)
                and not looks_like_serial(row.old_serial)
            ):
                ignored["no username in column D"] += 1
            continue
        if mode in ("new", "both"):
            if looks_like_serial(row.new_serial):
                actions.append(Action("New deployments", row.row_number, row.username, row.new_serial, NEW_STOCK))
            else:
                ignored["no usable new serial in column J"] += 1
        if mode in ("returns", "both") and looks_like_serial(row.old_serial):
            actions.append(Action("Old / pending return", row.row_number, row.username, row.old_serial, PENDING_RETURN))
        elif mode in ("returns", "both"):
            ignored["no usable old serial in column L"] += 1

    serial_spellings: dict[str, str] = {}
    counts: Counter[str] = Counter()
    for action in actions:
        key = action.serial.casefold()
        counts[key] += 1
        serial_spellings.setdefault(key, action.serial)
    duplicates = [serial_spellings[key] for key, count in counts.items() if count > 1]
    if duplicates:
        raise eudm.EUDMError(
            "The selected work contains duplicate serial numbers: " + ", ".join(duplicates)
        )
    return actions, ignored


def override_new_statuses(actions: list[Action]) -> list[Action]:
    new_indexes = [index for index, action in enumerate(actions) if action.group == "New deployments"]
    if not new_indexes:
        return actions
    print("\nNew deployments (default: Deployed - New Stock)")
    for display_index, action_index in enumerate(new_indexes, 1):
        action = actions[action_index]
        print(f"  {display_index}. {action.serial} → {action.username}")
    while True:
        raw = input(
            "Numbers to change to Deployed - Existing Stock "
            "(comma-separated; ranges such as 2-4; Enter for none): "
        )
        try:
            selected = parse_number_selection(raw, len(new_indexes))
            break
        except ValueError as exc:
            print(f"Please correct the selection: {exc}.")
    updated = list(actions)
    for selected_index in selected:
        action_index = new_indexes[selected_index]
        updated[action_index] = replace(updated[action_index], status=EXISTING_STOCK)
    return updated


def print_preview(
    path: Path,
    sheet_name: str,
    selected_date: date,
    actions: list[Action],
    ignored: Counter[str],
) -> None:
    print("\nPreview")
    print(f"  File: {path}")
    print(f"  Sheet: {sheet_name}")
    print(f"  Date: {selected_date.strftime('%A %-d %B %Y')}")
    for group in ("New deployments", "Old / pending return"):
        grouped = [action for action in actions if action.group == group]
        print(f"\n{group} ({len(grouped)})")
        if not grouped:
            print("  None")
        for index, action in enumerate(grouped, 1):
            print(
                f"  {index}. {action.serial} | "
                f"{action.username} | {action.status}"
            )
    if ignored:
        print("\nIgnored serial numbers")
        for reason, count in sorted(ignored.items()):
            print(f"  {count} × {reason}")


def execute(
    client: Any, actions: list[Action], request_for: str, manual_review_enabled: bool, concurrency: int
) -> list[DeploymentOutcome]:
    deployments = [
        UserDeployment(
            serial=action.serial,
            username=action.username,
            status=action.status,
            group=action.group,
            source=f"row {action.row_number}",
        )
        for action in actions
    ]
    return UserDeploymentRunner(
        client, request_for, manual_review=manual_review_enabled, concurrency=concurrency
    ).run(deployments)


def print_results(outcomes: list[DeploymentOutcome]) -> None:
    print_grouped_results(
        outcomes, ("New deployments", "Old / pending return"), command="eudm-inventory-import"
    )


def main() -> int:
    ensure_runtime(requirement_file="requirements-sheet.txt", import_name="openpyxl")
    try:
        config = AppConfig.load()
    except ValueError as exc:
        raise eudm.EUDMError(f"Could not load shared configuration: {exc}") from exc
    if "--no-simulate" in sys.argv[1:] or (
        "--simulate" not in sys.argv[1:] and not config.simulate
    ):
        ensure_runtime(requirement_file="requirements-browser.txt", import_name="playwright")
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Workbook rules:
  The newest 'Inventory Tracking - Sydney*.xlsx' or .xlsm in Downloads is used
  when FILE is omitted. 'Bookings 2026' is selected automatically; otherwise
  you choose a sheet. A=deployment date, D=username, J=new serial, L=old serial.
  Red rows and rows without a username in D are excluded before EUDM. New and
  return serials are assessed independently from columns J and L.

Modes:
  --dry-run ends after the preview with zero browser or EUDM API activity.
  --simulate continues after confirmation using local SIM-REQ/SIM-ORDER IDs,
  but never opens Chrome, reads cookies, contacts EUDM, or changes real data.
  --manual-review asks for a separate y/n approval after each request is populated.
""",
    )
    parser.add_argument("file", nargs="?", metavar="FILE", help="Optional .xlsx/.xlsm workbook. If omitted, choose the newest matching Downloads file or enter a path.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview only. Does not open Chrome or make any EUDM API requests.",
    )
    parser.add_argument(
        "--request-for",
        default=config.request_for,
        help="Remedy login ID for every request (default: EUDM_REQUEST_FOR).",
    )
    add_runtime_arguments(parser, config)
    args = parser.parse_args()
    validate_runtime_args(args)
    start_run(args, "eudm-inventory-import")

    path = choose_file(args.file)
    sheet_name, rows = load_sheet(path)
    dates = sorted({row.deployment_date for row in rows}, reverse=True)
    date_options = []
    for candidate in dates:
        new_count, return_count = eligible_counts(
            row for row in rows if row.deployment_date == candidate
        )
        date_options.append(
            f"{candidate.strftime('%A %-d %B %Y')} "
            f"({new_count} new, {return_count} returns)"
        )
    selected_date = dates[choose_number("Deployment date", date_options)]
    selected_counts = eligible_counts(
        row for row in rows if row.deployment_date == selected_date
    )
    mode_index = choose_number(
        "What should be deployed?",
        [
            f"New serials from column J [{selected_counts[0]}]",
            f"Returns from column L [{selected_counts[1]}]",
            f"Both new serials and returns [{sum(selected_counts)}]",
        ],
    )
    mode = ("new", "returns", "both")[mode_index]
    actions, ignored = build_actions(rows, selected_date, mode)
    if not actions:
        raise eudm.EUDMError("No deployable rows remain for that date and selection.")
    actions = override_new_statuses(actions)
    print_preview(path, sheet_name, selected_date, actions, ignored)

    if args.dry_run:
        print("\nDry run complete. No browser was opened and no EUDM API requests were made.")
        return 0
    requester = resolve_request_for(args, config)
    if not yes_no(f"Submit all {len(actions)} requests now?"):
        print("Cancelled before authentication. No EUDM API requests were made.")
        return 0

    client = open_client(args)
    outcomes = execute(client, actions, requester, args.manual_review, args.concurrency)
    print_results(outcomes)
    return 1 if any(outcome.error for outcome in outcomes) else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        raise SystemExit(130)
    except EOFError:
        print("\nInput ended before the import was complete.", file=sys.stderr)
        raise SystemExit(2)
    except eudm.EUDMError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2)
    except Exception:
        print(
            "Error: An unexpected problem occurred. Re-run with --verbose and "
            "report the last step shown.",
            file=sys.stderr,
        )
        raise SystemExit(2)
