from __future__ import annotations

import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from auto_eudm import run_reporting


class DiagnosticCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        run_reporting.configure_logging(enabled=False, command="test")

    def tearDown(self) -> None:
        run_reporting.configure_logging(enabled=False, command="test")

    def test_export_contains_api_bodies_and_redacts_credentials(self) -> None:
        run_reporting.network(
            "POST",
            "v2/example",
            status=422,
            duration_ms=12,
            transport="browser",
            request_body={
                "serial": "S1234567890",
                "username": "rkontos",
                "token": "do-not-export",
            },
            response_body={
                "error": "invalid selection",
                "accessToken": "do-not-export",
            },
            request_headers={"X-Request-Id": "request-id", "Cookie": "private"},
            response_headers={"Content-Type": "application/json", "Set-Cookie": "private"},
        )

        status = run_reporting.diagnostics_status()
        self.assertTrue(status["download_available"])
        downloaded = run_reporting.diagnostics_download()
        self.assertIsNotNone(downloaded)
        compressed, filename = downloaded or (b"", "")
        self.assertTrue(filename.endswith("-last-5-minutes.log.gz"))
        lines = gzip.decompress(compressed).decode("utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["event"], "api")
        self.assertIn("S1234567890", record["request_body"])
        self.assertIn("rkontos", record["request_body"])
        self.assertIn("invalid selection", record["response_body"])
        self.assertNotIn("do-not-export", lines[0])
        self.assertNotIn("private", lines[0])

    def test_empty_capture_has_no_download(self) -> None:
        self.assertFalse(run_reporting.diagnostics_status()["download_available"])
        self.assertIsNone(run_reporting.diagnostics_download())

    def test_pc_trace_keeps_correlation_metadata_and_safe_body_details(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch.object(run_reporting, "_PC_TOOLKIT_LOG_DIR", Path(folder)):
                run_reporting.configure_logging(enabled=False, command="test")
                run_reporting.network(
                    "POST",
                    "/auth/session",
                    status=401,
                    duration_ms=34,
                    transport="pc-toolkit",
                    request_url="https://example.test/auth/session?code=private-code&source=portal",
                    request_id="pc-http-test",
                    operation_id="pc-connect-test",
                    error="HTTPError",
                    error_detail="token=private-token",
                    request_body={"username": "example.user", "accessToken": "private-token"},
                    response_body={"message": "not authenticated", "requestId": "trace-123"},
                    request_headers={"Accept": "application/json", "Cookie": "private-cookie"},
                    response_headers={"Content-Type": "application/json"},
                    details={"channel": "test", "purpose": "role_probe"},
                )
                path = Path(run_reporting.pc_toolkit_log_status()["path"])
                record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])

        self.assertEqual(record["source"], "pc-toolkit")
        self.assertEqual(record["event"], "api")
        self.assertEqual(record["request_id"], "pc-http-test")
        self.assertEqual(record["operation_id"], "pc-connect-test")
        self.assertEqual(record["status_class"], "4xx")
        self.assertEqual(record["url_query_keys"], ["code", "source"])
        self.assertGreater(record["request_body_bytes"], 0)
        self.assertGreater(record["response_body_bytes"], 0)
        self.assertEqual(len(record["request_body_sha256"]), 64)
        self.assertEqual(len(record["response_body_sha256"]), 64)
        self.assertNotIn("private-token", json.dumps(record))
        self.assertNotIn("private-cookie", json.dumps(record))


if __name__ == "__main__":
    unittest.main()
