from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from auto_eudm import run_reporting
from auto_eudm.pc_toolkit import (
    DEFAULT_PC_TOOLKIT_ROLE,
    PCToolkitClient,
    PCToolkitService,
    normalise_lookup,
)


def device(
    serial: str,
    *,
    status: str,
    model: str = "",
    login: str = "",
    sccm: bool = True,
) -> dict[str, object]:
    return {
        "name": serial,
        "cmdb": {
            "ciDetails": {
                "ciName": serial,
                "serialNumber": serial,
                "status": status,
                "productName": "Laptop",
                "additionalInformation": "CMDB model",
                "site": "Sydney",
                "siteGroup": "1 Elizabeth Street",
            },
            "people": ([{
                "loginID": login,
                "fullName": "Example User",
                "role": "Used by",
                "type": "People",
            }] if login else []),
            "legalHold": "NotFlagged",
        },
        "sccm": ({
            "serial": serial,
            "name": serial,
            "model": model,
            "manufacturer": "Apple",
            "primaryUsers": [],
            "profileUsers": [],
        } if sccm else None),
    }


class PCToolkitNormalisationTests(unittest.TestCase):
    def test_active_sccm_record_wins_over_stale_delete_record(self) -> None:
        result = normalise_lookup(
            {"devices": [
                device("ABC123", status="Delete", sccm=False),
                device("ABC123", status="In Inventory", model="MacBook Pro (14-inch, 2023)"),
            ]},
            "ABC123",
        )

        self.assertEqual(result["record_count"], 2)
        self.assertEqual(result["primary"]["status"], "In Inventory")
        self.assertEqual(result["primary"]["model"], "MacBook Pro (14-inch, 2023)")
        self.assertFalse(result["ambiguous"])

    def test_conflicting_active_records_are_not_silently_collapsed(self) -> None:
        result = normalise_lookup(
            {"devices": [
                device("ABC123", status="Deployed", login="first.user"),
                device("ABC123", status="In Inventory"),
            ]},
            "ABC123",
        )

        self.assertTrue(result["ambiguous"])
        self.assertEqual(result["active_count"], 2)
        self.assertIn("multiple", result["warning"].lower())

    def test_username_lookup_is_identified_from_people_records(self) -> None:
        result = normalise_lookup(
            {"devices": [device("ABC123", status="Deployed", login="example.user")]},
            "example.user",
        )

        self.assertEqual(result["lookup_kind"], "username")
        self.assertEqual(result["primary"]["assigned_user"]["login"], "example.user")


class PCToolkitCacheTests(unittest.TestCase):
    def test_client_uses_portal_role_and_persists_full_safe_api_trace(self) -> None:
        payload = {"searchTerm": "ABC123", "devices": [
            device("ABC123", status="In Inventory", model="MacBook Pro (14-inch, 2023)")
        ]}

        class Response:
            status = 200
            headers = {
                "Content-Type": "application/json",
                "Set-Cookie": "private-cookie",
            }

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self) -> bytes:
                return json.dumps(payload).encode("utf-8")

        with tempfile.TemporaryDirectory() as folder:
            with mock.patch.object(run_reporting, "_PC_TOOLKIT_LOG_DIR", Path(folder)):
                run_reporting.configure_logging(enabled=False, command="test")
                with mock.patch("auto_eudm.pc_toolkit.urllib.request.urlopen", return_value=Response()) as urlopen:
                    result = PCToolkitClient(base_url="https://example.test/Computers").lookup("ABC123")

                request = urlopen.call_args.args[0]
                self.assertEqual(request.get_header("X-max-elevated-role"), DEFAULT_PC_TOOLKIT_ROLE)
                self.assertEqual(result["primary"]["model"], "MacBook Pro (14-inch, 2023)")
                log_status = run_reporting.pc_toolkit_log_status()
                log_text = Path(log_status["path"]).read_text(encoding="utf-8")

            self.assertTrue(log_status["available"])
            self.assertIn("MacBook Pro (14-inch, 2023)", log_text)
            self.assertIn("example.test/Computers/ABC123", log_text)
            self.assertIn('"event":"api_request_started"', log_text)
            self.assertIn('"event":"response_normalised"', log_text)
            self.assertIn('"request_body_present":false', log_text)
            self.assertIn('"response_body_sha256"', log_text)
            self.assertIn('"status_class":"2xx"', log_text)
            self.assertNotIn("private-cookie", log_text)

        run_reporting.configure_logging(enabled=False, command="test")

    def test_file_cache_is_used_and_stale_results_refresh_in_background(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            cache_path = Path(folder) / "pc-toolkit-cache.json"
            service = PCToolkitService(
                cache_path,
                preferences=lambda: {"pc_toolkit_enabled": True},
            )
            fetched = normalise_lookup(
                {"devices": [device("ABC123", status="In Inventory", model="Model 1")]},
                "ABC123",
            )
            with mock.patch.object(PCToolkitClient, "lookup", return_value=fetched) as lookup:
                first = service.lookup("ABC123")
                second = service.lookup(" abc123 ")

            self.assertEqual(first["primary"]["model"], "Model 1")
            self.assertTrue(second["cached"])
            lookup.assert_called_once()
            self.assertEqual(lookup.call_args.args, ("ABC123",))
            self.assertEqual(lookup.call_args.kwargs["purpose"], "lookup")
            self.assertIn("operation_id", lookup.call_args.kwargs)
            time.sleep(0.6)
            saved = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertIn("abc123", saved["entries"])
            self.assertEqual(saved["models"], ["Model 1"])

            restored = PCToolkitService(
                cache_path,
                preferences=lambda: {"pc_toolkit_enabled": True},
            )
            self.assertEqual(restored.status()["models"], ["Model 1"])
            restored.clear_models()
            self.assertEqual(restored.status()["models"], [])
            self.assertIn("abc123", json.loads(cache_path.read_text(encoding="utf-8"))["entries"])

    def test_bulk_lookup_deduplicates_case_and_whitespace(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service = PCToolkitService(
                Path(folder) / "cache.json",
                simulate=True,
                preferences=lambda: {"pc_toolkit_enabled": True},
            )
            result = service.bulk_lookup(["ABC123", " abc123 ", "example.user"])

        self.assertEqual(set(result["results"]), {"abc123", "example.user"})
        self.assertFalse(result["errors"])

    def test_existing_cached_models_are_migrated_into_the_catalogue(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            cache_path = Path(folder) / "cache.json"
            cache_path.write_text(json.dumps({"entries": {
                "abc123": {"fetched_at": time.time(), "result": {
                    "devices": [{"model": " Latitude 7440 "}, {"model": "latitude 7440"}],
                }},
            }}), encoding="utf-8")

            service = PCToolkitService(cache_path)

        self.assertEqual(service.status()["models"], ["Latitude 7440"])


if __name__ == "__main__":
    unittest.main()
