"""Small, privacy-conscious run logs and human-readable result files."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import gzip
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
import traceback
from typing import Any, Iterable, Mapping
import urllib.parse
import uuid


PROJECT_DIR = Path(__file__).resolve().parents[2]
LOGGER = logging.getLogger("auto_eudm")
_LOG_PATH: Path | None = None
_LOG_LOCK = threading.Lock()
_DIAGNOSTIC_EVENTS: deque[tuple[float, str]] = deque(maxlen=12000)
DIAGNOSTIC_WINDOW_SECONDS = 5 * 60
_SESSION_STAMP = datetime.now()
_SESSION_ID = uuid.uuid4().hex
_DIAGNOSTIC_SEQUENCE = 0
_PC_TOOLKIT_LOG_DIR = PROJECT_DIR / "results" / "pc-toolkit-logs"
_PC_TOOLKIT_LOG_PATH: Path | None = None
_PC_TOOLKIT_LOG_PART = 1
PC_TOOLKIT_LOG_MAX_BYTES = 8 * 1024 * 1024
PC_TOOLKIT_LOG_MAX_FILES = 20
PC_TOOLKIT_LOG_SCHEMA_VERSION = 2
PC_TOOLKIT_BODY_MAX_CHARS = 1_000_000

_SENSITIVE_NAME = re.compile(
    r"(?:authorization|cookie|password|passwd|secret|token|jwt|accesskey|refreshkey)",
    re.IGNORECASE,
)
_SENSITIVE_TEXT = re.compile(
    r"((?:authorization|cookie|password|passwd|secret|token|jwt|access[_-]?key|"
    r"access[_-]?token|refresh[_-]?key|refresh[_-]?token|id[_-]?token)"
    r"\s*[\"']?\s*[:=]\s*[\"']?)[^\s&\"']+",
    re.IGNORECASE,
)
_SENSITIVE_FORM_TEXT = re.compile(
    r"((?:authorization|cookie|password|passwd|secret|token|jwt|access[_-]?key|"
    r"access[_-]?token|refresh[_-]?key|refresh[_-]?token|id[_-]?token|code|state|"
    r"samlresponse|relaystate|assertion)\s*[\"']?\s*[:=]\s*[\"']?)"
    r"[^\s&\"']+",
    re.IGNORECASE,
)
_SENSITIVE_HTML_ATTRIBUTE = re.compile(
    r"((?:name|id)\s*=\s*[\"']?(?:samlresponse|relaystate|code|state|"
    r"access_token|refresh_token|id_token|assertion)[\"']?[^>]*?\bvalue\s*=\s*[\"']?)"
    r"[^\"'\s>]+",
    re.IGNORECASE,
)
_SENSITIVE_QUERY = re.compile(
    r"([?&](?:access_token|refresh_token|id_token|token|code|state|samlresponse|"
    r"assertion)=)[^&#\s]+",
    re.IGNORECASE,
)
_BEARER_TOKEN = re.compile(
    r"(\bBearer\s+)[A-Za-z0-9._~+/=-]+",
    re.IGNORECASE,
)


def _redacted_marker(value: Any) -> str:
    """Show that a secret was present without retaining the secret itself."""
    if value is None:
        return "[REDACTED]"
    try:
        length = len(value)  # type: ignore[arg-type]
    except TypeError:
        length = len(str(value))
    return f"[REDACTED; length={length}]"


def _redact_text(value: Any, *, form_fields: bool = False) -> str:
    """Remove credentials embedded in URLs, form data, and exception text."""
    text = str(value)
    text = _BEARER_TOKEN.sub(r"\1[REDACTED]", text)
    text = _SENSITIVE_QUERY.sub(r"\1[REDACTED]", text)
    text = _SENSITIVE_TEXT.sub(r"\1[REDACTED]", text)
    if form_fields:
        text = _SENSITIVE_FORM_TEXT.sub(r"\1[REDACTED]", text)
        text = _SENSITIVE_HTML_ATTRIBUTE.sub(r"\1[REDACTED]", text)
    return text


def _redact_value(value: Any) -> Any:
    """Keep diagnostic structure while removing credentials from payloads."""
    if isinstance(value, Mapping):
        return {
            str(key): _redacted_marker(item) if _SENSITIVE_NAME.search(str(key)) else _redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _compact_body(body: Any) -> str | None:
    if body is None:
        return None
    if isinstance(body, (Mapping, list, tuple)):
        return json.dumps(_redact_value(body), ensure_ascii=False, separators=(",", ":"))
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    if not isinstance(body, str):
        body = str(body)
    try:
        parsed = json.loads(body)
    except (TypeError, json.JSONDecodeError):
        return _redact_text(body, form_fields=True)
    return json.dumps(_redact_value(parsed), ensure_ascii=False, separators=(",", ":"))


def _safe_headers(headers: Mapping[str, Any] | None) -> dict[str, str] | None:
    if not headers:
        return None
    result: dict[str, str] = {}
    for key, value in headers.items():
        name = str(key)
        result[name] = _redacted_marker(value) if _SENSITIVE_NAME.search(name) else _redact_text(value)
    return result


def diagnostic_id(prefix: str = "event") -> str:
    """Return a short ID suitable for joining related diagnostic records."""
    safe_prefix = re.sub(r"[^A-Za-z0-9_-]+", "-", str(prefix)).strip("-") or "event"
    return f"{safe_prefix}-{uuid.uuid4().hex[:12]}"


def exception_details(error: BaseException) -> dict[str, Any]:
    """Return a useful, credential-safe description of an exception chain."""
    chain: list[dict[str, Any]] = []
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(chain) < 8:
        seen.add(id(current))
        item: dict[str, Any] = {
            "type": type(current).__name__,
            "module": type(current).__module__,
            "message": _redact_text(str(current)),
            "args": _redact_value(list(current.args)),
        }
        for attribute in ("errno", "strerror", "reason", "code", "filename"):
            value = getattr(current, attribute, None)
            if value is not None:
                item[attribute] = _redact_value(value)
        chain.append(item)
        current = current.__cause__ or current.__context__
    details = dict(chain[0]) if chain else {"type": type(error).__name__}
    if len(chain) > 1:
        details["chain"] = chain[1:]
    try:
        rendered = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    except Exception:
        rendered = ""
    if rendered:
        details["traceback"] = _redact_text(rendered)[-24_000:]
    return details


def _record_context(timestamp: float) -> dict[str, Any]:
    """Add stable session and ordering metadata to every diagnostic record."""
    global _DIAGNOSTIC_SEQUENCE
    with _LOG_LOCK:
        _DIAGNOSTIC_SEQUENCE += 1
        sequence = _DIAGNOSTIC_SEQUENCE
        session_id = _SESSION_ID
    return {
        "schema_version": PC_TOOLKIT_LOG_SCHEMA_VERSION,
        "session_id": session_id,
        "sequence": sequence,
        "time": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="milliseconds"),
        "monotonic_ms": round(time.monotonic() * 1000),
        "pid": os.getpid(),
        "thread": threading.current_thread().name,
    }


def _body_capture(body: Any) -> dict[str, Any]:
    """Return a compact body plus size/hash metadata for oversized responses."""
    compact = _compact_body(body)
    if compact is None:
        return {}
    encoded = compact.encode("utf-8", errors="replace")
    capture: dict[str, Any] = {
        "value": compact,
        "bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "truncated": False,
    }
    if len(compact) > PC_TOOLKIT_BODY_MAX_CHARS:
        capture["value"] = (
            compact[:PC_TOOLKIT_BODY_MAX_CHARS]
            + f"… [truncated; captured {PC_TOOLKIT_BODY_MAX_CHARS} of {len(compact)} characters]"
        )
        capture["truncated"] = True
        capture["captured_bytes"] = len(capture["value"].encode("utf-8", errors="replace"))
    return capture


def _safe_url_details(url: str) -> dict[str, Any]:
    safe_url = _redact_text(url)
    details: dict[str, Any] = {"request_url": safe_url}
    try:
        parsed = urllib.parse.urlsplit(safe_url)
    except ValueError:
        return details
    if parsed.scheme:
        details["url_scheme"] = parsed.scheme
    if parsed.hostname:
        details["url_host"] = parsed.hostname
    if parsed.port is not None:
        details["url_port"] = parsed.port
    if parsed.path:
        details["url_path"] = parsed.path
    query_keys = sorted({key for key, _ in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)})
    if query_keys:
        details["url_query_keys"] = query_keys
    return details


def configure_logging(*, enabled: bool, command: str) -> Path | None:
    """Start an optional log without recording credentials or cookies."""
    for handler in list(LOGGER.handlers):
        LOGGER.removeHandler(handler)
        handler.close()
    global _LOG_PATH, _PC_TOOLKIT_LOG_PATH, _PC_TOOLKIT_LOG_PART
    global _SESSION_STAMP, _SESSION_ID, _DIAGNOSTIC_SEQUENCE
    with _LOG_LOCK:
        _LOG_PATH = None
        _PC_TOOLKIT_LOG_PATH = None
        _PC_TOOLKIT_LOG_PART = 1
        _SESSION_STAMP = datetime.now()
        _SESSION_ID = uuid.uuid4().hex
        _DIAGNOSTIC_SEQUENCE = 0
        _DIAGNOSTIC_EVENTS.clear()
    LOGGER.setLevel(logging.DEBUG)
    LOGGER.propagate = False
    if not enabled:
        LOGGER.addHandler(logging.NullHandler())
        return None
    folder = PROJECT_DIR / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{datetime.now():%Y%m%d-%H%M%S-%f}-{command}.log"
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOGGER.addHandler(handler)
    _LOG_PATH = path
    LOGGER.info("Started %s", command)
    print(f"Detailed activity log: {path}")
    return path


def diagnostics_enabled() -> bool:
    """Whether the current process keeps the short in-memory API capture."""
    return True


def diagnostics_status() -> dict[str, Any]:
    with _LOG_LOCK:
        path = _LOG_PATH
        session_stamp = _SESSION_STAMP
        cutoff = time.time() - DIAGNOSTIC_WINDOW_SECONDS
        has_events = any(timestamp >= cutoff for timestamp, _ in _DIAGNOSTIC_EVENTS)
    return {
        "enabled": True,
        "filename": path.name if path else f"{session_stamp:%Y%m%d-%H%M%S}-helix-diagnostics.log",
        "download_available": has_events,
    }


def diagnostics_download() -> tuple[bytes, str] | None:
    with _LOG_LOCK:
        path = _LOG_PATH
        session_stamp = _SESSION_STAMP
        cutoff = time.time() - DIAGNOSTIC_WINDOW_SECONDS
        lines = [line for timestamp, line in _DIAGNOSTIC_EVENTS if timestamp >= cutoff]
    if not lines:
        return None
    content = ("\n".join(lines) + "\n").encode("utf-8")
    compressed = gzip.compress(content, compresslevel=9, mtime=0)
    stem = path.stem if path else f"{session_stamp:%Y%m%d-%H%M%S}-helix-diagnostics"
    return compressed, f"{stem}-last-5-minutes.log.gz"


def _record_diagnostic(
    entry: Mapping[str, Any],
    *,
    record_context: Mapping[str, Any] | None = None,
) -> None:
    timestamp = time.time()
    record = {
        **(record_context or _record_context(timestamp)),
        **entry,
    }
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    with _LOG_LOCK:
        _DIAGNOSTIC_EVENTS.append((timestamp, line))


def event(message: str, *args: object) -> None:
    try:
        rendered = message % args if args else message
    except (TypeError, ValueError):
        rendered = message
    _record_diagnostic({"event": "note", "message": rendered})
    LOGGER.info(message, *args)


def exception(message: str, *args: object) -> None:
    """Record the active exception without exposing it in the browser response."""
    LOGGER.exception(message, *args)


def network(
    method: str,
    path: str,
    *,
    status: int | None = None,
    duration_ms: int | None = None,
    transport: str,
    error: str | None = None,
    request_body: Any = None,
    response_body: Any = None,
    request_headers: Mapping[str, Any] | None = None,
    response_headers: Mapping[str, Any] | None = None,
    request_url: str | None = None,
    request_id: str | None = None,
    operation_id: str | None = None,
    error_detail: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> None:
    if not diagnostics_enabled():
        return
    entry: dict[str, Any] = {
        "event": "api",
        "event_id": diagnostic_id("api"),
        "phase": "completed",
        "transport": transport,
        "method": method,
        "path": path,
    }
    if status is not None:
        entry["status"] = status
        entry["status_class"] = f"{status // 100}xx"
    if duration_ms is not None:
        entry["duration_ms"] = duration_ms
    if error:
        entry["error"] = _redact_text(error)
    if error_detail:
        entry["error_detail"] = _redact_text(error_detail)
    if request_url:
        entry.update(_safe_url_details(request_url))
    if request_id:
        entry["request_id"] = request_id
    if operation_id:
        entry["operation_id"] = operation_id
    if details:
        for key, value in details.items():
            if key not in entry:
                entry[str(key)] = _redact_value(value)
    request_capture = _body_capture(request_body)
    response_capture = _body_capture(response_body)
    safe_request_headers = _safe_headers(request_headers)
    safe_response_headers = _safe_headers(response_headers)
    if request_capture:
        entry["request_body"] = request_capture.pop("value")
        entry["request_body_bytes"] = request_capture.pop("bytes")
        entry["request_body_sha256"] = request_capture.pop("sha256")
        if request_capture.pop("truncated", False):
            entry["request_body_truncated"] = True
            entry["request_body_captured_bytes"] = request_capture.pop("captured_bytes", None)
    if response_capture:
        entry["response_body"] = response_capture.pop("value")
        entry["response_body_bytes"] = response_capture.pop("bytes")
        entry["response_body_sha256"] = response_capture.pop("sha256")
        if response_capture.pop("truncated", False):
            entry["response_body_truncated"] = True
            entry["response_body_captured_bytes"] = response_capture.pop("captured_bytes", None)
    if safe_request_headers:
        entry["request_headers"] = safe_request_headers
    if safe_response_headers:
        entry["response_headers"] = safe_response_headers
    record_context = _record_context(time.time())
    _record_diagnostic(entry, record_context=record_context)
    if transport == "pc-toolkit":
        _write_pc_toolkit_entry(entry, record_context=record_context)
    LOGGER.info("API %s", json.dumps(entry, ensure_ascii=False, separators=(",", ":")))


def _pc_toolkit_safe_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Make a compact, credential-safe copy for the persisted PC Toolkit log."""
    safe: dict[str, Any] = {}
    for key, value in entry.items():
        if key in {"request_body", "response_body"}:
            capture = _body_capture(value)
            safe[key] = capture.get("value")
            if capture:
                safe[f"{key}_bytes"] = capture["bytes"]
                safe[f"{key}_sha256"] = capture["sha256"]
                if capture.get("truncated"):
                    safe[f"{key}_truncated"] = True
                    safe[f"{key}_captured_bytes"] = capture.get("captured_bytes")
        else:
            safe[key] = _redact_value(value)
    return safe


def _pc_toolkit_path_locked() -> Path:
    global _PC_TOOLKIT_LOG_PATH
    if _PC_TOOLKIT_LOG_PATH is None:
        _PC_TOOLKIT_LOG_DIR.mkdir(parents=True, exist_ok=True)
        _PC_TOOLKIT_LOG_PATH = _PC_TOOLKIT_LOG_DIR / (
            f"{_SESSION_STAMP:%Y%m%d-%H%M%S-%f}-pc-toolkit.log"
        )
    return _PC_TOOLKIT_LOG_PATH


def _pc_toolkit_session_paths_locked() -> list[Path]:
    prefix = f"{_SESSION_STAMP:%Y%m%d-%H%M%S-%f}-pc-toolkit"
    try:
        paths = list(_PC_TOOLKIT_LOG_DIR.glob(f"{prefix}*.log"))
        paths.sort(key=_pc_toolkit_path_sort_key)
    except OSError:
        paths = []
    if _PC_TOOLKIT_LOG_PATH is not None and _PC_TOOLKIT_LOG_PATH not in paths:
        paths.append(_PC_TOOLKIT_LOG_PATH)
    return paths


def _pc_toolkit_path_sort_key(path: Path) -> tuple[int, str]:
    match = re.search(r"-part-(\d+)\.log$", path.name)
    return (int(match.group(1)) if match else 1, path.name)


def _rotate_pc_toolkit_log_locked(next_line_bytes: int) -> Path:
    global _PC_TOOLKIT_LOG_PART, _PC_TOOLKIT_LOG_PATH
    path = _pc_toolkit_path_locked()
    try:
        current_size = path.stat().st_size
    except OSError:
        current_size = 0
    if current_size and current_size + next_line_bytes > PC_TOOLKIT_LOG_MAX_BYTES:
        _PC_TOOLKIT_LOG_PART += 1
        _PC_TOOLKIT_LOG_PATH = _PC_TOOLKIT_LOG_DIR / (
            f"{_SESSION_STAMP:%Y%m%d-%H%M%S-%f}-pc-toolkit-"
            f"part-{_PC_TOOLKIT_LOG_PART}.log"
        )
        path = _PC_TOOLKIT_LOG_PATH
    return path


def _prune_pc_toolkit_logs_locked() -> None:
    try:
        paths = sorted(
            _PC_TOOLKIT_LOG_DIR.glob("*-pc-toolkit*.log"),
            key=lambda candidate: candidate.stat().st_mtime,
        )
    except OSError:
        return
    current_prefix = f"{_SESSION_STAMP:%Y%m%d-%H%M%S-%f}-pc-toolkit"
    old_paths = [path for path in paths if not path.name.startswith(current_prefix)]
    for old_path in old_paths[:-PC_TOOLKIT_LOG_MAX_FILES]:
        try:
            old_path.unlink()
        except OSError:
            pass


def _write_pc_toolkit_entry(
    entry: Mapping[str, Any],
    *,
    record_context: Mapping[str, Any] | None = None,
) -> None:
    record = {
        "source": "pc-toolkit",
        **(record_context or _record_context(time.time())),
        **_pc_toolkit_safe_entry(entry),
    }
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    encoded = line.encode("utf-8")
    with _LOG_LOCK:
        path = _rotate_pc_toolkit_log_locked(len(encoded))
        try:
            with path.open("ab") as handle:
                handle.write(encoded)
            _prune_pc_toolkit_logs_locked()
        except OSError:
            # Diagnostics must never interrupt a lookup or submission.
            return


def pc_toolkit_event(event_name: str, **details: Any) -> None:
    """Persist a lifecycle event alongside PC Toolkit API traffic."""
    entry = {
        "event": event_name,
        "event_id": diagnostic_id("pc-event"),
        **details,
    }
    record_context = _record_context(time.time())
    _record_diagnostic(
        {"event": "pc_toolkit", **_pc_toolkit_safe_entry(entry)},
        record_context=record_context,
    )
    _write_pc_toolkit_entry(entry, record_context=record_context)
    LOGGER.info("PC Toolkit %s", json.dumps(_pc_toolkit_safe_entry(entry), ensure_ascii=False, separators=(",", ":")))


def pc_toolkit_log_status() -> dict[str, Any]:
    with _LOG_LOCK:
        path = _pc_toolkit_path_locked()
        size = 0
        session_paths = _pc_toolkit_session_paths_locked()
        latest_mtime = 0.0
        for session_path in session_paths:
            try:
                size += session_path.stat().st_size
                latest_mtime = max(latest_mtime, session_path.stat().st_mtime)
            except OSError:
                pass
    try:
        relative_path = str(path.relative_to(PROJECT_DIR))
    except ValueError:
        # Tests and embedders may redirect the diagnostic directory outside
        # the checkout; retain a useful path rather than failing status calls.
        relative_path = str(path)
    return {
        "path": str(path),
        "relative_path": relative_path,
        "available": size > 0,
        "size_bytes": size,
        "session_id": _SESSION_ID,
        "part_count": len(session_paths),
        "last_activity": (
            datetime.fromtimestamp(latest_mtime, timezone.utc).isoformat(timespec="seconds")
            if latest_mtime else None
        ),
    }


def pc_toolkit_log_download() -> tuple[bytes, str] | None:
    with _LOG_LOCK:
        path = _PC_TOOLKIT_LOG_PATH
        if path is None:
            return None
        chunks: list[bytes] = []
        for session_path in _pc_toolkit_session_paths_locked():
            try:
                chunks.append(session_path.read_bytes())
            except OSError:
                pass
        content = b"".join(chunks)
    if not content:
        return None
    return gzip.compress(content, compresslevel=9, mtime=0), (
        f"{_SESSION_STAMP:%Y%m%d-%H%M%S-%f}-pc-toolkit-session.log.gz"
    )


def write_result_file(command: str, lines: Iterable[str]) -> Path:
    folder = PROJECT_DIR / "results"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{datetime.now():%Y%m%d-%H%M%S-%f}-{command}.txt"
    content = [f"Deployments results — {command}", f"Generated: {datetime.now():%Y-%m-%d %H:%M:%S}", ""]
    content.extend(lines)
    path.write_text("\n".join(content).rstrip() + "\n", encoding="utf-8")
    print(f"Results saved: {path}")
    event("Results written to %s", path)
    return path
