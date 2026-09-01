from __future__ import annotations

import http.client
import json
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from auto_eudm.web_server import AutoEUDMServer, MAX_BODY, MAX_SEARCH_QUERY


class FakeClients:
    request_for = "request.user"

    @staticmethod
    def status() -> dict[str, object]:
        return {"state": "ready"}


class FakeJobs:
    @staticmethod
    def history() -> list[dict[str, object]]:
        return []

    @staticmethod
    def result_text(job_id: str) -> str:
        return f"Run: {job_id}\n"


class FakeApp:
    def __init__(self) -> None:
        self.config = SimpleNamespace(verbose=False, spreadsheet_import_enabled=False)
        self.clients = FakeClients()
        self.jobs = FakeJobs()
        self.saved_preferences: list[dict[str, object]] = []
        self.import_drafts: list[dict[str, object]] = []
        self.request_queue: list[dict[str, object]] = []
        self.cached: dict[tuple[str, str], dict[str, object]] = {}

    @staticmethod
    def config_json() -> dict[str, object]:
        return {"simulate": True}

    @staticmethod
    def preferences_json() -> dict[str, object]:
        return {"theme": "system"}

    def save_preferences(self, payload: dict[str, object]) -> dict[str, object]:
        self.saved_preferences.append(payload)
        return payload

    def import_drafts_json(self) -> list[dict[str, object]]:
        return self.import_drafts

    def save_import_draft(self, payload: dict[str, object]) -> list[dict[str, object]]:
        self.import_drafts = [draft for draft in self.import_drafts if draft.get("id") != payload.get("id")]
        self.import_drafts.insert(0, payload)
        return self.import_drafts

    def delete_import_draft(self, draft_id: str) -> list[dict[str, object]]:
        self.import_drafts = [draft for draft in self.import_drafts if draft.get("id") != draft_id]
        return self.import_drafts

    def request_queue_json(self) -> list[dict[str, object]]:
        return self.request_queue

    def save_request_queue(self, requests: object) -> list[dict[str, object]]:
        self.request_queue = list(requests) if isinstance(requests, list) else []
        return self.request_queue

    def verification_cache_lookup(self, kind: str, value: str) -> dict[str, object] | None:
        return self.cached.get((kind, value.casefold()))

    def record_verified_serial(self, result: dict[str, object]) -> None:
        self.cached[("serial", str(result["value"]).casefold())] = result

    def record_verified_username(self, result: dict[str, object]) -> None:
        self.cached[("username", str(result["value"]).casefold())] = result


class LocalWebServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = FakeApp()
        with mock.patch(
            "auto_eudm.web_server.repository_commit_id", return_value="test-commit"
        ):
            self.server = AutoEUDMServer(("127.0.0.1", 0), self.app)
        self.port = self.server.server_port
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.01),
            daemon=True,
        )
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[http.client.HTTPResponse, bytes]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request_headers = dict(headers or {})
        if body is not None:
            request_headers.setdefault("Content-Type", "application/json")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response, raw

    def test_runtime_response_has_private_security_headers(self) -> None:
        response, raw = self.request("GET", "/api/runtime")

        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(raw), {"commit_id": "test-commit", "pid": mock.ANY})
        self.assertEqual(response.getheader("Server"), "AutoEUDM/1.0")
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        self.assertEqual(response.getheader("X-Frame-Options"), "DENY")
        self.assertEqual(
            response.getheader("Cross-Origin-Resource-Policy"), "same-origin"
        )

    def test_unknown_api_get_returns_json_instead_of_static_html(self) -> None:
        response, raw = self.request("GET", "/api/not-real")

        self.assertEqual(response.status, 404)
        self.assertEqual(response.getheader("Content-Type"), "application/json; charset=utf-8")
        self.assertIn("Unknown local API endpoint", json.loads(raw)["error"])

    @mock.patch("auto_eudm.web_server.run_reporting.exception")
    def test_unexpected_errors_are_logged_but_sanitized_for_the_browser(
        self, log_exception: mock.Mock
    ) -> None:
        self.app.config_json = mock.Mock(side_effect=RuntimeError("private detail"))

        response, raw = self.request("GET", "/api/config")

        self.assertEqual(response.status, 500)
        self.assertNotIn("private detail", raw.decode("utf-8"))
        log_exception.assert_called_once_with(
            "Unhandled local HTTP GET for %s", "/api/config"
        )

    def test_post_accepts_only_the_same_localhost_port(self) -> None:
        allowed_origin = f"http://localhost:{self.port}"
        response, raw = self.request(
            "POST",
            "/api/preferences",
            payload={"theme": "dark"},
            headers={"Origin": allowed_origin},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(raw), {"theme": "dark"})

        response, _ = self.request(
            "POST",
            "/api/preferences",
            payload={"theme": "light"},
            headers={"Origin": "http://localhost:9999"},
        )
        self.assertEqual(response.status, 403)
        self.assertEqual(self.app.saved_preferences, [{"theme": "dark"}])

    def test_import_drafts_are_read_written_and_deleted_through_local_api(self) -> None:
        draft = {
            "id": "draft-1",
            "workbook": {"import_id": "import-1", "filename": "tracking.xlsx"},
            "phase": "review",
            "settings": {
                "mode": "backlog",
                "sheet": "Bookings 2026",
                "backlog_days": 30,
                "backlog_include_today": False,
            },
            "preview": {
                "mode": "backlog",
                "requests": [{
                    "id": "import-1-42-2",
                    "serial": "SERIAL123",
                    "username": "valid.user",
                    "included": False,
                    "status": "Deployed - Existing Stock",
                }],
            },
        }
        response, raw = self.request("POST", "/api/import-drafts", payload=draft)
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(raw), {"drafts": [draft]})

        response, raw = self.request("GET", "/api/import-drafts")
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(raw), {"drafts": [draft]})

        response, raw = self.request("DELETE", "/api/import-drafts/draft-1")
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(raw), {"drafts": []})

    def test_unsubmitted_queue_is_read_and_written_through_local_api(self) -> None:
        requests = [{"id": "queue-1", "kind": "user", "serials": ["SERIAL123"]}]

        response, raw = self.request("POST", "/api/queue", payload={"requests": requests})
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(raw), {"requests": requests})

        response, raw = self.request("GET", "/api/queue")
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(raw), {"requests": requests})

    def test_non_loopback_host_header_is_rejected(self) -> None:
        response, raw = self.request(
            "GET",
            "/api/runtime",
            headers={"Host": f"example.test:{self.port}"},
        )

        self.assertEqual(response.status, 403)
        self.assertIn("localhost server", json.loads(raw)["error"])

    def test_oversized_request_is_rejected_without_reading_the_body(self) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        connection.putrequest("POST", "/api/preferences")
        connection.putheader("Content-Length", str(MAX_BODY + 1))
        connection.endheaders()

        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()

        self.assertEqual(response.status, 413)
        self.assertIn("larger than", payload["error"])

    def test_malformed_content_length_has_a_clear_client_error(self) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        connection.putrequest("POST", "/api/preferences")
        connection.putheader("Content-Length", "not-a-number")
        connection.endheaders()

        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()

        self.assertEqual(response.status, 400)
        self.assertIn("Content-Length", payload["error"])

    def test_non_json_post_body_is_rejected(self) -> None:
        response, raw = self.request(
            "POST",
            "/api/preferences",
            headers={"Content-Type": "text/plain"},
            payload={"theme": "dark"},
        )

        self.assertEqual(response.status, 415)
        self.assertIn("application/json", json.loads(raw)["error"])
        self.assertEqual(self.app.saved_preferences, [])

    def test_search_text_is_bounded_before_reaching_eudm(self) -> None:
        response, raw = self.request(
            "POST",
            "/api/search/assets",
            payload={"query": "x" * (MAX_SEARCH_QUERY + 1)},
        )

        self.assertEqual(response.status, 422)
        self.assertIn(str(MAX_SEARCH_QUERY), json.loads(raw)["error"])

    def test_cached_serial_is_returned_without_connecting_to_eudm(self) -> None:
        cached = {"value": "ABC123", "columns": ["ABC123"], "device_type": "Laptop"}
        self.app.cached[("serial", "abc123")] = cached

        response, raw = self.request(
            "POST", "/api/search/assets", payload={"query": "ABC123", "fresh": True}
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(raw), {"results": [cached], "cached": True})

    def test_job_routes_reject_non_generated_identifiers(self) -> None:
        response, raw = self.request("GET", '/api/jobs/not-a-job/results.txt')

        self.assertEqual(response.status, 404)
        self.assertIn("Unknown submission run", json.loads(raw)["error"])

    def test_result_download_is_private_and_has_a_safe_filename(self) -> None:
        job_id = "a" * 32
        response, raw = self.request("GET", f"/api/jobs/{job_id}/results.txt")

        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        self.assertEqual(
            response.getheader("Content-Disposition"),
            'attachment; filename="auto-eudm-aaaaaaaa.txt"',
        )
        self.assertEqual(raw.decode("utf-8"), f"Run: {job_id}\n")


if __name__ == "__main__":
    unittest.main()
