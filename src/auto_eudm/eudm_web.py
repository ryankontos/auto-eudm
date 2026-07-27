#!/usr/bin/env python3
"""Local AutoEUDM web interface and request queue server."""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import socket
import threading
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser

from .bootstrap import ensure_runtime
from .eudm_config import AppConfig
from . import eudm_request as eudm
from . import run_reporting
from .web_models import (
    CITIES,
    LOCATION_STATUSES,
    USER_STATUSES,
    RequestSpec,
    WorkbookImport,
    validate_queue,
)


ROOT = Path(__file__).resolve().parents[2]
WEB_ROOT = ROOT / "web"
MAX_BODY = 42 * 1024 * 1024


def populate_spec(
    client: Any,
    spec: RequestSpec,
    request_for: str,
    *,
    submit: bool,
    on_request_created: Any | None = None,
) -> eudm.DeploymentResult:
    if spec.kind == "user":
        return eudm.deploy_device_to_user(
            client,
            serial=spec.serials[0],
            request_for=request_for,
            deployed_to=spec.user or "",
            status=spec.status,
            submit=submit,
            manual_review_enabled=False,
            on_request_created=on_request_created,
            interactive_matches=False,
        )
    location = spec.location
    assert location is not None
    return eudm.deploy_device_to_location(
        client,
        serials=list(spec.serials),
        request_for=request_for,
        status=spec.status,
        city=location.city,
        building=location.building,
        floor=location.floor,
        room=location.room,
        cabinet=location.cabinet,
        returning_user=spec.returning_user,
        return_confirmed=spec.return_confirmed,
        bulk=spec.kind == "bulk_location",
        submit=submit,
        on_request_created=on_request_created,
    )


def display_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "value": str(row.get("dataValue", "")),
            "columns": [str(value) for value in row.get("displayValue", [])],
        }
        for row in rows
    ]


def authenticated_user_id(payload: Any) -> str | None:
    """Extract EUDM's signed-in user ID from the authenticated cart response."""
    if isinstance(payload, dict):
        user = payload.get("user")
        if isinstance(user, dict):
            value = str(user.get("userId") or "").strip()
            if value:
                return value
        for value in payload.values():
            found = authenticated_user_id(value)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = authenticated_user_id(value)
            if found:
                return found
    return None


def open_existing_server(url: str) -> bool:
    """Open a live AutoEUDM server when this process cannot bind its port."""
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "AutoEUDM launcher"})
        with urllib.request.urlopen(request, timeout=0.8) as response:
            body = response.read(4096).decode("utf-8", errors="ignore")
        if response.status >= 400 or "AutoEUDM" not in body:
            return False
    except (OSError, urllib.error.URLError):
        return False
    webbrowser.open(url)
    print(f"AutoEUDM is already running at {url}; opening it in your browser.", flush=True)
    return True


class SearchProbe:
    """One stateful EUDM questionnaire session reused for live form options."""

    def __init__(self, client: Any, request_for: str) -> None:
        self.client = client
        self.request_for = request_for
        self.request_id: str | None = None
        self.questionnaire_id: str | None = None
        self.all_items: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self._search_ready = False

    def ensure(self) -> None:
        if self.request_id:
            return
        created = eudm.request_step(
            self.client,
            "Could not start the live EUDM search session",
            "POST",
            "v2/sbe/services/requests",
            {
                "serviceId": "25301",
                "quantity": 1,
                "requestedForLoginIds": [self.request_for],
            },
        )
        self.request_id = str(created["requests"][0]["requestId"])
        questionnaire = eudm.request_step(
            self.client,
            "Could not load live EUDM form options",
            "GET",
            f"v2/sbe/services/requests/{self.request_id}/questionnaire?timezoneId=Australia/Sydney",
        )["questionnaire"]
        self.questionnaire_id = str(questionnaire["id"])
        self.all_items = eudm.items(questionnaire)

    def _prepare_asset_search(self) -> None:
        self.ensure()
        if self._search_ready:
            return
        inventory = eudm.field_by_label(
            self.all_items, "Inventory Request Type", type_="RadioButtons"
        )
        eudm.answer(
            self.client,
            self.request_id,
            self.questionnaire_id,
            inventory,
            "ADD",
        )
        search_by = eudm.field_by_label(
            self.all_items, "Search by", type_="RadioButtons"
        )
        eudm.answer(
            self.client,
            self.request_id,
            self.questionnaire_id,
            search_by,
            "serial",
        )
        self._search_ready = True

    def options(self) -> dict[str, Any]:
        with self.lock:
            self.ensure()
            status = eudm.field_by_label(
                self.all_items, "Change Status to", type_="Dropdown"
            )
            city = eudm.field_by_label(
                self.all_items,
                "Building Location (City - Country code)",
                type_="Dropdown",
            )
            return {
                "statuses": [
                    {
                        "label": str(option.get("displayValue", "")),
                        "value": str(option.get("dataValue", "")),
                    }
                    for option in status.get("options", [])
                ],
                "cities": [
                    str(option.get("dataValue", ""))
                    for option in city.get("options", [])
                ],
            }

    def assets(self, query: str) -> list[dict[str, Any]]:
        with self.lock:
            self._prepare_asset_search()
            serial_field = eudm.field_by_label(
                self.all_items,
                "Type Hostname or Serial Number",
                type_="TextField",
            )
            device_table = eudm.field_by_label(
                self.all_items, "----- Device List", type_="DataTable"
            )
            events = eudm.answer(
                self.client,
                self.request_id,
                self.questionnaire_id,
                serial_field,
                query,
            )
            return display_rows(eudm.option_data(events, device_table["id"]))

    def users(self, query: str, returning: bool = False) -> list[dict[str, Any]]:
        with self.lock:
            self.ensure()
            # The return-specific EUDM picker cannot be enabled in a search
            # session: it only becomes valid after its device, status, city,
            # and location have been set. Use the normal EUDM directory lookup
            # to verify and preview the person here. The actual request follows
            # EUDM's required return sequence during submission.
            if returning:
                eudm.verbose_detail(
                    self.client,
                    "Verifying returning user through the EUDM directory.",
                )
            label = (
                "Please select user - device has been deployed to"
            )
            search = eudm.field_by_label(
                self.all_items, label, type_="DataTable"
            )
            result = self.client.request(
                "POST",
                f"v2/sbe/services/requests/{self.request_id}/questions/{search['id']}/lookup",
                {"query": query},
            ) or {}
            return display_rows(result.get("multiColumnOptions") or [])

    def locations(self, city_name: str) -> list[dict[str, Any]]:
        with self.lock:
            self.ensure()
            city = eudm.field_by_label(
                self.all_items,
                "Building Location (City - Country code)",
                type_="Dropdown",
            )
            location = eudm.field_by_label(
                self.all_items, "Please select location", type_="DataTable"
            )
            events = eudm.answer(
                self.client,
                self.request_id,
                self.questionnaire_id,
                city,
                city_name,
            )
            rows = eudm.option_data(
                events, location["id"], allow_single_fallback=True
            )
            eudm.verbose_detail(
                self.client,
                f"Location lookup for {city_name!r}: {len(rows)} row(s) returned.",
            )
            # A connected EUDM form always returns its location table after a
            # valid city selection. The SSO gateway can instead leave a stale
            # session with a superficially successful, but empty, answer
            # response. Fail closed so reconnection is explicit.
            if not rows:
                raise eudm.SSOExpiredError(
                    "EUDM did not return any locations. Your signed-in session "
                    "may have expired; reconnect before trying again."
                )
            return display_rows(rows)


class ClientManager:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.lock = threading.Lock()
        self.state = "simulation" if config.simulate else "disconnected"
        self.message = (
            "Simulation is ready. No browser or EUDM network access will be used."
            if config.simulate
            else "Connect to EUDM before using live search or submitting."
        )
        self.client: Any | None = (
            eudm.SimulationClient(config.verbose) if config.simulate else None
        )
        self.probe: SearchProbe | None = None
        self.connected_at: str | None = None
        self.request_for = (
            config.request_for
            or ("simulated.user" if config.simulate else "")
        )
        self.request_for_source = (
            "environment"
            if config.request_for
            else ("simulation" if config.simulate else "pending")
        )

    def status(self) -> dict[str, Any]:
        with self.lock:
            return {
                "state": self.state,
                "message": self.message,
                "simulation": self.config.simulate,
                "connected_at": self.connected_at,
                "request_for": self.request_for,
                "request_for_source": self.request_for_source,
                "search_request_id": (
                    self.probe.request_id if self.probe else None
                ),
            }

    def connect_async(self) -> None:
        with self.lock:
            if self.state in {"connecting", "connected", "simulation"}:
                return
            self.state = "connecting"
            self.message = "Opening the saved EUDM session…"
        thread = threading.Thread(target=self._connect, daemon=True)
        thread.start()

    def mark_sso_expired(self) -> None:
        """Discard the stale API client and make reconnecting the only next step."""
        if self.config.simulate:
            return
        with self.lock:
            self.client = None
            self.probe = None
            self.connected_at = None
            self.state = "expired"
            self.message = (
                "Your EUDM session has expired. Reconnect and complete SSO in Chrome."
            )
            self.request_for = self.config.request_for or ""
            self.request_for_source = (
                "environment" if self.config.request_for else "pending"
            )

    def _connect(self) -> None:
        browser: Any | None = None
        try:
            browser = eudm.open_client(
                base=self.config.base,
                browser_profile=self.config.browser_profile,
                simulate=False,
                verbose=self.config.verbose,
                headless=self.config.browser_headless,
                interactive_browser_auth=False,
            )
            with self.lock:
                self.message = (
                    "Checking the saved SSO session…"
                    if self.config.browser_headless
                    else "Complete SSO in the Chrome window; AutoEUDM is waiting…"
                )
            deadline = time.monotonic() + (
                15 if self.config.browser_headless else 120
            )
            last_error: Exception | None = None
            while time.monotonic() < deadline:
                try:
                    browser.request("GET", "sessionstatus")
                    last_error = None
                    break
                except eudm.EUDMError as exc:
                    last_error = exc
                    time.sleep(2)
            if last_error:
                if self.config.browser_headless:
                    raise eudm.EUDMError(
                        "The saved SSO session is not ready. Set "
                        "EUDM_BROWSER_HEADLESS=false once, connect, and complete SSO."
                    ) from last_error
                raise eudm.EUDMError(
                    "EUDM SSO did not complete within two minutes. Try Connect again."
                ) from last_error
            # Capture authenticated cookies while still on Playwright's owning
            # thread. Subsequent EUDM API calls use independent system-curl clients.
            client = browser.parallel_clients(1)[0]
            try:
                carts = client.request("GET", "v2/carts") or {}
            except eudm.EUDMError as exc:
                if eudm.is_sso_expired_error(exc):
                    raise
                carts = {}
            inferred_user = authenticated_user_id(carts)
            request_for = inferred_user or self.config.request_for or ""
            if not request_for:
                raise eudm.EUDMError(
                    "EUDM authenticated successfully but did not identify the signed-in user. "
                    "Set EUDM_REQUEST_FOR in .env and connect again."
                )
            # The copied API client is independent of Playwright. Once it has
            # proven the session and inferred the user, close the temporary
            # Chrome window instead of leaving it behind the workspace.
            try:
                browser.context.close()
                run_reporting.event("Closed Chrome after successful EUDM SSO")
            except Exception:
                run_reporting.event("Could not close Chrome after EUDM SSO")
            browser = None
        except Exception as exc:
            if browser is not None:
                try:
                    browser.context.close()
                except Exception:
                    pass
            with self.lock:
                self.state = "error"
                self.message = str(exc)
            return
        with self.lock:
            self.client = client
            self.probe = None
            self.state = "connected"
            self.connected_at = datetime.now().isoformat(timespec="seconds")
            self.request_for = request_for
            self.request_for_source = (
                "EUDM signed-in account" if inferred_user else "environment"
            )
            self.message = "Connected to EUDM."

    def require(self) -> Any:
        with self.lock:
            if self.client is None:
                if self.state == "expired":
                    raise eudm.SSOExpiredError(
                        "Your EUDM session has expired. Reconnect to continue."
                    )
                raise eudm.EUDMError(
                    "Connect to EUDM before searching or submitting."
                )
            return self.client

    def clients(self, count: int) -> list[Any]:
        client = self.require()
        return client.parallel_clients(max(1, count))

    def search(self) -> SearchProbe:
        with self.lock:
            if self.client is None:
                if self.state == "expired":
                    raise eudm.SSOExpiredError(
                        "Your EUDM session has expired. Reconnect to continue."
                    )
                raise eudm.EUDMError(
                    "Connect to EUDM before using live search."
                )
            if self.probe is None:
                self.probe = SearchProbe(
                    self.client, self.request_for
                )
            return self.probe


@dataclass
class JobEntry:
    spec: RequestSpec
    state: str = "queued"
    message: str = "Waiting for an available submission slot"
    step: int = 0
    step_count: int = 3
    request_id: str | None = None
    order_id: str | None = None
    started_at: float | None = None
    finished_at: float | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            **self.spec.to_json(),
            "state": self.state,
            "message": self.message,
            "step": self.step,
            "step_count": self.step_count,
            "progress_percent": round((self.step / self.step_count) * 100)
            if self.step_count
            else 0,
            "request_id": self.request_id,
            "order_id": self.order_id,
            "elapsed_seconds": (
                round((self.finished_at or time.time()) - self.started_at, 1)
                if self.started_at
                else None
            ),
        }


@dataclass
class SubmissionJob:
    job_id: str
    entries: list[JobEntry]
    request_for: str
    concurrency: int
    simulation: bool
    state: str = "queued"
    created_at: str = field(
        default_factory=lambda: datetime.now().isoformat(timespec="seconds")
    )
    finished_at: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def to_json(self) -> dict[str, Any]:
        with self.lock:
            entries = [entry.to_json() for entry in self.entries]
            counts = Counter(entry["state"] for entry in entries)
            return {
                "job_id": self.job_id,
                "state": self.state,
                "created_at": self.created_at,
                "finished_at": self.finished_at,
                "request_for": self.request_for,
                "simulation": self.simulation,
                "counts": {
                    "queued": counts["queued"],
                    "running": counts["running"],
                    "succeeded": counts["succeeded"],
                    "failed": counts["failed"],
                    "total": len(entries),
                    "devices": sum(
                        entry.spec.device_count() for entry in self.entries
                    ),
                },
                "entries": entries,
            }

    def update(
        self,
        entry: JobEntry,
        *,
        state: str | None = None,
        message: str | None = None,
        step: int | None = None,
        request_id: str | None = None,
        order_id: str | None = None,
    ) -> None:
        with self.lock:
            if state:
                entry.state = state
            if message:
                entry.message = message
            if step is not None:
                entry.step = step
            if request_id:
                entry.request_id = request_id
            if order_id:
                entry.order_id = order_id


class JobStore:
    def __init__(self, clients: ClientManager) -> None:
        self.clients = clients
        self.jobs: dict[str, SubmissionJob] = {}
        self.lock = threading.Lock()

    def create(
        self, specs: list[RequestSpec], request_for: str, concurrency: int
    ) -> SubmissionJob:
        job = SubmissionJob(
            uuid.uuid4().hex,
            [JobEntry(spec) for spec in specs],
            request_for,
            concurrency,
            self.clients.config.simulate,
        )
        with self.lock:
            self.jobs[job.job_id] = job
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job

    def get(self, job_id: str) -> SubmissionJob:
        with self.lock:
            job = self.jobs.get(job_id)
        if not job:
            raise eudm.EUDMError("That submission run was not found.")
        return job

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.lock:
            jobs = sorted(
                self.jobs.values(),
                key=lambda job: job.created_at,
                reverse=True,
            )[:limit]
        return [job.to_json() for job in jobs]

    def _run(self, job: SubmissionJob) -> None:
        job.state = "running"
        workers = min(job.concurrency, len(job.entries))
        try:
            clients = self.clients.clients(workers)
        except eudm.EUDMError as exc:
            for entry in job.entries:
                job.update(entry, state="failed", message=str(exc))
            job.state = "finished"
            job.finished_at = datetime.now().isoformat(timespec="seconds")
            return
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    self._run_one,
                    job,
                    entry,
                    clients[index % workers],
                ): entry
                for index, entry in enumerate(job.entries)
            }
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    entry = futures[future]
                    job.update(
                        entry,
                        state="failed",
                        message=f"Unexpected failure: {type(exc).__name__}",
                    )
                    entry.finished_at = time.time()
        job.state = "finished"
        job.finished_at = datetime.now().isoformat(timespec="seconds")
        self._write_results(job)

    def _run_one(
        self, job: SubmissionJob, entry: JobEntry, client: Any
    ) -> None:
        entry.started_at = time.time()
        spec = entry.spec
        job.update(
            entry,
            state="running",
            step=1,
            message="Creating the EUDM request",
        )

        def created(request_id: str) -> None:
            job.update(
                entry,
                request_id=request_id,
                step=2,
                message=f"Request {request_id} created — applying device details",
            )

        try:
            result = populate_spec(
                client,
                spec,
                job.request_for,
                submit=True,
                on_request_created=created,
            )
            job.update(
                entry,
                state="succeeded" if result.submitted else "failed",
                request_id=result.request_id,
                order_id=result.order_id,
                step=3,
                message=(
                    "Request submitted successfully"
                    if result.submitted
                    else result.not_submitted_reason or "Not submitted"
                ),
            )
        except eudm.EUDMError as exc:
            if eudm.is_sso_expired_error(exc):
                self.clients.mark_sso_expired()
            request_id = (
                exc.request_id
                if isinstance(exc, eudm.DeploymentExecutionError)
                else entry.request_id
            )
            job.update(
                entry,
                state="failed",
                request_id=request_id,
                step=entry.step,
                message=str(exc),
            )
        finally:
            entry.finished_at = time.time()

    def _write_results(self, job: SubmissionJob) -> None:
        lines = []
        for entry in job.entries:
            spec = entry.spec
            lines.append(
                " | ".join(
                    (
                        entry.state.upper(),
                        f"serials={','.join(spec.serials)}",
                        f"type={spec.kind}",
                        f"status={spec.status}",
                        f"destination={spec.destination()}",
                        f"returning_user={spec.returning_user or '-'}",
                        f"request={entry.request_id or '-'}",
                        f"order={entry.order_id or '-'}",
                        f"detail={entry.message}",
                    )
                )
            )
        run_reporting.write_result_file("eudm-web", lines)

    def result_text(self, job_id: str) -> str:
        job = self.get(job_id)
        data = job.to_json()
        lines = [
            "AutoEUDM submission summary",
            f"Run: {job.job_id}",
            f"Created: {job.created_at}",
            "",
        ]
        for entry in data["entries"]:
            lines.extend(
                (
                    f"{entry['state'].upper()}  {', '.join(entry['serials'])}",
                    f"  Type: {entry['kind']}",
                    f"  Status: {entry['status']}",
                    f"  Destination: {entry['destination']}",
                    f"  Returning user: {entry['returning_user'] or '-'}",
                    f"  Request ID: {entry['request_id'] or '-'}",
                    f"  Order ID: {entry['order_id'] or '-'}",
                    f"  Detail: {entry['message']}",
                    "",
                )
            )
        return "\n".join(lines)


@dataclass
class ImportJob:
    """A background workbook read with small, browser-friendly status data."""

    job_id: str
    filename: str
    state: str = "queued"
    message: str = "Waiting to read the workbook…"
    sheet: str | None = None
    processed_rows: int = 0
    total_rows: int = 0
    workbook: dict[str, Any] | None = None
    error: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def update(
        self,
        *,
        state: str | None = None,
        message: str | None = None,
        sheet: str | None = None,
        processed_rows: int | None = None,
        total_rows: int | None = None,
    ) -> None:
        with self._lock:
            if state is not None:
                self.state = state
            if message is not None:
                self.message = message
            if sheet is not None:
                self.sheet = sheet
            if processed_rows is not None:
                self.processed_rows = processed_rows
            if total_rows is not None:
                self.total_rows = total_rows

    def fail(self, error: str) -> None:
        with self._lock:
            self.state = "failed"
            self.message = "The workbook could not be imported."
            self.error = error

    def finish(self, workbook: dict[str, Any]) -> None:
        with self._lock:
            self.state = "ready"
            self.message = "Workbook ready."
            self.workbook = workbook
            self.processed_rows = self.total_rows or self.processed_rows

    def to_json(self) -> dict[str, Any]:
        with self._lock:
            return {
                "job_id": self.job_id,
                "filename": self.filename,
                "state": self.state,
                "message": self.message,
                "sheet": self.sheet,
                "processed_rows": self.processed_rows,
                "total_rows": self.total_rows,
                "workbook": self.workbook,
                "error": self.error,
            }


class Application:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.clients = ClientManager(config)
        self.jobs = JobStore(self.clients)
        self.imports: dict[str, WorkbookImport] = {}
        self.import_jobs: dict[str, ImportJob] = {}
        self.import_lock = threading.Lock()
        self.allowed_user_statuses = {value for _, value in USER_STATUSES}
        self.allowed_location_statuses = {
            value for _, value in LOCATION_STATUSES
        }

    def config_json(self) -> dict[str, Any]:
        default_location = {
            "city": self.config.city or "",
            "building": self.config.building or "",
            "floor": self.config.floor or "",
            "room": self.config.room or "",
            "cabinet": self.config.cabinet or "",
        }
        return {
            "request_for": self.clients.request_for,
            "request_for_source": self.clients.request_for_source,
            "simulation": self.config.simulate,
            "manual_review": self.config.manual_review,
            "concurrency": self.config.concurrency,
            "default_user_status": self.config.default_user_status,
            "default_location_status": self.config.default_location_status,
            "default_location": default_location,
            "user_statuses": [
                {"label": label, "value": value}
                for label, value in USER_STATUSES
            ],
            "location_statuses": [
                {"label": label, "value": value}
                for label, value in LOCATION_STATUSES
            ],
            "cities": list(CITIES),
        }

    def add_import(self, workbook: WorkbookImport) -> None:
        with self.import_lock:
            self.imports[workbook.import_id] = workbook
            if len(self.imports) > 10:
                first = next(iter(self.imports))
                self.imports.pop(first, None)

    def start_import(self, filename: str, encoded: str) -> ImportJob:
        job = ImportJob(job_id=uuid.uuid4().hex, filename=filename)
        with self.import_lock:
            self.import_jobs[job.job_id] = job
            if len(self.import_jobs) > 12:
                finished = next(
                    (
                        job_id
                        for job_id, existing in self.import_jobs.items()
                        if existing.state in {"ready", "failed"}
                    ),
                    next(iter(self.import_jobs)),
                )
                if finished != job.job_id:
                    self.import_jobs.pop(finished, None)
        threading.Thread(
            target=self._read_import,
            args=(job, encoded),
            daemon=True,
        ).start()
        return job

    def _read_import(self, job: ImportJob, encoded: str) -> None:
        job.update(state="reading", message="Opening the workbook…")

        def progress(sheet: str, completed: int, total: int) -> None:
            job.update(
                state="reading",
                message="Reading deployment rows…",
                sheet=sheet,
                processed_rows=completed,
                total_rows=total,
            )

        try:
            workbook = WorkbookImport.from_upload(
                job.filename, encoded, on_progress=progress
            )
            self.add_import(workbook)
            job.finish(workbook.summary())
        except eudm.EUDMError as exc:
            job.fail(str(exc))
        except Exception:
            job.fail("The workbook could not be read. Choose an unencrypted .xlsx or .xlsm file.")

    def import_status(self, job_id: str) -> dict[str, Any]:
        with self.import_lock:
            job = self.import_jobs.get(job_id)
        if not job:
            raise eudm.EUDMError(
                "That workbook import expired. Choose the file again."
            )
        return job.to_json()

    def form_options(self) -> dict[str, Any]:
        options = self.clients.search().options()
        user_statuses = {
            option["value"]
            for option in options["statuses"]
            if option["label"].startswith("Deployed -")
        }
        location_statuses = {
            option["value"]
            for option in options["statuses"]
            if not option["label"].startswith("Deployed -")
        }
        if user_statuses:
            self.allowed_user_statuses = user_statuses
        if location_statuses:
            self.allowed_location_statuses = location_statuses
        return options

    def get_import(self, import_id: str) -> WorkbookImport:
        with self.import_lock:
            workbook = self.imports.get(import_id)
        if not workbook:
            raise eudm.EUDMError(
                "That spreadsheet import expired. Choose the file again."
            )
        return workbook


class AutoEUDMHandler(BaseHTTPRequestHandler):
    server_version = "AutoEUDM/1.0"

    @property
    def app(self) -> Application:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        if self.app.config.verbose:
            super().log_message(format, *args)

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _error(
        self, message: str, status: int = 400, **extra: Any
    ) -> None:
        self._json({"error": message, **extra}, status)

    def _handle_eudm_error(self, exc: eudm.EUDMError, status: int) -> None:
        if eudm.is_sso_expired_error(exc):
            self.app.clients.mark_sso_expired()
            self._error(
                "Your EUDM session has expired. Reconnect and complete SSO in Chrome.",
                401,
                code="sso_expired",
                connection=self.app.clients.status(),
            )
            return
        self._error(str(exc), status)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise eudm.EUDMError("The request is larger than the local limit.")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise eudm.EUDMError("The browser sent invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise eudm.EUDMError("The browser request must be a JSON object.")
        return payload

    def _allow_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urllib.parse.urlparse(origin)
        return parsed.scheme == "http" and parsed.hostname in {
            "127.0.0.1",
            "localhost",
            "::1",
        }

    def do_GET(self) -> None:
        try:
            self._do_get()
        except eudm.EUDMError as exc:
            self._handle_eudm_error(exc, 404)
        except (KeyError, ValueError, TypeError):
            self._error("The local web request was invalid.", 400)
        except Exception:
            self._error("An unexpected local server error occurred.", 500)

    def _do_get(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/api/config":
            self._json(self.app.config_json())
            return
        if path == "/api/status":
            self._json(self.app.clients.status())
            return
        if path == "/api/options":
            self._json(self.app.form_options())
            return
        if path == "/api/history":
            self._json({"runs": self.app.jobs.history()})
            return
        if path.startswith("/api/imports/"):
            job_id = path.removeprefix("/api/imports/").strip()
            if not job_id or "/" in job_id:
                self._error("Unknown workbook import.", 404)
                return
            self._json(self.app.import_status(job_id))
            return
        if path.startswith("/api/jobs/"):
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[3] == "results.txt":
                body = self.app.jobs.result_text(parts[2]).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header(
                    "Content-Disposition",
                    f'attachment; filename="auto-eudm-{parts[2][:8]}.txt"',
                )
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if len(parts) == 3:
                self._json(self.app.jobs.get(parts[2]).to_json())
                return
        self._static(path)

    def _static(self, request_path: str) -> None:
        relative = "index.html" if request_path == "/" else request_path.lstrip("/")
        selected = (WEB_ROOT / relative).resolve()
        if WEB_ROOT.resolve() not in selected.parents or not selected.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = selected.read_bytes()
        content_type = mimetypes.guess_type(selected.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:;")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if not self._allow_origin():
            self._error("Requests are accepted only from this localhost app.", 403)
            return
        try:
            self._do_post()
        except eudm.EUDMError as exc:
            self._handle_eudm_error(exc, 422)
        except (KeyError, ValueError, TypeError):
            self._error("The local web request was invalid.", 400)
        except Exception:
            self._error("An unexpected local server error occurred.", 500)

    def _do_post(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        payload = self._read_json()
        if path == "/api/connect":
            self.app.clients.connect_async()
            self._json(self.app.clients.status(), 202)
            return
        if path == "/api/search/assets":
            query = str(payload.get("query", "")).strip()
            if len(query) < 2:
                raise eudm.EUDMError("Enter at least two serial characters.")
            self._json({"results": self.app.clients.search().assets(query)})
            return
        if path == "/api/search/users":
            query = str(payload.get("query", "")).strip()
            if len(query) < 2:
                raise eudm.EUDMError("Enter at least two username characters.")
            self._json(
                {
                    "results": self.app.clients.search().users(
                        query, bool(payload.get("returning"))
                    )
                }
            )
            return
        if path == "/api/search/locations":
            city = str(payload.get("city", "")).strip()
            if not city:
                raise eudm.EUDMError("Choose a city first.")
            self._json(
                {"results": self.app.clients.search().locations(city)}
            )
            return
        if path == "/api/import":
            job = self.app.start_import(
                str(payload.get("filename", "")),
                str(payload.get("data", "")),
            )
            self._json(job.to_json(), 202)
            return
        if path == "/api/import/prepare":
            workbook = self.app.get_import(str(payload.get("import_id", "")))
            self._json(
                workbook.prepare(
                    str(payload.get("sheet", "")),
                    str(payload.get("date", "")),
                    str(payload.get("mode", "")),
                )
            )
            return
        if path == "/api/jobs":
            raw_requests = payload.get("requests")
            if not isinstance(raw_requests, list):
                raise eudm.EUDMError("The request queue was missing.")
            if len(raw_requests) > 250:
                raise eudm.EUDMError(
                    "Submit at most 250 request groups in one run."
                )
            specs = [RequestSpec.from_json(item) for item in raw_requests]
            request_for = self.app.clients.request_for
            errors = validate_queue(
                specs,
                request_for,
                user_statuses=self.app.allowed_user_statuses,
                location_statuses=self.app.allowed_location_statuses,
            )
            if errors:
                self._json(
                    {
                        "error": "Correct the highlighted requests before submitting.",
                        "validation": errors,
                    },
                    422,
                )
                return
            concurrency = int(payload.get("concurrency") or self.app.config.concurrency)
            concurrency = max(1, min(50, concurrency))
            job = self.app.jobs.create(specs, request_for, concurrency)
            self._json(job.to_json(), 202)
            return
        self._error("Unknown local API endpoint.", 404)


class AutoEUDMServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], app: Application) -> None:
        super().__init__(address, AutoEUDMHandler)
        self.app = app


def main() -> int:
    ensure_runtime(
        requirement_file="requirements-sheet.txt", import_name="openpyxl"
    )
    try:
        config = AppConfig.load()
    except ValueError as exc:
        raise eudm.EUDMError(
            f"Could not load shared configuration: {exc}"
        ) from exc
    if not config.simulate:
        ensure_runtime(
            requirement_file="requirements-browser.txt",
            import_name="playwright",
        )
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
        server.server_close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except eudm.EUDMError as exc:
        print(f"Error: {exc}")
        raise SystemExit(2)
    except (socket.error, OSError) as exc:
        print(f"Error: Could not start the local web server: {exc}")
        raise SystemExit(2)
