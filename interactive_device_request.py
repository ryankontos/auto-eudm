#!/usr/bin/env python3
"""Interactive CLI frontend for the DWP device-management questionnaire."""

from __future__ import annotations

import argparse
import os
import urllib.parse
from typing import Any

import automate_device_request as dwp


def prompt_text(label: str) -> str:
    while True:
        value = input(f"{label}: ").strip()
        if value:
            return value
        print("A value is required.")


def choose(label: str, choices: list[tuple[str, Any]]) -> tuple[str, Any]:
    if not choices:
        raise dwp.DWPError(f"No choices are available for {label}")
    print(f"\n{label}")
    for index, (display, _) in enumerate(choices, 1):
        print(f"  {index}. {display}")
    while True:
        raw = input(f"Choose 1-{len(choices)}: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(choices):
            return choices[int(raw) - 1]
        print("Enter one of the listed numbers.")


def yes_no(label: str, *, default: bool = False) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        value = input(f"{label} {suffix}: ").strip().casefold()
        if not value:
            return default
        if value in ("y", "yes"):
            return True
        if value in ("n", "no"):
            return False
        print("Enter y or n.")


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


def open_client(args: argparse.Namespace) -> Any:
    return dwp.open_client(
        base=args.base,
        browser_profile=args.browser_profile,
        simulate=args.simulate,
        verbose=args.verbose,
    )


def select_lookup_person(
    client: Any,
    request_id: str,
    questionnaire_id: str,
    table: dict[str, Any],
    query: str,
) -> None:
    result = dwp.request_step(
        client,
        "Could not search for a user",
        "POST",
        f"v2/sbe/services/requests/{request_id}/questions/{table['id']}/lookup",
        {"query": query},
    ) or {}
    rows = result.get("multiColumnOptions") or []
    if not rows:
        raise dwp.DWPError(f"No users matched {query!r}.")
    _, value = choose(table["label"], row_choices(rows))
    dwp.answer(client, request_id, questionnaire_id, table, value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--browser-profile",
        default="~/.dwp-device-request-chrome",
        help="Dedicated installed-Chrome profile used for SSO",
    )
    parser.add_argument("--cookie-mode", action="store_true", help="Use DWP_COOKIE instead of Chrome")
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="Run the full wizard locally without authentication, network, or DWP changes",
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--base", default=os.getenv("DWP_BASE", dwp.DEFAULT_BASE))
    args = parser.parse_args()
    if args.cookie_mode:
        args.browser_profile = None
    parsed_base = urllib.parse.urlparse(args.base)
    if (
        parsed_base.scheme != "https"
        or not parsed_base.netloc
        or not parsed_base.path.rstrip("/").endswith("/rest")
    ):
        raise dwp.DWPError("--base must be an HTTPS DWP REST URL ending in /rest")

    mode_display, mode = choose(
        "Request mode",
        [("One device", "single"), ("Batch serial list to a location (no user)", "batch")],
    )
    request_for = prompt_text("Request-for login ID")
    if mode == "batch":
        raw_serials = prompt_text("Serial numbers, comma-separated").split(",")
        serials = [value.strip() for value in raw_serials]
        if any(not value for value in serials):
            raise dwp.DWPError("The batch serial list contains an empty entry")
        if any(any(character.isspace() for character in value) for value in serials):
            raise dwp.DWPError("Serial numbers cannot contain whitespace")
        if len({value.casefold() for value in serials}) != len(serials):
            raise dwp.DWPError("The batch serial list contains duplicates")
    else:
        serials = [prompt_text("Hostname or serial number")]

    print(f"\nStarting {mode_display.lower()} for {', '.join(serials)}.")
    client = open_client(args)
    created = dwp.request_step(
        client,
        "Could not create the DWP request",
        "POST",
        "v2/sbe/services/requests",
        {"serviceId": "25301", "quantity": 1, "requestedForLoginIds": [request_for]},
    )
    request_id = str(created["requests"][0]["requestId"])
    questionnaire = dwp.request_step(
        client,
        "Could not load the current questionnaire",
        "GET",
        f"v2/sbe/services/requests/{request_id}/questionnaire?timezoneId=Australia/Sydney",
    )["questionnaire"]
    questionnaire_id = str(questionnaire["id"])
    all_items = dwp.items(questionnaire)
    print(f"Created request {request_id}.")

    inventory = dwp.field_by_label(all_items, "Inventory Request Type", type_="RadioButtons")
    if mode == "batch":
        inventory_events = dwp.answer(
            client, request_id, questionnaire_id, inventory, "BULK"
        )
        serial_list = dwp.field_by_label(all_items, "Please add serial number list", type_="TextArea")
        serial_events = dwp.answer(
            client, request_id, questionnaire_id, serial_list, ",".join(serials)
        )
        events = dwp.merge_events(inventory_events, serial_events)
        asset_table, asset_values = dwp.batch_asset_selection(all_items, events, serials)
        print(f"Matched all {len(asset_values)} requested assets.")
        dwp.answer_values(client, request_id, questionnaire_id, asset_table, asset_values)
    else:
        dwp.answer(client, request_id, questionnaire_id, inventory, "ADD")
        search_by = dwp.field_by_label(all_items, "Search by", type_="RadioButtons")
        dwp.answer(client, request_id, questionnaire_id, search_by, "serial")
        serial_field = dwp.field_by_label(
            all_items, "Type Hostname or Serial Number", type_="TextField"
        )
        events = dwp.answer(
            client, request_id, questionnaire_id, serial_field, serials[0]
        )
        device_table = dwp.field_by_label(all_items, "----- Device List", type_="DataTable")
        devices = dwp.option_data(events, device_table["id"])
        _, device_value = choose("Select device", row_choices(devices))
        dwp.answer(client, request_id, questionnaire_id, device_table, device_value)

    status_item = dwp.field_by_label(all_items, "Change Status to", type_="Dropdown")
    statuses = static_choices(status_item)
    if mode == "batch":
        statuses = [choice for choice in statuses if not choice[0].startswith("Deployed - ")]
    status_display, status_value = choose("Change status to", statuses)
    dwp.answer(client, request_id, questionnaire_id, status_item, status_value)
    user_target = mode == "single" and status_display.startswith("Deployed - ")

    if user_target:
        user_table = dwp.field_by_label(
            all_items, "Please select user - device has been deployed to", type_="DataTable"
        )
        select_lookup_person(
            client,
            request_id,
            questionnaire_id,
            user_table,
            prompt_text("Search deployed-to user"),
        )
    else:
        city_item = dwp.field_by_label(
            all_items, "Building Location (City - Country code)", type_="Dropdown"
        )
        _, city_value = choose("City", static_choices(city_item))
        events = dwp.answer(client, request_id, questionnaire_id, city_item, city_value)
        location_table = dwp.field_by_label(
            all_items, "Please select location", type_="DataTable"
        )
        locations = dwp.option_data(events, location_table["id"])
        _, location_value = choose("Specific location", row_choices(locations))
        dwp.answer(client, request_id, questionnaire_id, location_table, location_value)

        returned = dwp.field_by_label(
            all_items, "Is this a return from a user", type_="RadioButtons"
        )
        if mode == "batch":
            dwp.answer(client, request_id, questionnaire_id, returned, "NO")
        else:
            is_return = yes_no("Is this a return from a user?")
            dwp.answer(
                client, request_id, questionnaire_id, returned, "YES" if is_return else "NO"
            )
            if is_return and yes_no("Add the name of the person who dropped it off?", default=True):
                add_dropoff = dwp.field_by_label(
                    all_items, "Add Name of person who dropped off device", type_="YesNo"
                )
                dwp.answer(client, request_id, questionnaire_id, add_dropoff, "true")
                search_item = dwp.field_by_label(
                    all_items,
                    "Search Name or User ID that dropped off devices",
                    type_="TextField",
                )
                table = dwp.field_by_label(
                    all_items, "Select person who dropped device/s off", type_="DataTable"
                )
                events = dwp.answer(
                    client,
                    request_id,
                    questionnaire_id,
                    search_item,
                    prompt_text("Search drop-off user"),
                )
                _, dropoff_value = choose(
                    "Select drop-off user", row_choices(dwp.option_data(events, table["id"]))
                )
                dwp.answer(client, request_id, questionnaire_id, table, dropoff_value)

    print(f"\nRequest {request_id} is populated but not submitted.")
    if not yes_no("Submit this request now?"):
        return 0
    order = dwp.request_step(
        client,
        "Could not submit the order",
        "POST",
        "v2/sbe/orders",
        {"requestIds": [request_id], "title": None},
    )
    order_id = order.get("id") if isinstance(order, dict) else None
    print(f"Submitted successfully{f' (order {order_id})' if order_id else ''}. Request {request_id}.")
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
    except dwp.DWPError as exc:
        print(f"Error: {exc}")
        raise SystemExit(2)
    except (KeyError, IndexError, TypeError):
        print("Error: DWP returned an incomplete or unexpected response.")
        raise SystemExit(2)
    except Exception:
        print("Error: An unexpected problem occurred. Re-run with --verbose and report the step shown before it.")
        raise SystemExit(2)
