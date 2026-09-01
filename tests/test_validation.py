from __future__ import annotations

import io
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

from auto_eudm import eudm_request as eudm
from auto_eudm import eudm_web
from auto_eudm.identifiers import is_login_id, is_serial


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class IdentifierTests(unittest.TestCase):
    def test_serial_rules_are_shared_across_valid_and_invalid_shapes(self) -> None:
        for value in ("ABC123", "host.name", "device_123", "serial-123"):
            with self.subTest(value=value):
                self.assertTrue(is_serial(value))
        for value in (None, "", "short", "bad serial", "serial@example"):
            with self.subTest(value=value):
                self.assertFalse(is_serial(value))

    def test_login_id_rejects_names_emails_and_non_string_values(self) -> None:
        for value in ("valid.user", "User_123", "login-id"):
            with self.subTest(value=value):
                self.assertTrue(is_login_id(value))
        for value in (None, "", "Jane Doe", "person@example.com", "1invalid"):
            with self.subTest(value=value):
                self.assertFalse(is_login_id(value))


class RequestValidationTests(unittest.TestCase):
    def test_direct_deployment_rejects_bad_identifiers_before_api_work(self) -> None:
        client = mock.Mock()

        with self.assertRaisesRegex(eudm.EUDMError, "deployed-to user"):
            eudm.deploy_device_to_user(
                client,
                serial="SERIAL123",
                request_for="request.user",
                deployed_to="person@example.com",
                status="Deployed - New Stock",
            )

        client.request.assert_not_called()


class WebEntrypointTests(unittest.TestCase):
    def run_script(
        self, script: str, *arguments: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(REPOSITORY_ROOT / script), *arguments],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def assert_friendly_error(
        self, completed: subprocess.CompletedProcess[str]
    ) -> None:
        output = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 2, output)
        self.assertIn("Error:", output)
        self.assertNotIn("Traceback", output)

    def test_web_wrapper_validates_arguments_before_runtime_setup(self) -> None:
        completed = self.run_script("eudm_web.py", "--port", "1", "--no-open")

        self.assert_friendly_error(completed)
        self.assertIn("--port", completed.stdout)

    @mock.patch.object(eudm_web, "ensure_runtime")
    def test_web_help_does_not_perform_dependency_setup(
        self, ensure_runtime: mock.Mock
    ) -> None:
        with mock.patch.object(
            sys, "argv", ["eudm_web.py", "--help"]
        ), mock.patch.object(sys, "stdout", new=io.StringIO()):
            with self.assertRaisesRegex(SystemExit, "0"):
                eudm_web.main()

        ensure_runtime.assert_not_called()


if __name__ == "__main__":
    unittest.main()
