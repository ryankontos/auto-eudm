#!/usr/bin/env python3
"""Automate one Macquarie EUDM device-management request.

Normal mode talks to EUDM using either EUDM_COOKIE or a dedicated Chrome profile.
It creates and populates a request, but only submits the final order with
--submit. Creating/populating a non-submitted request is still a real EUDM
server-side change.

Use --simulate to exercise the same validation and questionnaire path locally.
Simulation never starts Chrome, reads cookies, reaches EUDM, or changes data.
"""

from __future__ import annotations

import argparse
import atexit
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .bootstrap import browser_runtime_required, ensure_runtime
from .eudm_config import AppConfig
from .identifiers import is_login_id, is_serial
from . import run_reporting
from . import presentation


DEFAULT_BASE = "https://macquarie-dwp.onbmc.com/dwp/rest"
DEFAULT_BROWSER_PROFILE = "~/.auto-eudm-chrome"


class EUDMError(RuntimeError):
    pass


class SSOExpiredError(EUDMError):
    """EUDM responded with its SSO page instead of the requested API data."""


def is_sso_expired_error(exc: BaseException) -> bool:
    """Preserve SSO-expiry detection through the contextual error wrappers."""
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        if isinstance(current, SSOExpiredError):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


class MatchError(EUDMError):
    """A search completed successfully but did not identify one record."""


class MatchSkipped(EUDMError):
    """The operator chose not to continue after a failed or ambiguous match."""


class DeploymentExecutionError(EUDMError):
    def __init__(self, request_id: str, message: str) -> None:
        super().__init__(message)
        self.request_id = request_id


@dataclass(frozen=True)
class DeploymentResult:
    request_id: str
    order_id: str | None
    submitted: bool = False
    not_submitted_reason: str | None = None
    resolved_serial: str | None = None
    resolved_username: str | None = None


PROMPT_LOCK = threading.Lock()


def http_error_message(status: int, action: str) -> str:
    messages = {
        400: "The service rejected the request data.",
        401: "Authentication was rejected. Refresh the Helix login and try again.",
        403: "Your account is not allowed to perform this request.",
        404: "The Helix endpoint or questionnaire field was not found.",
        409: "Helix reported a conflict with this request.",
        422: "Helix rejected one of the selected values.",
    }
    if status >= 500:
        detail = "The Helix service is temporarily unavailable."
    else:
        detail = messages.get(status, f"Helix returned HTTP {status}.")
    return f"{action}: {detail}"


def is_sso_html(raw: str) -> bool:
    sample = raw[:4000].casefold()
    return "single sign on" in sample or "redirecting to single sign" in sample


def request_step(
    client: Any, action: str, method: str, path: str, payload: Any | None = None
) -> Any:
    try:
        return client.request(method, path, payload)
    except EUDMError as exc:
        raise EUDMError(f"{action}: {exc}") from exc


def verbose_detail(client: Any, message: str) -> None:
    """Show implementation-level progress only when the caller asks for it."""
    if getattr(client, "verbose", False):
        print(message, file=sys.stderr)
    run_reporting.event("%s", message)


def retry_or_skip(kind: str, value: str, error: MatchError) -> str:
    """Explain an unsuccessful exact match and obtain a replacement or skip.

    Match correction is intentionally independent of --manual-review: selecting
    a wrong asset or person must never be an automatic outcome.
    """
    with PROMPT_LOCK:
        print(f"\nCould not uniquely match {kind} {value!r}.")
        print(f"  {error}")
        while True:
            replacement = input(f"Enter a different {kind}, or type 'skip': ").strip()
            if replacement.casefold() == "skip":
                raise MatchSkipped(f"Skipped because {kind} {value!r} was not uniquely matched.")
            if replacement:
                return replacement
            print(f"Enter a {kind} or type 'skip'.")


def manual_review(
    *,
    request_id: str,
    request_for: str,
    serials: list[str],
    status: str,
    target: str,
    destination: str,
    detail: str | None = None,
) -> bool:
    """Display the populated values and require an explicit final approval."""
    print("\nReview before final submission")
    print(f"  Request: {request_id}")
    print(f"  Request for: {request_for}")
    print(f"  Device{'s' if len(serials) != 1 else ''}: {', '.join(serials)}")
    print(f"  Change status to: {status}")
    print(f"  Deploy to: {target} — {destination}")
    if detail:
        print(f"  Details: {detail}")
    while True:
        response = input("Submit this populated request? [y/N]: ").strip().casefold()
        if not response or response in ("n", "no"):
            return False
        if response in ("y", "yes"):
            return True
        print("Enter y or n.")


class BrowserClient:
    """API client that stays inside the authenticated Playwright browser context."""

    def __init__(self, base: str, context: Any, verbose: bool = False) -> None:
        self.base = base
        self.context = context
        self.verbose = verbose

    def request(self, method: str, path: str, payload: Any | None = None) -> Any:
        started = time.monotonic()
        if path.startswith("/"):
            parsed = urllib.parse.urlsplit(self.base)
            url = urllib.parse.urlunsplit(
                (parsed.scheme, parsed.netloc, path, "", "")
            )
        else:
            url = self.base.rstrip("/") + "/" + path.lstrip("/")
        body = None if payload is None else json.dumps(payload)
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Origin": self.base.split("/rest", 1)[0],
            "Referer": self.base.split("/rest", 1)[0] + "/",
            "X-Requested-By": "XMLHttpRequest",
            "User-Agent": "auto-eudm/1.0",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            response = self.context.request.fetch(
                url, method=method, headers=headers, data=body, fail_on_status_code=False
            )
        except Exception as exc:
            run_reporting.network(
                method, path, duration_ms=int((time.monotonic() - started) * 1000),
                transport="browser", error=type(exc).__name__,
            )
            raise EUDMError(
                "Could not reach Helix from the authenticated Chrome session. "
                "Check the network connection and try again."
            ) from exc
        raw = response.text()
        run_reporting.network(
            method, path, status=response.status,
            duration_ms=int((time.monotonic() - started) * 1000), transport="browser",
        )
        if self.verbose:
            print(f"{method} {path} -> {response.status}", file=sys.stderr)
        if response.status >= 400:
            if response.status in (401, 403) and is_sso_html(raw):
                raise SSOExpiredError(
                    "Helix redirected to SSO. Reconnect and complete sign-in in Chrome."
                )
            raise EUDMError(http_error_message(response.status, f"Helix request {method} {path}"))
        if not raw:
            return None
        if is_sso_html(raw):
            raise SSOExpiredError(
                "Helix redirected to SSO. Reconnect and complete sign-in in Chrome."
            )
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise EUDMError(f"{method} {path} returned non-JSON data") from exc

    def parallel_clients(self, count: int) -> list["Client"]:
        """Create short-lived in-memory HTTP clients from the signed-in Chrome session."""
        host = urllib.parse.urlparse(self.base).hostname or ""
        cookies = [
            item for item in self.context.cookies()
            if host == item.get("domain", "").lstrip(".")
            or host.endswith(item.get("domain", "").lstrip("."))
        ]
        header = "; ".join(f"{item['name']}={item['value']}" for item in cookies)
        if not header:
            raise EUDMError("The authenticated Chrome session did not provide any Helix cookies.")
        run_reporting.event("Prepared %d in-memory clients from authenticated Chrome session", count)
        return [Client(self.base, header, self.verbose) for _ in range(count)]


class SimulationClient:
    """Small in-memory implementation of the EUDM paths used by these CLIs."""

    # The real EUDM directory/device searches typically take a few seconds.
    # Keeping that latency in the simulator makes the UI's loading states and
    # debounce behaviour useful to exercise on a laptop without EUDM access.
    SIMULATED_LOOKUP_DELAY_SECONDS = 3.5

    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose
        self.request_number = 0
        self.order_number = 0
        self._lock = threading.Lock()

    def parallel_clients(self, count: int) -> list["SimulationClient"]:
        return [self] * count

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
            self._item(
                "RadioButtons",
                "return-confirmed",
                "Does this look right?",
                [("YES", "Yes"), ("NO", "No")],
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
        started = time.monotonic()
        is_lookup_request = (
            method == "POST"
            and (
                path.endswith("/lookup")
                or (
                    path.endswith("/questionnaire/answers")
                    and str((payload or {}).get("questionId", ""))
                    in {"serial-search", "dropoff-search"}
                )
            )
        )
        if is_lookup_request:
            time.sleep(self.SIMULATED_LOOKUP_DELAY_SECONDS)
        if self.verbose:
            print(f"SIMULATE {method} {path}", file=sys.stderr)
        run_reporting.network(
            method,
            path,
            status=200,
            duration_ms=round((time.monotonic() - started) * 1000),
            transport="simulation",
        )
        if method == "POST" and path == "v2/sbe/services/requests":
            with self._lock:
                self.request_number += 1
                return {"requests": [{"requestId": f"SIM-REQ-{self.request_number:04d}"}]}
        if method == "GET" and path.endswith(
            "/questionnaire?timezoneId=Australia/Sydney"
        ):
            return {"questionnaire": self.questionnaire()}
        if method == "POST" and path.endswith("/lookup"):
            query = str((payload or {}).get("query", "")).strip() or "simulated.user"
            if query.casefold() == "no.user":
                return {"multiColumnOptions": []}
            if query.casefold() == "ambiguous.user":
                return {
                    "multiColumnOptions": [
                        {
                            "dataValue": "SIM-USER:ambiguous.one",
                            "displayValue": [query, "Simulated User One", query],
                        },
                        {
                            "dataValue": "SIM-USER:ambiguous.two",
                            "displayValue": [query, "Simulated User Two", query],
                        },
                    ]
                }
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
                if value.casefold() == "no-match":
                    return self._event("device-list", [])
                if value.casefold() == "ambiguous":
                    return self._event(
                        "device-list",
                        [
                            {"dataValue": "SIM-ASSET:ambiguous-one", "displayValue": [value, "Asset One"]},
                            {"dataValue": "SIM-ASSET:ambiguous-two", "displayValue": [value, "Asset Two"]},
                        ],
                    )
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
            with self._lock:
                self.order_number += 1
                return {"id": f"SIM-ORDER-{self.order_number:04d}"}
        if method == "GET" and path.startswith("/dwp/api/v1.0/events/"):
            token = path.rsplit("/", 1)[-1]
            padded = token + "=" * (-len(token) % 4)
            try:
                decoded = base64.b64decode(padded).decode()
                event_request_id = decoded.removeprefix("REQ:")
            except (ValueError, UnicodeDecodeError):
                event_request_id = token
            return {
                "state": "active",
                "title": "End User Device Management",
                "subtitle": "In Progress",
                "updateTime": datetime.now().isoformat(timespec="seconds"),
                "type": "ORDER",
                "orderId": "SIM-ORDER",
                "requests": [
                    {
                        "displayId": event_request_id,
                        "requestId": event_request_id,
                        "status": "IN_PROGRESS",
                        "requestedFor": {
                            "loginId": "simulated.user",
                            "displayName": "Simulated User",
                        },
                    }
                ],
            }
        raise EUDMError(f"Simulation does not implement {method} {path}.")


def browser_client_from_profile(
    profile: str,
    app_url: str,
    base: str,
    verbose: bool,
    headless: bool = False,
    interactive_auth: bool = True,
) -> BrowserClient:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise EUDMError(
            "Playwright could not be loaded after automatic setup. Re-run the command; "
            "if it persists, set EUDM_LOGGING=true and share the new log file."
        ) from exc
    try:
        playwright = sync_playwright().start()
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(Path(profile).expanduser()),
            channel="chrome",
            headless=headless,
        )
        # Chrome's persistent context starts with a visible blank tab. Close
        # those startup tabs before creating the verification page; otherwise
        # the user can be left looking at an unrelated about:blank window.
        for existing_page in list(context.pages):
            if str(existing_page.url) in {"", "about:blank"}:
                try:
                    existing_page.close()
                except Exception:
                    pass
        page = context.new_page()
        run_reporting.event("Opening Chrome for EUDM SSO")
        page.goto(app_url, wait_until="domcontentloaded", timeout=60_000)
        if headless:
            print("Checking the saved Chrome SSO session in the background...")
            page.wait_for_timeout(2_000)
        elif not interactive_auth:
            print("Chrome opened for EUDM authentication.")
            page.wait_for_timeout(5_000)
        else:
            print("Chrome opened for EUDM authentication. Complete SSO in that window.")
            input("After the EUDM page is signed in, press Enter here to continue: ")
    except Exception as exc:
        run_reporting.event("Chrome/SSO setup failed: %s", type(exc).__name__)
        raise EUDMError(
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
    headless: bool = False,
    interactive_browser_auth: bool = True,
) -> Any:
    """Open one authenticated client that can be reused for many requests."""
    if simulate:
        print("Simulation mode: no browser, authentication, network, or EUDM data will be used.")
        return SimulationClient(verbose)
    if browser_profile:
        return browser_client_from_profile(
            browser_profile,
            base.split("/rest", 1)[0] + "/app/",
            base,
            verbose,
            headless,
            interactive_browser_auth,
        )
    cookie = os.getenv("EUDM_COOKIE", "").strip()
    if cookie.lower().startswith("cookie:"):
        cookie = cookie.split(":", 1)[1].strip()
    if not cookie:
        raise EUDMError(
            "Set EUDM_COOKIE or use --browser-profile for an authenticated Chrome session"
        )
    return Client(base, cookie, verbose)


@dataclass
class Client:
    base: str
    cookie: str
    verbose: bool = False

    def parallel_clients(self, count: int) -> list["Client"]:
        return [Client(self.base, self.cookie, self.verbose) for _ in range(count)]

    def _curl_request(self, method: str, url: str, body: bytes | None, headers: dict[str, str]) -> tuple[int, str]:
        """Send an EUDM request through the operating system curl trust store."""
        config: list[str] = [f"url = {json.dumps(url)}", f"request = {json.dumps(method)}"]
        for name, value in headers.items():
            config.append(f"header = {json.dumps(f'{name}: {value}')}")
        if body is not None:
            config.append(f"data-binary = {json.dumps(body.decode('utf-8'))}")
        config.append('write-out = "\\n__EUDM_HTTP_STATUS:%{http_code}"')
        result = subprocess.run(
            [
                "curl",
                "--silent",
                "--show-error",
                "--location",
                "--connect-timeout",
                "15",
                "--max-time",
                "60",
                "--config",
                "-",
            ],
            input=("\n".join(config) + "\n").encode(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
        )
        if result.returncode:
            raise EUDMError(
                "The system curl transport could not reach Helix. "
                "Check the network connection or corporate certificate setup."
            )
        raw = result.stdout.decode("utf-8", errors="replace")
        marker = "\n__EUDM_HTTP_STATUS:"
        if marker not in raw:
            raise EUDMError("The system curl transport returned an invalid response.")
        content, status_text = raw.rsplit(marker, 1)
        return int(status_text.strip()), content

    def request(self, method: str, path: str, payload: Any | None = None) -> Any:
        started = time.monotonic()
        if path.startswith("/"):
            parsed = urllib.parse.urlsplit(self.base)
            url = urllib.parse.urlunsplit(
                (parsed.scheme, parsed.netloc, path, "", "")
            )
        else:
            url = self.base.rstrip("/") + "/" + path.lstrip("/")
        body = None if payload is None else json.dumps(payload).encode()
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Origin": self.base.split("/rest", 1)[0],
            "Referer": self.base.split("/rest", 1)[0] + "/",
            "X-Requested-By": "XMLHttpRequest",
            "User-Agent": "auto-eudm/1.0",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self.cookie:
            headers["Cookie"] = self.cookie
        try:
            status, raw = self._curl_request(method, url, body, headers)
        except EUDMError:
            run_reporting.network(
                method,
                path,
                duration_ms=int((time.monotonic() - started) * 1000),
                transport="curl",
                error="transport",
            )
            raise
        run_reporting.network(
            method, path, status=status,
            duration_ms=int((time.monotonic() - started) * 1000), transport="curl",
        )
        if self.verbose:
            print(f"{method} {path} -> {status}", file=sys.stderr)
        if status >= 400:
            if status in (401, 403) and is_sso_html(raw):
                raise SSOExpiredError(
                    "Helix redirected to SSO. Reconnect and complete sign-in in Chrome."
                )
            raise EUDMError(http_error_message(status, f"Helix request {method} {path}"))
        if not raw:
            return None
        if is_sso_html(raw):
            raise SSOExpiredError(
                "Helix redirected to SSO. Reconnect and complete sign-in in Chrome."
            )
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise EUDMError(f"{method} {path} returned non-JSON data") from exc


def items(questionnaire: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for page in questionnaire["pages"] for item in page["pageItems"]]


def field_by_label(all_items: list[dict[str, Any]], label: str, *, type_: str | None = None) -> dict[str, Any]:
    matches = [x for x in all_items if x.get("label") == label and (type_ is None or x.get("type") == type_)]
    if not matches:
        raise EUDMError(f"The current questionnaire is missing the expected field {label!r}.")
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
    except EUDMError as exc:
        raise EUDMError(f"Could not set {label!r}: {exc}") from exc
    if item.get("type") == "MultiSelectDataTable":
        verbose_detail(client, f"{label}: selected {len(values)} asset(s)")
    elif item.get("type") == "DataTable":
        verbose_detail(client, f"{label}: selected")
    else:
        verbose_detail(client, f"{label}: {values if len(values) != 1 else values[0]}")
    return result or {}


def answer(client: Any, request_id: str, questionnaire_id: str, item: dict[str, Any], value: Any) -> dict[str, Any]:
    return answer_values(client, request_id, questionnaire_id, item, [value])


def questionnaire_choice_value(item: dict[str, Any], desired: Any) -> Any:
    """Resolve a displayed questionnaire choice to its current Helix value.

    Helix occasionally changes the data values behind otherwise unchanged
    Yes/No controls. Prefer the value advertised by the live questionnaire so
    submissions do not depend on the old ``YES``/``NO`` constants.
    """
    options = item.get("options")
    if not isinstance(options, list):
        return desired

    def normalise(value: Any) -> str:
        return " ".join(str(value).split()).casefold()

    wanted = normalise(desired)
    yes_values = {"1", "true", "y", "yes"}
    no_values = {"0", "false", "n", "no"}
    wanted_group = yes_values if wanted in yes_values else no_values if wanted in no_values else None

    for option in options:
        if not isinstance(option, dict):
            continue
        display = next(
            (option[key] for key in ("displayValue", "label", "name") if key in option),
            None,
        )
        value = next(
            (option[key] for key in ("dataValue", "value", "id") if key in option),
            None,
        )
        candidates = {normalise(candidate) for candidate in (display, value) if candidate is not None}
        if wanted in candidates or (wanted_group is not None and candidates & wanted_group):
            return value if value is not None else display
    return desired


def answer_choice(
    client: Any,
    request_id: str,
    questionnaire_id: str,
    item: dict[str, Any],
    desired: Any,
) -> dict[str, Any]:
    return answer(
        client,
        request_id,
        questionnaire_id,
        item,
        questionnaire_choice_value(item, desired),
    )


def option_data(
    events: dict[str, Any],
    question_id: str,
    *,
    allow_single_fallback: bool = False,
) -> list[dict[str, Any]]:
    """Return data-table rows from a questionnaire answer response.

    EUDM usually keys ``ListOptionsChange`` by the table question ID. Some live
    forms regenerate the dynamic location table after a city change and return
    its single list change under a replacement ID. Location callers can opt in
    to that safe fallback.
    """
    if not isinstance(events, dict):
        return []

    def rows(change: dict[str, Any]) -> list[dict[str, Any]]:
        data = change.get("questionMultiColumn", {}).get("data", [])
        return data if isinstance(data, list) else []

    changes = events.get(question_id, [])
    if isinstance(changes, list):
        for change in reversed(changes):
            if isinstance(change, dict) and change.get("type") == "ListOptionsChange":
                return rows(change)
    if not allow_single_fallback:
        return []
    candidates = [
        change
        for changes in events.values()
        if isinstance(changes, list)
        for change in changes
        if isinstance(change, dict) and change.get("type") == "ListOptionsChange"
    ]
    return rows(candidates[0]) if len(candidates) == 1 else []


def merge_events(*event_maps: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for event_map in event_maps:
        for question_id, changes in event_map.items():
            merged.setdefault(question_id, []).extend(changes)
    return merged


def choose_data_value(
    rows: list[dict[str, Any]], needle: str, *, exact: bool = True, kind: str = "option"
) -> str:
    needle_l = needle.casefold()
    matches = []
    for row in rows:
        values = [str(v) for v in row.get("displayValue", [])]
        if any((needle_l == value.casefold() if exact else needle_l in value.casefold()) for value in values):
            matches.append(row)
    if not matches:
        qualifier = "exact " if exact else ""
        raise MatchError(f"No {qualifier}{kind} match was returned for {needle!r}.")
    if len(matches) > 1:
        examples = [" → ".join(str(value) for value in row.get("displayValue", []) if str(value)) for row in matches[:3]]
        suffix = f" Examples: {'; '.join(examples)}." if examples else ""
        raise MatchError(
            f"More than one {kind} matched {needle!r}; refine the value before retrying.{suffix}"
        )
    return matches[0]["dataValue"]


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
        raise EUDMError(f"No returned location matched {target!r}.")
    raise EUDMError(
        f"More than one location matched {target!r}; add --cabinet to identify one row."
    )


def lookup_and_answer(
    client: Any,
    request_id: str,
    questionnaire_id: str,
    all_items: list[dict[str, Any]],
    search_label: str,
    table_label: str,
    query: str,
    search_type: str = "TextField",
) -> str:
    """Search a server-backed person table, then persist the selected dataValue."""
    search = field_by_label(all_items, search_label, type_=search_type)
    table = field_by_label(all_items, table_label, type_="DataTable")
    current = query
    while True:
        try:
            result = client.request(
                "POST",
                f"v2/sbe/services/requests/{request_id}/questions/{search['id']}/lookup",
                {"query": current},
            ) or {}
        except EUDMError as exc:
            raise EUDMError(f"Could not search {search_label!r}: {exc}") from exc
        rows = result.get("multiColumnOptions") or []
        try:
            value = choose_data_value(
                [{"dataValue": row["dataValue"], "displayValue": row.get("displayValue", [])} for row in rows],
                current,
                exact=True,
                kind="user",
            )
        except MatchError as exc:
            current = retry_or_skip("username", current, exc)
            continue
        answer(client, request_id, questionnaire_id, table, value)
        return current


def lookup_and_answer_exact(
    client: Any,
    request_id: str,
    questionnaire_id: str,
    all_items: list[dict[str, Any]],
    search_label: str,
    table_label: str,
    query: str,
    search_type: str = "TextField",
) -> str:
    """Resolve one exact person without using terminal prompts.

    Interactive CLIs retain their retry/skip loop through ``lookup_and_answer``.
    Local web requests use this strict variant so a failed or ambiguous match is
    returned to the browser instead of blocking a server thread on ``input()``.
    """
    search = field_by_label(all_items, search_label, type_=search_type)
    table = field_by_label(all_items, table_label, type_="DataTable")
    try:
        result = client.request(
            "POST",
            f"v2/sbe/services/requests/{request_id}/questions/{search['id']}/lookup",
            {"query": query},
        ) or {}
    except EUDMError as exc:
        raise EUDMError(f"Could not search {search_label!r}: {exc}") from exc
    rows = result.get("multiColumnOptions") or []
    value = choose_data_value(
        [
            {
                "dataValue": row["dataValue"],
                "displayValue": row.get("displayValue", []),
            }
            for row in rows
        ],
        query,
        exact=True,
        kind="user",
    )
    answer(client, request_id, questionnaire_id, table, value)
    return query


def search_question_and_answer_exact(
    client: Any,
    request_id: str,
    questionnaire_id: str,
    all_items: list[dict[str, Any]],
    search_label: str,
    table_label: str,
    query: str,
) -> str:
    """Search a dynamic questionnaire field and select one exact returned row.

    EUDM's returning-user branch does not expose a ``/lookup`` endpoint. Typing
    into its search field is itself a questionnaire answer, and the response
    contains the matching rows for the adjacent table.
    """
    search = field_by_label(all_items, search_label, type_="TextField")
    table = field_by_label(all_items, table_label, type_="DataTable")
    events = answer(
        client,
        request_id,
        questionnaire_id,
        search,
        query,
    )
    rows = option_data(events, table["id"])
    value = choose_data_value(rows, query, exact=True, kind="user")
    answer(client, request_id, questionnaire_id, table, value)
    return query


def search_question_and_answer(
    client: Any,
    request_id: str,
    questionnaire_id: str,
    all_items: list[dict[str, Any]],
    search_label: str,
    table_label: str,
    query: str,
) -> str:
    """Interactive variant of ``search_question_and_answer_exact``."""
    current = query
    while True:
        try:
            return search_question_and_answer_exact(
                client,
                request_id,
                questionnaire_id,
                all_items,
                search_label,
                table_label,
                current,
            )
        except MatchError as exc:
            current = retry_or_skip("username", current, exc)


def answer_single_asset_with_retry(
    client: Any,
    request_id: str,
    questionnaire_id: str,
    serial_field: dict[str, Any],
    device_table: dict[str, Any],
    serial: str,
) -> str:
    """Search and select one exact asset, asking for correction when needed."""
    current = serial
    while True:
        events = answer(client, request_id, questionnaire_id, serial_field, current)
        devices = option_data(events, device_table["id"])
        try:
            value = choose_data_value(devices, current, exact=True, kind="serial number")
        except MatchError as exc:
            current = retry_or_skip("serial number", current, exc)
            continue
        answer(client, request_id, questionnaire_id, device_table, value)
        return current


def answer_single_asset_exact(
    client: Any,
    request_id: str,
    questionnaire_id: str,
    serial_field: dict[str, Any],
    device_table: dict[str, Any],
    serial: str,
) -> str:
    """Resolve one exact asset without prompting for terminal input."""
    events = answer(client, request_id, questionnaire_id, serial_field, serial)
    devices = option_data(events, device_table["id"])
    value = choose_data_value(devices, serial, exact=True, kind="serial number")
    answer(client, request_id, questionnaire_id, device_table, value)
    return serial


def deploy_device_to_user(
    client: Any,
    *,
    serial: str,
    request_for: str,
    deployed_to: str,
    status: str,
    submit: bool = True,
    manual_review_enabled: bool = False,
    on_request_created: Any | None = None,
    interactive_matches: bool = True,
) -> DeploymentResult:
    """Populate and optionally submit one user deployment request."""
    for label, value in (
        ("serial number", serial),
        ("request-for login ID", request_for),
        ("deployed-to login ID", deployed_to),
        ("status", status),
    ):
        if not value or value != value.strip():
            raise EUDMError(f"The {label} cannot be empty or have surrounding whitespace.")
    if not is_serial(serial):
        raise EUDMError(
            "The serial number must be at least 6 characters and contain only letters, "
            "numbers, periods, underscores, or hyphens."
        )
    for label, value in (
        ("request-for user", request_for),
        ("deployed-to user", deployed_to),
    ):
        if not is_login_id(value):
            raise EUDMError(
                f"The {label} must be a login ID, not a display name or email address."
            )

    created = request_step(
        client,
        "Could not create the Helix request",
        "POST",
        "v2/sbe/services/requests",
        {"serviceId": "25301", "quantity": 1, "requestedForLoginIds": [request_for]},
    )
    request_id = str(created["requests"][0]["requestId"])
    if on_request_created:
        on_request_created(request_id)
    verbose_detail(client, f"Created request {request_id}.")
    try:
        return _complete_user_deployment(
            client,
            request_id=request_id,
            serial=serial,
            deployed_to=deployed_to,
            request_for=request_for,
            status=status,
            submit=submit,
            manual_review_enabled=manual_review_enabled,
            interactive_matches=interactive_matches,
        )
    except MatchSkipped as exc:
        return DeploymentResult(
            request_id=request_id,
            order_id=None,
            not_submitted_reason=str(exc),
        )
    except EUDMError as exc:
        raise DeploymentExecutionError(request_id, str(exc)) from exc


def _complete_user_deployment(
    client: Any,
    *,
    request_id: str,
    serial: str,
    deployed_to: str,
    request_for: str,
    status: str,
    submit: bool,
    manual_review_enabled: bool,
    interactive_matches: bool,
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
    device_table = field_by_label(all_items, "----- Device List", type_="DataTable")
    if interactive_matches:
        serial = answer_single_asset_with_retry(
            client, request_id, questionnaire_id, serial_field, device_table, serial
        )
    else:
        serial = answer_single_asset_exact(
            client, request_id, questionnaire_id, serial_field, device_table, serial
        )

    status_item = field_by_label(all_items, "Change Status to", type_="Dropdown")
    answer(client, request_id, questionnaire_id, status_item, status)
    lookup = lookup_and_answer if interactive_matches else lookup_and_answer_exact
    deployed_to = lookup(
        client,
        request_id,
        questionnaire_id,
        all_items,
        "Please select user - device has been deployed to",
        "Please select user - device has been deployed to",
        deployed_to,
        search_type="DataTable",
    )
    if not submit:
        verbose_detail(client, f"Request {request_id} is populated but not submitted.")
        return DeploymentResult(
            request_id=request_id,
            order_id=None,
            not_submitted_reason="final submission was not requested",
            resolved_serial=serial,
            resolved_username=deployed_to,
        )
    if manual_review_enabled and not manual_review(
        request_id=request_id,
        request_for=request_for,
        serials=[serial],
        status=status,
        target="user",
        destination=deployed_to,
    ):
        return DeploymentResult(
            request_id=request_id,
            order_id=None,
            not_submitted_reason="manual review declined final submission",
            resolved_serial=serial,
            resolved_username=deployed_to,
        )

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
    return DeploymentResult(
        request_id=request_id,
        order_id=order_id,
        submitted=True,
        resolved_serial=serial,
        resolved_username=deployed_to,
    )


def deploy_device_to_location(
    client: Any,
    *,
    serials: list[str],
    request_for: str,
    status: str,
    city: str,
    building: str,
    floor: str,
    room: str,
    cabinet: str | None = None,
    returning_user: str | None = None,
    bulk: bool = False,
    submit: bool = True,
    on_request_created: Any | None = None,
) -> DeploymentResult:
    """Populate and optionally submit one strict location deployment.

    A bulk deployment is one EUDM request containing multiple serial numbers.
    Individual location deployments may also identify the user returning the
    device. Bulk requests cannot use EUDM's return-from-user branch because it
    applies to one device and one returning user.
    """
    cleaned_serials = [serial.strip() for serial in serials]
    if not cleaned_serials or any(not serial for serial in cleaned_serials):
        raise EUDMError("At least one serial number is required.")
    if not bulk and len(cleaned_serials) != 1:
        raise EUDMError("A non-bulk location request must contain exactly one serial number.")
    if bulk and len(cleaned_serials) < 1:
        raise EUDMError("A bulk location request must contain at least one serial number.")
    if len({serial.casefold() for serial in cleaned_serials}) != len(cleaned_serials):
        raise EUDMError("A location request cannot contain duplicate serial numbers.")
    if any(not is_serial(serial) for serial in cleaned_serials):
        raise EUDMError(
            "Serial numbers must be at least 6 characters and contain only letters, "
            "numbers, periods, underscores, or hyphens."
        )
    if bulk and returning_user:
        raise EUDMError(
            "Bulk location requests cannot include a returning user; use an individual request."
        )
    for label, value in (
        ("request-for login ID", request_for),
        ("status", status),
        ("city", city),
        ("building", building),
        ("floor", floor),
        ("room", room),
    ):
        if not value or value != value.strip():
            raise EUDMError(f"The {label} cannot be empty or have surrounding whitespace.")
    if not is_login_id(request_for):
        raise EUDMError(
            "The request-for user must be a login ID, not a display name or email address."
        )
    if returning_user and not is_login_id(returning_user):
        raise EUDMError(
            "The returning user must be a login ID, not a display name or email address."
        )

    created = request_step(
        client,
        "Could not create the Helix request",
        "POST",
        "v2/sbe/services/requests",
        {
            "serviceId": "25301",
            "quantity": 1,
            "requestedForLoginIds": [request_for],
        },
    )
    request_id = str(created["requests"][0]["requestId"])
    if on_request_created:
        on_request_created(request_id)
    try:
        questionnaire = request_step(
            client,
            "Could not load the current questionnaire",
            "GET",
            f"v2/sbe/services/requests/{request_id}/questionnaire?timezoneId=Australia/Sydney",
        )["questionnaire"]
        questionnaire_id = str(questionnaire["id"])
        all_items = items(questionnaire)

        inventory = field_by_label(
            all_items, "Inventory Request Type", type_="RadioButtons"
        )
        if bulk:
            answer(client, request_id, questionnaire_id, inventory, "BULK")
            serial_list = field_by_label(
                all_items, "Please add serial number list", type_="TextArea"
            )
            # EUDM bulk mode accepts the comma-separated list directly. It does
            # not expose or require the asset-selection table used by ADD mode.
            answer(
                client,
                request_id,
                questionnaire_id,
                serial_list,
                ",".join(cleaned_serials),
            )
            selected_serials = cleaned_serials
        else:
            answer(client, request_id, questionnaire_id, inventory, "ADD")
            search_by = field_by_label(all_items, "Search by", type_="RadioButtons")
            answer(client, request_id, questionnaire_id, search_by, "serial")
            serial_field = field_by_label(
                all_items, "Type Hostname or Serial Number", type_="TextField"
            )
            device_table = field_by_label(
                all_items, "----- Device List", type_="DataTable"
            )
            selected_serials = [
                answer_single_asset_exact(
                    client,
                    request_id,
                    questionnaire_id,
                    serial_field,
                    device_table,
                    cleaned_serials[0],
                )
            ]

        status_item = field_by_label(
            all_items, "Change Status to", type_="Dropdown"
        )
        answer(client, request_id, questionnaire_id, status_item, status)

        city_item = field_by_label(
            all_items,
            "Building Location (City - Country code)",
            type_="Dropdown",
        )
        city_events = answer(
            client, request_id, questionnaire_id, city_item, city
        )
        location_table = field_by_label(
            all_items, "Please select location", type_="DataTable"
        )
        location_rows = option_data(city_events, location_table["id"])
        if not location_rows:
            raise EUDMError("The city selection did not return any selectable locations.")
        location_value = choose_location_data_value(
            location_rows, building, floor, room, cabinet
        )
        answer(
            client,
            request_id,
            questionnaire_id,
            location_table,
            location_value,
        )

        if not bulk:
            returned = field_by_label(
                all_items,
                "Is this a return from a user",
                type_="RadioButtons",
            )
            answer_choice(
                client,
                request_id,
                questionnaire_id,
                returned,
                "Yes" if returning_user else "No",
            )
            if returning_user:
                add_dropoff = field_by_label(
                    all_items,
                    "Add Name of person who dropped off device",
                    type_="YesNo",
                )
                answer_choice(client, request_id, questionnaire_id, add_dropoff, "true")
                search_question_and_answer_exact(
                    client,
                    request_id,
                    questionnaire_id,
                    all_items,
                    "Search Name or User ID that dropped off devices",
                    "Select person who dropped device/s off",
                    returning_user,
                )
                confirmation = field_by_label(
                    all_items, "Does this look right?", type_="RadioButtons"
                )
                answer_choice(
                    client,
                    request_id,
                    questionnaire_id,
                    confirmation,
                    "YES",
                )

        if not submit:
            return DeploymentResult(
                request_id=request_id,
                order_id=None,
                not_submitted_reason="final submission was not requested",
                resolved_serial=",".join(selected_serials),
                resolved_username=returning_user,
            )
        order = request_step(
            client,
            "Could not submit the order",
            "POST",
            "v2/sbe/orders",
            {"requestIds": [request_id], "title": None},
        )
        order_id = (
            str(order["id"])
            if isinstance(order, dict) and order.get("id")
            else None
        )
        return DeploymentResult(
            request_id=request_id,
            order_id=order_id,
            submitted=True,
            resolved_serial=",".join(selected_serials),
            resolved_username=returning_user,
        )
    except EUDMError as exc:
        raise DeploymentExecutionError(request_id, str(exc)) from exc


def validate_args(args: argparse.Namespace) -> bool:
    """Reject contradictory inputs before browser startup or any API request."""
    for name in ("request_for", "status"):
        value = getattr(args, name)
        if not value or not value.strip():
            raise EUDMError(f"--{name.replace('_', '-')} cannot be empty")
        if value != value.strip():
            raise EUDMError(f"--{name.replace('_', '-')} cannot begin or end with whitespace")
    if not is_login_id(args.request_for):
        raise EUDMError(
            "--request-for must be a login ID, not a display name or email address"
        )
    if args.batch:
        if args.serial:
            raise EUDMError("Batch mode uses --serials, not --serial")
        if not args.serials:
            raise EUDMError("--serials is required in batch mode")
        serials = [value.strip() for value in args.serials.split(",")]
        if not serials or any(not value for value in serials):
            raise EUDMError("--serials must be a comma-separated list without empty entries")
        if any(not is_serial(value) for value in serials):
            raise EUDMError(
                "Serial numbers in --serials must be at least 6 characters and contain only "
                "letters, numbers, periods, underscores, or hyphens"
            )
        if len({value.casefold() for value in serials}) != len(serials):
            raise EUDMError("--serials cannot contain duplicates")
        args.batch_serials = serials
    else:
        if args.serials:
            raise EUDMError("--serials requires --batch")
        if not args.serial or not args.serial.strip():
            raise EUDMError("--serial is required outside batch mode")
        if not is_serial(args.serial):
            raise EUDMError(
                "--serial must be at least 6 characters and contain only letters, numbers, "
                "periods, underscores, or hyphens"
            )

    parsed_base = urllib.parse.urlparse(args.base)
    if parsed_base.scheme != "https" or not parsed_base.netloc or not parsed_base.path.rstrip("/").endswith("/rest"):
        raise EUDMError("--base must be an HTTPS EUDM REST URL ending in /rest")

    user_deployment = args.target == "user"
    location_names = ("city", "building", "floor", "room", "cabinet", "dropped_by")
    if user_deployment:
        if args.batch:
            raise EUDMError("Batch mode supports --target location only")
        if not args.deployed_to or not args.deployed_to.strip():
            raise EUDMError("--deployed-to is required when --target user")
        if not is_login_id(args.deployed_to):
            raise EUDMError(
                "--deployed-to must be a login ID, not a display name or email address"
            )
        conflicting = [f"--{name.replace('_', '-')}" for name in location_names if getattr(args, name)]
        if conflicting:
            raise EUDMError(
                "User deployment cannot use location arguments: " + ", ".join(conflicting)
            )
    else:
        if args.status.startswith("Deployed - "):
            raise EUDMError(
                "A 'Deployed - ...' status is a user deployment; use --target user"
            )
        if args.deployed_to:
            raise EUDMError("Location deployment cannot use --deployed-to; use --dropped-by")
        required = ("city", "building", "floor", "room")
        if not args.batch:
            required += ("dropped_by",)
        missing = [f"--{name.replace('_', '-')}" for name in required if not getattr(args, name)]
        if missing:
            raise EUDMError("Location deployment requires: " + ", ".join(missing))
        for name in required:
            if not getattr(args, name).strip():
                raise EUDMError(f"--{name.replace('_', '-')} cannot be empty")
        if not args.batch and not is_login_id(args.dropped_by):
            raise EUDMError(
                "--dropped-by must be a login ID, not a display name or email address"
            )
        if args.cabinet is not None and not args.cabinet.strip():
            raise EUDMError("--cabinet cannot be empty when supplied")
        if args.batch and args.dropped_by:
            raise EUDMError("Batch location mode does not accept --dropped-by")

    if (
        not getattr(args, "simulate", False)
        and args.browser_profile
        and os.getenv("EUDM_COOKIE", "").strip()
    ):
        raise EUDMError("Choose either --browser-profile or EUDM_COOKIE, not both")
    return user_deployment


def main() -> int:
    try:
        config = AppConfig.load()
    except ValueError as exc:
        raise EUDMError(f"Could not load shared configuration: {exc}") from exc
    if browser_runtime_required(
        sys.argv[1:], default_simulate=config.simulate
    ):
        ensure_runtime(requirement_file="requirements-browser.txt", import_name="playwright")
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Modes:
  Single user:     --serial ... --target user --status 'Deployed - New Stock' --deployed-to ...
  Single location: --serial ... --target location --status 'Used Stock' --city ... --building ... --floor ... --room ... --dropped-by ...
  Batch location:  --batch --serials 'SERIAL1,SERIAL2' --target location ... (no user fields)

Safety:
  Arguments are checked before authentication or API calls. Without --submit,
  EUDM still receives a created and populated request, but no final order is sent.
  --manual-review shows the populated values and asks before the final order.
  Use --simulate for a completely local rehearsal; it produces SIM-REQ IDs.
""",
    )
    parser.add_argument("--serial", help="One hostname/serial. Required unless --batch is used.")
    parser.add_argument(
        "--batch",
        action="store_true",
        help="Use EUDM BULK by Serial Number for one location; rejects all user fields.",
    )
    parser.add_argument(
        "--serials",
        help="Comma-separated serials for --batch; duplicates, spaces, and empty entries are rejected.",
    )
    parser.add_argument("--request-for", default=config.request_for, help="Remedy login ID (default: EUDM_REQUEST_FOR).")
    parser.add_argument(
        "--target",
        required=True,
        choices=("user", "location"),
        help="Required deployment destination. This controls which mutually exclusive fields are valid.",
    )
    parser.add_argument("--city", help="Exact city (location default: EUDM_CITY).")
    parser.add_argument("--building", help="Exact building (location default: EUDM_BUILDING).")
    parser.add_argument("--floor", help="Exact floor (location default: EUDM_FLOOR).")
    parser.add_argument("--room", help="Exact room (location default: EUDM_ROOM).")
    parser.add_argument("--cabinet", help="Optional exact cabinet (location default: EUDM_CABINET).")
    parser.add_argument("--status", help="Exact EUDM data value; defaults by target from the shared env.")
    parser.add_argument("--deployed-to", help="Receiving login ID. Required for --target user; invalid for locations.")
    parser.add_argument("--dropped-by", help="Drop-off login ID. Required for normal locations; invalid for batch locations.")
    parser.add_argument(
        "--submit",
        action="store_true",
        help="Send the final EUDM order. Without it, the request is populated but not ordered.",
    )
    parser.add_argument(
        "--manual-review",
        "--review",
        "--manual",
        action=argparse.BooleanOptionalAction,
        default=config.manual_review,
        help="With --submit, show the populated request summary and require y/n approval before ordering.",
    )
    parser.add_argument(
        "--browser-profile",
        default=config.browser_profile,
        help="Dedicated installed-Chrome profile for SSO (default: EUDM_BROWSER_PROFILE).",
    )
    parser.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=config.browser_headless,
        help="Run Chrome without a visible window (default: EUDM_BROWSER_HEADLESS).",
    )
    parser.add_argument(
        "--cookie-mode",
        action="store_true",
        help="Use EUDM_COOKIE instead of the configured Chrome profile.",
    )
    parser.add_argument(
        "--verbose",
        action=argparse.BooleanOptionalAction,
        default=config.verbose,
        help="Show questionnaire field updates, matching details, and request/status diagnostics; never prints cookies or response bodies.",
    )
    parser.add_argument(
        "--logging",
        action=argparse.BooleanOptionalAction,
        default=config.logging,
        help="Write safe API/authentication activity to logs/ (default: EUDM_LOGGING).",
    )
    parser.add_argument(
        "--simulate",
        action=argparse.BooleanOptionalAction,
        default=config.simulate,
        help="Local in-memory rehearsal. Ignores authentication and makes no browser, network, or EUDM changes.",
    )
    parser.add_argument("--base", default=config.base, help="Override EUDM REST base URL (default: EUDM_BASE).")
    args = parser.parse_args()

    run_reporting.configure_logging(enabled=args.logging, command="eudm-request")

    if args.cookie_mode:
        args.browser_profile = None
    if args.target == "location":
        for name in ("city", "building", "floor", "room", "cabinet"):
            if getattr(args, name) is None:
                setattr(args, name, getattr(config, name))
    if not args.status:
        args.status = (
            config.default_user_status
            if args.target == "user"
            else config.default_location_status
        )

    user_deployment = validate_args(args)
    if args.manual_review and not args.submit:
        raise EUDMError("--manual-review requires --submit; without --submit no final order is sent")

    client = open_client(
        base=args.base,
        browser_profile=args.browser_profile,
        simulate=args.simulate,
        verbose=args.verbose,
        headless=args.headless,
    )
    created = request_step(client, "Could not create the Helix request", "POST", "v2/sbe/services/requests", {
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
        answer(client, request_id, questionnaire_id, inventory, "BULK")
        serial_list = field_by_label(all_items, "Please add serial number list", type_="TextArea")
        answer(
            client,
            request_id,
            questionnaire_id,
            serial_list,
            ",".join(args.batch_serials),
        )
        selected_serials = args.batch_serials
    else:
        answer(client, request_id, questionnaire_id, inventory, "ADD")
        search_by = field_by_label(all_items, "Search by", type_="RadioButtons")
        answer(client, request_id, questionnaire_id, search_by, "serial")
        serial_field = field_by_label(all_items, "Type Hostname or Serial Number", type_="TextField")
        device_table = field_by_label(all_items, "----- Device List", type_="DataTable")
        selected_serial = answer_single_asset_with_retry(
            client, request_id, questionnaire_id, serial_field, device_table, args.serial
        )

    status = field_by_label(all_items, "Change Status to", type_="Dropdown")
    answer(client, request_id, questionnaire_id, status, args.status)

    if user_deployment:
        # User deployments expose a server-backed "deployed to" table.
        selected_deployed_to = lookup_and_answer(
            client, request_id, questionnaire_id, all_items,
            "Please select user - device has been deployed to",
            "Please select user - device has been deployed to",
            args.deployed_to,
            search_type="DataTable",
        )
        verbose_detail(client, f"Deployment target: user {selected_deployed_to}")
    else:
        city = field_by_label(all_items, "Building Location (City - Country code)", type_="Dropdown")
        events = answer(client, request_id, questionnaire_id, city, args.city)
        location_table = field_by_label(all_items, "Please select location", type_="DataTable")
        locations = option_data(events, location_table["id"])
        if not locations:
            raise EUDMError("The city selection did not return any selectable locations")
        location_value = choose_location_data_value(
            locations, args.building, args.floor, args.room, args.cabinet
        )
        answer(client, request_id, questionnaire_id, location_table, location_value)

        if not args.batch:
            returned = field_by_label(
                all_items,
                "Is this a return from a user",
                type_="RadioButtons",
            )
            answer_choice(client, request_id, questionnaire_id, returned, "Yes")
            add_dropoff = field_by_label(
                all_items,
                "Add Name of person who dropped off device",
                type_="YesNo",
            )
            answer_choice(client, request_id, questionnaire_id, add_dropoff, "true")
            search_question_and_answer(
                client,
                request_id,
                questionnaire_id,
                all_items,
                "Search Name or User ID that dropped off devices",
                "Select person who dropped device/s off",
                args.dropped_by,
            )
            confirmation = field_by_label(
                all_items, "Does this look right?", type_="RadioButtons"
            )
            answer_choice(client, request_id, questionnaire_id, confirmation, "Yes")

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
    if user_deployment:
        review_target = "user"
        review_destination = selected_deployed_to
        review_detail = None
    else:
        review_target = "location"
        review_destination = location_summary
        review_detail = "No associated user" if args.batch else f"Dropped by {args.dropped_by}"
    review_serials = selected_serials if args.batch else [selected_serial]
    if args.manual_review and not manual_review(
        request_id=request_id,
        request_for=args.request_for,
        serials=review_serials,
        status=args.status,
        target=review_target,
        destination=review_destination,
        detail=review_detail,
    ):
        print(f"Not submitted. Request {request_id} remains populated for review.")
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
    presentation.summary(
        "Request summary",
        [
            (
                "success",
                f"{', '.join(review_serials)} → {review_target} {review_destination} | "
                f"{args.status} | request {request_id}" + (f" | order {order_id}" if order_id else ""),
            )
        ],
    )
    run_reporting.write_result_file(
        "eudm-request",
        [
            " | ".join(
                (
                    "SUBMITTED",
                    f"serials={','.join(review_serials)}",
                    f"request_for={args.request_for}",
                    f"status={args.status}",
                    f"target={review_target}",
                    f"destination={review_destination}",
                    f"request={request_id}",
                    f"order={order_id or '-'}",
                )
            )
        ],
    )
    return 0


def cli() -> None:
    """Run the command with stable, user-facing error handling."""
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        raise SystemExit(130)
    except EOFError:
        print("Input ended before the request was complete.", file=sys.stderr)
        raise SystemExit(2)
    except MatchSkipped as exc:
        print(f"Not submitted. {exc}")
        raise SystemExit(0)
    except DeploymentExecutionError as exc:
        print(
            f"Error: Request {exc.request_id} was created but not ordered: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    except EUDMError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2)
    except (KeyError, IndexError, TypeError):
        print("Error: EUDM returned an incomplete or unexpected response.", file=sys.stderr)
        raise SystemExit(2)
    except Exception:
        print("Error: An unexpected problem occurred. Re-run with --verbose and report the step shown before it.", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    cli()
