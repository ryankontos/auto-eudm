from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from auto_eudm.pc_toolkit import (
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
            lookup.assert_called_once_with("ABC123")
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
