"""Read-only PC Toolkit enrichment for AutoEUDM.

PC Toolkit combines CMDB and SCCM records behind a small lookup API.  This
module deliberately normalises that response before it reaches the UI: the
raw payload is large, contains more personal data than AutoEUDM needs, and a
single search can contain both active and stale records for the same device.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable
import urllib.error
import urllib.parse
import urllib.request

from .eudm_request import EUDMError
from . import run_reporting


DEFAULT_DEVICE_URL = (
    "https://autoscalecomponent.prod-eapi-devices.wkpautoapps.iptauto."
    "syd.c1.macquarie.com/v1/Computers"
)
DEFAULT_PORTAL_URL = (
    "https://portal.platform.infraportal.syd.c1.macquarie.com/details/45sf2q7-07c"
)
DEFAULT_ROLE_URL = (
    "https://portal.platform.infraportal.syd.c1.macquarie.com/auth/session/maxroles"
)
ACTIVE_CACHE_SECONDS = 10 * 60
STALE_CACHE_SECONDS = 30 * 24 * 60 * 60
MAX_CACHE_ENTRIES = 10_000
MAX_PARALLEL_LOOKUPS = 10


class PCToolkitError(EUDMError):
    pass


def normalise_key(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _people(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    people: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        login = clean(item.get("loginID") or item.get("userId"))
        name = clean(item.get("fullName"))
        role = clean(item.get("role") or item.get("computerUserType"))
        key = (normalise_key(login or name), normalise_key(role))
        if not key[0] or key in seen:
            continue
        seen.add(key)
        people.append({"login": login, "name": name, "role": role})
    return people


def _status_rank(status: str) -> int:
    return {
        "deployed": 90,
        "in inventory": 80,
        "received": 70,
        "in repair": 60,
        "pending rebuild": 55,
        "pending decom": 50,
        "disposed": 10,
        "delete": 0,
    }.get(normalise_key(status), 40)


def _is_active(status: str) -> bool:
    return normalise_key(status) not in {"delete", "disposed"}


def normalise_device(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    cmdb = raw.get("cmdb") if isinstance(raw.get("cmdb"), dict) else {}
    sccm = raw.get("sccm") if isinstance(raw.get("sccm"), dict) else {}
    details = cmdb.get("ciDetails") if isinstance(cmdb.get("ciDetails"), dict) else {}
    cmdb_people = _people(cmdb.get("people"))
    primary_users = _people(sccm.get("primaryUsers"))
    profile_users = _people(sccm.get("profileUsers"))
    used_by = next((person for person in cmdb_people if normalise_key(person["role"]) == "used by"), None)
    owner = next((person for person in cmdb_people if normalise_key(person["role"]) == "owned by"), None)
    assigned = used_by or (primary_users[0] if primary_users else None)
    status = clean(details.get("status"))
    model = clean(sccm.get("model") or details.get("additionalInformation") or details.get("productName"))
    serial = clean(sccm.get("serial") or details.get("serialNumber"))
    name = clean(sccm.get("name") or details.get("ciName") or raw.get("name"))
    location = " · ".join(
        value for value in (
            clean(details.get("site")),
            clean(details.get("siteGroup")),
            clean(details.get("region")),
        ) if value
    )
    device = {
        "serial": serial,
        "name": name,
        "model": model,
        "manufacturer": clean(sccm.get("manufacturer") or details.get("manufacturerName")),
        "status": status,
        "active": _is_active(status),
        "has_sccm": bool(sccm),
        "has_cmdb": bool(cmdb),
        "assigned_user": assigned or None,
        "used_by": used_by or None,
        "owner": owner or None,
        "people": cmdb_people,
        "primary_users": primary_users,
        "profile_users": profile_users,
        "location": location,
        "organisation": clean(details.get("organisation")),
        "department": clean(details.get("department")),
        "description": clean(details.get("ciDescription")),
        "legal_hold": clean(cmdb.get("legalHold")),
        "ci_id": clean(details.get("ciid")),
        "reconciliation_id": clean(details.get("reconciliationId")),
        "sccm_freshness": {
            "hardware_inventory_days": sccm.get("daysSinceHWInventory"),
            "health_check_days": sccm.get("daysSinceHealthCheck"),
            "software_inventory_days": sccm.get("daysSinceSWInventory"),
        } if sccm else None,
        "hardware": {
            "chassis": clean(sccm.get("chassisType")),
            "memory_gb": sccm.get("memoryTotalGB"),
            "operating_system": clean(sccm.get("operatingSystem")),
            "os_build": clean(sccm.get("osBuild")),
            "bitlocker": clean(sccm.get("bitLockerProtectionStatus")),
            "tpm_enabled": sccm.get("tpmEnabled"),
            "reboot_needed": sccm.get("rebootNeeded"),
        } if sccm else None,
    }
    device["_rank"] = (
        (100 if device["active"] else 0)
        + (30 if device["has_sccm"] else 0)
        + _status_rank(status)
        + (5 if assigned else 0)
    )
    return device


def normalise_lookup(payload: Any, query: str) -> dict[str, Any]:
    raw_devices = payload.get("devices", []) if isinstance(payload, dict) else []
    devices = [device for item in raw_devices if (device := normalise_device(item))]
    devices.sort(key=lambda item: (-int(item.get("_rank", 0)), normalise_key(item.get("serial")), normalise_key(item.get("name"))))
    for device in devices:
        device.pop("_rank", None)
    active = [device for device in devices if device.get("active")]
    primary = active[0] if active else (devices[0] if devices else None)
    active_signatures = {
        (
            normalise_key(device.get("serial")),
            normalise_key(device.get("status")),
            normalise_key((device.get("assigned_user") or {}).get("login")),
        )
        for device in active
    }
    ambiguous = len(active_signatures) > 1
    key = normalise_key(query)
    matches_person = any(
        key == normalise_key(person.get("login"))
        for device in devices
        for person in [
            *(device.get("people") or []),
            *(device.get("primary_users") or []),
            *(device.get("profile_users") or []),
        ]
    )
    return {
        "query": clean(query),
        "found": bool(devices),
        "lookup_kind": "username" if matches_person else "device",
        "primary": primary,
        "devices": devices,
        "active_count": len(active),
        "record_count": len(devices),
        "ambiguous": ambiguous,
        "warning": (
            "PC Toolkit returned multiple conflicting active records."
            if ambiguous else ""
        ),
    }


class PCToolkitClient:
    def __init__(self, base_url: str = DEFAULT_DEVICE_URL, role: str = "", timeout: float = 18.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.role = role.strip()
        self.timeout = timeout

    def lookup(self, query: str) -> dict[str, Any]:
        value = clean(query)
        if len(value) < 2:
            raise PCToolkitError("Enter at least two characters for PC Toolkit.")
        url = f"{self.base_url}/{urllib.parse.quote(value, safe='-._')}?sources=cmdb,sccm"
        # The device service is behind the portal's elevated-role gateway.
        # These are the same origin/referrer headers sent by the portal's
        # browser client; without them the gateway can reject a valid role.
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://portal.platform.infraportal.syd.c1.macquarie.com",
            "Referer": "https://portal.platform.infraportal.syd.c1.macquarie.com/",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
        }
        if self.role:
            headers["X-Max-Elevated-Role"] = self.role
        started = time.monotonic()
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            run_reporting.network(
                "GET", f"pc-toolkit/v1/Computers/{value}", status=exc.code,
                duration_ms=round((time.monotonic() - started) * 1000),
                transport="pc-toolkit", error="HTTPError",
            )
            if exc.code in {401, 403}:
                raise PCToolkitError("PC Toolkit authentication is required.") from exc
            raise PCToolkitError(f"PC Toolkit returned HTTP {exc.code}.") from exc
        except (OSError, urllib.error.URLError) as exc:
            run_reporting.network(
                "GET", f"pc-toolkit/v1/Computers/{value}",
                duration_ms=round((time.monotonic() - started) * 1000),
                transport="pc-toolkit", error=type(exc).__name__,
            )
            raise PCToolkitError("PC Toolkit could not be reached.") from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PCToolkitError("PC Toolkit returned an unreadable response.") from exc
        result = normalise_lookup(payload, value)
        result["duration_ms"] = round((time.monotonic() - started) * 1000)
        run_reporting.network(
            "GET", f"pc-toolkit/v1/Computers/{value}", status=200,
            duration_ms=result["duration_ms"], transport="pc-toolkit",
            response_body={
                "found": result["found"],
                "record_count": result["record_count"],
                "ambiguous": result["ambiguous"],
            },
        )
        return result


class PCToolkitService:
    """Optional enrichment service with a stale-while-revalidate file cache."""

    def __init__(
        self,
        cache_path: Path,
        *,
        simulate: bool = False,
        browser_profile: str = "",
        browser_headless: bool = False,
        verbose: bool = False,
        preferences: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.cache_path = cache_path
        self.simulate = simulate
        self.browser_profile = browser_profile
        self.browser_headless = browser_headless
        self.verbose = verbose
        self.preferences = preferences or (lambda: {})
        self.lock = threading.RLock()
        self.cache = self._load_cache()
        self.models = self._load_models()
        self.cache_write_timer: threading.Timer | None = None
        self.inflight: set[str] = set()
        self.role = os.getenv("PC_TOOLKIT_ROLE", "").strip()
        self.state = "simulation" if simulate else "idle"
        self.message = "Simulation data available." if simulate else "Not connected."
        self.last_error = ""
        self.connected_at: str | None = None

    def enabled(self) -> bool:
        return bool(self.preferences().get("pc_toolkit_enabled", False))

    def _load_cache(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        entries = raw.get("entries", raw) if isinstance(raw, dict) else {}
        if not isinstance(entries, dict):
            return {}
        return {
            str(key): dict(value)
            for key, value in list(entries.items())[-MAX_CACHE_ENTRIES:]
            if isinstance(value, dict)
        }

    def _load_models(self) -> set[str]:
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return set()
        if not isinstance(raw, dict):
            return set()
        candidates = list(raw.get("models", [])) if isinstance(raw.get("models"), list) else []
        entries = raw.get("entries", raw)
        if isinstance(entries, dict):
            for entry in entries.values():
                result = entry.get("result", {}) if isinstance(entry, dict) else {}
                devices = result.get("devices", []) if isinstance(result, dict) else []
                candidates.extend(
                    device.get("model")
                    for device in devices
                    if isinstance(device, dict)
                )
        by_key: dict[str, str] = {}
        for model in candidates:
            value = clean(model)
            if value:
                by_key.setdefault(normalise_key(value), value)
        return set(by_key.values())

    def _remember_models_locked(self, result: dict[str, Any]) -> None:
        existing = {normalise_key(model) for model in self.models}
        for device in result.get("devices", []):
            model = clean(device.get("model")) if isinstance(device, dict) else ""
            key = normalise_key(model)
            if model and key not in existing:
                self.models.add(model)
                existing.add(key)

    def _write_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix(".tmp")
        payload = {"version": 2, "entries": self.cache, "models": sorted(self.models, key=str.casefold)}
        temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        temporary.replace(self.cache_path)

    def _schedule_cache_write_locked(self) -> None:
        if self.cache_write_timer is not None and self.cache_write_timer.is_alive():
            return
        self.cache_write_timer = threading.Timer(0.5, self._flush_cache)
        self.cache_write_timer.daemon = True
        self.cache_write_timer.start()

    def _flush_cache(self) -> None:
        with self.lock:
            self.cache_write_timer = None
            self._write_cache()

    def status(self) -> dict[str, Any]:
        with self.lock:
            return {
                "enabled": self.enabled(),
                "state": self.state,
                "message": self.message,
                "connected_at": self.connected_at,
                "cached_queries": len(self.cache),
                "models": sorted(self.models, key=str.casefold),
                "last_error": self.last_error,
            }

    def clear_cache(self) -> None:
        with self.lock:
            self.cache = {}
            self._write_cache()

    def clear_models(self) -> None:
        with self.lock:
            self.models = set()
            self._write_cache()

    def _simulation_lookup(self, query: str) -> dict[str, Any]:
        value = clean(query)
        username = "." in value or value.casefold().startswith("user")
        serial = f"SIM{abs(hash(normalise_key(value))) % 10_000_000:07d}"
        devices = []
        for index in range(2 if username else 1):
            device_serial = serial if index == 0 else f"{serial[:-1]}{index}"
            devices.append({
                "serial": device_serial,
                "name": f"SIM-{device_serial[-6:]}",
                "model": "MacBook Pro (14-inch, 2023)" if index == 0 else "Latitude 7440",
                "manufacturer": "Apple" if index == 0 else "Dell",
                "status": "Deployed" if username else "In Inventory",
                "active": True,
                "has_sccm": True,
                "has_cmdb": True,
                "assigned_user": {"login": value if username else "simulated.user", "name": "Simulated User", "role": "Used by"},
                "used_by": {"login": value if username else "simulated.user", "name": "Simulated User", "role": "Used by"},
                "owner": None,
                "people": [], "primary_users": [], "profile_users": [],
                "location": "Sydney · 1 Elizabeth Street",
                "organisation": "Technology", "department": "Workplace",
                "description": "Simulated PC Toolkit result", "legal_hold": "NotFlagged",
                "ci_id": f"SIM-CI-{index}", "reconciliation_id": "",
                "sccm_freshness": {"hardware_inventory_days": 1, "health_check_days": 1, "software_inventory_days": 2},
                "hardware": {"chassis": "Laptop", "memory_gb": 16, "operating_system": "macOS", "os_build": "", "bitlocker": "", "tpm_enabled": True, "reboot_needed": False},
            })
        return {
            "query": value, "found": True,
            "lookup_kind": "username" if username else "device",
            "primary": devices[0], "devices": devices,
            "active_count": len(devices), "record_count": len(devices),
            "ambiguous": False, "warning": "", "duration_ms": 5,
        }

    def _client(self) -> PCToolkitClient:
        return PCToolkitClient(role=self.role)

    def _fetch(self, query: str) -> dict[str, Any]:
        result = self._simulation_lookup(query) if self.simulate else self._client().lookup(query)
        key = normalise_key(query)
        stored = {"fetched_at": time.time(), "result": result}
        with self.lock:
            self._remember_models_locked(result)
            self.cache.pop(key, None)
            self.cache[key] = stored
            while len(self.cache) > MAX_CACHE_ENTRIES:
                self.cache.pop(next(iter(self.cache)))
            self._schedule_cache_write_locked()
            self.state = "simulation" if self.simulate else "connected"
            self.message = "PC Toolkit enrichment is ready."
            self.last_error = ""
            self.connected_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return deepcopy(result)

    def _refresh(self, query: str) -> None:
        key = normalise_key(query)
        try:
            self._fetch(query)
        except PCToolkitError as exc:
            with self.lock:
                self.last_error = str(exc)
                if not self.cache.get(key):
                    self.state = "error"
                    self.message = str(exc)
        finally:
            with self.lock:
                self.inflight.discard(key)

    def lookup(self, query: str, *, fresh: bool = False) -> dict[str, Any]:
        if not self.enabled() and not self.simulate:
            raise PCToolkitError("PC Toolkit enrichment is disabled in Settings.")
        key = normalise_key(query)
        if len(key) < 2:
            raise PCToolkitError("Enter at least two characters for PC Toolkit.")
        now = time.time()
        with self.lock:
            cached = deepcopy(self.cache.get(key))
        if cached and not fresh:
            age = max(0.0, now - float(cached.get("fetched_at", 0)))
            if age <= STALE_CACHE_SECONDS:
                result = deepcopy(cached.get("result", {}))
                result["cached"] = True
                result["stale"] = age > ACTIVE_CACHE_SECONDS
                result["age_seconds"] = round(age)
                if age > ACTIVE_CACHE_SECONDS:
                    with self.lock:
                        if key not in self.inflight:
                            self.inflight.add(key)
                            threading.Thread(target=self._refresh, args=(query,), daemon=True).start()
                return result
        return self._fetch(query)

    def bulk_lookup(self, queries: list[str], *, fresh: bool = False) -> dict[str, Any]:
        unique: dict[str, str] = {}
        for query in queries:
            value = clean(query)
            key = normalise_key(value)
            if len(key) >= 2:
                unique.setdefault(key, value)
        results: dict[str, Any] = {}
        errors: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_LOOKUPS, max(1, len(unique)))) as executor:
            futures = {executor.submit(self.lookup, value, fresh=fresh): key for key, value in unique.items()}
            for future in as_completed(futures):
                key = futures[future]
                try:
                    results[key] = future.result()
                except PCToolkitError as exc:
                    errors[key] = str(exc)
        return {"results": results, "errors": errors, "status": self.status()}

    def connect_async(self) -> None:
        if self.simulate:
            with self.lock:
                self.state = "simulation"
                self.message = "Simulation data available."
            return
        with self.lock:
            if self.state == "connecting":
                return
            self.state = "connecting"
            self.message = "Connecting to PC Toolkit…"
            self.last_error = ""
        threading.Thread(target=self._connect, daemon=True).start()

    def _connect(self) -> None:
        try:
            # Most installations accept the role-less read request.  Use a
            # harmless query so connecting does not expose a real user/device.
            self._client().lookup("auto-eudm-health-check")
        except PCToolkitError as first_error:
            if "authentication" not in str(first_error).casefold():
                with self.lock:
                    self.state = "error"
                    self.message = str(first_error)
                    self.last_error = str(first_error)
                return
            try:
                self.role = self._discover_role()
            except PCToolkitError as exc:
                with self.lock:
                    self.state = "error"
                    self.message = str(exc)
                    self.last_error = str(exc)
                return
        with self.lock:
            self.state = "connected"
            self.message = "PC Toolkit enrichment is ready."
            self.connected_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _discover_role(self) -> str:
        if not self.browser_profile:
            raise PCToolkitError("Open PC Toolkit once, then connect again.")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise PCToolkitError("Browser support is not installed for PC Toolkit.") from exc
        playwright = sync_playwright().start()
        context = None
        try:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(Path(self.browser_profile).expanduser()),
                channel="chrome",
                headless=self.browser_headless,
            )
            pages = context.pages or [context.new_page()]
            page = pages[0]
            page.goto(DEFAULT_PORTAL_URL, wait_until="domcontentloaded", timeout=60_000)
            deadline = time.monotonic() + (20 if self.browser_headless else 120)
            while time.monotonic() < deadline:
                # Use the context request client rather than page JavaScript:
                # this preserves the persistent browser cookies without being
                # affected by a welcome-page redirect or cross-origin policy.
                try:
                    response = context.request.get(DEFAULT_ROLE_URL, timeout=5_000)
                    if response.ok:
                        payload = response.json()
                        roles = payload.get("maxRoles", []) if isinstance(payload, dict) else []
                        if isinstance(roles, list) and roles and clean(roles[0]):
                            return clean(roles[0])
                except Exception:
                    pass
                # Some SSO flows finish in a newly opened tab. Refresh the
                # list so a successful login in that tab is observed too.
                pages = context.pages or [page]
                page.wait_for_timeout(1500)
            raise PCToolkitError("PC Toolkit sign-in did not complete. Open it in Chrome and try again.")
        except PCToolkitError:
            raise
        except Exception as exc:
            raise PCToolkitError("PC Toolkit authentication could not be opened.") from exc
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            playwright.stop()
