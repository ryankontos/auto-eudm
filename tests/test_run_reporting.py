from __future__ import annotations

import gzip
import json
import unittest

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


if __name__ == "__main__":
    unittest.main()
