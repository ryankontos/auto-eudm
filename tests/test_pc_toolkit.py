from __future__ import annotations

import gzip
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from auto_eudm import run_reporting
from auto_eudm.pc_toolkit import (
    DEFAULT_PC_TOOLKIT_ROLE,
    PCToolkitClient,
    PCToolkitBrowserClient,
    PCToolkitError,
    PCToolkitPuppeteerClient,
    PCToolkitService,
    normalise_lookup,
    xsrf_cookie_value,
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
    def test_xsrf_cookie_accepts_portal_name_and_decodes_value(self) -> None:
        self.assertEqual(
            xsrf_cookie_value([
                {"name": "XSRF_TOKEN", "value": "csrf%2Bvalue"},
            ]),
            "csrf+value",
        )
        self.assertEqual(
            xsrf_cookie_value([
                {"name": "XSRF-TOKEN", "value": "legacy-value"},
            ]),
            "legacy-value",
        )

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
                "Content-Encoding": "gzip",
                "Set-Cookie": "private-cookie",
            }

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self) -> bytes:
                return gzip.compress(json.dumps(payload).encode("utf-8"))

        with tempfile.TemporaryDirectory() as folder:
            with mock.patch.object(run_reporting, "_PC_TOOLKIT_LOG_DIR", Path(folder)):
                run_reporting.configure_logging(enabled=False, command="test")
                with mock.patch("auto_eudm.pc_toolkit.urllib.request.urlopen", return_value=Response()) as urlopen:
                    result = PCToolkitClient(
                        base_url="https://example.test/Computers",
                        access_token="token-for-test",
                    ).lookup("ABC123")

                request = urlopen.call_args.args[0]
                self.assertEqual(request.get_header("X-max-elevated-role"), DEFAULT_PC_TOOLKIT_ROLE)
                self.assertEqual(request.get_header("Authorization"), "Bearer token-for-test")
                self.assertEqual(request.get_header("Sec-fetch-dest"), "empty")
                self.assertEqual(request.get_header("Sec-fetch-mode"), "cors")
                self.assertEqual(request.get_header("Sec-fetch-site"), "same-site")
                self.assertEqual(request.get_header("Sec-ch-ua-platform"), '"macOS"')
                self.assertEqual(request.get_header("Accept-language"), "en-GB,en-US;q=0.9,en;q=0.8")
                self.assertEqual(request.get_header("Connection"), "keep-alive")
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
                preferences=lambda: {"pc_toolkit_enabled": True, "pc_toolkit_transport": "api"},
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
                preferences=lambda: {"pc_toolkit_enabled": True, "pc_toolkit_transport": "api"},
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
                preferences=lambda: {"pc_toolkit_enabled": True, "pc_toolkit_transport": "api"},
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

    def test_connect_authenticates_without_fabricated_device_probe(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service = PCToolkitService(
                Path(folder) / "cache.json",
                preferences=lambda: {"pc_toolkit_enabled": True, "pc_toolkit_transport": "api"},
            )
            service.state = "connecting"
            with (
                mock.patch.object(service, "_discover_role", return_value="maxrole:personal") as discover,
            ):
                service._connect("pc-connect-test")

        self.assertEqual(service.status()["state"], "connected")
        discover.assert_called_once_with("pc-connect-test")

    def test_real_device_api_auth_error_marks_service_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service = PCToolkitService(
                Path(folder) / "cache.json",
                preferences=lambda: {"pc_toolkit_enabled": True, "pc_toolkit_transport": "api"},
            )
            client = mock.Mock()
            client.lookup.side_effect = PCToolkitError(
                "PC Toolkit authentication is required."
            )
            with mock.patch.object(service, "_client", return_value=client):
                with self.assertRaises(PCToolkitError):
                    service._fetch(
                        "ABC123",
                        request_id="pc-request-test",
                        operation_id="pc-operation-test",
                        purpose="lookup",
                    )

        status = service.status()
        self.assertEqual(status["state"], "error")
        self.assertIn("device API rejected", status["message"])
        self.assertEqual(client.lookup.call_count, 2)
        client.lookup.assert_called_with(
            "ABC123",
            request_id="pc-request-test",
            operation_id="pc-operation-test",
            purpose="lookup",
        )

    def test_failed_lookup_falls_back_to_even_an_old_cached_result(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service = PCToolkitService(
                Path(folder) / "cache.json",
                preferences=lambda: {"pc_toolkit_enabled": True, "pc_toolkit_transport": "api"},
            )
            cached_result = normalise_lookup(
                {"devices": [device("ABC123", status="Deployed", model="Known model")]},
                "ABC123",
            )
            service.cache["abc123"] = {
                "fetched_at": time.time() - (31 * 24 * 60 * 60),
                "result": cached_result,
            }
            client = mock.Mock()
            client.lookup.side_effect = PCToolkitError("temporary gateway failure")
            with mock.patch.object(service, "_client", return_value=client):
                result = service.lookup("ABC123")

        self.assertEqual(result["primary"]["model"], "Known model")
        self.assertTrue(result["cached"])
        self.assertTrue(result["stale"])

    def test_browser_bulk_lookups_are_serial_to_avoid_queue_timeouts(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service = PCToolkitService(
                Path(folder) / "cache.json",
                preferences=lambda: {"pc_toolkit_enabled": True, "pc_toolkit_transport": "browser"},
            )
            active = 0
            peak = 0
            active_lock = threading.Lock()

            def lookup(query, **_kwargs):
                nonlocal active, peak
                with active_lock:
                    active += 1
                    peak = max(peak, active)
                time.sleep(0.01)
                with active_lock:
                    active -= 1
                return {"query": query, "found": False, "devices": []}

            with mock.patch.object(service, "lookup", side_effect=lookup):
                result = service.bulk_lookup(["ABC123", "DEF456", "GHI789"])

        self.assertFalse(result["errors"])
        self.assertEqual(peak, 1)

    def test_browser_client_uses_authenticated_page_fetch_shape(self) -> None:
        calls: list[dict[str, object]] = []

        class Transport:
            def get(self, url, headers, *, timeout):
                calls.append({"url": url, "headers": headers, "timeout": timeout})
                return {
                    "status": 200,
                    "url": url,
                    "headers": {"content-type": "application/json"},
                    "body": json.dumps({"devices": [device(
                        "ABC123", status="In Inventory", model="MacBook Air"
                    )]}),
                }

        result = PCToolkitBrowserClient(
            Transport(),
            role="maxrole:personal",
            access_token="test-token",
        ).lookup("ABC123")

        self.assertEqual(result["primary"]["model"], "MacBook Air")
        self.assertEqual(calls[0]["headers"]["Authorization"], "Bearer test-token")
        self.assertEqual(calls[0]["headers"]["X-Max-Elevated-Role"], "maxrole:personal")
        self.assertNotIn("Connection", calls[0]["headers"])
        self.assertNotIn("Sec-Fetch-Mode", calls[0]["headers"])

    def test_browser_transport_connection_keeps_profile_without_fabricated_probe(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service = PCToolkitService(
                Path(folder) / "cache.json",
                browser_profile=str(Path(folder) / "chrome-profile"),
                preferences=lambda: {"pc_toolkit_enabled": True},
            )
            transport = mock.Mock()
            client = mock.Mock()
            with (
                mock.patch.object(service, "_discover_role", return_value="maxrole:personal"),
                mock.patch("auto_eudm.pc_toolkit.PCToolkitBrowserTransport", return_value=transport) as transport_type,
                mock.patch.object(service, "_client", return_value=client),
            ):
                service.state = "connecting"
                service._connect("pc-browser-connect-test")

        self.assertEqual(service.status()["state"], "connected")
        transport_type.assert_called_once_with(
            browser_profile=str(Path(folder) / "chrome-profile"),
            timeout=18.0,
            service_id=service.service_id,
            operation_id="pc-browser-connect-test",
            headless=False,
        )
        transport.start.assert_called_once_with()
        client.lookup.assert_not_called()

    def test_puppeteer_client_keeps_bearer_token_inside_the_bridge(self) -> None:
        calls: list[dict[str, object]] = []

        class Transport:
            def lookup(self, query, *, timeout):
                calls.append({"query": query, "timeout": timeout})
                return {
                    "status": 200,
                    "url": "https://autoscalecomponent.example/v1/Computers/ABC123",
                    "headers": {"content-type": "application/json"},
                    "body": json.dumps({"devices": [device(
                        "ABC123", status="In Inventory", model="MacBook Air"
                    )]}),
                }

        result = PCToolkitPuppeteerClient(
            Transport(),
            role="maxrole:personal",
        ).lookup(" ABC123 ")

        self.assertEqual(result["primary"]["model"], "MacBook Air")
        self.assertEqual(calls, [{"query": "ABC123", "timeout": 18.0}])

    def test_puppeteer_transport_can_be_used_for_connection(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service = PCToolkitService(
                Path(folder) / "cache.json",
                browser_profile=str(Path(folder) / "chrome-profile"),
                preferences=lambda: {
                    "pc_toolkit_enabled": True,
                    "pc_toolkit_transport": "puppeteer",
                },
            )
            transport = mock.Mock()
            transport.role = "maxrole:personal"
            with mock.patch(
                "auto_eudm.pc_toolkit.PCToolkitPuppeteerTransport",
                return_value=transport,
            ) as transport_type:
                service.state = "connecting"
                service._connect("pc-puppeteer-connect-test")

        self.assertEqual(service.status()["state"], "connected")
        transport_type.assert_called_once_with(
            browser_profile=str(Path(folder) / "chrome-profile"),
            timeout=18.0,
            service_id=service.service_id,
            operation_id="pc-puppeteer-connect-test",
            headless=False,
        )
        transport.start.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
