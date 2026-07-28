"""HTTP routes and static-file serving for the local AutoEUDM web UI."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
from typing import Any
import urllib.parse

from . import eudm_request as eudm
from .web_models import Location, RequestSpec, validate_queue


ROOT = Path(__file__).resolve().parents[2]
WEB_ROOT = ROOT / "web"
# Workbook uploads are base64 encoded in JSON, so allow headroom for the
# roughly 4/3 expansion of a 100 MB workbook plus the surrounding payload.
MAX_BODY = 140 * 1024 * 1024


class AutoEUDMHandler(BaseHTTPRequestHandler):
    server_version = "AutoEUDM/1.0"

    @property
    def app(self) -> Any:
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
        if path == "/api/preferences":
            self._json(self.app.preferences_json())
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
        if path == "/api/preferences":
            self._json(self.app.save_preferences(payload))
            return
        if path == "/api/connection/health":
            self._json(self.app.clients.check_connection())
            return
        if path == "/api/search/assets":
            query = str(payload.get("query", "")).strip()
            if len(query) < 2:
                raise eudm.EUDMError("Enter at least two serial characters.")
            probe = self.app.clients.fresh_search() if payload.get("fresh") else self.app.clients.search()
            self._json({"results": probe.assets(query)})
            return
        if path == "/api/search/users":
            query = str(payload.get("query", "")).strip()
            if len(query) < 2:
                raise eudm.EUDMError("Enter at least two username characters.")
            self._json(
                {
                    "results": (self.app.clients.fresh_search() if payload.get("fresh") else self.app.clients.search()).users(
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
            if not self.app.config.spreadsheet_import_enabled:
                raise eudm.EUDMError("Spreadsheet import is disabled by this AutoEUDM environment.")
            job = self.app.start_import(
                str(payload.get("filename", "")),
                str(payload.get("data", "")),
            )
            self._json(job.to_json(), 202)
            return
        if path == "/api/import/download":
            if not self.app.config.spreadsheet_import_enabled:
                raise eudm.EUDMError("Spreadsheet import is disabled by this AutoEUDM environment.")
            job = self.app.start_remote_import(str(payload.get("url", "")))
            self._json(job.to_json(), 202)
            return
        if path == "/api/import/map":
            if not self.app.config.spreadsheet_import_enabled:
                raise eudm.EUDMError("Spreadsheet import is disabled by this AutoEUDM environment.")
            columns = payload.get("columns")
            if not isinstance(columns, dict):
                raise eudm.EUDMError("Choose all spreadsheet columns.")
            job = self.app.start_mapped_import(str(payload.get("import_id", "")), columns)
            self._json(job.to_json(), 202)
            return
        if path == "/api/import/prepare":
            workbook = self.app.get_import(str(payload.get("import_id", "")))
            self._json(
                workbook.prepare(
                    str(payload.get("sheet", "")),
                    str(payload.get("date", "")),
                    str(payload.get("mode", "")),
                    Location.from_json(payload.get("location")) if payload.get("location") else None,
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

    def __init__(self, address: tuple[str, int], app: Any) -> None:
        super().__init__(address, AutoEUDMHandler)
        self.app = app
