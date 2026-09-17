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
import gzip
import json
import os
import platform
from pathlib import Path
import sys
import threading
import time
from typing import Any, Callable
import urllib.error
import urllib.parse
import urllib.request
import zlib

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
# The production PC Toolkit client sends this role on its read requests.  It
# is also returned by the portal's maxroles endpoint for the normal personal
# session.  Keeping it as a default means enrichment works immediately after
# Helix/PC Toolkit authentication instead of requiring a separate role
# discovery browser pass.  PC_TOOLKIT_ROLE still overrides it for accounts
# with a different elevated role.
DEFAULT_PC_TOOLKIT_ROLE = "maxrole:personal"
PC_TOOLKIT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)
PC_TOOLKIT_CLIENT_HINT = (
    '"Chromium";v="152", "Not?A_Brand";v="24", "Google Chrome";v="152"'
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


def pc_toolkit_request_headers(role: str) -> dict[str, str]:
    """Return the request shape used by PC Toolkit's production browser bundle.

    The device gateway distinguishes the browser fetch from a generic HTTP
    client.  Keep these headers aligned with a current successful browser
    capture instead of sending only Origin and the elevated role.
    """
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
        "Origin": "https://portal.platform.infraportal.syd.c1.macquarie.com",
        "Referer": "https://portal.platform.infraportal.syd.c1.macquarie.com/",
        "Sec-CH-UA": PC_TOOLKIT_CLIENT_HINT,
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": '"macOS"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
        "User-Agent": PC_TOOLKIT_USER_AGENT,
    }
    if role.strip():
        headers["X-Max-Elevated-Role"] = role.strip()
    return headers


def _decoded_http_body(raw: bytes, headers: Any) -> bytes:
    """Decode the encodings advertised by ``pc_toolkit_request_headers``."""
    try:
        encoding = str(headers.get("Content-Encoding", "")).casefold().strip()
    except (AttributeError, TypeError):
        encoding = ""
    if encoding == "gzip":
        return gzip.decompress(raw)
    if encoding == "deflate":
        try:
            return zlib.decompress(raw)
        except zlib.error:
            return zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw


def _playwright_request_key(request: Any) -> int:
    """Use Playwright's shared implementation object across event wrappers."""
    return id(getattr(request, "_impl_obj", request))


def _profile_lock_is_live(profile: str) -> bool:
    """Return whether Chrome's profile lock belongs to a live local process."""
    if platform.system() != "Darwin":
        return False
    lock_path = Path(profile).expanduser() / "SingletonLock"
    if not os.path.lexists(lock_path):
        return False
    try:
        target = os.readlink(lock_path)
        pid = int(target.rsplit("-", 1)[-1])
    except (OSError, TypeError, ValueError):
        # An unfamiliar lock format is safer to wait on briefly than to launch
        # a second Chrome process into the same profile.
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _profile_in_use_error(exc: BaseException) -> bool:
    detail = " ".join(str(exc).split()).casefold()
    return any(
        marker in detail
        for marker in (
            "opening in existing browser session",
            "processsingleton",
            "profile appears to be in use",
            "user data directory is already in use",
        )
    )


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
    def __init__(
        self,
        base_url: str = DEFAULT_DEVICE_URL,
        role: str = DEFAULT_PC_TOOLKIT_ROLE,
        timeout: float = 18.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.role = role.strip()
        self.timeout = timeout

    def lookup(
        self,
        query: str,
        *,
        request_id: str | None = None,
        operation_id: str | None = None,
        purpose: str = "lookup",
    ) -> dict[str, Any]:
        value = clean(query)
        request_id = request_id or run_reporting.diagnostic_id("pc-http")
        started_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        started = time.monotonic()
        if len(value) < 2:
            run_reporting.pc_toolkit_event(
                "lookup_rejected",
                request_id=request_id,
                operation_id=operation_id,
                purpose=purpose,
                query=value,
                reason="query_too_short",
            )
            raise PCToolkitError("Enter at least two characters for PC Toolkit.")
        url = f"{self.base_url}/{urllib.parse.quote(value, safe='-._')}?sources=cmdb,sccm"
        endpoint_path = urllib.parse.urlsplit(url).path
        headers = pc_toolkit_request_headers(self.role)
        run_reporting.pc_toolkit_event(
            "api_request_started",
            request_id=request_id,
            operation_id=operation_id,
            purpose=purpose,
            method="GET",
            query=value,
            request_url=url,
            request_headers=headers,
            timeout_seconds=self.timeout,
            request_body_present=False,
            started_at=started_at,
        )
        request = urllib.request.Request(url, headers=headers)
        response_status: int | None = None
        response_headers: dict[str, str] | None = None
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                response_status = int(getattr(response, "status", 200))
                raw_headers = getattr(response, "headers", None)
                if raw_headers is not None and hasattr(raw_headers, "items"):
                    response_headers = {
                        str(key): str(value) for key, value in raw_headers.items()
                    }
                raw = _decoded_http_body(raw, raw_headers)
        except urllib.error.HTTPError as exc:
            try:
                error_body = exc.read()
            except OSError:
                error_body = None
            raw_headers = getattr(exc, "headers", None)
            if raw_headers is not None and hasattr(raw_headers, "items"):
                response_headers = {
                    str(key): str(value) for key, value in raw_headers.items()
                }
            if error_body is not None:
                try:
                    error_body = _decoded_http_body(error_body, raw_headers)
                except (OSError, EOFError, zlib.error):
                    # Preserve the original bytes in diagnostics if a gateway
                    # labels an error response with the wrong encoding.
                    pass
            run_reporting.network(
                "GET", endpoint_path, status=exc.code,
                duration_ms=round((time.monotonic() - started) * 1000),
                transport="pc-toolkit", error="HTTPError",
                request_url=url,
                request_headers=headers,
                response_headers=response_headers,
                response_body=error_body,
                request_id=request_id,
                operation_id=operation_id,
                error_detail=str(exc),
                details={
                    "channel": "urllib",
                    "purpose": purpose,
                    "query": value,
                    "started_at": started_at,
                    "response_body_present": error_body is not None,
                    "response_body_bytes_read": len(error_body or b""),
                },
            )
            run_reporting.pc_toolkit_event(
                "api_request_failed",
                request_id=request_id,
                operation_id=operation_id,
                purpose=purpose,
                query=value,
                status=exc.code,
                duration_ms=round((time.monotonic() - started) * 1000),
                exception=run_reporting.exception_details(exc),
                response_body_bytes_read=len(error_body or b""),
            )
            if exc.code in {401, 403}:
                raise PCToolkitError("PC Toolkit authentication is required.") from exc
            raise PCToolkitError(f"PC Toolkit returned HTTP {exc.code}.") from exc
        except (OSError, urllib.error.URLError) as exc:
            run_reporting.network(
                "GET", endpoint_path,
                duration_ms=round((time.monotonic() - started) * 1000),
                transport="pc-toolkit", error=type(exc).__name__,
                request_url=url,
                request_headers=headers,
                request_id=request_id,
                operation_id=operation_id,
                error_detail=str(exc),
                details={
                    "channel": "urllib",
                    "purpose": purpose,
                    "query": value,
                    "started_at": started_at,
                    "response_body_present": False,
                },
            )
            run_reporting.pc_toolkit_event(
                "api_request_failed",
                request_id=request_id,
                operation_id=operation_id,
                purpose=purpose,
                query=value,
                duration_ms=round((time.monotonic() - started) * 1000),
                exception=run_reporting.exception_details(exc),
            )
            raise PCToolkitError("PC Toolkit could not be reached.") from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            run_reporting.network(
                "GET", endpoint_path, status=response_status or 200,
                duration_ms=round((time.monotonic() - started) * 1000),
                transport="pc-toolkit", error="InvalidJSON",
                request_url=url,
                request_headers=headers,
                response_headers=response_headers,
                response_body=raw,
                request_id=request_id,
                operation_id=operation_id,
                error_detail=str(exc),
                details={
                    "channel": "urllib",
                    "purpose": purpose,
                    "query": value,
                    "started_at": started_at,
                    "response_body_present": True,
                },
            )
            run_reporting.pc_toolkit_event(
                "response_parse_failed",
                request_id=request_id,
                operation_id=operation_id,
                purpose=purpose,
                query=value,
                status=response_status or 200,
                duration_ms=round((time.monotonic() - started) * 1000),
                exception=run_reporting.exception_details(exc),
            )
            raise PCToolkitError("PC Toolkit returned an unreadable response.") from exc
        try:
            result = normalise_lookup(payload, value)
        except Exception as exc:
            run_reporting.network(
                "GET", endpoint_path, status=response_status or 200,
                duration_ms=round((time.monotonic() - started) * 1000),
                transport="pc-toolkit", error="NormalisationError",
                request_url=url,
                request_headers=headers,
                response_headers=response_headers,
                response_body=payload,
                request_id=request_id,
                operation_id=operation_id,
                error_detail=str(exc),
                details={
                    "channel": "urllib",
                    "purpose": purpose,
                    "query": value,
                    "started_at": started_at,
                },
            )
            run_reporting.pc_toolkit_event(
                "response_normalisation_failed",
                request_id=request_id,
                operation_id=operation_id,
                purpose=purpose,
                query=value,
                status=response_status or 200,
                duration_ms=round((time.monotonic() - started) * 1000),
                exception=run_reporting.exception_details(exc),
            )
            raise PCToolkitError("PC Toolkit returned an unreadable response.") from exc
        result["duration_ms"] = round((time.monotonic() - started) * 1000)
        run_reporting.network(
            "GET", endpoint_path, status=response_status or 200,
            duration_ms=result["duration_ms"], transport="pc-toolkit",
            request_url=url,
            request_headers=headers,
            response_headers=response_headers,
            response_body=payload,
            request_id=request_id,
            operation_id=operation_id,
            details={
                "channel": "urllib",
                "purpose": purpose,
                "query": value,
                "started_at": started_at,
                "response_body_present": True,
            },
        )
        run_reporting.pc_toolkit_event(
            "response_normalised",
            request_id=request_id,
            operation_id=operation_id,
            purpose=purpose,
            query=value,
            status=response_status or 200,
            duration_ms=result["duration_ms"],
            payload_type=type(payload).__name__,
            payload_keys=sorted(payload.keys()) if isinstance(payload, dict) else [],
            raw_device_count=(
                len(payload.get("devices", []))
                if isinstance(payload, dict) and isinstance(payload.get("devices"), list)
                else None
            ),
            normalised_device_count=int(result.get("record_count", 0) or 0),
            active_count=int(result.get("active_count", 0) or 0),
            lookup_kind=result.get("lookup_kind"),
            primary_status=(result.get("primary") or {}).get("status") if result.get("primary") else None,
            primary_model=(result.get("primary") or {}).get("model") if result.get("primary") else None,
            ambiguous=bool(result.get("ambiguous")),
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
        self.service_id = run_reporting.diagnostic_id("pc-service")
        self._context_logged = False
        self.cache = self._load_cache()
        self.models = self._load_models()
        self.cache_write_timer: threading.Timer | None = None
        self.inflight: set[str] = set()
        self.role = os.getenv("PC_TOOLKIT_ROLE", DEFAULT_PC_TOOLKIT_ROLE).strip()
        self.state = "simulation" if simulate else "idle"
        self.message = "Simulation data available." if simulate else "Not connected."
        self.last_error = ""
        self.connected_at: str | None = None

    def enabled(self) -> bool:
        return bool(self.preferences().get("pc_toolkit_enabled", False))

    def _log_context(self, *, reason: str, operation_id: str | None = None) -> None:
        """Record the local conditions that affect PC Toolkit connectivity once."""
        with self.lock:
            if self._context_logged:
                return
            self._context_logged = True
            enabled = self.enabled()
            simulate = self.simulate
            browser_profile = self.browser_profile
            browser_headless = self.browser_headless
            role = self.role
        profile_path = Path(browser_profile).expanduser() if browser_profile else None
        try:
            profile_exists = bool(profile_path and profile_path.exists())
            profile_is_dir = bool(profile_path and profile_path.is_dir())
            profile_readable = bool(profile_path and os.access(profile_path, os.R_OK))
        except OSError:
            profile_exists = profile_is_dir = profile_readable = False
        try:
            cache_exists = self.cache_path.exists()
            cache_bytes = self.cache_path.stat().st_size if cache_exists else 0
        except OSError:
            cache_exists = False
            cache_bytes = 0
        run_reporting.pc_toolkit_event(
            "service_context",
            service_id=self.service_id,
            operation_id=operation_id,
            reason=reason,
            enabled=enabled,
            simulate=simulate,
            browser_profile_configured=bool(browser_profile),
            browser_profile_exists=profile_exists,
            browser_profile_is_directory=profile_is_dir,
            browser_profile_readable=profile_readable,
            browser_headless=browser_headless,
            role_configured=bool(role),
            role_source="PC_TOOLKIT_ROLE" if os.getenv("PC_TOOLKIT_ROLE") else "default",
            cache_file=self.cache_path.name,
            cache_exists=cache_exists,
            cache_bytes=cache_bytes,
            cached_queries=len(self.cache),
            known_models=len(self.models),
            python_version=sys.version.split()[0],
            platform=platform.platform(),
            machine=platform.machine(),
            proxy_environment={
                name: bool(os.getenv(name))
                for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
            },
            certificate_environment={
                name: bool(os.getenv(name))
                for name in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")
            },
            device_endpoint=DEFAULT_DEVICE_URL,
            portal_endpoint=DEFAULT_PORTAL_URL,
            role_endpoint=DEFAULT_ROLE_URL,
        )

    def _set_state(
        self,
        state: str,
        message: str,
        *,
        operation_id: str | None = None,
        error: str = "",
    ) -> None:
        with self.lock:
            previous = self.state
            previous_error = self.last_error
            self.state = state
            self.message = message
            self.last_error = error
        if previous != state or previous_error != error:
            run_reporting.pc_toolkit_event(
                "state_changed",
                service_id=self.service_id,
                operation_id=operation_id,
                previous_state=previous,
                state=state,
                message=message,
                error=error or None,
            )

    def _load_cache(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            if self.cache_path.exists():
                run_reporting.pc_toolkit_event(
                    "cache_load_failed",
                    service_id=self.service_id,
                    cache_file=self.cache_path.name,
                    exception=run_reporting.exception_details(exc),
                )
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

    def _write_cache(self, *, reason: str = "update") -> None:
        started = time.monotonic()
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.cache_path.with_suffix(".tmp")
            payload = {
                "version": 2,
                "entries": self.cache,
                "models": sorted(self.models, key=str.casefold),
            }
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
            temporary.write_text(encoded, encoding="utf-8")
            temporary.replace(self.cache_path)
        except Exception as exc:
            run_reporting.pc_toolkit_event(
                "cache_write_failed",
                service_id=self.service_id,
                cache_file=self.cache_path.name,
                reason=reason,
                duration_ms=round((time.monotonic() - started) * 1000),
                exception=run_reporting.exception_details(exc),
            )
            raise
        run_reporting.pc_toolkit_event(
            "cache_written",
            service_id=self.service_id,
            cache_file=self.cache_path.name,
            reason=reason,
            duration_ms=round((time.monotonic() - started) * 1000),
            cache_bytes=len(encoded.encode("utf-8")),
            cached_queries=len(self.cache),
            known_models=len(self.models),
        )

    def _schedule_cache_write_locked(self) -> None:
        if self.cache_write_timer is not None and self.cache_write_timer.is_alive():
            return
        self.cache_write_timer = threading.Timer(0.5, self._flush_cache)
        self.cache_write_timer.daemon = True
        self.cache_write_timer.start()

    def _flush_cache(self) -> None:
        with self.lock:
            self.cache_write_timer = None
            self._write_cache(reason="scheduled_update")

    def status(self) -> dict[str, Any]:
        with self.lock:
            status = {
                "enabled": self.enabled(),
                "state": self.state,
                "message": self.message,
                "connected_at": self.connected_at,
                "cached_queries": len(self.cache),
                "models": sorted(self.models, key=str.casefold),
                "last_error": self.last_error,
            }
        status["log"] = run_reporting.pc_toolkit_log_status()
        return status

    def clear_cache(self) -> None:
        with self.lock:
            self.cache = {}
            self._write_cache(reason="clear_cache")
        run_reporting.pc_toolkit_event(
            "cache_cleared", service_id=self.service_id, cache_kind="queries"
        )

    def clear_models(self) -> None:
        with self.lock:
            self.models = set()
            self._write_cache(reason="clear_models")
        run_reporting.pc_toolkit_event(
            "cache_cleared", service_id=self.service_id, cache_kind="models"
        )

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

    def _fetch(
        self,
        query: str,
        *,
        request_id: str,
        operation_id: str,
        purpose: str,
    ) -> dict[str, Any]:
        started = time.monotonic()
        value = clean(query)
        run_reporting.pc_toolkit_event(
            "fetch_started",
            service_id=self.service_id,
            request_id=request_id,
            operation_id=operation_id,
            purpose=purpose,
            query=value,
            simulation=self.simulate,
        )
        try:
            if self.simulate:
                result = self._simulation_lookup(value)
                run_reporting.pc_toolkit_event(
                    "simulation_response",
                    service_id=self.service_id,
                    request_id=request_id,
                    operation_id=operation_id,
                    query=value,
                    record_count=int(result.get("record_count", 0) or 0),
                )
            else:
                result = self._client().lookup(
                    value,
                    request_id=request_id,
                    operation_id=operation_id,
                    purpose=purpose,
                )
        except Exception as exc:
            run_reporting.pc_toolkit_event(
                "fetch_failed",
                service_id=self.service_id,
                request_id=request_id,
                operation_id=operation_id,
                purpose=purpose,
                query=value,
                duration_ms=round((time.monotonic() - started) * 1000),
                exception=run_reporting.exception_details(exc),
            )
            if "authentication" in str(exc).casefold():
                self._set_state(
                    "error",
                    "PC Toolkit's device API rejected the connection.",
                    operation_id=operation_id,
                    error=str(exc),
                )
            raise
        key = normalise_key(query)
        stored = {"fetched_at": time.time(), "result": result}
        with self.lock:
            self._remember_models_locked(result)
            self.cache.pop(key, None)
            self.cache[key] = stored
            while len(self.cache) > MAX_CACHE_ENTRIES:
                self.cache.pop(next(iter(self.cache)))
            self._schedule_cache_write_locked()
            previous_state = self.state
            next_state = "simulation" if self.simulate else "connected"
            self.state = next_state
            self.message = "PC Toolkit enrichment is ready."
            self.last_error = ""
            self.connected_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if previous_state != next_state:
            run_reporting.pc_toolkit_event(
                "state_changed",
                service_id=self.service_id,
                operation_id=operation_id,
                previous_state=previous_state,
                state=next_state,
                message="PC Toolkit enrichment is ready.",
            )
        run_reporting.pc_toolkit_event(
            "lookup_stored",
            service_id=self.service_id,
            request_id=request_id,
            operation_id=operation_id,
            purpose=purpose,
            query=clean(query),
            cached=False,
            found=bool(result.get("found")),
            record_count=int(result.get("record_count", 0) or 0),
            active_count=int(result.get("active_count", 0) or 0),
            duration_ms=round((time.monotonic() - started) * 1000),
        )
        return deepcopy(result)

    def _refresh(self, query: str, operation_id: str) -> None:
        key = normalise_key(query)
        request_id = run_reporting.diagnostic_id("pc-refresh")
        try:
            self._fetch(
                query,
                request_id=request_id,
                operation_id=operation_id,
                purpose="stale_cache_refresh",
            )
            run_reporting.pc_toolkit_event(
                "background_lookup_complete",
                service_id=self.service_id,
                request_id=request_id,
                operation_id=operation_id,
                query=clean(query),
            )
        except Exception as exc:
            run_reporting.pc_toolkit_event(
                "background_lookup_failed",
                service_id=self.service_id,
                request_id=request_id,
                operation_id=operation_id,
                query=clean(query),
                exception=run_reporting.exception_details(exc),
            )
            with self.lock:
                self.last_error = str(exc)
                should_mark_error = not self.cache.get(key)
                previous_state = self.state
                if should_mark_error:
                    self.state = "error"
                    self.message = str(exc)
            if should_mark_error and previous_state != "error":
                run_reporting.pc_toolkit_event(
                    "state_changed",
                    service_id=self.service_id,
                    operation_id=operation_id,
                    previous_state=previous_state,
                    state="error",
                    message=str(exc),
                    error=str(exc),
                )
        finally:
            with self.lock:
                self.inflight.discard(key)

    def lookup(
        self,
        query: str,
        *,
        fresh: bool = False,
        operation_id: str | None = None,
        purpose: str = "lookup",
    ) -> dict[str, Any]:
        operation_id = operation_id or run_reporting.diagnostic_id("pc-lookup")
        request_id = run_reporting.diagnostic_id("pc-query")
        started = time.monotonic()
        value = clean(query)
        self._log_context(reason=purpose, operation_id=operation_id)
        run_reporting.pc_toolkit_event(
            "lookup_started",
            service_id=self.service_id,
            request_id=request_id,
            operation_id=operation_id,
            purpose=purpose,
            query=value,
            fresh=bool(fresh),
        )
        if not self.enabled() and not self.simulate:
            run_reporting.pc_toolkit_event(
                "lookup_rejected",
                service_id=self.service_id,
                request_id=request_id,
                operation_id=operation_id,
                purpose=purpose,
                query=value,
                reason="disabled",
            )
            raise PCToolkitError("PC Toolkit enrichment is disabled in Settings.")
        key = normalise_key(value)
        if len(key) < 2:
            run_reporting.pc_toolkit_event(
                "lookup_rejected",
                service_id=self.service_id,
                request_id=request_id,
                operation_id=operation_id,
                purpose=purpose,
                query=value,
                reason="query_too_short",
            )
            raise PCToolkitError("Enter at least two characters for PC Toolkit.")
        now = time.time()
        with self.lock:
            cached = deepcopy(self.cache.get(key))
        if cached and not fresh:
            try:
                age = max(0.0, now - float(cached.get("fetched_at", 0)))
            except (TypeError, ValueError):
                age = STALE_CACHE_SECONDS + 1
                run_reporting.pc_toolkit_event(
                    "cache_entry_invalid",
                    service_id=self.service_id,
                    request_id=request_id,
                    operation_id=operation_id,
                    query=value,
                    reason="invalid_fetched_at",
                )
            if age <= STALE_CACHE_SECONDS:
                result = deepcopy(cached.get("result", {}))
                if not isinstance(result, dict):
                    run_reporting.pc_toolkit_event(
                        "cache_entry_invalid",
                        service_id=self.service_id,
                        request_id=request_id,
                        operation_id=operation_id,
                        query=value,
                        reason="result_not_object",
                        result_type=type(result).__name__,
                    )
                    result = None
                if result is None:
                    return self._fetch(
                        value,
                        request_id=request_id,
                        operation_id=operation_id,
                        purpose=purpose,
                    )
                result["cached"] = True
                result["stale"] = age > ACTIVE_CACHE_SECONDS
                result["age_seconds"] = round(age)
                run_reporting.pc_toolkit_event(
                    "cache_hit",
                    service_id=self.service_id,
                    request_id=request_id,
                    operation_id=operation_id,
                    purpose=purpose,
                    query=clean(query),
                    age_seconds=round(age),
                    stale=bool(result["stale"]),
                    record_count=int(result.get("record_count", 0) or 0),
                    duration_ms=round((time.monotonic() - started) * 1000),
                )
                if age > ACTIVE_CACHE_SECONDS:
                    with self.lock:
                        if key not in self.inflight:
                            self.inflight.add(key)
                            run_reporting.pc_toolkit_event(
                                "cache_refresh_scheduled",
                                service_id=self.service_id,
                                request_id=request_id,
                                operation_id=operation_id,
                                query=value,
                                age_seconds=round(age),
                            )
                            threading.Thread(
                                target=self._refresh,
                                args=(value, operation_id),
                                name="pc-toolkit-cache-refresh",
                                daemon=True,
                            ).start()
                return result
        return self._fetch(
            value,
            request_id=request_id,
            operation_id=operation_id,
            purpose=purpose,
        )

    def bulk_lookup(self, queries: list[str], *, fresh: bool = False) -> dict[str, Any]:
        operation_id = run_reporting.diagnostic_id("pc-bulk")
        started = time.monotonic()
        unique: dict[str, str] = {}
        invalid_count = 0
        for query in queries:
            value = clean(query)
            key = normalise_key(value)
            if len(key) >= 2:
                unique.setdefault(key, value)
            else:
                invalid_count += 1
        self._log_context(reason="bulk_lookup", operation_id=operation_id)
        run_reporting.pc_toolkit_event(
            "bulk_lookup_started",
            service_id=self.service_id,
            operation_id=operation_id,
            input_count=len(queries),
            unique_count=len(unique),
            duplicate_count=max(0, len(queries) - invalid_count - len(unique)),
            invalid_count=invalid_count,
            fresh=bool(fresh),
        )
        results: dict[str, Any] = {}
        errors: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_LOOKUPS, max(1, len(unique)))) as executor:
            futures = {
                executor.submit(
                    self.lookup,
                    value,
                    fresh=fresh,
                    operation_id=operation_id,
                    purpose="bulk_lookup",
                ): key
                for key, value in unique.items()
            }
            for future in as_completed(futures):
                key = futures[future]
                try:
                    results[key] = future.result()
                except Exception as exc:
                    errors[key] = str(exc)
                    run_reporting.pc_toolkit_event(
                        "bulk_lookup_item_failed",
                        service_id=self.service_id,
                        operation_id=operation_id,
                        query=unique[key],
                        exception=run_reporting.exception_details(exc),
                    )
        run_reporting.pc_toolkit_event(
            "bulk_lookup_complete",
            service_id=self.service_id,
            operation_id=operation_id,
            query_count=len(unique),
            result_count=len(results),
            error_count=len(errors),
            invalid_count=invalid_count,
            fresh=bool(fresh),
            duration_ms=round((time.monotonic() - started) * 1000),
        )
        return {"results": results, "errors": errors, "status": self.status()}

    def connect_async(self) -> None:
        operation_id = run_reporting.diagnostic_id("pc-connect")
        self._log_context(reason="connect", operation_id=operation_id)
        if self.simulate:
            with self.lock:
                self.state = "simulation"
                self.message = "Simulation data available."
            run_reporting.pc_toolkit_event(
                "connect_simulation",
                service_id=self.service_id,
                operation_id=operation_id,
            )
            return
        with self.lock:
            if self.state == "connecting":
                run_reporting.pc_toolkit_event(
                    "connect_ignored",
                    service_id=self.service_id,
                    operation_id=operation_id,
                    reason="already_connecting",
                )
                return
            self.state = "connecting"
            self.message = "Connecting to PC Toolkit…"
            self.last_error = ""
        run_reporting.pc_toolkit_event(
            "connect_started",
            service_id=self.service_id,
            operation_id=operation_id,
            state="connecting",
        )
        threading.Thread(
            target=self._connect,
            args=(operation_id,),
            name="pc-toolkit-connect",
            daemon=True,
        ).start()

    def _connect(self, operation_id: str) -> None:
        started = time.monotonic()
        authenticated_in_browser = False
        try:
            # Use a harmless query so connecting does not expose a real
            # user/device. The client includes the production portal role by
            # default, then role discovery remains available for accounts
            # whose session exposes a different role.
            self._client().lookup(
                "auto-eudm-health-check",
                operation_id=operation_id,
                purpose="connect_health_check",
            )
        except Exception as first_error:
            run_reporting.pc_toolkit_event(
                "connect_probe_failed",
                service_id=self.service_id,
                operation_id=operation_id,
                phase="health_check",
                duration_ms=round((time.monotonic() - started) * 1000),
                exception=run_reporting.exception_details(first_error),
            )
            if "authentication" not in str(first_error).casefold():
                self._set_state(
                    "error",
                    str(first_error),
                    operation_id=operation_id,
                    error=str(first_error),
                )
                run_reporting.pc_toolkit_event(
                    "connect_failed",
                    service_id=self.service_id,
                    operation_id=operation_id,
                    error=str(first_error),
                    phase="health_check",
                    duration_ms=round((time.monotonic() - started) * 1000),
                    exception=run_reporting.exception_details(first_error),
                )
                return
            try:
                self.role = self._discover_role(operation_id)
                authenticated_in_browser = True
            except Exception as exc:
                self._set_state(
                    "error",
                    str(exc),
                    operation_id=operation_id,
                    error=str(exc),
                )
                run_reporting.pc_toolkit_event(
                    "connect_failed",
                    service_id=self.service_id,
                    operation_id=operation_id,
                    error=str(exc),
                    phase="role_discovery",
                    duration_ms=round((time.monotonic() - started) * 1000),
                    exception=run_reporting.exception_details(exc),
                )
                return
            # A working portal session and an elevated role do not prove that
            # the separate device gateway accepts our request.  Re-run the
            # same harmless lookup after SSO before reporting readiness.
            try:
                self._client().lookup(
                    "auto-eudm-health-check",
                    operation_id=operation_id,
                    purpose="post_auth_health_check",
                )
            except Exception as verification_error:
                message = (
                    "PC Toolkit signed in, but its device API rejected the connection."
                    if "authentication" in str(verification_error).casefold()
                    else str(verification_error)
                )
                self._set_state(
                    "error",
                    message,
                    operation_id=operation_id,
                    error=str(verification_error),
                )
                run_reporting.pc_toolkit_event(
                    "connect_failed",
                    service_id=self.service_id,
                    operation_id=operation_id,
                    error=str(verification_error),
                    phase="post_auth_health_check",
                    role=self.role,
                    duration_ms=round((time.monotonic() - started) * 1000),
                    exception=run_reporting.exception_details(verification_error),
                )
                return
        with self.lock:
            self.connected_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._set_state(
            "connected",
            "PC Toolkit enrichment is ready.",
            operation_id=operation_id,
        )
        run_reporting.pc_toolkit_event(
            "connect_succeeded",
            service_id=self.service_id,
            operation_id=operation_id,
            role=self.role,
            authenticated_in_browser=authenticated_in_browser,
            duration_ms=round((time.monotonic() - started) * 1000),
        )

    def _discover_role(self, operation_id: str | None = None) -> str:
        if not self.browser_profile:
            run_reporting.pc_toolkit_event(
                "role_discovery_rejected",
                service_id=self.service_id,
                operation_id=operation_id,
                reason="browser_profile_not_configured",
            )
            raise PCToolkitError("Open PC Toolkit once, then connect again.")
        run_reporting.pc_toolkit_event(
            "role_discovery_started",
            service_id=self.service_id,
            operation_id=operation_id,
            portal_url=DEFAULT_PORTAL_URL,
            role_url=DEFAULT_ROLE_URL,
            browser_profile_configured=True,
            browser_headless=self.browser_headless,
        )
        profile_wait_started = time.monotonic()
        profile_wait_logged = False
        profile_wait_deadline = profile_wait_started + 45
        while _profile_lock_is_live(self.browser_profile):
            if not profile_wait_logged:
                profile_wait_logged = True
                run_reporting.pc_toolkit_event(
                    "browser_profile_wait_started",
                    service_id=self.service_id,
                    operation_id=operation_id,
                    reason="profile_in_use",
                )
            if time.monotonic() >= profile_wait_deadline:
                run_reporting.pc_toolkit_event(
                    "browser_profile_wait_timed_out",
                    service_id=self.service_id,
                    operation_id=operation_id,
                    elapsed_ms=round((time.monotonic() - profile_wait_started) * 1000),
                )
                raise PCToolkitError(
                    "PC Toolkit is waiting for the Helix Chrome window to close. Try again shortly."
                )
            time.sleep(0.5)
        if profile_wait_logged:
            run_reporting.pc_toolkit_event(
                "browser_profile_released",
                service_id=self.service_id,
                operation_id=operation_id,
                elapsed_ms=round((time.monotonic() - profile_wait_started) * 1000),
            )
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            run_reporting.pc_toolkit_event(
                "role_discovery_dependency_failed",
                service_id=self.service_id,
                operation_id=operation_id,
                exception=run_reporting.exception_details(exc),
            )
            raise PCToolkitError("Browser support is not installed for PC Toolkit.") from exc
        try:
            playwright = sync_playwright().start()
        except Exception as exc:
            run_reporting.pc_toolkit_event(
                "browser_runtime_start_failed",
                service_id=self.service_id,
                operation_id=operation_id,
                exception=run_reporting.exception_details(exc),
            )
            raise PCToolkitError("PC Toolkit browser support could not be started.") from exc
        context = None
        browser_requests: dict[int, dict[str, Any]] = {}
        attached_pages: dict[int, str] = {}
        last_page_snapshot: tuple[tuple[str, str], ...] | None = None

        def request_headers(request: Any) -> dict[str, Any]:
            try:
                headers = request.all_headers()
            except Exception:
                headers = getattr(request, "headers", {})
            return dict(headers) if hasattr(headers, "items") else {}

        def response_headers(response: Any) -> dict[str, Any]:
            try:
                headers = response.all_headers()
            except Exception:
                headers = getattr(response, "headers", {})
                if callable(headers):
                    headers = headers()
            return dict(headers) if hasattr(headers, "items") else {}

        def interesting_browser_request(url: str, resource_type: str) -> bool:
            lowered = url.casefold()
            return resource_type in {"document", "xhr", "fetch"} or any(
                marker in lowered
                for marker in (
                    "/auth/",
                    "/login",
                    "/oauth",
                    "/saml",
                    "/signin",
                    "maxroles",
                    "session",
                )
            )

        def page_snapshot() -> tuple[tuple[str, str], ...]:
            if context is None:
                return ()
            snapshot: list[tuple[str, str]] = []
            for candidate in context.pages:
                try:
                    snapshot.append((str(candidate.url or ""), "open"))
                except Exception:
                    snapshot.append(("<unreadable>", "open"))
            return tuple(snapshot)

        def attach_page(candidate: Any) -> None:
            nonlocal last_page_snapshot
            identity = id(candidate)
            if identity in attached_pages:
                return
            page_id = run_reporting.diagnostic_id("pc-page")
            attached_pages[identity] = page_id
            try:
                current_url = str(candidate.url or "")
            except Exception:
                current_url = ""
            run_reporting.pc_toolkit_event(
                "browser_page_opened",
                service_id=self.service_id,
                operation_id=operation_id,
                page_id=page_id,
                page_url=current_url,
                page_count=len(context.pages) if context is not None else None,
            )

            def on_request(request: Any) -> None:
                try:
                    url = str(request.url or "")
                    resource_type = str(request.resource_type or "")
                    if not interesting_browser_request(url, resource_type):
                        return
                    request_id = run_reporting.diagnostic_id("pc-browser")
                    started = time.monotonic()
                    try:
                        body = request.post_data
                    except Exception:
                        body = None
                    browser_requests[_playwright_request_key(request)] = {
                        "request_id": request_id,
                        "started": started,
                        "method": str(request.method or "GET"),
                        "url": url,
                        "headers": request_headers(request),
                        "body": body,
                        "resource_type": resource_type,
                        "page_id": page_id,
                    }
                    run_reporting.pc_toolkit_event(
                        "browser_request_started",
                        service_id=self.service_id,
                        operation_id=operation_id,
                        request_id=request_id,
                        page_id=page_id,
                        method=str(request.method or "GET"),
                        resource_type=resource_type,
                        request_url=url,
                        request_headers=request_headers(request),
                        request_body=body,
                        is_navigation=bool(request.is_navigation_request()),
                    )
                except Exception as exc:
                    run_reporting.pc_toolkit_event(
                        "browser_request_logging_failed",
                        service_id=self.service_id,
                        operation_id=operation_id,
                        page_id=page_id,
                        exception=run_reporting.exception_details(exc),
                    )

            def on_response(response: Any) -> None:
                try:
                    request = response.request
                    url = str(response.url or getattr(request, "url", "") or "")
                    resource_type = str(getattr(request, "resource_type", "") or "")
                    metadata = browser_requests.pop(_playwright_request_key(request), None)
                    if metadata is None and not interesting_browser_request(url, resource_type):
                        return
                    if metadata is None:
                        metadata = {
                            "request_id": run_reporting.diagnostic_id("pc-browser"),
                            "started": time.monotonic(),
                            "method": str(getattr(request, "method", "GET") or "GET"),
                            "url": url,
                            "headers": request_headers(request),
                            "body": None,
                            "resource_type": resource_type,
                            "page_id": page_id,
                        }
                    try:
                        body = response.body()
                        body_error = None
                    except Exception as exc:
                        body = None
                        body_error = run_reporting.exception_details(exc)
                    try:
                        status = int(response.status)
                    except (TypeError, ValueError, AttributeError):
                        status = None
                    run_reporting.network(
                        metadata["method"],
                        urllib.parse.urlsplit(url).path or url,
                        status=status,
                        duration_ms=round((time.monotonic() - metadata["started"]) * 1000),
                        transport="pc-toolkit",
                        request_url=url,
                        request_headers=metadata["headers"],
                        response_headers=response_headers(response),
                        request_body=metadata["body"],
                        response_body=body,
                        request_id=metadata["request_id"],
                        operation_id=operation_id,
                        error_detail=(str(body_error) if body_error else None),
                        details={
                            "channel": "browser-page",
                            "page_id": page_id,
                            "resource_type": metadata["resource_type"],
                            "response_body_read": body_error is None,
                        },
                    )
                    run_reporting.pc_toolkit_event(
                        "browser_response_received",
                        service_id=self.service_id,
                        operation_id=operation_id,
                        request_id=metadata["request_id"],
                        page_id=page_id,
                        method=metadata["method"],
                        status=status,
                        response_url=url,
                        resource_type=metadata["resource_type"],
                        duration_ms=round((time.monotonic() - metadata["started"]) * 1000),
                        response_body_bytes=(len(body) if isinstance(body, bytes) else None),
                        response_body_read=body_error is None,
                        response_body_error=body_error,
                    )
                except Exception as exc:
                    run_reporting.pc_toolkit_event(
                        "browser_response_logging_failed",
                        service_id=self.service_id,
                        operation_id=operation_id,
                        page_id=page_id,
                        exception=run_reporting.exception_details(exc),
                    )

            def on_request_failed(request: Any) -> None:
                metadata = browser_requests.pop(_playwright_request_key(request), None)
                if metadata is None:
                    return
                try:
                    failure = request.failure
                    if callable(failure):
                        failure = failure()
                except Exception as exc:
                    failure = run_reporting.exception_details(exc)
                run_reporting.pc_toolkit_event(
                    "browser_request_failed",
                    service_id=self.service_id,
                    operation_id=operation_id,
                    request_id=metadata["request_id"],
                    page_id=page_id,
                    method=metadata["method"],
                    resource_type=metadata["resource_type"],
                    request_url=metadata["url"],
                    duration_ms=round((time.monotonic() - metadata["started"]) * 1000),
                    failure=failure,
                )

            def on_navigation(frame: Any) -> None:
                try:
                    if frame != candidate.main_frame:
                        return
                    run_reporting.pc_toolkit_event(
                        "browser_navigation",
                        service_id=self.service_id,
                        operation_id=operation_id,
                        page_id=page_id,
                        page_url=str(frame.url or ""),
                    )
                except Exception as exc:
                    run_reporting.pc_toolkit_event(
                        "browser_navigation_logging_failed",
                        service_id=self.service_id,
                        operation_id=operation_id,
                        page_id=page_id,
                        exception=run_reporting.exception_details(exc),
                    )

            candidate.on("request", on_request)
            candidate.on("response", on_response)
            candidate.on("requestfailed", on_request_failed)
            candidate.on("framenavigated", on_navigation)
            candidate.on(
                "close",
                lambda: run_reporting.pc_toolkit_event(
                    "browser_page_closed",
                    service_id=self.service_id,
                    operation_id=operation_id,
                    page_id=page_id,
                ),
            )
            current_snapshot = page_snapshot()
            if current_snapshot != last_page_snapshot:
                last_page_snapshot = current_snapshot
                run_reporting.pc_toolkit_event(
                    "browser_pages_snapshot",
                    service_id=self.service_id,
                    operation_id=operation_id,
                    pages=[{"url": url, "state": state} for url, state in current_snapshot],
                )

        try:
            run_reporting.pc_toolkit_event(
                "browser_launch_started",
                service_id=self.service_id,
                operation_id=operation_id,
                channel="chrome",
                headless=self.browser_headless,
                user_data_dir_configured=bool(self.browser_profile),
            )
            launch_error: BaseException | None = None
            for launch_attempt in range(1, 4):
                try:
                    context = playwright.chromium.launch_persistent_context(
                        user_data_dir=str(Path(self.browser_profile).expanduser()),
                        channel="chrome",
                        headless=self.browser_headless,
                    )
                    launch_error = None
                    break
                except Exception as exc:
                    launch_error = exc
                    if not _profile_in_use_error(exc) or launch_attempt >= 3:
                        raise
                    run_reporting.pc_toolkit_event(
                        "browser_launch_retry",
                        service_id=self.service_id,
                        operation_id=operation_id,
                        attempt=launch_attempt,
                        reason="profile_in_use",
                        exception=run_reporting.exception_details(exc),
                    )
                    time.sleep(1.5 * launch_attempt)
            if context is None and launch_error is not None:
                raise launch_error
            run_reporting.pc_toolkit_event(
                "browser_launch_succeeded",
                service_id=self.service_id,
                operation_id=operation_id,
                page_count=len(context.pages),
            )
            context.on("page", attach_page)
            pages = context.pages or [context.new_page()]
            for candidate in pages:
                attach_page(candidate)
            page = pages[0]
            try:
                navigation_started = time.monotonic()
                run_reporting.pc_toolkit_event(
                    "browser_navigation_started",
                    service_id=self.service_id,
                    operation_id=operation_id,
                    page_id=attached_pages.get(id(page)),
                    page_url=DEFAULT_PORTAL_URL,
                    wait_until="domcontentloaded",
                    timeout_ms=60_000,
                )
                navigation_response = page.goto(
                    DEFAULT_PORTAL_URL,
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
                run_reporting.pc_toolkit_event(
                    "browser_navigation_completed",
                    service_id=self.service_id,
                    operation_id=operation_id,
                    page_id=attached_pages.get(id(page)),
                    requested_url=DEFAULT_PORTAL_URL,
                    final_url=str(page.url or ""),
                    status=(int(navigation_response.status) if navigation_response else None),
                    duration_ms=round((time.monotonic() - navigation_started) * 1000),
                )
            except Exception as exc:
                run_reporting.pc_toolkit_event(
                    "browser_navigation_failed",
                    service_id=self.service_id,
                    operation_id=operation_id,
                    page_id=attached_pages.get(id(page)),
                    requested_url=DEFAULT_PORTAL_URL,
                    final_url=str(page.url or ""),
                    exception=run_reporting.exception_details(exc),
                )
                raise
            try:
                cookies = context.cookies()
                run_reporting.pc_toolkit_event(
                    "browser_cookie_snapshot",
                    service_id=self.service_id,
                    operation_id=operation_id,
                    cookie_count=len(cookies),
                    cookie_domains=sorted({str(cookie.get("domain", "")) for cookie in cookies if cookie.get("domain")}),
                    cookie_names=sorted({str(cookie.get("name", "")) for cookie in cookies if cookie.get("name")}),
                )
            except Exception as exc:
                run_reporting.pc_toolkit_event(
                    "browser_cookie_snapshot_failed",
                    service_id=self.service_id,
                    operation_id=operation_id,
                    exception=run_reporting.exception_details(exc),
                )
            role_probe_started = time.monotonic()
            role_probe_timeout = 20 if self.browser_headless else 120
            deadline = role_probe_started + role_probe_timeout
            role_request_headers = {
                "Accept": "application/json, text/plain, */*",
                "Referer": str(page.url or DEFAULT_PORTAL_URL),
                "User-Agent": PC_TOOLKIT_USER_AGENT,
            }
            while time.monotonic() < deadline:
                # Use the context request client rather than page JavaScript:
                # this preserves the persistent browser cookies without being
                # affected by a welcome-page redirect or cross-origin policy.
                request_id = run_reporting.diagnostic_id("pc-role")
                probe_started = time.monotonic()
                try:
                    response = context.request.get(DEFAULT_ROLE_URL, timeout=5_000)
                    try:
                        raw_body = response.body()
                        body_error = None
                    except Exception as exc:
                        raw_body = None
                        body_error = exc
                    raw_response_headers = response_headers(response)
                    status = int(response.status)
                    run_reporting.network(
                        "GET",
                        urllib.parse.urlsplit(DEFAULT_ROLE_URL).path,
                        status=status,
                        duration_ms=round((time.monotonic() - probe_started) * 1000),
                        transport="pc-toolkit",
                        request_url=str(response.url or DEFAULT_ROLE_URL),
                        request_headers=role_request_headers,
                        response_headers=raw_response_headers,
                        response_body=raw_body,
                        request_id=request_id,
                        operation_id=operation_id,
                        error_detail=(str(body_error) if body_error else None),
                        details={
                            "channel": "playwright-context-request",
                            "purpose": "role_probe",
                            "response_body_read": body_error is None,
                            "browser_cookie_jar_used": True,
                        },
                    )
                    payload: Any = None
                    parse_error: BaseException | None = None
                    if raw_body is not None:
                        try:
                            payload = json.loads(raw_body.decode("utf-8"))
                        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                            parse_error = exc
                    run_reporting.pc_toolkit_event(
                        "role_probe_result",
                        service_id=self.service_id,
                        operation_id=operation_id,
                        request_id=request_id,
                        request_url=str(response.url or DEFAULT_ROLE_URL),
                        status=status,
                        ok=bool(response.ok),
                        duration_ms=round((time.monotonic() - probe_started) * 1000),
                        response_body_bytes=(len(raw_body) if isinstance(raw_body, bytes) else None),
                        response_body_read=body_error is None,
                        response_body_error=(
                            run_reporting.exception_details(body_error)
                            if body_error else None
                        ),
                        parse_error=(
                            run_reporting.exception_details(parse_error)
                            if parse_error else None
                        ),
                        payload_type=type(payload).__name__ if payload is not None else None,
                        payload_keys=sorted(payload.keys()) if isinstance(payload, dict) else [],
                        page_urls=[url for url, _ in page_snapshot()],
                    )
                    roles = payload.get("maxRoles", []) if isinstance(payload, dict) else []
                    if isinstance(roles, list) and roles and clean(roles[0]):
                        role = clean(roles[0])
                        run_reporting.pc_toolkit_event(
                            "role_discovered",
                            service_id=self.service_id,
                            operation_id=operation_id,
                            request_id=request_id,
                            role=role,
                            role_count=len(roles),
                        )
                        return role
                    run_reporting.pc_toolkit_event(
                        "role_probe_no_role",
                        service_id=self.service_id,
                        operation_id=operation_id,
                        request_id=request_id,
                        status=status,
                        ok=bool(response.ok),
                        roles_type=type(roles).__name__,
                        role_count=len(roles) if isinstance(roles, list) else None,
                    )
                except Exception as exc:
                    run_reporting.pc_toolkit_event(
                        "role_probe_failed",
                        service_id=self.service_id,
                        operation_id=operation_id,
                        request_id=request_id,
                        request_url=DEFAULT_ROLE_URL,
                        duration_ms=round((time.monotonic() - probe_started) * 1000),
                        exception=run_reporting.exception_details(exc),
                    )
                # Some SSO flows finish in a newly opened tab. Refresh the
                # list so a successful login in that tab is observed too.
                pages = context.pages or [page]
                for candidate in pages:
                    attach_page(candidate)
                current_snapshot = page_snapshot()
                if current_snapshot != last_page_snapshot:
                    last_page_snapshot = current_snapshot
                    run_reporting.pc_toolkit_event(
                        "browser_pages_snapshot",
                        service_id=self.service_id,
                        operation_id=operation_id,
                        pages=[{"url": url, "state": state} for url, state in current_snapshot],
                    )
                wait_page = next(
                    (
                        candidate
                        for candidate in pages
                        if not bool(getattr(candidate, "is_closed", lambda: False)())
                    ),
                    None,
                )
                if wait_page is None:
                    time.sleep(1.5)
                else:
                    try:
                        wait_page.wait_for_timeout(1500)
                    except Exception as exc:
                        run_reporting.pc_toolkit_event(
                            "browser_wait_failed",
                            service_id=self.service_id,
                            operation_id=operation_id,
                            exception=run_reporting.exception_details(exc),
                        )
                        time.sleep(1.5)
            run_reporting.pc_toolkit_event(
                "role_discovery_timed_out",
                service_id=self.service_id,
                operation_id=operation_id,
                page_urls=[url for url, _ in page_snapshot()],
                elapsed_ms=round((time.monotonic() - role_probe_started) * 1000),
                timeout_seconds=role_probe_timeout,
            )
            raise PCToolkitError(
                "PC Toolkit sign-in did not complete. Open it in Chrome and try again."
            )
        except PCToolkitError:
            raise
        except Exception as exc:
            run_reporting.pc_toolkit_event(
                "role_discovery_failed",
                service_id=self.service_id,
                operation_id=operation_id,
                exception=run_reporting.exception_details(exc),
            )
            raise PCToolkitError("PC Toolkit authentication could not be opened.") from exc
        finally:
            if context is not None:
                try:
                    context.close()
                    run_reporting.pc_toolkit_event(
                        "browser_context_closed",
                        service_id=self.service_id,
                        operation_id=operation_id,
                    )
                except Exception as exc:
                    run_reporting.pc_toolkit_event(
                        "browser_context_close_failed",
                        service_id=self.service_id,
                        operation_id=operation_id,
                        exception=run_reporting.exception_details(exc),
                    )
            try:
                playwright.stop()
                run_reporting.pc_toolkit_event(
                    "browser_runtime_stopped",
                    service_id=self.service_id,
                    operation_id=operation_id,
                )
            except Exception as exc:
                run_reporting.pc_toolkit_event(
                    "browser_runtime_stop_failed",
                    service_id=self.service_id,
                    operation_id=operation_id,
                    exception=run_reporting.exception_details(exc),
                )
