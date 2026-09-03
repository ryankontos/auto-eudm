"""Stateful AutoEUDM application services used by the local web UI."""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any
import urllib.error
import urllib.request
import uuid
import webbrowser

from . import eudm_inventory_import as inventory
from . import eudm_request as eudm
from . import run_reporting
from .eudm_config import AppConfig
from .web_models import (
    CITIES,
    LOCATION_STATUSES,
    USER_STATUSES,
    Location,
    RequestSpec,
    WorkbookImport,
)


ROOT = Path(__file__).resolve().parents[2]

MAX_IMPORT_JOBS = 12
MAX_PENDING_IMPORTS = 2
MAX_LIVE_SUBMISSION_JOBS = 100
SEARCH_PROBE_POOL_SIZE = 16
VERIFICATION_CACHE_MAX_ENTRIES = 10000
VERIFICATION_CACHE_WRITE_COALESCE_SECONDS = 0.75
MAX_ALM_IMPORT_DRAFTS = 10
ALM_IMPORT_DRAFT_MAX_AGE = timedelta(hours=6)
ALM_LOCAL_SYNC_TIMEOUT_SECONDS = 60
ALM_LOCAL_SYNC_RETRY_SECONDS = 3
MAX_ALM_WORKBOOK_CACHE_FILES = 12
HISTORY_FILENAME = "request-history.json"
LEGACY_HISTORY_FILENAMES = ("web-request-history.json",)
MAX_QUEUED_REQUESTS = 1000
MAX_QUEUED_REQUEST_BYTES = 5 * 1024 * 1024


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
        returning_user=spec.returning_user if spec.returning_requested else None,
        bulk=spec.kind == "bulk_location",
        submit=submit,
        on_request_created=on_request_created,
    )


def display_rows(
    rows: list[dict[str, Any]],
    *,
    include_device_type: bool = False,
) -> list[dict[str, Any]]:
    displayed: list[dict[str, Any]] = []
    for row in rows:
        value = str(row.get("dataValue", ""))
        columns = [str(item) for item in row.get("displayValue", [])]
        result = {"value": value, "columns": columns}
        if include_device_type:
            device_type = ""
            for key in (
                "deviceType",
                "device_type",
                "modelName",
                "model_name",
                "typeName",
                "type_name",
            ):
                candidate = str(row.get(key, "") or "").strip()
                if candidate:
                    device_type = candidate
                    break
            if not device_type and len(columns) > 1:
                candidate = columns[1].strip()
                if candidate and candidate.casefold() not in {value.casefold(), ""}:
                    device_type = candidate
            result["device_type"] = device_type
        displayed.append(result)
    return displayed


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


def open_existing_server(
    url: str,
) -> bool:
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
            return display_rows(
                eudm.option_data(events, device_table["id"]),
                include_device_type=True,
            )

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
        self.fresh_probes: list[SearchProbe] = []
        self.fresh_probe_cursor = 0
        self.connected_at: str | None = None
        self.last_checked_at: str | None = None
        self.health_lock = threading.Lock()
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
                "last_checked_at": self.last_checked_at,
                "request_for": self.request_for,
                "request_for_source": self.request_for_source,
                "search_request_id": (
                    self.probe.request_id if self.probe else None
                ),
            }

    def connect_async(self) -> None:
        with self.lock:
            if self.state in {"connecting", "simulation"}:
                return
            if self.state == "connected":
                self.client = None
                self.probe = None
                self.fresh_probes = []
                self.fresh_probe_cursor = 0
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
            self.fresh_probes = []
            self.fresh_probe_cursor = 0
            self.connected_at = None
            self.last_checked_at = None
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
            # proven the session and inferred the user, close its private
            # authentication context.
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
            self.fresh_probes = []
            self.fresh_probe_cursor = 0
            self.state = "connected"
            self.connected_at = datetime.now().isoformat(timespec="seconds")
            self.last_checked_at = self.connected_at
            self.request_for = request_for
            self.request_for_source = (
                "EUDM signed-in account" if inferred_user else "environment"
            )
            self.message = "Connected to EUDM."

    def check_connection(self) -> dict[str, Any]:
        """Verify the authenticated API session with a small live EUDM request."""
        if self.config.simulate:
            return self.status()
        with self.health_lock:
            with self.lock:
                client = self.client
                connected = self.state == "connected"
            if not client or not connected:
                return self.status()
            try:
                # Carts is authenticated, has no form side effect, and is also
                # what connection setup uses to identify the signed-in person.
                client.request("GET", "v2/carts")
            except eudm.EUDMError as exc:
                message = str(exc).casefold()
                if eudm.is_sso_expired_error(exc) or "401" in message or "403" in message or "single sign on" in message:
                    self.mark_sso_expired()
                else:
                    with self.lock:
                        if self.client is client:
                            self.state = "error"
                            self.message = "Could not verify the EUDM connection. Refresh it before continuing."
                return self.status()
            with self.lock:
                if self.client is client and self.state == "connected":
                    self.last_checked_at = datetime.now().isoformat(timespec="seconds")
                    self.message = "Connected to EUDM."
            return self.status()

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

    def fresh_search(self) -> SearchProbe:
        """Return a reusable probe for concurrent import preflight checks.

        A fresh probe still remains independent from the interactive search
        probe, but its questionnaire is reused for several lookups. This
        avoids creating and loading a new EUDM request for every serial and
        username verification while retaining separate locks for concurrent
        callers.
        """
        with self.lock:
            if self.client is None:
                if self.state == "expired":
                    raise eudm.SSOExpiredError(
                        "Your EUDM session has expired. Reconnect to continue."
                    )
                raise eudm.EUDMError(
                    "Connect to EUDM before using live search."
                )
            client = self.client
            if len(self.fresh_probes) < SEARCH_PROBE_POOL_SIZE:
                probe = SearchProbe(
                    client.parallel_clients(1)[0], self.request_for
                )
                self.fresh_probes.append(probe)
                return probe
            probe = self.fresh_probes[
                self.fresh_probe_cursor % len(self.fresh_probes)
            ]
            self.fresh_probe_cursor = (
                self.fresh_probe_cursor + 1
            ) % len(self.fresh_probes)
            return probe


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
        started_at: float | None = None,
        finished_at: float | None = None,
    ) -> None:
        with self.lock:
            if state is not None:
                entry.state = state
            if message is not None:
                entry.message = message
            if step is not None:
                entry.step = step
            if request_id is not None:
                entry.request_id = request_id
            if order_id is not None:
                entry.order_id = order_id
            if started_at is not None:
                entry.started_at = started_at
            if finished_at is not None:
                entry.finished_at = finished_at

    def set_state(self, state: str, *, finished_at: str | None = None) -> None:
        with self.lock:
            self.state = state
            if finished_at is not None:
                self.finished_at = finished_at

    def is_finished(self) -> bool:
        with self.lock:
            return self.state == "finished"


class JobStore:
    def __init__(self, clients: ClientManager) -> None:
        self.clients = clients
        self.jobs: dict[str, SubmissionJob] = {}
        self.lock = threading.Lock()
        self.history_path = ROOT / "results" / HISTORY_FILENAME
        self.legacy_history_paths = tuple(
            ROOT / "results" / filename
            for filename in LEGACY_HISTORY_FILENAMES
        )
        self.persisted_history = self._load_history()

    def _load_history(self) -> list[dict[str, Any]]:
        """Load saved runs and migrate the pre-filesystem history location."""
        paths = (self.history_path, *getattr(self, "legacy_history_paths", ()))
        for path in paths:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            if isinstance(raw, dict):
                raw = raw.get("runs")
            if not isinstance(raw, list):
                continue
            history = [
                item for item in raw
                if isinstance(item, dict) and item.get("job_id")
            ][:100]
            if path != self.history_path and history:
                self._write_history(history)
            return history
        return []

    def _write_history(self, history: list[dict[str, Any]]) -> None:
        payload = json.dumps(history[:100], ensure_ascii=False, indent=2)
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.history_path.with_suffix(".tmp")
        temporary.write_text(payload + "\n", encoding="utf-8")
        temporary.replace(self.history_path)

    def _persist_history(self, job: SubmissionJob) -> None:
        snapshot = job.to_json()
        with self.lock:
            self.persisted_history = [
                existing
                for existing in self.persisted_history
                if existing.get("job_id") != job.job_id
            ]
            self.persisted_history.insert(0, snapshot)
            self.persisted_history = self.persisted_history[:100]
            try:
                # Serialize the atomic replacement with the in-memory update.
                # Parallel jobs otherwise race through the shared .tmp path
                # and an older snapshot can overwrite a newer completion.
                self._write_history(self.persisted_history)
            except OSError:
                # History is a convenience feature; it must never affect a
                # completed EUDM request.
                pass

    def _register_job(self, job: SubmissionJob) -> None:
        """Bound completed live state while retaining every active run."""
        with self.lock:
            self.jobs[job.job_id] = job
            while len(self.jobs) > MAX_LIVE_SUBMISSION_JOBS:
                removable = next(
                    (
                        job_id
                        for job_id, existing in self.jobs.items()
                        if job_id != job.job_id and existing.is_finished()
                    ),
                    None,
                )
                if removable is None:
                    break
                self.jobs.pop(removable, None)

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
        self._register_job(job)
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
            live = [job.to_json() for job in self.jobs.values()]
            live_ids = {job["job_id"] for job in live}
            runs = live + [
                run for run in self.persisted_history if run.get("job_id") not in live_ids
            ]
        return sorted(runs, key=lambda job: str(job.get("created_at", "")), reverse=True)[:limit]

    def _run(self, job: SubmissionJob) -> None:
        job.set_state("running")
        workers = min(job.concurrency, len(job.entries))
        try:
            clients = self.clients.clients(workers)
        except eudm.EUDMError as exc:
            for entry in job.entries:
                job.update(entry, state="failed", message=str(exc))
            job.set_state(
                "finished",
                finished_at=datetime.now().isoformat(timespec="seconds"),
            )
            self._write_results(job)
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
                        finished_at=time.time(),
                    )
        job.set_state(
            "finished",
            finished_at=datetime.now().isoformat(timespec="seconds"),
        )
        self._write_results(job)

    def _run_one(
        self, job: SubmissionJob, entry: JobEntry, client: Any
    ) -> None:
        spec = entry.spec
        job.update(
            entry,
            state="running",
            step=1,
            message="Creating the EUDM request",
            started_at=time.time(),
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
            job.update(entry, finished_at=time.time())

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
                        f"device_allocation={spec.device_allocation or '-'}",
                        f"destination={spec.destination()}",
                        f"request={entry.request_id or '-'}",
                        f"order={entry.order_id or '-'}",
                        f"detail={entry.message}",
                    )
                )
            )
        try:
            run_reporting.write_result_file("eudm-web", lines)
        except OSError:
            # A result text file is useful but must not prevent the durable
            # history snapshot from being updated.
            pass
        self._persist_history(job)

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
                    f"  ALM allocation: {entry.get('device_allocation') or '-'}",
                    f"  Destination: {entry['destination']}",
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

    def is_terminal(self) -> bool:
        with self._lock:
            return self.state in {"ready", "failed"}

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
        self.pending_imports: dict[str, tuple[str, bytes]] = {}
        self.import_jobs: dict[str, ImportJob] = {}
        self.import_lock = threading.Lock()
        self.pending_import_paths: dict[
            str,
            tuple[Path, tuple[int, int], bool, dict[str, Any]],
        ] = {}
        self.import_drafts_path = ROOT / "results" / "web-alm-import-drafts.json"
        self.import_drafts_lock = threading.Lock()
        self.import_drafts = self._load_import_drafts()
        self.import_payload_path = ROOT / "results" / "web-alm-imports"
        self.import_payload_lock = threading.Lock()
        self.workbook_cache_path = ROOT / "results" / "web-alm-workbook-cache"
        self.workbook_cache_lock = threading.Lock()
        self.request_queue_path = ROOT / "results" / "web-request-queue.json"
        self.request_queue_lock = threading.Lock()
        self.request_queue = self._load_request_queue()
        self.verification_cache_path = ROOT / "results" / "web-verification-cache.json"
        self.verification_cache_lock = threading.Lock()
        self.verification_cache = self._load_verification_cache()
        self.verification_cache_alias_index: dict[str, dict[str, str]] | None = None
        self.verification_cache_write_timer: threading.Timer | None = None
        self.verification_cache_write_lock = threading.Lock()
        self.verification_cache_dirty = False
        self.verification_cache_last_write = 0.0
        self.alm_backlog_ignored_path = ROOT / "results" / "web-alm-backlog-ignored.json"
        self.alm_backlog_ignored_lock = threading.Lock()
        self.alm_backlog_ignored = self._load_alm_backlog_ignored()
        self.preferences_path = ROOT / "results" / "web-settings.json"
        self.preferences_lock = threading.Lock()
        self.preferences = self._load_preferences()
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
            "spreadsheet_import_enabled": self.config.spreadsheet_import_enabled,
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

    def _preference_defaults(self) -> dict[str, Any]:
        return {
            "concurrency": max(1, min(50, int(self.config.concurrency or 1))),
            "validate_editor_serials": True,
            "validate_editor_users": True,
            "validate_bulk_serials": True,
            "validate_quick_import": True,
            "validate_workbook_import": True,
            "save_alm_import_drafts": True,
            "alm_workbook_path": "",
            "alm_workbook_sync_before_load": False,
            "request_statuses": [
                value for _, value in USER_STATUSES + LOCATION_STATUSES
            ],
            "import_columns": {
                "username": "Username",
                "deployment_serial": "SN",
                "returned_device": "",
                "pending_return": "OLD Device SN",
                "enabled": "",
                "device_allocation": "Device(s) Allocation",
                "new_asset_status": "New Asset Status",
                "first_name": "First Name",
                "last_name": "Last Name",
            },
        }

    def _normalise_preferences(
        self,
        raw: dict[str, Any],
        *,
        base: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise eudm.EUDMError("Settings must be a JSON object.")
        values = dict(base or self._preference_defaults())
        concurrency = raw.get("concurrency", values["concurrency"])
        if isinstance(concurrency, bool):
            raise eudm.EUDMError("Parallel requests must be between 1 and 50.")
        try:
            concurrency = int(concurrency)
        except (TypeError, ValueError) as exc:
            raise eudm.EUDMError("Parallel requests must be between 1 and 50.") from exc
        if not 1 <= concurrency <= 50:
            raise eudm.EUDMError("Parallel requests must be between 1 and 50.")
        values["concurrency"] = concurrency

        for key in (
            "validate_editor_serials",
            "validate_editor_users",
            "validate_bulk_serials",
            "validate_quick_import",
            "validate_workbook_import",
            "save_alm_import_drafts",
        ):
            if key in raw and not isinstance(raw[key], bool):
                raise eudm.EUDMError("A settings toggle had an invalid value.")
            if key in raw:
                values[key] = raw[key]

        if "alm_workbook_path" in raw:
            if not isinstance(raw["alm_workbook_path"], str):
                raise eudm.EUDMError("The SharePoint ALM workbook path must be text.")
            values["alm_workbook_path"] = raw["alm_workbook_path"].strip()
        if "alm_workbook_sync_before_load" in raw:
            if not isinstance(raw["alm_workbook_sync_before_load"], bool):
                raise eudm.EUDMError("The OneDrive sync setting had an invalid value.")
            values["alm_workbook_sync_before_load"] = raw["alm_workbook_sync_before_load"]

        if "request_statuses" in raw:
            request_statuses = raw["request_statuses"]
            if isinstance(request_statuses, dict):
                # Accept the short-lived grouped format so settings saved by
                # an earlier build are upgraded without being discarded.
                grouped_user = request_statuses.get("user", [])
                grouped_location = request_statuses.get("location", [])
                if not isinstance(grouped_user, list) or not isinstance(grouped_location, list):
                    raise eudm.EUDMError("Request status settings must be a list.")
                selected_statuses = [*grouped_user, *grouped_location]
            elif isinstance(request_statuses, list):
                selected_statuses = request_statuses
            else:
                raise eudm.EUDMError("Request status settings must be a list.")
            available_statuses = {
                value for _, value in USER_STATUSES + LOCATION_STATUSES
            }
            normalised_statuses: list[str] = []
            seen: set[str] = set()
            for status in selected_statuses:
                value = str(status or "").strip()
                if value not in available_statuses:
                    raise eudm.EUDMError(
                        f"'{value or 'Blank'}' is not a valid deployment status."
                    )
                if value in seen:
                    raise eudm.EUDMError(
                        f"The deployment status '{value}' is listed more than once."
                    )
                seen.add(value)
                normalised_statuses.append(value)
            user_statuses = {value for _, value in USER_STATUSES}
            location_statuses = {value for _, value in LOCATION_STATUSES}
            if not any(value in user_statuses for value in normalised_statuses):
                raise eudm.EUDMError("Keep at least one user deployment status visible.")
            if not any(value in location_statuses for value in normalised_statuses):
                raise eudm.EUDMError("Keep at least one location deployment status visible.")
            values["request_statuses"] = normalised_statuses

        if "import_columns" in raw:
            columns = raw["import_columns"]
            if not isinstance(columns, dict):
                raise eudm.EUDMError("Workbook columns must be a JSON object.")
            normalised = {
                key: str(columns.get(key, "") or "").strip()
                for key in (
                    "username",
                    "deployment_serial",
                    "returned_device",
                    "pending_return",
                    "enabled",
                    "device_allocation",
                    "new_asset_status",
                    "first_name",
                    "last_name",
                )
            }
            # These columns were added after the first settings format. Keep
            # old settings useful by applying the normal default when the key
            # is absent, while preserving an explicit blank as disabled.
            for key, default in (
                ("device_allocation", "Device(s) Allocation"),
                ("new_asset_status", "New Asset Status"),
                ("first_name", "First Name"),
                ("last_name", "Last Name"),
            ):
                if key not in columns:
                    normalised[key] = default
            if not all(normalised[key] for key in ("username", "deployment_serial", "pending_return")):
                raise eudm.EUDMError(
                    "Set the username, deployment serial, and pending return columns."
                )
            values["import_columns"] = normalised
        return values

    def _load_preferences(self) -> dict[str, Any]:
        defaults = self._preference_defaults()
        try:
            raw = json.loads(self.preferences_path.read_text(encoding="utf-8"))
            return self._normalise_preferences(raw, base=defaults)
        except (OSError, ValueError, TypeError, eudm.EUDMError):
            return defaults

    def preferences_json(self) -> dict[str, Any]:
        with self.preferences_lock:
            values = json.loads(json.dumps(self.preferences))
            values["_saved"] = self.preferences_path.is_file()
            return values

    def save_preferences(self, raw: dict[str, Any]) -> dict[str, Any]:
        with self.preferences_lock:
            saved = self._normalise_preferences(raw, base=self.preferences)
            payload = json.dumps(saved, ensure_ascii=False, indent=2)
            self.preferences_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.preferences_path.with_suffix(".tmp")
            temporary.write_text(payload + "\n", encoding="utf-8")
            temporary.replace(self.preferences_path)
            self.preferences = saved
            values = json.loads(json.dumps(saved))
            values["_saved"] = True
            return values

    @staticmethod
    def _normalise_local_workbook_path(raw: Any) -> Path:
        value = str(raw or "").strip()
        if not value:
            raise eudm.EUDMError(
                "Set the SharePoint ALM workbook path in Settings first."
            )
        path = Path(value).expanduser()
        try:
            path = path.resolve()
        except (OSError, RuntimeError) as exc:
            raise eudm.EUDMError("The SharePoint ALM workbook path was invalid.") from exc
        if path.suffix.casefold() not in {".xlsx", ".xlsm"}:
            raise eudm.EUDMError("Choose an .xlsx or .xlsm ALM workbook.")
        return path

    @staticmethod
    def _local_workbook_signature(path: Path) -> tuple[int, int] | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        return int(stat.st_size), int(stat.st_mtime_ns)

    @staticmethod
    def _wake_onedrive() -> None:
        """Ask the macOS OneDrive app to run without requiring admin access."""
        if sys.platform != "darwin":
            return
        try:
            # OneDrive has no supported public download CLI on macOS. Opening
            # the installed app wakes its File Provider sync service; reading
            # the file below then asks macOS to hydrate a cloud placeholder.
            subprocess.run(
                ["open", "-g", "-a", "OneDrive"],
                capture_output=True,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    def _read_local_workbook_payload(
        self,
        path: Path,
        *,
        sync_before_load: bool,
    ) -> tuple[bytes, tuple[int, int]]:
        """Read a local/File Provider workbook, optionally waiting for sync."""
        deadline = (
            time.monotonic() + ALM_LOCAL_SYNC_TIMEOUT_SECONDS
            if sync_before_load
            else time.monotonic()
        )
        if sync_before_load:
            self._wake_onedrive()
        last_error = "The file was not available."
        while True:
            try:
                before = path.stat()
                if not path.is_file():
                    raise OSError("The configured path is not a file.")
                if before.st_size > inventory.MAX_WORKBOOK_BYTES:
                    raise eudm.EUDMError(
                        "The ALM Workbook is larger than the 100 MB local limit."
                    )
                payload = path.read_bytes()
                if not payload:
                    raise OSError("The workbook is empty.")
                signature = self._local_workbook_signature(path)
                if signature is None:
                    raise OSError("The file disappeared while it was being read.")
                if sync_before_load and signature != (
                    int(before.st_size),
                    int(before.st_mtime_ns),
                ):
                    raise OSError("OneDrive was still updating the workbook.")
                return payload, signature
            except eudm.EUDMError:
                raise
            except OSError as exc:
                last_error = str(exc) or last_error
                if not sync_before_load:
                    raise eudm.EUDMError(
                        f"Could not read the configured ALM Workbook: {last_error}"
                    ) from exc
                if time.monotonic() >= deadline:
                    raise eudm.EUDMError(
                        "OneDrive did not make the configured ALM Workbook available "
                        "within 60 seconds. Check that the file is online and try again."
                    ) from exc
                time.sleep(ALM_LOCAL_SYNC_RETRY_SECONDS)

    @staticmethod
    def _workbook_columns_cache_payload(
        columns: inventory.ImportColumns,
    ) -> dict[str, str]:
        return {
            key: str(getattr(columns, key))
            for key in (
                "username",
                "deployment_serial",
                "returned_device",
                "pending_return",
                "enabled",
                "device_allocation",
                "new_asset_status",
                "first_name",
                "last_name",
            )
        }

    def _local_workbook_cache_key(
        self,
        path: Path,
        signature: tuple[int, int],
        columns: inventory.ImportColumns,
    ) -> str:
        source = json.dumps(
            {
                "path": str(path),
                "signature": list(signature),
                "columns": self._workbook_columns_cache_payload(columns),
            },
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(source).hexdigest()

    def _load_local_workbook_cache(
        self,
        path: Path,
        signature: tuple[int, int],
        columns: inventory.ImportColumns,
    ) -> WorkbookImport | None:
        cache_root = getattr(self, "workbook_cache_path", None)
        if not cache_root:
            return None
        cache_path = Path(cache_root) / (
            f"{self._local_workbook_cache_key(path, signature, columns)}.json"
        )
        try:
            raw = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        if not isinstance(raw, dict):
            return None
        if raw.get("path") != str(path) or raw.get("signature") != list(signature):
            return None
        if raw.get("columns") != self._workbook_columns_cache_payload(columns):
            return None
        return WorkbookImport.from_cache_json(
            uuid.uuid4().hex,
            path.name,
            raw.get("workbook"),
        )

    def _write_local_workbook_cache(
        self,
        path: Path,
        signature: tuple[int, int],
        columns: inventory.ImportColumns,
        workbook: WorkbookImport,
    ) -> None:
        cache_root = getattr(self, "workbook_cache_path", None)
        if not cache_root:
            return
        cache_root = Path(cache_root)
        cache_path = cache_root / (
            f"{self._local_workbook_cache_key(path, signature, columns)}.json"
        )
        payload = {
            "path": str(path),
            "signature": list(signature),
            "columns": self._workbook_columns_cache_payload(columns),
            "workbook": workbook.cache_json(),
        }
        try:
            cache_lock = getattr(self, "workbook_cache_lock", None)
            if cache_lock is None:
                cache_lock = threading.Lock()
                self.workbook_cache_lock = cache_lock
            with cache_lock:
                cache_root.mkdir(parents=True, exist_ok=True)
                temporary = cache_path.with_suffix(".tmp")
                temporary.write_text(
                    json.dumps(payload, ensure_ascii=False),
                    encoding="utf-8",
                )
                temporary.replace(cache_path)
                cache_files = sorted(
                    cache_root.glob("*.json"),
                    key=lambda candidate: candidate.stat().st_mtime_ns,
                )
                for old_cache in cache_files[:-MAX_ALM_WORKBOOK_CACHE_FILES]:
                    try:
                        old_cache.unlink()
                    except OSError:
                        pass
        except (OSError, TypeError, ValueError):
            # A parsed cache is an optimization only; never make an import
            # fail because its optional cache could not be written.
            run_reporting.event("Could not persist the ALM workbook cache")

    def choose_alm_workbook_path(self) -> str:
        """Open the native macOS file chooser and return a validated path."""
        if sys.platform != "darwin":
            raise eudm.EUDMError(
                "Use the path field in Settings to select an ALM Workbook on this system."
            )
        try:
            result = subprocess.run(
                [
                    "osascript",
                    "-e",
                    'POSIX path of (choose file with prompt "Choose SharePoint ALM workbook")',
                ],
                capture_output=True,
                check=False,
                text=True,
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise eudm.EUDMError("The macOS file chooser could not be opened.") from exc
        if result.returncode != 0 or not result.stdout.strip():
            return ""
        return str(self._normalise_local_workbook_path(result.stdout.strip()))

    def start_local_import(self) -> ImportJob:
        with self.preferences_lock:
            raw_path = self.preferences.get("alm_workbook_path", "")
            sync_before_load = bool(
                self.preferences.get("alm_workbook_sync_before_load", False)
            )
        path = self._normalise_local_workbook_path(raw_path)
        job = ImportJob(job_id=uuid.uuid4().hex, filename=path.name)
        self._register_import_job(job)
        threading.Thread(
            target=self._inspect_local_import,
            args=(job, path, sync_before_load),
            daemon=True,
        ).start()
        return job

    def _load_import_drafts(self) -> list[dict[str, Any]]:
        """Load resumable ALM import state from the project filesystem."""
        try:
            raw = json.loads(self.import_drafts_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return []
        if not isinstance(raw, list):
            return []
        now = datetime.now()
        drafts = [
            draft
            for draft in raw[:MAX_ALM_IMPORT_DRAFTS]
            if isinstance(draft, dict)
            and str(draft.get("id", "")).strip()
            and self._draft_is_current(draft, now=now)
        ]
        if len(drafts) != len(raw):
            try:
                self._write_import_drafts(drafts)
            except OSError:
                pass
        return drafts

    @staticmethod
    def _draft_is_current(draft: dict[str, Any], *, now: datetime | None = None) -> bool:
        saved_at = str(draft.get("saved_at", "")).strip()
        if not saved_at:
            return False
        try:
            saved = datetime.fromisoformat(saved_at.replace("Z", "+00:00"))
            current = now or datetime.now(saved.tzinfo)
            if saved.tzinfo is not None and current.tzinfo is None:
                current = current.astimezone(saved.tzinfo)
            if saved.tzinfo is None and current.tzinfo is not None:
                current = current.replace(tzinfo=None)
        except ValueError:
            return False
        return current - saved <= ALM_IMPORT_DRAFT_MAX_AGE

    def _write_import_drafts(self, drafts: list[dict[str, Any]]) -> None:
        payload = json.dumps(drafts[:MAX_ALM_IMPORT_DRAFTS], ensure_ascii=False, indent=2)
        self.import_drafts_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.import_drafts_path.with_suffix(".tmp")
        temporary.write_text(payload + "\n", encoding="utf-8")
        temporary.replace(self.import_drafts_path)

    def import_drafts_json(self) -> list[dict[str, Any]]:
        with self.import_drafts_lock:
            return json.loads(json.dumps(self.import_drafts))

    def save_import_draft(self, raw: dict[str, Any]) -> list[dict[str, Any]]:
        if not isinstance(raw, dict):
            raise eudm.EUDMError("The ALM import draft was invalid.")
        draft_id = str(raw.get("id", "")).strip()
        if not draft_id or len(draft_id) > 100 or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
            for character in draft_id
        ):
            raise eudm.EUDMError("The ALM import draft identifier was invalid.")
        if not isinstance(raw.get("workbook"), dict) or not raw["workbook"].get("import_id"):
            raise eudm.EUDMError("The ALM import draft did not include workbook state.")
        try:
            stored = json.loads(json.dumps(raw))
        except (TypeError, ValueError) as exc:
            raise eudm.EUDMError("The ALM import draft could not be saved.") from exc
        stored["id"] = draft_id
        stored["saved_at"] = str(stored.get("saved_at", "")) or datetime.now().isoformat(timespec="seconds")
        with self.import_drafts_lock:
            drafts = [draft for draft in self.import_drafts if draft.get("id") != draft_id]
            drafts.insert(0, stored)
            self.import_drafts = drafts[:MAX_ALM_IMPORT_DRAFTS]
            self._write_import_drafts(self.import_drafts)
            return json.loads(json.dumps(self.import_drafts))

    def delete_import_draft(self, draft_id: str) -> list[dict[str, Any]]:
        with self.import_drafts_lock:
            self.import_drafts = [draft for draft in self.import_drafts if draft.get("id") != draft_id]
            self._write_import_drafts(self.import_drafts)
            return json.loads(json.dumps(self.import_drafts))

    @staticmethod
    def _normalise_request_queue(raw: Any) -> list[dict[str, Any]]:
        if not isinstance(raw, list):
            raise eudm.EUDMError("The saved request queue was invalid.")
        if len(raw) > MAX_QUEUED_REQUESTS:
            raise eudm.EUDMError(
                f"The saved request queue can contain at most {MAX_QUEUED_REQUESTS} requests."
            )
        if any(not isinstance(request, dict) for request in raw):
            raise eudm.EUDMError("Each saved queue request must be an object.")
        try:
            payload = json.dumps(raw, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise eudm.EUDMError("The request queue could not be saved.") from exc
        if len(payload.encode("utf-8")) > MAX_QUEUED_REQUEST_BYTES:
            raise eudm.EUDMError("The saved request queue is too large.")
        return json.loads(payload)

    def _load_request_queue(self) -> list[dict[str, Any]]:
        try:
            raw = json.loads(self.request_queue_path.read_text(encoding="utf-8"))
            return self._normalise_request_queue(raw)
        except (OSError, ValueError, TypeError, eudm.EUDMError):
            return []

    def _write_request_queue(self, requests: list[dict[str, Any]]) -> None:
        payload = json.dumps(requests, ensure_ascii=False, indent=2)
        self.request_queue_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.request_queue_path.with_suffix(".tmp")
        temporary.write_text(payload + "\n", encoding="utf-8")
        temporary.replace(self.request_queue_path)

    def request_queue_json(self) -> list[dict[str, Any]]:
        with self.request_queue_lock:
            return json.loads(json.dumps(self.request_queue))

    def save_request_queue(self, raw: Any) -> list[dict[str, Any]]:
        requests = self._normalise_request_queue(raw)
        with self.request_queue_lock:
            self._write_request_queue(requests)
            self.request_queue = requests
            return json.loads(json.dumps(self.request_queue))

    @staticmethod
    def _verification_cache_key(value: str) -> str:
        return " ".join(str(value or "").split()).casefold()

    def _load_verification_cache(self) -> dict[str, dict[str, dict[str, Any]]]:
        empty = {"serials": {}, "usernames": {}}
        try:
            raw = json.loads(self.verification_cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return empty
        if not isinstance(raw, dict):
            return empty
        cache: dict[str, dict[str, dict[str, Any]]] = {}
        for category in ("serials", "usernames"):
            values = raw.get(category)
            if not isinstance(values, dict):
                cache[category] = {}
                continue
            cache[category] = {
                str(key): dict(value)
                for key, value in list(values.items())[-VERIFICATION_CACHE_MAX_ENTRIES:]
                if isinstance(value, dict) and str(key).strip()
            }
        return {**empty, **cache}

    def _verification_cache_snapshot_locked(self) -> dict[str, dict[str, dict[str, Any]]]:
        return {
            category: {
                key: dict(value)
                for key, value in values.items()
            }
            for category, values in self.verification_cache.items()
        }

    def _write_verification_cache(
        self,
        snapshot: dict[str, dict[str, dict[str, Any]]] | None = None,
    ) -> None:
        source = snapshot if snapshot is not None else self.verification_cache
        payload = json.dumps(source, ensure_ascii=False, indent=2)
        write_lock = getattr(self, "verification_cache_write_lock", None)
        if write_lock is None:
            write_lock = threading.Lock()
            self.verification_cache_write_lock = write_lock
        with write_lock:
            self.verification_cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.verification_cache_path.with_suffix(".tmp")
            temporary.write_text(payload + "\n", encoding="utf-8")
            temporary.replace(self.verification_cache_path)

    def _verification_cache_aliases_locked(self, category: str) -> dict[str, str]:
        index = getattr(self, "verification_cache_alias_index", None)
        if index is None:
            index = {"serials": {}, "usernames": {}}
            for indexed_category, values in self.verification_cache.items():
                category_index = index[indexed_category]
                for stored_key, candidate in values.items():
                    aliases = [
                        stored_key,
                        candidate.get("value"),
                        *(candidate.get("columns") or []),
                    ]
                    # Values are stored oldest-to-newest, so later records
                    # intentionally win when two records share a display alias.
                    for alias in aliases:
                        alias_key = self._verification_cache_key(str(alias or ""))
                        if alias_key:
                            category_index[alias_key] = str(stored_key)
            self.verification_cache_alias_index = index
        return index.get(category, {})

    def verification_cache_lookup(self, kind: str, value: str) -> dict[str, Any] | None:
        category = "serials" if kind == "serial" else "usernames" if kind == "username" else ""
        key = self._verification_cache_key(value)
        if not category or not key:
            return None
        with self.verification_cache_lock:
            values = self.verification_cache[category]
            cached = values.get(key)
            if cached is None:
                stored_key = self._verification_cache_aliases_locked(category).get(key)
                if stored_key:
                    cached = values.get(stored_key)
            return deepcopy(cached) if cached else None

    def record_verified_serial(self, result: dict[str, Any]) -> None:
        self._record_verification("serials", result)

    def record_verified_username(self, result: dict[str, Any]) -> None:
        self._record_verification("usernames", result)

    def _record_verification(self, category: str, result: dict[str, Any]) -> None:
        value = str(result.get("value", "")).strip()
        key = self._verification_cache_key(value)
        if not key:
            return
        stored = {
            "value": value,
            "columns": [str(item) for item in result.get("columns", [])],
        }
        if category == "serials":
            stored["device_type"] = str(result.get("device_type", "") or "")
        snapshot: dict[str, dict[str, dict[str, Any]]] | None = None
        with self.verification_cache_lock:
            values = self.verification_cache[category]
            values.pop(key, None)
            values[key] = stored
            while len(values) > VERIFICATION_CACHE_MAX_ENTRIES:
                values.pop(next(iter(values)))
            self.verification_cache_alias_index = None
            now = time.monotonic()
            last_write = getattr(self, "verification_cache_last_write", 0.0)
            if now - last_write >= VERIFICATION_CACHE_WRITE_COALESCE_SECONDS:
                snapshot = self._verification_cache_snapshot_locked()
                self.verification_cache_dirty = False
                self.verification_cache_last_write = now
            else:
                self.verification_cache_dirty = True
                self._schedule_verification_cache_flush_locked()
        if snapshot is not None:
            try:
                self._write_verification_cache(snapshot)
            except OSError:
                with self.verification_cache_lock:
                    self.verification_cache_dirty = True
                    self._schedule_verification_cache_flush_locked()
                raise

    def _schedule_verification_cache_flush_locked(self) -> None:
        timer = getattr(self, "verification_cache_write_timer", None)
        if timer is not None and timer.is_alive():
            return
        timer = threading.Timer(
            VERIFICATION_CACHE_WRITE_COALESCE_SECONDS,
            self._flush_verification_cache,
        )
        timer.daemon = True
        self.verification_cache_write_timer = timer
        timer.start()

    def _flush_verification_cache(self) -> None:
        snapshot: dict[str, dict[str, dict[str, Any]]] | None = None
        with self.verification_cache_lock:
            self.verification_cache_write_timer = None
            if not getattr(self, "verification_cache_dirty", False):
                return
            snapshot = self._verification_cache_snapshot_locked()
            self.verification_cache_dirty = False
            self.verification_cache_last_write = time.monotonic()
        try:
            self._write_verification_cache(snapshot)
        except OSError:
            # The next verification will retry the deferred write. Cache
            # failures must not make an otherwise successful lookup fail.
            with self.verification_cache_lock:
                self.verification_cache_dirty = True
            run_reporting.event("Could not persist the verification cache")

    def flush_pending_state(self) -> None:
        """Persist deferred cache updates before the local server exits."""
        with self.verification_cache_lock:
            timer = getattr(self, "verification_cache_write_timer", None)
            if timer is not None:
                timer.cancel()
        self._flush_verification_cache()

    def _load_alm_backlog_ignored(self) -> dict[str, dict[str, str]]:
        try:
            raw = json.loads(self.alm_backlog_ignored_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        values = raw.get("ignored", raw) if isinstance(raw, dict) else raw
        if not isinstance(values, dict):
            return {}
        return {
            str(key): {
                "serial": str(value.get("serial", "")),
                "username": str(value.get("username", "")),
            }
            for key, value in values.items()
            if isinstance(value, dict) and str(key).strip()
        }

    def _write_alm_backlog_ignored(self) -> None:
        payload = json.dumps({"ignored": self.alm_backlog_ignored}, ensure_ascii=False, indent=2)
        self.alm_backlog_ignored_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.alm_backlog_ignored_path.with_suffix(".tmp")
        temporary.write_text(payload + "\n", encoding="utf-8")
        temporary.replace(self.alm_backlog_ignored_path)

    def alm_backlog_ignored_keys(self) -> set[str]:
        with self.alm_backlog_ignored_lock:
            return set(self.alm_backlog_ignored)

    def ignore_alm_backlog(self, serial: str, username: str) -> None:
        key = WorkbookImport.backlog_key(serial, username)
        if not key or "\u0000" not in key:
            raise eudm.EUDMError("The ALM backlog row was missing a serial or username.")
        with self.alm_backlog_ignored_lock:
            self.alm_backlog_ignored[key] = {
                "serial": " ".join(str(serial or "").split()),
                "username": " ".join(str(username or "").split()),
            }
            self._write_alm_backlog_ignored()

    def unignore_alm_backlog(self, serial: str, username: str) -> None:
        key = WorkbookImport.backlog_key(serial, username)
        if not key or "\u0000" not in key:
            raise eudm.EUDMError("The ALM backlog row was missing a serial or username.")
        with self.alm_backlog_ignored_lock:
            if key in self.alm_backlog_ignored:
                self.alm_backlog_ignored.pop(key, None)
                self._write_alm_backlog_ignored()

    def clear_alm_backlog_ignored(self) -> None:
        with self.alm_backlog_ignored_lock:
            self.alm_backlog_ignored = {}
            self._write_alm_backlog_ignored()

    def add_import(self, workbook: WorkbookImport) -> None:
        with self.import_lock:
            self.imports[workbook.import_id] = workbook
            if len(self.imports) > 10:
                first = next(iter(self.imports))
                self.imports.pop(first, None)

    @staticmethod
    def _workbook_job_payload(workbook: WorkbookImport) -> dict[str, Any]:
        payload = workbook.summary()
        if workbook._inspection_cache:
            payload["inspection"] = workbook._inspection_cache
        return payload

    def _import_payload_paths(self, import_id: str) -> tuple[Path, Path] | None:
        try:
            parsed = uuid.UUID(str(import_id))
        except (ValueError, AttributeError, TypeError):
            return None
        safe_id = parsed.hex
        return (
            self.import_payload_path / f"{safe_id}.workbook",
            self.import_payload_path / f"{safe_id}.json",
        )

    def _persist_import_payload(
        self,
        import_id: str,
        filename: str,
        payload: bytes,
        columns: inventory.ImportColumns | None = None,
    ) -> None:
        paths = self._import_payload_paths(import_id)
        if not paths:
            raise eudm.EUDMError("The workbook import identifier was invalid.")
        payload_path, metadata_path = paths
        metadata = {
            "import_id": str(import_id),
            "filename": filename,
            "columns": {
                key: getattr(columns, key)
                for key in (
                    "username",
                    "deployment_serial",
                    "returned_device",
                    "pending_return",
                    "enabled",
                    "device_allocation",
                    "new_asset_status",
                    "first_name",
                    "last_name",
                )
            } if columns else None,
            "saved_at": datetime.now().isoformat(timespec="seconds"),
        }
        try:
            with self.import_payload_lock:
                self.import_payload_path.mkdir(parents=True, exist_ok=True)
                payload_tmp = payload_path.with_name(payload_path.name + ".tmp")
                metadata_tmp = metadata_path.with_name(metadata_path.name + ".tmp")
                payload_tmp.write_bytes(payload)
                metadata_tmp.write_text(
                    json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                payload_tmp.replace(payload_path)
                metadata_tmp.replace(metadata_path)
        except OSError as exc:
            raise eudm.EUDMError(
                "The workbook could not be saved for import resume."
            ) from exc

    def _load_import_payload(
        self,
        import_id: str,
    ) -> tuple[str, bytes, inventory.ImportColumns | None] | None:
        paths = self._import_payload_paths(import_id)
        if not paths:
            return None
        payload_path, metadata_path = paths
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            payload = payload_path.read_bytes()
        except (OSError, ValueError, TypeError):
            return None
        if not isinstance(metadata, dict) or not payload:
            return None
        filename = str(metadata.get("filename", "ALM Workbook"))
        raw_columns = metadata.get("columns")
        columns = (
            inventory.columns_from_mapping(raw_columns)
            if isinstance(raw_columns, dict)
            else None
        )
        return filename, payload, columns

    def _register_import_job(self, job: ImportJob) -> None:
        """Retain bounded history without evicting jobs still being polled."""
        with self.import_lock:
            self.import_jobs[job.job_id] = job
            while len(self.import_jobs) > MAX_IMPORT_JOBS:
                removable = next(
                    (
                        job_id
                        for job_id, existing in self.import_jobs.items()
                        if job_id != job.job_id
                        and existing.is_terminal()
                    ),
                    None,
                )
                if removable is None:
                    break
                self.import_jobs.pop(removable, None)

    def start_import(self, filename: str, encoded: str) -> ImportJob:
        job = ImportJob(job_id=uuid.uuid4().hex, filename=filename)
        self._register_import_job(job)
        threading.Thread(
            target=self._inspect_import,
            args=(job, encoded),
            daemon=True,
        ).start()
        return job

    def start_mapped_import(
        self,
        import_id: str,
        columns: dict[str, Any],
    ) -> ImportJob:
        with self.import_lock:
            pending = self.pending_imports.pop(import_id, None)
            source = getattr(self, "pending_import_paths", {}).pop(import_id, None)
        if not pending:
            restored = self._load_import_payload(import_id)
            if not restored:
                raise eudm.EUDMError("That workbook import expired. Choose the file again.")
            filename, payload, _ = restored
        else:
            filename, payload = pending
        job = ImportJob(job_id=uuid.uuid4().hex, filename=filename)
        self._register_import_job(job)
        threading.Thread(
            target=self._read_import,
            args=(job, payload, inventory.columns_from_mapping(columns)),
            kwargs={
                "source_path": source[0] if source else None,
                "source_signature": source[1] if source else None,
                "source_sync_before_load": source[2] if source else False,
                "source_inspection": source[3] if source else None,
            },
            daemon=True,
        ).start()
        return job

    def _inspect_import(self, job: ImportJob, encoded: str) -> None:
        job.update(state="reading", message="Reading workbook headings…")
        try:
            payload = WorkbookImport.decode_upload(job.filename, encoded)
            inspected = WorkbookImport.inspect_payload(job.filename, payload)
            import_id = uuid.uuid4().hex
            inspected["import_id"] = import_id
            self._persist_import_payload(import_id, job.filename, payload)
            with self.import_lock:
                self.pending_imports[import_id] = (job.filename, payload)
                while len(self.pending_imports) > MAX_PENDING_IMPORTS:
                    expired_id = next(iter(self.pending_imports))
                    self.pending_imports.pop(expired_id, None)
                    getattr(self, "pending_import_paths", {}).pop(expired_id, None)
            job.finish(inspected)
        except eudm.EUDMError as exc:
            job.fail(str(exc))

    def _inspect_local_import(
        self,
        job: ImportJob,
        path: Path,
        sync_before_load: bool,
    ) -> None:
        job.update(state="reading", message="Loading the SharePoint ALM workbook…")
        try:
            with self.preferences_lock:
                saved_columns = inventory.columns_from_mapping(
                    self.preferences.get("import_columns")
                )
            if not sync_before_load:
                signature = self._local_workbook_signature(path)
                if signature and path.is_file():
                    cached = self._load_local_workbook_cache(
                        path,
                        signature,
                        saved_columns,
                    )
                    if cached:
                        self.add_import(cached)
                        job.finish(self._workbook_job_payload(cached))
                        return
            payload, signature = self._read_local_workbook_payload(
                path,
                sync_before_load=sync_before_load,
            )
            # The optional freshness check still benefits from the parsed
            # cache once OneDrive has confirmed a stable local file. Reading
            # the file is enough to hydrate a File Provider placeholder; a
            # cached signature then avoids re-parsing thousands of rows.
            cached = self._load_local_workbook_cache(path, signature, saved_columns)
            if cached:
                self.add_import(cached)
                job.finish(self._workbook_job_payload(cached))
                return
            inspected = WorkbookImport.inspect_payload(path.name, payload)
            import_id = uuid.uuid4().hex
            inspected["import_id"] = import_id
            inspected["local_source"] = True
            self._persist_import_payload(import_id, path.name, payload)
            with self.import_lock:
                self.pending_imports[import_id] = (path.name, payload)
                pending_paths = getattr(self, "pending_import_paths", None)
                if pending_paths is None:
                    pending_paths = {}
                    self.pending_import_paths = pending_paths
                pending_paths[import_id] = (
                    path,
                    signature,
                    sync_before_load,
                    inspected,
                )
                while len(self.pending_imports) > MAX_PENDING_IMPORTS:
                    expired_id = next(iter(self.pending_imports))
                    self.pending_imports.pop(expired_id, None)
                    pending_paths.pop(expired_id, None)
            job.finish(inspected)
        except eudm.EUDMError as exc:
            job.fail(str(exc))
        except Exception:
            job.fail(
                "The SharePoint ALM workbook could not be read. "
                "Check the saved path and try again."
            )

    def _read_import(
        self,
        job: ImportJob,
        payload: bytes,
        columns: inventory.ImportColumns,
        *,
        source_path: Path | None = None,
        source_signature: tuple[int, int] | None = None,
        source_sync_before_load: bool = False,
        source_inspection: dict[str, Any] | None = None,
    ) -> None:
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
            if source_path and source_signature and not source_sync_before_load:
                cached = self._load_local_workbook_cache(
                    source_path,
                    source_signature,
                    columns,
                )
                if cached:
                    self.add_import(cached)
                    job.finish(self._workbook_job_payload(cached))
                    return
            workbook = WorkbookImport.from_payload(
                job.filename, payload, columns=columns, on_progress=progress
            )
            self._persist_import_payload(workbook.import_id, job.filename, payload, columns)
            if source_path and source_signature:
                if source_inspection:
                    workbook._inspection_cache = {
                        **source_inspection,
                        "import_id": workbook.import_id,
                    }
                self._write_local_workbook_cache(
                    source_path,
                    source_signature,
                    columns,
                    workbook,
                )
            self.add_import(workbook)
            job.finish(self._workbook_job_payload(workbook))
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
        if workbook:
            return workbook
        restored = self._load_import_payload(import_id)
        if not restored:
            raise eudm.EUDMError(
                "That spreadsheet import expired. Choose the file again."
            )
        filename, payload, columns = restored
        if columns is None:
            raise eudm.EUDMError(
                "That workbook still needs its columns mapped. Resume the import from the column step."
            )
        try:
            workbook = WorkbookImport.from_payload(filename, payload, columns=columns)
        except Exception as exc:
            raise eudm.EUDMError(
                "The saved workbook could not be restored. Choose the file again."
            ) from exc
        workbook.import_id = str(import_id)
        self.add_import(workbook)
        return workbook
