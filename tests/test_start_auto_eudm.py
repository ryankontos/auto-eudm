from __future__ import annotations

import unittest
from unittest import mock

import start_auto_eudm as launcher


class WebTargetTests(unittest.TestCase):
    def test_no_open_still_resolves_the_server_target(self) -> None:
        self.assertEqual(
            launcher.web_target(["--no-open", "--port", "8877"]),
            ("127.0.0.1", 8877),
        )

    def test_help_does_not_probe_or_start_a_server(self) -> None:
        self.assertIsNone(launcher.web_target(["--help"]))


class ExistingServerTests(unittest.TestCase):
    @mock.patch.object(launcher.webbrowser, "open")
    @mock.patch.object(launcher, "request_json", return_value={"commit_id": "abc", "pid": 42})
    @mock.patch.object(launcher, "current_commit_id", return_value="abc")
    @mock.patch.object(launcher, "web_ui_is_running", return_value=True)
    def test_matching_commit_is_reused_without_opening_for_no_open(
        self,
        _running: mock.Mock,
        _commit: mock.Mock,
        _request: mock.Mock,
        open_browser: mock.Mock,
    ) -> None:
        self.assertTrue(
            launcher.open_existing_web_ui(["--no-open", "--port", "8877"])
        )
        open_browser.assert_not_called()

    @mock.patch.object(launcher, "wait_for_web_server_stop", return_value=True)
    @mock.patch.object(launcher, "stop_server_process")
    @mock.patch.object(launcher, "request_json")
    @mock.patch.object(launcher, "current_commit_id", return_value="new")
    @mock.patch.object(launcher, "web_ui_is_running", return_value=True)
    def test_mismatched_commit_requests_graceful_shutdown(
        self,
        _running: mock.Mock,
        _commit: mock.Mock,
        request_json: mock.Mock,
        stop_process: mock.Mock,
        _wait: mock.Mock,
    ) -> None:
        request_json.side_effect = [
            {"commit_id": "old", "pid": 42},
            {"stopping": True},
        ]

        self.assertFalse(
            launcher.open_existing_web_ui(["--no-open", "--port", "8877"])
        )
        stop_process.assert_not_called()

    @mock.patch.object(launcher, "wait_for_web_server_stop", return_value=True)
    @mock.patch.object(launcher, "stop_server_process", return_value=True)
    @mock.patch.object(launcher, "request_json", side_effect=[None, None])
    @mock.patch.object(launcher, "current_commit_id", return_value="new")
    @mock.patch.object(launcher, "web_ui_is_running", return_value=True)
    def test_server_without_runtime_endpoint_uses_process_fallback(
        self,
        _running: mock.Mock,
        _commit: mock.Mock,
        _request: mock.Mock,
        stop_process: mock.Mock,
        _wait: mock.Mock,
    ) -> None:
        self.assertFalse(
            launcher.open_existing_web_ui(["--no-open", "--port", "8877"])
        )
        stop_process.assert_called_once_with(8877, None)

    @mock.patch.object(launcher, "stop_server_process", return_value=False)
    @mock.patch.object(launcher, "request_json", side_effect=[None, None])
    @mock.patch.object(launcher, "current_commit_id", return_value="new")
    @mock.patch.object(launcher, "web_ui_is_running", return_value=True)
    def test_failed_fallback_reports_an_actionable_error(
        self,
        _running: mock.Mock,
        _commit: mock.Mock,
        _request: mock.Mock,
        _stop: mock.Mock,
    ) -> None:
        with self.assertRaisesRegex(ValueError, "Close its launcher window"):
            launcher.open_existing_web_ui(["--no-open", "--port", "8877"])


class ProcessFallbackTests(unittest.TestCase):
    @mock.patch.object(launcher.subprocess, "run", side_effect=FileNotFoundError)
    def test_missing_lsof_does_not_crash_the_launcher(self, _run: mock.Mock) -> None:
        with mock.patch.object(launcher.os, "name", "posix"):
            self.assertFalse(launcher.stop_server_process(8877))


if __name__ == "__main__":
    unittest.main()
