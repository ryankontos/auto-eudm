#!/usr/bin/env python3
"""Automate one Macquarie DWP device-management request.

Normal mode talks to DWP using either DWP_COOKIE or a dedicated Chrome profile.
It creates and populates a request, but only submits the final order with
--submit. Creating/populating a non-submitted request is still a real DWP
server-side change.

Use --simulate to exercise the same validation and questionnaire path locally.
Simulation never starts Chrome, reads cookies, reaches DWP, or changes data.
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
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


DEFAULT_BASE = "https://macquarie-dwp.onbmc.com/dwp/rest"
DEFAULT_BROWSER_PROFILE = "~/.dwp-device-request-chrome"


class DWPError(RuntimeError):
    pass


class DeploymentExecutionError(DWPError):
    def __init__(self, request_id: str, message: str) -> None:
        super().__init__(message)
        self.request_id = request_id


@dataclass(frozen=True)
class DeploymentResult:
    request_id: str
    order_id: str | None


def http_error_message(status: int, action: str) -> str:
    messages = {
        400: "The service rejected the request data.",
        401: "Authentication was rejected. Refresh the DWP login and try again.",
        403: "Your account is not allowed to perform this request.",
        404: "The DWP endpoint or questionnaire field was not found.",
        409: "DWP reported a conflict with this request.",
        422: "DWP rejected one of the selected values.",
    }
    if status >= 500:
        detail = "The DWP service is temporarily unavailable."
    else:
        detail = messages.get(status, f"DWP returned HTTP {status}.")
    return f"{action}: {detail}"


def is_sso_html(raw: str) -> bool:
    sample = raw[:4000].casefold()
    return "single sign on" in sample or "redirecting to single sign" in sample


def request_step(
    client: Any, action: str, method: str, path: str, payload: Any | None = None
) -> Any:
    try:
        return client.request(method, path, payload)
    except DWPError as exc:
        raise DWPError(f"{action}: {exc}") from exc


def verbose_detail(client: Any, message: str) -> None:
    """Show implementation-level progress only when the caller asks for it."""
    if getattr(client, "verbose", False):
        print(message, file=sys.stderr)


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
        try:
            response = self.context.request.fetch(
                url, method=method, headers=headers, data=body, fail_on_status_code=False
            )
        except Exception as exc:
            raise DWPError(
                "Could not reach DWP from the authenticated Chrome session. "
                "Check the network connection and try again."
            ) from exc
        raw = response.text()
        if self.verbose:
            print(f"{method} {path} -> {response.status}", file=sys.stderr)
        if response.status >= 400:
            if response.status in (401, 403) and is_sso_html(raw):
                raise DWPError(
                    "The Chrome session is not authenticated. Complete SSO in the DWP window, "
                    "then press Enter to continue."
                )
            raise DWPError(http_error_message(response.status, f"DWP request {method} {path}"))
        if not raw:
            return None
        if is_sso_html(raw):
            raise DWPError(
                "DWP redirected to SSO. Refresh the login and provide a current "
                "authenticated Chrome session or cookie."
            )
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DWPError(f"{method} {path} returned non-JSON data") from exc


class SimulationClient:
    """Small in-memory implementation of the DWP paths used by these CLIs."""

    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose
        self.request_number = 0
        self.order_number = 0

    @staticmethod
    def _item(
        type_: str,
        id_: str,
        label: str,
        options: list[tuple[str, str]] | None = None,
    ) -> dict[str, Any]:
        item: dict[str, Any] = {"type": type_, "id": id_, "label": label}
        if options is not None:
            item["options"] = [
                {"dataValue": data, "displayValue": display}
                for data, display in options
            ]
        return item

    def questionnaire(self) -> dict[str, Any]:
        status_options = [
            ("Deployed - Existing Stock", "Deployed - Existing Stock"),
            ("Deployed - New Stock", "Deployed - New Stock"),
            ("Loan", "Deployed - Loan"),
            ("Pending Return", "Deployed - Pending Return"),
            ("New Stock", "New Stock"),
            ("Used Stock", "Used Stock"),
            ("Pending Pickup", "Pending Pickup"),
        ]
        page_items = [
            self._item(
                "RadioButtons",
                "inventory-type",
                "Inventory Request Type",
                [("ADD", "Search Full devices Inventory"), ("BULK", "BULK by Serial Number")],
            ),
            self._item("TextArea", "serial-list", "Please add serial number list"),
            self._item(
                "RadioButtons",
                "search-by",
                "Search by",
                [("serial", "Hostname/Serial Number"), ("userid", "User ID or Full Name")],
            ),
            self._item("TextField", "serial-search", "Type Hostname or Serial Number"),
            self._item("DataTable", "device-list", "----- Device List"),
            self._item("MultiSelectDataTable", "bulk-assets", "Select Asset"),
            self._item("Dropdown", "status", "Change Status to", status_options),
            self._item(
                "DataTable",
                "deployed-user",
                "Please select user - device has been deployed to",
            ),
            self._item(
                "Dropdown",
                "city",
                "Building Location (City - Country code)",
                [
                    ("Sydney, AU", "Sydney, AU"),
                    ("Melbourne, AU", "Melbourne, AU"),
                    ("London, UK", "London, UK"),
                ],
            ),
            self._item("DataTable", "location", "Please select location"),
            self._item(
                "RadioButtons",
                "is-return",
                "Is this a return from a user",
                [("YES", "Yes"), ("NO", "No")],
            ),
            self._item("YesNo", "add-dropoff", "Add Name of person who dropped off device"),
            self._item(
                "TextField",
                "dropoff-search",
                "Search Name or User ID that dropped off devices",
            ),
            self._item(
                "DataTable",
                "dropoff-user",
                "Select person who dropped device/s off",
            ),
        ]
        return {"id": "SIM-QUESTIONNAIRE", "pages": [{"pageItems": page_items}]}

    @staticmethod
    def _event(question_id: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            question_id: [
                {
                    "type": "ListOptionsChange",
                    "questionMultiColumn": {"data": rows},
                }
            ]
        }

    @staticmethod
    def _locations(city: str) -> list[dict[str, Any]]:
        locations = {
            "Sydney, AU": [
                ("SIM-LOC-SYD-15", ["1 Elizabeth Street", "Level 15", "Store Room"]),
                ("SIM-LOC-SYD-10", ["1 Elizabeth Street", "Level 10", "TA Storage"]),
                ("SIM-LOC-SYD-50MP", ["50 Martin Place", "Level 05", "Hardware Room"]),
            ],
            "Melbourne, AU": [
                ("SIM-LOC-MEL", ["Simulation Building", "Level 01", "IT Store"]),
            ],
            "London, UK": [
                ("SIM-LOC-LON", ["28 Ropemaker Street", "Level 08", "Build Room"]),
            ],
        }
        return [
            {"dataValue": data_value, "displayValue": display}
            for data_value, display in locations.get(city, [])
        ]

    def request(self, method: str, path: str, payload: Any | None = None) -> Any:
        if self.verbose:
            print(f"SIMULATE {method} {path}", file=sys.stderr)
        if method == "POST" and path == "v2/sbe/services/requests":
            self.request_number += 1
            return {"requests": [{"requestId": f"SIM-REQ-{self.request_number:04d}"}]}
        if method == "GET" and path.endswith(
            "/questionnaire?timezoneId=Australia/Sydney"
        ):
            return {"questionnaire": self.questionnaire()}
        if method == "POST" and path.endswith("/lookup"):
            query = str((payload or {}).get("query", "")).strip() or "simulated.user"
            return {
                "multiColumnOptions": [
                    {
                        "dataValue": f"SIM-USER:{query}",
                        "displayValue": [query, "Simulated User", query],
                    }
                ]
            }
        if method == "POST" and path.endswith("/questionnaire/answers"):
            question_id = str((payload or {}).get("questionId", ""))
            answers = (payload or {}).get("answers") or []
            value = str(answers[0]) if answers else ""
            if question_id == "serial-search":
                return self._event(
                    "device-list",
                    [{"dataValue": f"SIM-ASSET:{value}", "displayValue": [value, value]}],
                )
            if question_id == "serial-list":
                serials = [serial.strip() for serial in value.split(",") if serial.strip()]
                return self._event(
                    "bulk-assets",
                    [
                        {
                            "dataValue": f"SIM-ASSET:{serial}",
                            "displayValue": [serial, serial],
                        }
                        for serial in serials
                    ],
                )
            if question_id == "city":
                return self._event("location", self._locations(value))
            if question_id == "dropoff-search":
                return self._event(
                    "dropoff-user",
                    [
                        {
                            "dataValue": f"SIM-USER:{value}",
                            "displayValue": [value, "Simulated User", value],
                        }
                    ],
                )
            return {}
        if method == "POST" and path == "v2/sbe/orders":
            self.order_number += 1
            return {"id": f"SIM-ORDER-{self.order_number:04d}"}
        raise DWPError(f"Simulation does not implement {method} {path}.")


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
        raise DWPError(
            "Could not start Chrome or complete browser authentication. "
            "Check that Google Chrome and Playwright are installed."
        ) from exc
    # Keep the browser context alive for every API call; do not extract/replay cookies.
    atexit.register(playwright.stop)
    atexit.register(context.close)
    return BrowserClient(base, context, verbose)


def open_client(
    *,
    base: str = DEFAULT_BASE,
    browser_profile: str | None = None,
    simulate: bool = False,
    verbose: bool = False,
) -> Any:
    """Open one authenticated client that can be reused for many requests."""
    if simulate:
        print("Simulation mode: no browser, authentication, network, or DWP data will be used.")
        return SimulationClient(verbose)
    if browser_profile:
        return browser_client_from_profile(
            browser_profile,
            base.split("/rest", 1)[0] + "/app/",
            base,
            verbose,
        )
    cookie = os.getenv("DWP_COOKIE", "").strip()
    if cookie.lower().startswith("cookie:"):
        cookie = cookie.split(":", 1)[1].strip()
    if not cookie:
        raise DWPError(
            "Set DWP_COOKIE or use --browser-profile for an authenticated Chrome session"
        )
    return Client(base, cookie, verbose)


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
            raise DWPError(
                "The system curl transport could not reach DWP. "
                "Check the network connection or corporate certificate setup."
            )
        raw = result.stdout.decode("utf-8", errors="replace")
        marker = "\n__DWP_HTTP_STATUS:"
        if marker not in raw:
            raise DWPError("The system curl transport returned an invalid response.")
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
                    "DWP redirected to SSO. Refresh the browser login and provide a "
                    "current DWP cookie, or use --browser-profile."
                ) from exc
            raise DWPError(http_error_message(exc.code, f"DWP request {method} {path}")) from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            cert_error = isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(reason)
            if not cert_error:
                raise DWPError(
                    f"DWP request {method} {path} could not connect. "
                    "Check the network connection and try again."
                ) from exc
            if self.verbose:
                print("Python TLS validation failed; retrying with system curl trust store", file=sys.stderr)
            status, raw = self._curl_request(method, url, body, headers)
        if self.verbose:
            print(f"{method} {path} -> {status}", file=sys.stderr)
        if status >= 400:
            raise DWPError(http_error_message(status, f"DWP request {method} {path}"))
        if not raw:
            return None
        if is_sso_html(raw):
            raise DWPError(
                "DWP redirected to SSO. Refresh the login and provide a current "
                "authenticated Chrome session or cookie."
            )
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DWPError(f"{method} {path} returned non-JSON data") from exc


def items(questionnaire: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for page in questionnaire["pages"] for item in page["pageItems"]]


def field_by_label(all_items: list[dict[str, Any]], label: str, *, type_: str | None = None) -> dict[str, Any]:
    matches = [x for x in all_items if x.get("label") == label and (type_ is None or x.get("type") == type_)]
    if not matches:
        raise DWPError(f"The current questionnaire is missing the expected field {label!r}.")
    return matches[0]


def answer_values(
    client: Any,
    request_id: str,
    questionnaire_id: str,
    item: dict[str, Any],
    values: list[Any],
) -> dict[str, Any]:
    payload = {
        "serviceRequestId": request_id,
        "questionnaireId": questionnaire_id,
        "questionId": item["id"],
        "answers": values,
    }
    label = item.get("label") or "this field"
    try:
        result = client.request("POST", f"v2/sbe/services/{request_id}/questionnaire/answers", payload)
    except DWPError as exc:
        raise DWPError(f"Could not set {label!r}: {exc}") from exc
    if item.get("type") == "MultiSelectDataTable":
        verbose_detail(client, f"{label}: selected {len(values)} asset(s)")
    elif item.get("type") == "DataTable":
        verbose_detail(client, f"{label}: selected")
    else:
        verbose_detail(client, f"{label}: {values if len(values) != 1 else values[0]}")
    return result or {}


def answer(client: Any, request_id: str, questionnaire_id: str, item: dict[str, Any], value: Any) -> dict[str, Any]:
    return answer_values(client, request_id, questionnaire_id, item, [value])


def option_data(events: dict[str, Any], question_id: str) -> list[dict[str, Any]]:
    changes = events.get(question_id, [])
    for change in reversed(changes):
        if change.get("type") == "ListOptionsChange":
            return change.get("questionMultiColumn", {}).get("data", [])
    return []


def merge_events(*event_maps: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for event_map in event_maps:
        for question_id, changes in event_map.items():
            merged.setdefault(question_id, []).extend(changes)
    return merged


def choose_data_value(rows: list[dict[str, Any]], needle: str, *, exact: bool = True) -> str:
    needle_l = needle.casefold()
    for row in rows:
        values = [str(v) for v in row.get("displayValue", [])]
        if any((needle_l == v.casefold() if exact else needle_l in v.casefold()) for v in values):
            return row["dataValue"]
    raise DWPError(f"No returned option matched {needle!r}.")


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
    target = " --> ".join(expected)
    if not matches:
        raise DWPError(f"No returned location matched {target!r}.")
    raise DWPError(
        f"More than one location matched {target!r}; add --cabinet to identify one row."
    )


def batch_asset_selection(
    all_items: list[dict[str, Any]], events: dict[str, Any], serials: list[str]
) -> tuple[dict[str, Any], list[str]]:
    """Find the live bulk asset table and match every requested serial."""
    tables_by_id = {
        item["id"]: item
        for item in all_items
        if item.get("type") == "MultiSelectDataTable" and item.get("label") == "Select Asset"
    }
    candidates = [
        (tables_by_id[question_id], option_data(events, question_id))
        for question_id in tables_by_id
        if option_data(events, question_id)
    ]
    if len(candidates) != 1:
        raise DWPError(
            "Bulk serial entry did not expose exactly one populated 'Select Asset' table; "
            f"found {len(candidates)}"
        )
    table, rows = candidates[0]
    selected: list[str] = []
    missing: list[str] = []
    for serial in serials:
        matches = [
            row for row in rows
            if any(str(value).casefold() == serial.casefold() for value in row.get("displayValue", []))
        ]
        if len(matches) == 1:
            selected.append(matches[0]["dataValue"])
        else:
            missing.append(serial)
    if missing:
        raise DWPError(
            "The bulk search did not uniquely match these serials: " + ", ".join(missing)
        )
    return table, selected


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
    try:
        result = client.request(
            "POST",
            f"v2/sbe/services/requests/{request_id}/questions/{search['id']}/lookup",
            {"query": query},
        ) or {}
    except DWPError as exc:
        raise DWPError(f"Could not search {search_label!r}: {exc}") from exc
    rows = result.get("multiColumnOptions") or []
    if not rows:
        raise DWPError(f"No users matched {query!r}.")
    value = choose_data_value(
        [{"dataValue": row["dataValue"], "displayValue": row.get("displayValue", [])} for row in rows],
        match,
        exact=True,
    )
    answer(client, request_id, questionnaire_id, table, value)


def deploy_device_to_user(
    client: Any,
    *,
    serial: str,
    request_for: str,
    deployed_to: str,
    status: str,
    submit: bool = True,
) -> DeploymentResult:
    """Populate and optionally submit one user deployment request."""
    for label, value in (
        ("serial number", serial),
        ("request-for login ID", request_for),
        ("deployed-to login ID", deployed_to),
        ("status", status),
    ):
        if not value or value != value.strip():
            raise DWPError(f"The {label} cannot be empty or have surrounding whitespace.")
    if any(character.isspace() for character in serial):
        raise DWPError(f"Serial number {serial!r} cannot contain whitespace.")

    created = request_step(
        client,
        "Could not create the DWP request",
        "POST",
        "v2/sbe/services/requests",
        {"serviceId": "25301", "quantity": 1, "requestedForLoginIds": [request_for]},
    )
    request_id = str(created["requests"][0]["requestId"])
    verbose_detail(client, f"Created request {request_id}.")
    try:
        return _complete_user_deployment(
            client,
            request_id=request_id,
            serial=serial,
            deployed_to=deployed_to,
            status=status,
            submit=submit,
        )
    except DWPError as exc:
        raise DeploymentExecutionError(request_id, str(exc)) from exc


def _complete_user_deployment(
    client: Any,
    *,
    request_id: str,
    serial: str,
    deployed_to: str,
    status: str,
    submit: bool,
) -> DeploymentResult:
    questionnaire = request_step(
        client,
        "Could not load the current questionnaire",
        "GET",
        f"v2/sbe/services/requests/{request_id}/questionnaire?timezoneId=Australia/Sydney",
    )["questionnaire"]
    questionnaire_id = str(questionnaire["id"])
    all_items = items(questionnaire)

    inventory = field_by_label(all_items, "Inventory Request Type", type_="RadioButtons")
    answer(client, request_id, questionnaire_id, inventory, "ADD")
    search_by = field_by_label(all_items, "Search by", type_="RadioButtons")
    answer(client, request_id, questionnaire_id, search_by, "serial")
    serial_field = field_by_label(
        all_items, "Type Hostname or Serial Number", type_="TextField"
    )
    events = answer(client, request_id, questionnaire_id, serial_field, serial)
    device_table = field_by_label(all_items, "----- Device List", type_="DataTable")
    devices = option_data(events, device_table["id"])
    device_value = choose_data_value(devices, serial, exact=True)
    answer(client, request_id, questionnaire_id, device_table, device_value)

    status_item = field_by_label(all_items, "Change Status to", type_="Dropdown")
    answer(client, request_id, questionnaire_id, status_item, status)
    lookup_and_answer(
        client,
        request_id,
        questionnaire_id,
        all_items,
        "Please select user - device has been deployed to",
        "Please select user - device has been deployed to",
        deployed_to,
        deployed_to,
        search_type="DataTable",
    )
    if not submit:
        verbose_detail(client, f"Request {request_id} is populated but not submitted.")
        return DeploymentResult(request_id=request_id, order_id=None)

    order = request_step(
        client,
        "Could not submit the order",
        "POST",
        "v2/sbe/orders",
        {"requestIds": [request_id], "title": None},
    )
    order_id = str(order["id"]) if isinstance(order, dict) and order.get("id") else None
    verbose_detail(
        client,
        f"Submitted request {request_id}{f' (order {order_id})' if order_id else ''}.",
    )
    return DeploymentResult(request_id=request_id, order_id=order_id)


def validate_args(args: argparse.Namespace) -> bool:
    """Reject contradictory inputs before browser startup or any API request."""
    for name in ("request_for", "status"):
        value = getattr(args, name)
        if not value or not value.strip():
            raise DWPError(f"--{name.replace('_', '-')} cannot be empty")
        if value != value.strip():
            raise DWPError(f"--{name.replace('_', '-')} cannot begin or end with whitespace")
    if args.batch:
        if args.serial:
            raise DWPError("Batch mode uses --serials, not --serial")
        if not args.serials:
            raise DWPError("--serials is required in batch mode")
        serials = [value.strip() for value in args.serials.split(",")]
        if not serials or any(not value for value in serials):
            raise DWPError("--serials must be a comma-separated list without empty entries")
        if any(any(character.isspace() for character in value) for value in serials):
            raise DWPError("Serial numbers in --serials cannot contain whitespace")
        if len({value.casefold() for value in serials}) != len(serials):
            raise DWPError("--serials cannot contain duplicates")
        args.batch_serials = serials
    else:
        if args.serials:
            raise DWPError("--serials requires --batch")
        if not args.serial or not args.serial.strip():
            raise DWPError("--serial is required outside batch mode")
        if args.serial != args.serial.strip() or any(character.isspace() for character in args.serial):
            raise DWPError("--serial cannot contain leading, trailing, or embedded whitespace")

    parsed_base = urllib.parse.urlparse(args.base)
    if parsed_base.scheme != "https" or not parsed_base.netloc or not parsed_base.path.rstrip("/").endswith("/rest"):
        raise DWPError("--base must be an HTTPS DWP REST URL ending in /rest")

    user_deployment = args.target == "user"
    location_names = ("city", "building", "floor", "room", "cabinet", "dropped_by")
    if user_deployment:
        if args.batch:
            raise DWPError("Batch mode supports --target location only")
        if not args.deployed_to or not args.deployed_to.strip():
            raise DWPError("--deployed-to is required when --target user")
        conflicting = [f"--{name.replace('_', '-')}" for name in location_names if getattr(args, name)]
        if conflicting:
            raise DWPError(
                "User deployment cannot use location arguments: " + ", ".join(conflicting)
            )
    else:
        if args.status.startswith("Deployed - "):
            raise DWPError(
                "A 'Deployed - ...' status is a user deployment; use --target user"
            )
        if args.deployed_to:
            raise DWPError("Location deployment cannot use --deployed-to; use --dropped-by")
        required = ("city", "building", "floor", "room")
        if not args.batch:
            required += ("dropped_by",)
        missing = [f"--{name.replace('_', '-')}" for name in required if not getattr(args, name)]
        if missing:
            raise DWPError("Location deployment requires: " + ", ".join(missing))
        for name in required:
            if not getattr(args, name).strip():
                raise DWPError(f"--{name.replace('_', '-')} cannot be empty")
        if args.cabinet is not None and not args.cabinet.strip():
            raise DWPError("--cabinet cannot be empty when supplied")
        if args.batch and args.dropped_by:
            raise DWPError("Batch location mode does not accept --dropped-by")

    if (
        not getattr(args, "simulate", False)
        and args.browser_profile
        and os.getenv("DWP_COOKIE", "").strip()
    ):
        raise DWPError("Choose either --browser-profile or DWP_COOKIE, not both")
    return user_deployment


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Modes:
  Single user:     --serial ... --target user --status 'Deployed - New Stock' --deployed-to ...
  Single location: --serial ... --target location --status 'Used Stock' --city ... --building ... --floor ... --room ... --dropped-by ...
  Batch location:  --batch --serials 'SERIAL1,SERIAL2' --target location ... (no user fields)

Safety:
  Arguments are checked before authentication or API calls. Without --submit,
  DWP still receives a created and populated request, but no final order is sent.
  Use --simulate for a completely local rehearsal; it produces SIM-REQ IDs.
""",
    )
    parser.add_argument("--serial", help="One hostname/serial. Required unless --batch is used.")
    parser.add_argument(
        "--batch",
        action="store_true",
        help="Use DWP BULK by Serial Number for one location; rejects all user fields.",
    )
    parser.add_argument(
        "--serials",
        help="Comma-separated serials for --batch; duplicates, spaces, and empty entries are rejected.",
    )
    parser.add_argument("--request-for", required=True, help="Remedy login ID that requests the change.")
    parser.add_argument(
        "--target",
        required=True,
        choices=("user", "location"),
        help="Required deployment destination. This controls which mutually exclusive fields are valid.",
    )
    parser.add_argument("--city", help="Exact DWP city label for a location, for example 'Sydney, AU'.")
    parser.add_argument("--building", help="Exact building value from the returned DWP location row.")
    parser.add_argument("--floor", help="Exact floor value from the returned DWP location row, for example 'Level 15'.")
    parser.add_argument("--room", help="Exact room value from the returned DWP location row, for example 'Store Room'.")
    parser.add_argument("--cabinet", help="Exact cabinet value only when building/floor/room still match multiple locations.")
    parser.add_argument("--status", required=True, help="Exact DWP data value, for example 'Deployed - New Stock' or 'Used Stock'.")
    parser.add_argument("--deployed-to", help="Receiving login ID. Required for --target user; invalid for locations.")
    parser.add_argument("--dropped-by", help="Drop-off login ID. Required for normal locations; invalid for batch locations.")
    parser.add_argument(
        "--submit",
        action="store_true",
        help="Send the final DWP order. Without it, the request is populated but not ordered.",
    )
    parser.add_argument(
        "--browser-profile",
        help="Dedicated installed-Chrome profile for SSO; cannot be combined with DWP_COOKIE.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show questionnaire field updates, matching details, and request/status diagnostics; never prints cookies or response bodies.",
    )
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="Local in-memory rehearsal. Ignores authentication and makes no browser, network, or DWP changes.",
    )
    parser.add_argument("--base", default=os.getenv("DWP_BASE", DEFAULT_BASE), help="Override DWP REST base URL (HTTPS URL ending in /rest).")
    args = parser.parse_args()

    user_deployment = validate_args(args)

    client = open_client(
        base=args.base,
        browser_profile=args.browser_profile,
        simulate=args.simulate,
        verbose=args.verbose,
    )
    created = request_step(client, "Could not create the DWP request", "POST", "v2/sbe/services/requests", {
        "serviceId": "25301", "quantity": 1, "requestedForLoginIds": [args.request_for]
    })
    request_id = str(created["requests"][0]["requestId"])
    questionnaire = request_step(
        client,
        "Could not load the current questionnaire",
        "GET",
        f"v2/sbe/services/requests/{request_id}/questionnaire?timezoneId=Australia/Sydney",
    )["questionnaire"]
    questionnaire_id = str(questionnaire["id"])
    all_items = items(questionnaire)
    verbose_detail(client, f"Created request {request_id}; questionnaire {questionnaire_id}")

    inventory = field_by_label(all_items, "Inventory Request Type", type_="RadioButtons")
    if args.batch:
        inventory_events = answer(client, request_id, questionnaire_id, inventory, "BULK")
        serial_list = field_by_label(all_items, "Please add serial number list", type_="TextArea")
        serial_events = answer(
            client, request_id, questionnaire_id, serial_list, ",".join(args.batch_serials)
        )
        events = merge_events(inventory_events, serial_events)
        asset_table, asset_values = batch_asset_selection(all_items, events, args.batch_serials)
        answer_values(client, request_id, questionnaire_id, asset_table, asset_values)
    else:
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
            "Please select user - device has been deployed to",
            "Please select user - device has been deployed to",
            args.deployed_to, args.deployed_to,
            search_type="DataTable",
        )
        verbose_detail(client, f"Deployment target: user {args.deployed_to}")
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

        # Batch location changes have no associated user.
        returned = field_by_label(all_items, "Is this a return from a user", type_="RadioButtons")
        if args.batch:
            answer(client, request_id, questionnaire_id, returned, "NO")
        else:
            answer(client, request_id, questionnaire_id, returned, "YES")
            add_dropoff = field_by_label(all_items, "Add Name of person who dropped off device", type_="YesNo")
            answer(client, request_id, questionnaire_id, add_dropoff, "true")
            dropoff_search = field_by_label(
                all_items, "Search Name or User ID that dropped off devices", type_="TextField"
            )
            dropoff_table = field_by_label(
                all_items, "Select person who dropped device/s off", type_="DataTable"
            )
            dropoff_events = answer(
                client, request_id, questionnaire_id, dropoff_search, args.dropped_by
            )
            dropoff_rows = option_data(dropoff_events, dropoff_table["id"])
            dropoff_value = choose_data_value(dropoff_rows, args.dropped_by, exact=True)
            answer(client, request_id, questionnaire_id, dropoff_table, dropoff_value)
        location_summary = " --> ".join([args.building, args.floor, args.room])
        if args.cabinet:
            location_summary += f" --> {args.cabinet}"
        if args.batch:
            verbose_detail(
                client,
                f"Batch deployment target: location {location_summary}; "
                f"serials {', '.join(args.batch_serials)}; no user"
            )
        else:
            verbose_detail(
                client,
                f"Deployment target: location {location_summary}; dropped by {args.dropped_by}",
            )

    verbose_detail(client, "Reached the dynamic questionnaire path.")
    if not args.submit:
        print(f"Dry run: request {request_id} was created but not submitted.")
        return 0
    order = request_step(
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
        print("Cancelled.", file=sys.stderr)
        raise SystemExit(130)
    except EOFError:
        print("Input ended before the request was complete.", file=sys.stderr)
        raise SystemExit(2)
    except DWPError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2)
    except (KeyError, IndexError, TypeError):
        print("Error: DWP returned an incomplete or unexpected response.", file=sys.stderr)
        raise SystemExit(2)
    except Exception:
        print("Error: An unexpected problem occurred. Re-run with --verbose and report the step shown before it.", file=sys.stderr)
        raise SystemExit(2)
