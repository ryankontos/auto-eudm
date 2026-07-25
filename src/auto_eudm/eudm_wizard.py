#!/usr/bin/env python3
"""Guided, numbered frontend for the EUDM device-management questionnaire.

The wizard asks for each dynamic EUDM choice and asks before submitting. Use
--simulate to rehearse all prompts and selections without Chrome, network, or
real EUDM changes.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from .bootstrap import ensure_runtime
from . import eudm_request as eudm
from . import run_reporting
from . import presentation
from .cli_common import add_runtime_arguments, console, open_client, start_run, validate_runtime_args
from .eudm_config import AppConfig


def prompt_text(label: str) -> str:
    return console.text(label)


def choose(label: str, choices: list[tuple[str, Any]]) -> tuple[str, Any]:
    return console.choose(label, choices)


def choose_preferred(
    label: str, choices: list[tuple[str, Any]], preferred: str | None
) -> tuple[str, Any]:
    """Use an exact shared-env default when it is present in the live choices."""
    if preferred:
        matches = [choice for choice in choices if choice[0].casefold() == preferred.casefold()]
        if len(matches) == 1:
            print(f"{label}: {matches[0][0]} (from shared configuration)")
            return matches[0]
    return choose(label, choices)


def yes_no(label: str, *, default: bool = False) -> bool:
    return console.yes_no(label, default=default)


def static_choices(item: dict[str, Any]) -> list[tuple[str, str]]:
    return [
        (str(option["displayValue"]), str(option["dataValue"]))
        for option in item.get("options", [])
    ]


def row_choices(rows: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [
        (" → ".join(str(value) for value in row.get("displayValue", []) if str(value)), row["dataValue"])
        for row in rows
    ]


def select_lookup_person(
    client: Any,
    request_id: str,
    questionnaire_id: str,
    table: dict[str, Any],
    query: str,
) -> str:
    current = query
    while True:
        result = eudm.request_step(
            client,
            "Could not search for a user",
            "POST",
            f"v2/sbe/services/requests/{request_id}/questions/{table['id']}/lookup",
            {"query": current},
        ) or {}
        rows = result.get("multiColumnOptions") or []
        try:
            value = eudm.choose_data_value(rows, current, exact=True, kind="user")
        except eudm.MatchError as exc:
            current = eudm.retry_or_skip("username", current, exc)
            continue
        eudm.answer(client, request_id, questionnaire_id, table, value)
        return current


def main() -> int:
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
        epilog="""Authentication:
  By default, a separate Chrome profile is opened for SSO. Use --cookie-mode
  only when EUDM_COOKIE contains the complete browser Cookie request header.

Simulation:
  --simulate runs the same numbered flow using sample users, locations, devices,
  and SIM-REQ IDs. It never opens Chrome or contacts EUDM.

Review:
  --manual-review displays a concise summary of the populated request before the
  final y/n submission prompt.
""",
    )
    add_runtime_arguments(parser, config)
    args = parser.parse_args()
    validate_runtime_args(args)
    start_run(args, "eudm-wizard")

    mode_display, mode = choose(
        "Request mode",
        [("One device", "single"), ("Batch serial list to a location (no user)", "batch")],
    )
    if config.request_for:
        request_for = config.request_for
        print(f"Request-for login ID: {request_for} (from shared configuration)")
    else:
        request_for = console.text("Request-for login ID")
    if mode == "batch":
        raw_serials = prompt_text("Serial numbers, comma-separated").split(",")
        serials = [value.strip() for value in raw_serials]
        if any(not value for value in serials):
            raise eudm.EUDMError("The batch serial list contains an empty entry")
        if any(any(character.isspace() for character in value) for value in serials):
            raise eudm.EUDMError("Serial numbers cannot contain whitespace")
        if len({value.casefold() for value in serials}) != len(serials):
            raise eudm.EUDMError("The batch serial list contains duplicates")
    else:
        serials = [prompt_text("Hostname or serial number")]

    print(f"\nStarting {mode_display.lower()} for {', '.join(serials)}.")
    client = open_client(args)
    created = eudm.request_step(
        client,
        "Could not create the EUDM request",
        "POST",
        "v2/sbe/services/requests",
        {"serviceId": "25301", "quantity": 1, "requestedForLoginIds": [request_for]},
    )
    request_id = str(created["requests"][0]["requestId"])
    questionnaire = eudm.request_step(
        client,
        "Could not load the current questionnaire",
        "GET",
        f"v2/sbe/services/requests/{request_id}/questionnaire?timezoneId=Australia/Sydney",
    )["questionnaire"]
    questionnaire_id = str(questionnaire["id"])
    all_items = eudm.items(questionnaire)
    eudm.verbose_detail(client, f"Created request {request_id}.")

    inventory = eudm.field_by_label(all_items, "Inventory Request Type", type_="RadioButtons")
    if mode == "batch":
        inventory_events = eudm.answer(
            client, request_id, questionnaire_id, inventory, "BULK"
        )
        serial_list = eudm.field_by_label(all_items, "Please add serial number list", type_="TextArea")
        serials = eudm.answer_batch_assets_with_retry(
            client,
            request_id,
            questionnaire_id,
            all_items,
            inventory_events,
            serial_list,
            serials,
        )
        eudm.verbose_detail(client, f"Matched all {len(serials)} requested assets.")
    else:
        eudm.answer(client, request_id, questionnaire_id, inventory, "ADD")
        search_by = eudm.field_by_label(all_items, "Search by", type_="RadioButtons")
        eudm.answer(client, request_id, questionnaire_id, search_by, "serial")
        serial_field = eudm.field_by_label(
            all_items, "Type Hostname or Serial Number", type_="TextField"
        )
        device_table = eudm.field_by_label(all_items, "----- Device List", type_="DataTable")
        serials[0] = eudm.answer_single_asset_with_retry(
            client, request_id, questionnaire_id, serial_field, device_table, serials[0]
        )

    status_item = eudm.field_by_label(all_items, "Change Status to", type_="Dropdown")
    statuses = static_choices(status_item)
    if mode == "batch":
        statuses = [choice for choice in statuses if not choice[0].startswith("Deployed - ")]
    status_display, status_value = choose("Change status to", statuses)
    eudm.answer(client, request_id, questionnaire_id, status_item, status_value)
    user_target = mode == "single" and status_display.startswith("Deployed - ")

    if user_target:
        user_table = eudm.field_by_label(
            all_items, "Please select user - device has been deployed to", type_="DataTable"
        )
        deployed_to_display = select_lookup_person(
            client,
            request_id,
            questionnaire_id,
            user_table,
            prompt_text("Search deployed-to user"),
        )
        review_target = "user"
        review_destination = deployed_to_display
        review_detail = None
    else:
        city_item = eudm.field_by_label(
            all_items, "Building Location (City - Country code)", type_="Dropdown"
        )
        city_display, city_value = choose_preferred(
            "City", static_choices(city_item), config.city
        )
        events = eudm.answer(client, request_id, questionnaire_id, city_item, city_value)
        location_table = eudm.field_by_label(
            all_items, "Please select location", type_="DataTable"
        )
        locations = eudm.option_data(events, location_table["id"])
        if not locations:
            raise eudm.EUDMError("The selected city returned no selectable locations.")
        location_choices = row_choices(locations)
        preferred_location = " → ".join(
            value for value in (config.building, config.floor, config.room, config.cabinet) if value
        )
        location_display, location_value = choose_preferred(
            "Specific location", location_choices, preferred_location
        )
        eudm.answer(client, request_id, questionnaire_id, location_table, location_value)
        review_target = "location"
        review_destination = location_display
        review_detail = "No associated user" if mode == "batch" else None

        returned = eudm.field_by_label(
            all_items, "Is this a return from a user", type_="RadioButtons"
        )
        if mode == "batch":
            eudm.answer(client, request_id, questionnaire_id, returned, "NO")
        else:
            is_return = yes_no("Is this a return from a user?")
            eudm.answer(
                client, request_id, questionnaire_id, returned, "YES" if is_return else "NO"
            )
            if is_return and yes_no("Add the name of the person who dropped it off?", default=True):
                add_dropoff = eudm.field_by_label(
                    all_items, "Add Name of person who dropped off device", type_="YesNo"
                )
                eudm.answer(client, request_id, questionnaire_id, add_dropoff, "true")
                search_item = eudm.field_by_label(
                    all_items,
                    "Search Name or User ID that dropped off devices",
                    type_="TextField",
                )
                table = eudm.field_by_label(
                    all_items, "Select person who dropped device/s off", type_="DataTable"
                )
                dropoff_query = prompt_text("Search drop-off user")
                while True:
                    events = eudm.answer(
                        client, request_id, questionnaire_id, search_item, dropoff_query
                    )
                    dropoff_rows = eudm.option_data(events, table["id"])
                    try:
                        dropoff_value = eudm.choose_data_value(
                            dropoff_rows, dropoff_query, exact=True, kind="drop-off user"
                        )
                    except eudm.MatchError as exc:
                        dropoff_query = eudm.retry_or_skip("username", dropoff_query, exc)
                        continue
                    eudm.answer(client, request_id, questionnaire_id, table, dropoff_value)
                    review_detail = f"Dropped by {dropoff_query}"
                    break

    if args.manual_review:
        approved = eudm.manual_review(
            request_id=request_id,
            request_for=request_for,
            serials=serials,
            status=status_display,
            target=review_target,
            destination=review_destination,
            detail=review_detail,
        )
    else:
        print(f"\nRequest {request_id} is populated but not submitted.")
        approved = yes_no("Submit this request now?")
    if not approved:
        print(f"Not submitted. Request {request_id} remains populated for review.")
        return 0
    order = eudm.request_step(
        client,
        "Could not submit the order",
        "POST",
        "v2/sbe/orders",
        {"requestIds": [request_id], "title": None},
    )
    order_id = order.get("id") if isinstance(order, dict) else None
    print(f"Submitted successfully{f' (order {order_id})' if order_id else ''}. Request {request_id}.")
    presentation.summary(
        "Request summary",
        [
            (
                "success",
                f"{', '.join(serials)} → {review_target} {review_destination} | "
                f"{status_display} | request {request_id}" + (f" | order {order_id}" if order_id else ""),
            )
        ],
    )
    run_reporting.write_result_file(
        "eudm-wizard",
        [
            " | ".join(
                (
                    "SUBMITTED",
                    f"serials={','.join(serials)}",
                    f"request_for={request_for}",
                    f"status={status_display}",
                    f"target={review_target}",
                    f"destination={review_destination}",
                    f"request={request_id}",
                    f"order={order_id or '-'}",
                )
            )
        ],
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Cancelled.")
        raise SystemExit(130)
    except EOFError:
        print("Input ended before the request was complete.")
        raise SystemExit(2)
    except eudm.MatchSkipped as exc:
        print(f"Not submitted. {exc}")
        raise SystemExit(0)
    except eudm.EUDMError as exc:
        print(f"Error: {exc}")
        raise SystemExit(2)
    except (KeyError, IndexError, TypeError):
        print("Error: EUDM returned an incomplete or unexpected response.")
        raise SystemExit(2)
    except Exception:
        print("Error: An unexpected problem occurred. Re-run with --verbose and report the step shown before it.")
        raise SystemExit(2)
