"""Small, privacy-conscious run logs and human-readable result files."""

from __future__ import annotations

from collections import deque
from datetime import datetime
import gzip
import json
import logging
from pathlib import Path
import re
import threading
import time
from typing import Any, Iterable, Mapping


PROJECT_DIR = Path(__file__).resolve().parents[2]
LOGGER = logging.getLogger("auto_eudm")
_LOG_PATH: Path | None = None
_LOG_LOCK = threading.Lock()
_DIAGNOSTIC_EVENTS: deque[tuple[float, str]] = deque(maxlen=12000)
DIAGNOSTIC_WINDOW_SECONDS = 5 * 60
_SESSION_STAMP = datetime.now()

_SENSITIVE_NAME = re.compile(
    r"(?:authorization|cookie|password|passwd|secret|token|jwt|accesskey|refreshkey)",
    re.IGNORECASE,
)


def _redact_value(value: Any) -> Any:
    """Keep diagnostic structure while removing credentials from payloads."""
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if _SENSITIVE_NAME.search(str(key)) else _redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_value(item) for item in value]
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
        return re.sub(
            r"((?:authorization|cookie|password|passwd|secret|token|jwt|code|state)\s*[:=]\s*[\"']?)[^\s&\"']+",
            r"\1[REDACTED]",
            body,
            flags=re.IGNORECASE,
        )
    return json.dumps(_redact_value(parsed), ensure_ascii=False, separators=(",", ":"))


def _safe_headers(headers: Mapping[str, Any] | None) -> dict[str, str] | None:
    if not headers:
        return None
    result: dict[str, str] = {}
    for key, value in headers.items():
        name = str(key)
        result[name] = "[REDACTED]" if _SENSITIVE_NAME.search(name) else str(value)
    return result


def configure_logging(*, enabled: bool, command: str) -> Path | None:
    """Start an optional log without recording credentials or cookies."""
    for handler in list(LOGGER.handlers):
        LOGGER.removeHandler(handler)
        handler.close()
    global _LOG_PATH, _SESSION_STAMP
    with _LOG_LOCK:
        _LOG_PATH = None
        _SESSION_STAMP = datetime.now()
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


def _record_diagnostic(entry: Mapping[str, Any]) -> None:
    timestamp = time.time()
    record = {
        "time": datetime.fromtimestamp(timestamp).isoformat(timespec="milliseconds"),
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
) -> None:
    if not diagnostics_enabled():
        return
    entry: dict[str, Any] = {
        "event": "api",
        "transport": transport,
        "method": method,
        "path": path,
    }
    if status is not None:
        entry["status"] = status
    if duration_ms is not None:
        entry["duration_ms"] = duration_ms
    if error:
        entry["error"] = error
    compact_request = _compact_body(request_body)
    compact_response = _compact_body(response_body)
    safe_request_headers = _safe_headers(request_headers)
    safe_response_headers = _safe_headers(response_headers)
    if compact_request is not None:
        entry["request_body"] = compact_request
    if compact_response is not None:
        entry["response_body"] = compact_response
    if safe_request_headers:
        entry["request_headers"] = safe_request_headers
    if safe_response_headers:
        entry["response_headers"] = safe_response_headers
    _record_diagnostic(entry)
    LOGGER.info("API %s", json.dumps(entry, ensure_ascii=False, separators=(",", ":")))


def write_result_file(command: str, lines: Iterable[str]) -> Path:
    folder = PROJECT_DIR / "results"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{datetime.now():%Y%m%d-%H%M%S-%f}-{command}.txt"
    content = [f"AutoEUDM results — {command}", f"Generated: {datetime.now():%Y-%m-%d %H:%M:%S}", ""]
    content.extend(lines)
    path.write_text("\n".join(content).rstrip() + "\n", encoding="utf-8")
    print(f"Results saved: {path}")
    event("Results written to %s", path)
    return path
