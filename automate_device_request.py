#!/usr/bin/env python3
"""First-pass automation for the Macquarie DWP device-management request.

This deliberately stops before order submission unless --submit is supplied.
Authentication is supplied by DWP_COOKIE or an authenticated Playwright Chrome
context; credentials are never written to disk.
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
from pathlib import Path
import subprocess
import sys
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


DEFAULT_BASE = "https://macquarie-dwp.onbmc.com/dwp/rest"


class DWPError(RuntimeError):
    pass


class BrowserClient:
    """API client that stays inside the authenticated Playwright browser context."""

    def __init__(self, base: str, context: Any, verbose: bool = False) -> None:
        self.base = base
        self.context = context
        self.verbose = verbose

    def request(self, method: str, path: str, payload: Any | None = None) -> Any:
        url = self.base.rstrip("/") + "/" + path.lstrip("/")
        body = None if payload is None else json.dumps(payload)
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Origin": self.base.split("/rest", 1)[0],
            "Referer": self.base.split("/rest", 1)[0] + "/",
            "X-Requested-By": "XMLHttpRequest",
            "User-Agent": "dwp-device-request-browser/0.1",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        response = self.context.request.fetch(
            url, method=method, headers=headers, data=body, fail_on_status_code=False
        )
        raw = response.text()
        if self.verbose:
            print(f"{method} {path} -> {response.status}", file=sys.stderr)
        if response.status >= 400:
            if response.status in (401, 403) and "single sign on" in raw.lower():
                raise DWPError(
                    f"{method} {path} -> HTTP {response.status}: the browser context is not authenticated"
                )
            raise DWPError(f"{method} {path} -> HTTP {response.status}: {raw[:500]}")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DWPError(f"{method} {path} returned non-JSON data") from exc


def browser_client_from_profile(profile: str, app_url: str, base: str, verbose: bool) -> BrowserClient:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise DWPError(
            "Browser mode requires Playwright. Install it with: python3 -m pip install playwright"
        ) from exc
    try:
        playwright = sync_playwright().start()
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(Path(profile).expanduser()),
            channel="chrome",
            headless=False,
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(app_url, wait_until="domcontentloaded", timeout=60_000)
        print("Chrome opened for DWP authentication. Complete SSO in that window.")
        input("After the DWP page is signed in, press Enter here to continue: ")
    except Exception as exc:
        raise DWPError(f"Browser startup or authentication failed: {exc}") from exc
    # Keep the browser context alive for every API call; do not extract/replay cookies.
    atexit.register(context.close)
    atexit.register(playwright.stop)
    return BrowserClient(base, context, verbose)


@dataclass
class Client:
    base: str
    cookie: str
    verbose: bool = False

    def _curl_request(self, method: str, url: str, body: bytes | None, headers: dict[str, str]) -> tuple[int, str]:
        """Use the OS curl trust store when Python/OpenSSL rejects a chain."""
        config: list[str] = [f"url = {json.dumps(url)}", f"request = {json.dumps(method)}"]
        for name, value in headers.items():
            config.append(f"header = {json.dumps(f'{name}: {value}')}")
        if body is not None:
            config.append(f"data-binary = {json.dumps(body.decode('utf-8'))}")
        config.append('write-out = "\\n__DWP_HTTP_STATUS:%{http_code}"')
        result = subprocess.run(
            ["curl", "--silent", "--show-error", "--location", "--config", "-"],
            input=("\n".join(config) + "\n").encode(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
        )
        if result.returncode:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise DWPError(f"curl transport failed: {detail[:500]}")
        raw = result.stdout.decode("utf-8", errors="replace")
        marker = "\n__DWP_HTTP_STATUS:"
        if marker not in raw:
            raise DWPError("curl transport returned no HTTP status")
        content, status_text = raw.rsplit(marker, 1)
        return int(status_text.strip()), content

    def request(self, method: str, path: str, payload: Any | None = None) -> Any:
        url = self.base.rstrip("/") + "/" + path.lstrip("/")
        body = None if payload is None else json.dumps(payload).encode()
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Origin": self.base.split("/rest", 1)[0],
            "Referer": self.base.split("/rest", 1)[0] + "/",
            "X-Requested-By": "XMLHttpRequest",
            "User-Agent": "dwp-device-request-first-pass/0.1",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self.cookie:
            headers["Cookie"] = self.cookie
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=45) as response:
                raw = response.read().decode("utf-8", errors="replace")
                status = response.status
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if exc.code in (401, 403) and "single sign on" in detail.lower():
                raise DWPError(
                    f"{method} {path} -> HTTP {exc.code}: DWP redirected to SSO. "
                    "Refresh the browser login and copy the Cookie value from a "
                    "macquarie-dwp.onbmc.com /dwp/rest request (without the 'Cookie:' label)."
                ) from exc
            raise DWPError(f"{method} {path} -> HTTP {exc.code}: {detail[:500]}") from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            cert_error = isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(reason)
            if not cert_error:
                raise DWPError(f"{method} {path} failed: {reason}") from exc
            if self.verbose:
                print("Python TLS validation failed; retrying with system curl trust store", file=sys.stderr)
            status, raw = self._curl_request(method, url, body, headers)
        if self.verbose:
            print(f"{method} {path} -> {status}", file=sys.stderr)
        if status >= 400:
            raise DWPError(f"{method} {path} -> HTTP {status}: {raw[:500]}")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DWPError(f"{method} {path} returned non-JSON data") from exc


def items(questionnaire: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for page in questionnaire["pages"] for item in page["pageItems"]]


def field_by_label(all_items: list[dict[str, Any]], label: str, *, type_: str | None = None) -> dict[str, Any]:
    matches = [x for x in all_items if x.get("label") == label and (type_ is None or x.get("type") == type_)]
    if not matches:
        raise DWPError(f"Question not found: {label!r}")
    return matches[0]


def answer(client: Client, request_id: str, questionnaire_id: str, item: dict[str, Any], value: Any) -> dict[str, Any]:
    payload = {
        "serviceRequestId": request_id,
        "questionnaireId": questionnaire_id,
        "questionId": item["id"],
        "answers": [value],
    }
    result = client.request("POST", f"v2/sbe/services/{request_id}/questionnaire/answers", payload)
    print(f"{item.get('label') or item['id']}: {value}")
    return result or {}


def option_data(events: dict[str, Any], question_id: str) -> list[dict[str, Any]]:
    changes = events.get(question_id, [])
    for change in reversed(changes):
        if change.get("type") == "ListOptionsChange":
            return change.get("questionMultiColumn", {}).get("data", [])
    return []


def choose_data_value(rows: list[dict[str, Any]], needle: str, *, exact: bool = True) -> str:
    needle_l = needle.casefold()
    for row in rows:
        values = [str(v) for v in row.get("displayValue", [])]
        if any((needle_l == v.casefold() if exact else needle_l in v.casefold()) for v in values):
            return row["dataValue"]
    examples = [row.get("displayValue", []) for row in rows[:5]]
    raise DWPError(f"No dynamic option matched {needle!r}; examples: {examples}")


def choose_location_data_value(
    rows: list[dict[str, Any]],
    building: str,
    floor: str,
    room: str,
    cabinet: str | None = None,
) -> str:
    """Select one exact Building/Floor/Room[/Cabinet] row from the city results."""
    expected = [building, floor, room]
    if cabinet is not None:
        expected.append(cabinet)
    matches = []
    for row in rows:
        displayed = [str(value) for value in row.get("displayValue", [])]
        if len(displayed) >= len(expected) and all(
            actual.casefold() == wanted.casefold()
            for actual, wanted in zip(displayed, expected)
        ):
            matches.append(row)
    if len(matches) == 1:
        return matches[0]["dataValue"]
    examples = [row.get("displayValue", []) for row in rows[:12]]
    target = " --> ".join(expected)
    if not matches:
        raise DWPError(f"No location matched {target!r}; available rows: {examples}")
    raise DWPError(
        f"More than one location matched {target!r}; add --cabinet to identify one row: "
        f"{[row.get('displayValue', []) for row in matches]}"
    )


def lookup_and_answer(
    client: Client,
    request_id: str,
    questionnaire_id: str,
    all_items: list[dict[str, Any]],
    search_label: str,
    table_label: str,
    query: str,
    match: str,
    search_type: str = "TextField",
) -> None:
    """Search a server-backed person table, then persist the selected dataValue."""
    search = field_by_label(all_items, search_label, type_=search_type)
    table = field_by_label(all_items, table_label, type_="DataTable")
    result = client.request(
        "POST",
        f"v2/sbe/services/requests/{request_id}/questions/{search['id']}/lookup",
        {"query": query},
    ) or {}
    rows = result.get("multiColumnOptions") or []
    if not rows:
        raise DWPError(f"No lookup results for {query!r} in {search_label!r}")
    value = choose_data_value(
        [{"dataValue": row["dataValue"], "displayValue": row.get("displayValue", [])} for row in rows],
        match,
        exact=True,
    )
    answer(client, request_id, questionnaire_id, table, value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True, help="Hostname or serial number")
    parser.add_argument("--request-for", required=True, help="Remedy login ID requesting the change")
    parser.add_argument("--city", help="City filter label for location deployment, e.g. 'Sydney, AU'")
    parser.add_argument("--building", help="Exact building from the returned location row")
    parser.add_argument("--floor", help="Exact floor from the returned location row, e.g. 'Level 15'")
    parser.add_argument("--room", help="Exact room from the returned location row, e.g. 'Store Room'")
    parser.add_argument("--cabinet", help="Exact cabinet when multiple rows share building, floor, and room")
    parser.add_argument("--status", required=True, help="Exact status label, e.g. 'Deployed - New Stock'")
    parser.add_argument("--deployed-to", help="Login ID of the user receiving a user deployment")
    parser.add_argument("--dropped-by", help="Login ID of the user who dropped off a location deployment")
    parser.add_argument("--submit", action="store_true", help="Commit the order; otherwise print a dry-run summary")
    parser.add_argument(
        "--browser-profile",
        help="Use a dedicated Chrome profile for SSO and extract DWP cookies automatically",
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--base", default=os.getenv("DWP_BASE", DEFAULT_BASE))
    args = parser.parse_args()

    if args.browser_profile:
        client = browser_client_from_profile(
            args.browser_profile, args.base.split("/rest", 1)[0] + "/app/", args.base, args.verbose
        )
    else:
        cookie = os.getenv("DWP_COOKIE", "").strip()
        if cookie.lower().startswith("cookie:"):
            cookie = cookie.split(":", 1)[1].strip()
        if not cookie:
            raise DWPError("Set DWP_COOKIE or use --browser-profile for an authenticated Chrome session")
        client = Client(args.base, cookie, args.verbose)
    user_deployment = args.status.startswith("Deployed - ")
    if user_deployment and not args.deployed_to:
        raise DWPError("--deployed-to is required for a 'Deployed - ...' status")
    if not user_deployment and (
        not args.city or not args.building or not args.floor or not args.room or not args.dropped_by
    ):
        raise DWPError(
            "--city, --building, --floor, --room, and --dropped-by are required for a location status"
        )

    created = client.request("POST", "v2/sbe/services/requests", {
        "serviceId": "25301", "quantity": 1, "requestedForLoginIds": [args.request_for]
    })
    request_id = str(created["requests"][0]["requestId"])
    questionnaire = client.request("GET", f"v2/sbe/services/requests/{request_id}/questionnaire?timezoneId=Australia/Sydney")["questionnaire"]
    questionnaire_id = str(questionnaire["id"])
    all_items = items(questionnaire)
    print(f"Created request {request_id}; questionnaire {questionnaire_id}")

    inventory = field_by_label(all_items, "Inventory Request Type", type_="RadioButtons")
    answer(client, request_id, questionnaire_id, inventory, "ADD")
    search_by = field_by_label(all_items, "Search by", type_="RadioButtons")
    answer(client, request_id, questionnaire_id, search_by, "serial")
    serial_field = field_by_label(all_items, "Type Hostname or Serial Number", type_="TextField")
    events = answer(client, request_id, questionnaire_id, serial_field, args.serial)
    device_table = field_by_label(all_items, "----- Device List", type_="DataTable")
    devices = option_data(events, device_table["id"])
    device_value = choose_data_value(devices, args.serial, exact=True)
    answer(client, request_id, questionnaire_id, device_table, device_value)

    status = field_by_label(all_items, "Change Status to", type_="Dropdown")
    answer(client, request_id, questionnaire_id, status, args.status)

    if user_deployment:
        # User deployments expose a server-backed "deployed to" table.
        lookup_and_answer(
            client, request_id, questionnaire_id, all_items,
            "Type User ID or Full Name",
            "Please select user - device has been deployed to",
            args.deployed_to, args.deployed_to,
            search_type="DataTable",
        )
        print(f"Deployment target: user {args.deployed_to}")
    else:
        city = field_by_label(all_items, "Building Location (City - Country code)", type_="Dropdown")
        events = answer(client, request_id, questionnaire_id, city, args.city)
        location_table = field_by_label(all_items, "Please select location", type_="DataTable")
        locations = option_data(events, location_table["id"])
        if not locations:
            raise DWPError("The city selection did not return any selectable locations")
        location_value = choose_location_data_value(
            locations, args.building, args.floor, args.room, args.cabinet
        )
        answer(client, request_id, questionnaire_id, location_table, location_value)

        # Location deployments require the return toggle before the drop-off user list appears.
        returned = field_by_label(all_items, "Is this a return from a user", type_="RadioButtons")
        answer(client, request_id, questionnaire_id, returned, "YES")
        add_dropoff = field_by_label(all_items, "Add Name of person who dropped off device", type_="YesNo")
        answer(client, request_id, questionnaire_id, add_dropoff, "true")
        lookup_and_answer(
            client, request_id, questionnaire_id, all_items,
            "Search Name or User ID that dropped off devices",
            "Select person who dropped device/s off",
            args.dropped_by, args.dropped_by,
        )
        location_summary = " --> ".join([args.building, args.floor, args.room])
        if args.cabinet:
            location_summary += f" --> {args.cabinet}"
        print(f"Deployment target: location {location_summary}; dropped by {args.dropped_by}")

    print("\nReached the dynamic questionnaire path.")
    if not args.submit:
        print(f"Dry run: request {request_id} was created but not submitted.")
        return 0
    order = client.request("POST", "v2/sbe/orders", {"requestIds": [request_id], "title": None})
    print(json.dumps(order, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (DWPError, KeyError, IndexError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
