"""HTTP routes and static-file serving for the local AutoEUDM web UI."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import socket
import subprocess
import threading
from typing import Any
import urllib.parse

from . import eudm_request as eudm
from . import run_reporting
from .web_models import Location, RequestSpec, validate_queue


ROOT = Path(__file__).resolve().parents[2]
WEB_ROOT = (ROOT / "web").resolve()
# Workbook uploads are base64 encoded in JSON, so allow headroom for the
# roughly 4/3 expansion of a 100 MB workbook plus the surrounding payload.
MAX_BODY = 140 * 1024 * 1024
MAX_SEARCH_QUERY = 200
REQUEST_TIMEOUT_SECONDS = 30
JOB_ID_PATTERN = re.compile(r"[a-f0-9]{32}")
IMPORT_DRAFT_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,100}")
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class HTTPInputError(eudm.EUDMError):
    """A malformed local HTTP request with an explicit response status."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def repository_commit_id() -> str:
    """Return the checkout commit that launched this server."""
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            check=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    commit = result.stdout.strip()
    return commit or "unknown"


class AutoEUDMHandler(BaseHTTPRequestHandler):
    server_version = "AutoEUDM/1.0"
    sys_version = ""

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(REQUEST_TIMEOUT_SECONDS)

    @property
    def app(self) -> Any:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        if self.app.config.verbose:
            super().log_message(format, *args)

    def version_string(self) -> str:
        return self.server_version

    def _bytes(
        self,
        body: bytes,
        *,
        status: int = 200,
        content_type: str,
        cache_control: str = "no-store",
        headers: dict[str, str] | None = None,
    ) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache_control)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._bytes(
            body,
            status=status,
            content_type="application/json; charset=utf-8",
        )

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
        if self.headers.get("Transfer-Encoding"):
            raise HTTPInputError("Chunked request bodies are not supported.")
        length_headers = self.headers.get_all("Content-Length") or []
        if len(length_headers) > 1:
            raise HTTPInputError("The request contained multiple Content-Length headers.")
        raw_length = length_headers[0] if length_headers else "0"
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise HTTPInputError("The request Content-Length was invalid.") from exc
        if length < 0:
            raise HTTPInputError("The request Content-Length cannot be negative.")
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise HTTPInputError("The request is larger than the local limit.", 413)
        if self.headers.get_content_type() != "application/json":
            raise HTTPInputError("POST request bodies must use application/json.", 415)
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise HTTPInputError("The browser request body was incomplete.")
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HTTPInputError("The browser sent invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise HTTPInputError("The browser request must be a JSON object.")
        return payload

    def _loopback_url_matches_server(self, raw_url: str) -> bool:
        try:
            parsed = urllib.parse.urlparse(raw_url)
            port = parsed.port
        except ValueError:
            return False
        return bool(
            parsed.scheme == "http"
            and parsed.hostname in LOOPBACK_HOSTS
            and port == self.server.server_port  # type: ignore[attr-defined]
            and not parsed.username
            and not parsed.password
            and parsed.path in {"", "/"}
            and not parsed.params
            and not parsed.query
            and not parsed.fragment
        )

    def _allow_host(self) -> bool:
        host = self.headers.get("Host")
        if not host:
            return False
        return self._loopback_url_matches_server(f"http://{host}")

    def _allow_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return self._loopback_url_matches_server(origin)

    @staticmethod
    def _search_query(payload: dict[str, Any], *, minimum: int, message: str) -> str:
        query = str(payload.get("query", "")).strip()
        if len(query) < minimum:
            raise eudm.EUDMError(message)
        if len(query) > MAX_SEARCH_QUERY:
            raise eudm.EUDMError(
                f"Search text must be {MAX_SEARCH_QUERY} characters or fewer."
            )
        return query

    @staticmethod
    def _exact_search_result(
        results: list[dict[str, Any]], query: str
    ) -> dict[str, Any] | None:
        wanted = " ".join(query.split()).casefold()
        for result in results:
            if not isinstance(result, dict):
                continue
            values = [result.get("value"), *(result.get("columns") or [])]
            if any(" ".join(str(value or "").split()).casefold() == wanted for value in values):
                return result
        return None

    def do_GET(self) -> None:
        if not self._allow_host():
            self._error("Requests are accepted only on this localhost server.", 403)
            return
        try:
            self._do_get()
        except eudm.EUDMError as exc:
            self._handle_eudm_error(exc, 404)
        except (KeyError, ValueError, TypeError):
            self._error("The local web request was invalid.", 400)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            return
        except Exception:
            run_reporting.exception(
                "Unhandled local HTTP GET for %s",
                urllib.parse.urlparse(self.path).path,
            )
            self._error("An unexpected local server error occurred.", 500)

    def _do_get(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/api/runtime":
            self._json(
                {
                    "commit_id": self.server.commit_id,  # type: ignore[attr-defined]
                    "pid": os.getpid(),
                }
            )
            return
        if path == "/api/config":
            self._json(self.app.config_json())
            return
        if path == "/api/preferences":
            self._json(self.app.preferences_json())
            return
        if path == "/api/import-drafts":
            self._json({"drafts": self.app.import_drafts_json()})
            return
        if path == "/api/queue":
            self._json({"requests": self.app.request_queue_json()})
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
            if not JOB_ID_PATTERN.fullmatch(job_id):
                self._error("Unknown workbook import.", 404)
                return
            self._json(self.app.import_status(job_id))
            return
        if path.startswith("/api/jobs/"):
            parts = path.strip("/").split("/")
            job_id = parts[2] if len(parts) >= 3 else ""
            if not JOB_ID_PATTERN.fullmatch(job_id):
                self._error("Unknown submission run.", 404)
                return
            if len(parts) == 4 and parts[3] == "results.txt":
                body = self.app.jobs.result_text(parts[2]).encode("utf-8")
                self._bytes(
                    body,
                    content_type="text/plain; charset=utf-8",
                    headers={
                        "Content-Disposition": (
                            f'attachment; filename="auto-eudm-{parts[2][:8]}.txt"'
                        )
                    },
                )
                return
            if len(parts) == 3:
                self._json(self.app.jobs.get(parts[2]).to_json())
                return
        if path.startswith("/api/"):
            self._error("Unknown local API endpoint.", 404)
            return
        self._static(path)

    def _static(self, request_path: str) -> None:
        relative = "index.html" if request_path == "/" else request_path.lstrip("/")
        selected = (WEB_ROOT / relative).resolve()
        if WEB_ROOT not in selected.parents or not selected.is_file():
            self._bytes(
                b"Not found.\n",
                status=HTTPStatus.NOT_FOUND,
                content_type="text/plain; charset=utf-8",
            )
            return
        body = selected.read_bytes()
        content_type = mimetypes.guess_type(selected.name)[0] or "application/octet-stream"
        self._bytes(
            body,
            content_type=content_type,
            cache_control="no-cache",
            headers={
                "Content-Security-Policy": (
                    "default-src 'self'; style-src 'self'; script-src 'self'; "
                    "connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
                    "form-action 'self'; frame-ancestors 'none';"
                )
            },
        )

    def do_POST(self) -> None:
        if not self._allow_host() or not self._allow_origin():
            self._error("Requests are accepted only from this localhost app.", 403)
            return
        try:
            self._do_post()
        except HTTPInputError as exc:
            self._error(str(exc), exc.status)
        except eudm.EUDMError as exc:
            self._handle_eudm_error(exc, 422)
        except (KeyError, ValueError, TypeError):
            self._error("The local web request was invalid.", 400)
        except socket.timeout:
            self._error("The local request timed out before it was complete.", 408)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            run_reporting.exception(
                "Unhandled local HTTP POST for %s",
                urllib.parse.urlparse(self.path).path,
            )
            self._error("An unexpected local server error occurred.", 500)

    def do_DELETE(self) -> None:
        if not self._allow_host() or not self._allow_origin():
            self._error("Requests are accepted only from this localhost app.", 403)
            return
        try:
            path = urllib.parse.urlparse(self.path).path
            if path == "/api/import/backlog/ignored":
                self.app.clear_alm_backlog_ignored()
                self._json({"cleared": True})
                return
            prefix = "/api/import-drafts/"
            if not path.startswith(prefix):
                self._error("Unknown local API endpoint.", 404)
                return
            draft_id = path.removeprefix(prefix).strip()
            if not IMPORT_DRAFT_ID_PATTERN.fullmatch(draft_id):
                self._error("Unknown ALM import draft.", 404)
                return
            self._json({"drafts": self.app.delete_import_draft(draft_id)})
        except eudm.EUDMError as exc:
            self._handle_eudm_error(exc, 422)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            return
        except Exception:
            run_reporting.exception(
                "Unhandled local HTTP DELETE for %s",
                urllib.parse.urlparse(self.path).path,
            )
            self._error("An unexpected local server error occurred.", 500)

    def _do_post(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        payload = self._read_json()
        if path == "/api/shutdown":
            self._json({"stopping": True}, 202)
            threading.Thread(
                target=self.server.shutdown,
                name="auto-eudm-shutdown",
                daemon=True,
            ).start()
            return
        if path == "/api/connect":
            self.app.clients.connect_async()
            self._json(self.app.clients.status(), 202)
            return
        if path == "/api/preferences":
            self._json(self.app.save_preferences(payload))
            return
        if path == "/api/import-drafts":
            self._json({"drafts": self.app.save_import_draft(payload)})
            return
        if path == "/api/queue":
            self._json({"requests": self.app.save_request_queue(payload.get("requests"))})
            return
        if path == "/api/connection/health":
            self._json(self.app.clients.check_connection())
            return
        if path == "/api/search/assets":
            query = self._search_query(
                payload,
                minimum=2,
                message="Enter at least two serial characters.",
            )
            cached = None if payload.get("bypass_cache") else self.app.verification_cache_lookup("serial", query)
            if cached:
                self._json({"results": [cached], "cached": True})
                return
            probe = self.app.clients.fresh_search() if payload.get("fresh") else self.app.clients.search()
            results = probe.assets(query)
            exact = self._exact_search_result(results, query)
            if exact:
                self.app.record_verified_serial(exact)
            self._json({"results": results})
            return
        if path == "/api/search/users":
            query = self._search_query(
                payload,
                minimum=2,
                message="Enter at least two name or username characters.",
            )
            cached = None if payload.get("bypass_cache") else self.app.verification_cache_lookup("username", query)
            if cached:
                self._json({"results": [cached], "cached": True})
                return
            probe = self.app.clients.fresh_search() if payload.get("fresh") else self.app.clients.search()
            results = probe.users(query, bool(payload.get("returning")))
            exact = self._exact_search_result(results, query)
            if exact:
                self.app.record_verified_username(exact)
            self._json({"results": results})
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
            if not self.app.config.spreadsheet_import_enabled:
                raise eudm.EUDMError("Spreadsheet import is disabled by this AutoEUDM environment.")
            job = self.app.start_import(
                str(payload.get("filename", "")),
                str(payload.get("data", "")),
            )
            self._json(job.to_json(), 202)
            return
        if path == "/api/import/map":
            if not self.app.config.spreadsheet_import_enabled:
                raise eudm.EUDMError("Spreadsheet import is disabled by this AutoEUDM environment.")
            columns = payload.get("columns")
            if not isinstance(columns, dict):
                raise eudm.EUDMError("Choose all spreadsheet columns.")
            job = self.app.start_mapped_import(
                str(payload.get("import_id", "")),
                columns,
            )
            self._json(job.to_json(), 202)
            return
        if path == "/api/import/prepare":
            workbook = self.app.get_import(str(payload.get("import_id", "")))
            selected_dates = payload.get("dates")
            if not isinstance(selected_dates, list):
                selected_dates = str(payload.get("date", ""))
            group_selections = payload.get("group_selections")
            if group_selections is not None and not isinstance(group_selections, dict):
                raise eudm.EUDMError("Date section selections were invalid.")
            self._json(
                workbook.prepare(
                    str(payload.get("sheet", "")),
                    selected_dates,
                    str(payload.get("mode", "")),
                    Location.from_json(payload.get("location")) if payload.get("location") else None,
                    payload.get("group_selection"),
                    group_selections=group_selections,
                )
            )
            return
        if path == "/api/import/backlog":
            workbook = self.app.get_import(str(payload.get("import_id", "")))
            self._json(
                workbook.prepare_backlog(
                    str(payload.get("sheet", "")),
                    payload.get("days_back", 30),
                    bool(payload.get("include_today")),
                    self.app.alm_backlog_ignored_keys(),
                )
            )
            return
        if path == "/api/import/backlog/ignore":
            self.app.ignore_alm_backlog(
                str(payload.get("serial", "")),
                str(payload.get("username", "")),
            )
            self._json({"saved": True})
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

    def __init__(self, address: tuple[str, int], app: Any) -> None:
        super().__init__(address, AutoEUDMHandler)
        self.app = app
        self.commit_id = repository_commit_id()
