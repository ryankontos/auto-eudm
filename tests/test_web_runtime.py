from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from auto_eudm import eudm_request as eudm
from auto_eudm import eudm_inventory_import as inventory
from auto_eudm import run_reporting
from auto_eudm import web_runtime as eudm_runtime
from auto_eudm.web_models import RequestSpec, WorkbookImport
from auto_eudm.web_runtime import (
    Application,
    SubmissionConflict,
    ClientManager,
    ImportJob,
    JobEntry,
    JobStore,
    MAX_IMPORT_JOBS,
    MAX_LIVE_SUBMISSION_JOBS,
    MAX_PENDING_IMPORTS,
    SEARCH_PROBE_POOL_SIZE,
    SubmissionJob,
    merge_request_queues,
)


def bare_application() -> Application:
    app = Application.__new__(Application)
    app.import_lock = threading.Lock()
    app.import_jobs = {}
    app.pending_imports = {}
    app.import_payload_path = Path("imports")
    app.import_payload_lock = threading.Lock()
    app._persist_import_payload = mock.Mock()
    return app


def submission_job(job_id: str, state: str = "queued") -> SubmissionJob:
    return SubmissionJob(
        job_id=job_id,
        entries=[],
        request_for="simulated.user",
        concurrency=1,
        simulation=True,
        state=state,
    )


def bare_job_store(history_path: Path) -> JobStore:
    store = JobStore.__new__(JobStore)
    store.jobs = {}
    store.lock = threading.Lock()
    store.history_path = history_path
    store.persisted_history = []
    return store


def valid_request(client_id: str) -> RequestSpec:
    return RequestSpec.from_json(
        {
            "id": client_id,
            "kind": "user",
            "serials": [f"SERIAL{client_id}"],
            "status": "Deployed - New Stock",
            "user": "valid.user",
            "group": "Deployments",
        }
    )


class ReturnQuestionSubmissionTests(unittest.TestCase):
    def test_yes_no_answers_use_live_questionnaire_values(self) -> None:
        class ChangedChoiceClient(eudm.SimulationClient):
            def __init__(self) -> None:
                super().__init__()
                self.answers: list[dict[str, object]] = []

            def questionnaire(self) -> dict[str, object]:
                questionnaire = super().questionnaire()
                replacements = {
                    "is-return": ("return-yes-v2", "return-no-v2"),
                    "return-confirmed": ("confirm-yes-v2", "confirm-no-v2"),
                }
                for item in questionnaire["pages"][0]["pageItems"]:
                    if item["id"] in replacements:
                        yes_value, no_value = replacements[item["id"]]
                        item["options"] = [
                            {"dataValue": yes_value, "displayValue": "Yes"},
                            {"dataValue": no_value, "displayValue": "No"},
                        ]
                return questionnaire

            def request(self, method: str, path: str, payload: object | None = None) -> object:
                if method == "POST" and path.endswith("/questionnaire/answers"):
                    self.answers.append(payload if isinstance(payload, dict) else {})
                return super().request(method, path, payload)

        client = ChangedChoiceClient()
        with mock.patch.object(eudm.SimulationClient, "SIMULATED_LOOKUP_DELAY_SECONDS", 0):
            eudm.deploy_device_to_location(
                client,
                serials=["SERIAL123"],
                request_for="requester.user",
                status="Pending Rebuild",
                city="Sydney, AU",
                building="1 Elizabeth Street",
                floor="Level 15",
                room="Store Room",
                returning_user="returning.user",
                submit=False,
            )

        values = {
            str(answer.get("questionId")): answer.get("answers")
            for answer in client.answers
        }
        self.assertEqual(values["is-return"], ["return-yes-v2"])
        self.assertEqual(values["return-confirmed"], ["confirm-yes-v2"])

    def test_location_submission_answers_eudm_return_question(self) -> None:
        class RecordingSimulationClient(eudm.SimulationClient):
            def __init__(self) -> None:
                super().__init__()
                self.answers: list[dict[str, object]] = []

            def request(self, method: str, path: str, payload: object | None = None) -> object:
                if method == "POST" and path.endswith("/questionnaire/answers"):
                    self.answers.append(payload if isinstance(payload, dict) else {})
                return super().request(method, path, payload)

        client = RecordingSimulationClient()
        with mock.patch.object(eudm.SimulationClient, "SIMULATED_LOOKUP_DELAY_SECONDS", 0):
            result = eudm.deploy_device_to_location(
                client,
                serials=["SERIAL123"],
                request_for="requester.user",
                status="Pending Rebuild",
                city="Sydney, AU",
                building="1 Elizabeth Street",
                floor="Level 15",
                room="Store Room",
                returning_user="returning.user",
                submit=False,
            )

        values = {
            str(answer.get("questionId")): answer.get("answers")
            for answer in client.answers
        }
        self.assertEqual(values["is-return"], ["YES"])
        self.assertEqual(values["add-dropoff"], ["true"])
        self.assertEqual(values["return-confirmed"], ["YES"])
        self.assertEqual(result.resolved_username, "returning.user")

    def test_bulk_location_submission_skips_return_question(self) -> None:
        class RecordingSimulationClient(eudm.SimulationClient):
            def __init__(self) -> None:
                super().__init__()
                self.answer_ids: list[str] = []

            def request(self, method: str, path: str, payload: object | None = None) -> object:
                if method == "POST" and path.endswith("/questionnaire/answers") and isinstance(payload, dict):
                    self.answer_ids.append(str(payload.get("questionId", "")))
                return super().request(method, path, payload)

        client = RecordingSimulationClient()
        with mock.patch.object(eudm.SimulationClient, "SIMULATED_LOOKUP_DELAY_SECONDS", 0):
            eudm.deploy_device_to_location(
                client,
                serials=["SERIAL123", "SERIAL456"],
                request_for="requester.user",
                status="Pending Rebuild",
                city="Sydney, AU",
                building="1 Elizabeth Street",
                floor="Level 15",
                room="Store Room",
                bulk=True,
                submit=False,
            )

        self.assertNotIn("is-return", client.answer_ids)

    def test_location_submission_without_return_answers_no(self) -> None:
        class RecordingSimulationClient(eudm.SimulationClient):
            def __init__(self) -> None:
                super().__init__()
                self.answers: list[dict[str, object]] = []

            def request(self, method: str, path: str, payload: object | None = None) -> object:
                if method == "POST" and path.endswith("/questionnaire/answers"):
                    self.answers.append(payload if isinstance(payload, dict) else {})
                return super().request(method, path, payload)

        client = RecordingSimulationClient()
        with mock.patch.object(eudm.SimulationClient, "SIMULATED_LOOKUP_DELAY_SECONDS", 0):
            eudm.deploy_device_to_location(
                client,
                serials=["SERIAL123"],
                request_for="requester.user",
                status="Pending Rebuild",
                city="Sydney, AU",
                building="1 Elizabeth Street",
                floor="Level 15",
                room="Store Room",
                submit=False,
            )

        values = {
            str(answer.get("questionId")): answer.get("answers")
            for answer in client.answers
        }
        self.assertEqual(values["is-return"], ["NO"])
        self.assertNotIn("add-dropoff", values)

    def test_location_stock_submission_skips_hidden_return_question(self) -> None:
        class RecordingSimulationClient(eudm.SimulationClient):
            def __init__(self) -> None:
                super().__init__()
                self.answer_ids: list[str] = []

            def request(self, method: str, path: str, payload: object | None = None) -> object:
                if method == "POST" and path.endswith("/questionnaire/answers") and isinstance(payload, dict):
                    self.answer_ids.append(str(payload.get("questionId", "")))
                return super().request(method, path, payload)

        client = RecordingSimulationClient()
        with mock.patch.object(eudm.SimulationClient, "SIMULATED_LOOKUP_DELAY_SECONDS", 0):
            eudm.deploy_device_to_location(
                client,
                serials=["SERIAL123"],
                request_for="requester.user",
                status="New Stock",
                city="Sydney, AU",
                building="1 Elizabeth Street",
                floor="Level 15",
                room="Store Room",
                submit=False,
            )

        self.assertNotIn("is-return", client.answer_ids)
        self.assertNotIn("add-dropoff", client.answer_ids)


class HelixApiVerificationTests(unittest.TestCase):
    def test_verification_reads_catalogue_and_authenticated_carts(self) -> None:
        client = mock.Mock()
        client.request.side_effect = [
            {"id": "25301", "available": True},
            [{"user": {"userId": "signed.in.user"}}],
        ]

        session = eudm_runtime.verify_helix_api(client)

        self.assertEqual(session.request_for, "signed.in.user")
        self.assertEqual(session.authenticated_user, "signed.in.user")
        self.assertEqual(
            client.request.call_args_list,
            [
                mock.call("GET", "v2/sbe/services/25301"),
                mock.call("GET", "v2/carts"),
            ],
        )

    def test_verification_rejects_a_non_catalogue_response(self) -> None:
        client = mock.Mock()
        client.request.return_value = {"session": 0}

        with self.assertRaises(eudm.EUDMError):
            eudm_runtime.verify_helix_api(client)

    def test_configured_requester_is_used_when_carts_have_no_user(self) -> None:
        client = mock.Mock()
        client.request.side_effect = [
            {"id": "25301", "available": True},
            [],
        ]

        session = eudm_runtime.verify_helix_api(client, "configured.user")

        self.assertEqual(session.request_for, "configured.user")
        self.assertIsNone(session.authenticated_user)

    def test_connection_proves_browser_and_transferred_api_clients(self) -> None:
        def api_response(method: str, path: str, payload=None):
            if path == "/dwp/restapi/users/sessions":
                self.assertEqual(method, "POST")
                self.assertEqual(payload["appName"], "dwp")
                return {"loginId": "signed.in.user"}
            self.assertEqual(method, "GET")
            if path == "v2/sbe/services/25301":
                return {"id": "25301", "available": True}
            if path == "v2/carts":
                return [{"user": {"userId": "signed.in.user"}}]
            self.fail(f"Unexpected Helix path: {path}")

        transferred = mock.Mock()
        transferred.request.side_effect = api_response
        browser = mock.Mock()
        browser.request.side_effect = api_response
        browser.parallel_clients.return_value = [transferred]
        config = SimpleNamespace(
            simulate=False,
            verbose=False,
            request_for=None,
            base="https://macquarie-dwp.onbmc.com/dwp/rest",
            browser_profile="~/.auto-eudm-chrome",
            browser_headless=False,
        )
        manager = ClientManager(config)
        with mock.patch.object(eudm, "open_client", return_value=browser):
            manager._connect()

        self.assertEqual(manager.state, "connected")
        self.assertIs(manager.client, transferred)
        self.assertIsNone(manager.probe)
        self.assertEqual(manager.request_for, "signed.in.user")
        self.assertEqual(
            [call.args[1] for call in browser.request.call_args_list],
            ["v2/sbe/services/25301", "v2/carts"],
        )
        self.assertEqual(
            [call.args[1] for call in transferred.request.call_args_list],
            ["v2/sbe/services/25301", "v2/carts"],
        )


class RequestStatusPreferenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = Application.__new__(Application)
        self.app.config = SimpleNamespace(concurrency=4)

    def test_request_statuses_keep_the_saved_order_and_visibility(self) -> None:
        preferences = self.app._normalise_preferences(
            {
                "request_statuses": [
                    "Pending Rebuild",
                    "Deployed - New Stock",
                    "New Stock",
                ]
            }
        )

        self.assertEqual(
            preferences["request_statuses"],
            ["Pending Rebuild", "Deployed - New Stock", "New Stock"],
        )

    def test_legacy_grouped_statuses_are_upgraded(self) -> None:
        preferences = self.app._normalise_preferences(
            {
                "request_statuses": {
                    "user": ["Deployed - New Stock"],
                    "location": ["Pending Rebuild"],
                }
            }
        )

        self.assertEqual(
            preferences["request_statuses"],
            ["Deployed - New Stock", "Pending Rebuild"],
        )

    def test_statuses_must_keep_each_destination_available(self) -> None:
        with self.assertRaises(eudm.EUDMError):
            self.app._normalise_preferences(
                {"request_statuses": ["Deployed - New Stock"]}
            )

    def test_pc_toolkit_model_mappings_are_normalised_and_optional_per_destination(self) -> None:
        preferences = self.app._normalise_preferences({
            "pc_toolkit_enabled": True,
            "pc_toolkit_model_mappings": [{
                "model": "  MacBook   Pro 14  ",
                "user_status": "Deployed - New Stock",
                "location_status": "",
            }],
        })

        self.assertTrue(preferences["pc_toolkit_enabled"])
        self.assertEqual(preferences["pc_toolkit_model_mappings"], [{
            "model": "MacBook Pro 14",
            "user_status": "Deployed - New Stock",
            "location_status": "",
        }])

    def test_returned_serials_on_hand_visibility_is_enabled_by_default_and_toggleable(self) -> None:
        self.assertTrue(self.app._normalise_preferences({})["show_returned_serials_on_hand"])
        self.assertFalse(
            self.app._normalise_preferences({"show_returned_serials_on_hand": False})[
                "show_returned_serials_on_hand"
            ]
        )

    def test_pc_toolkit_browser_transport_is_the_default_and_api_is_supported(self) -> None:
        defaults = self.app._normalise_preferences({})
        self.assertEqual(defaults["pc_toolkit_transport"], "browser")
        self.assertEqual(
            self.app._normalise_preferences({"pc_toolkit_transport": " API "})[
                "pc_toolkit_transport"
            ],
            "api",
        )
        with self.assertRaises(eudm.EUDMError):
            self.app._normalise_preferences({"pc_toolkit_transport": "python"})

    def test_pc_toolkit_auto_connect_is_opt_in_and_toggleable(self) -> None:
        self.assertFalse(self.app._normalise_preferences({})["pc_toolkit_auto_connect"])
        self.assertTrue(
            self.app._normalise_preferences({"pc_toolkit_auto_connect": True})[
                "pc_toolkit_auto_connect"
            ]
        )
        with self.assertRaises(eudm.EUDMError):
            self.app._normalise_preferences({"pc_toolkit_auto_connect": "yes"})

    def test_pc_toolkit_model_names_are_case_insensitively_unique(self) -> None:
        with self.assertRaises(eudm.EUDMError):
            self.app._normalise_preferences({
                "pc_toolkit_model_mappings": [
                    {"model": "Latitude 7440", "user_status": "Deployed - Existing Stock"},
                    {"model": " latitude 7440 ", "location_status": "Pending Rebuild"},
                ],
            })

    def test_pc_toolkit_discovered_model_can_be_saved_before_statuses_are_chosen(self) -> None:
        preferences = self.app._normalise_preferences({
            "pc_toolkit_model_mappings": [{
                "model": "  MacBook   Air  ",
                "user_status": "",
                "location_status": "",
            }],
        })

        self.assertEqual(preferences["pc_toolkit_model_mappings"], [{
            "model": "MacBook Air",
            "user_status": "",
            "location_status": "",
        }])


class SearchProbePoolTests(unittest.TestCase):
    def test_fresh_search_reuses_a_bounded_pool(self) -> None:
        manager = ClientManager.__new__(ClientManager)
        manager.lock = threading.Lock()
        manager.state = "connected"
        manager.client = mock.Mock()
        manager.client.parallel_clients.side_effect = lambda _count: [mock.Mock()]
        manager.request_for = "valid.user"
        manager.fresh_probes = []
        manager.fresh_probe_cursor = 0

        probes = [
            manager.fresh_search()
            for _ in range(SEARCH_PROBE_POOL_SIZE + 2)
        ]

        self.assertEqual(
            manager.client.parallel_clients.call_count,
            SEARCH_PROBE_POOL_SIZE,
        )
        self.assertIs(probes[0], probes[SEARCH_PROBE_POOL_SIZE])
        self.assertIs(probes[1], probes[SEARCH_PROBE_POOL_SIZE + 1])


class ImportJobRetentionTests(unittest.TestCase):
    def test_completed_jobs_are_pruned_from_both_import_stages(self) -> None:
        app = bare_application()
        for index in range(MAX_IMPORT_JOBS):
            app._register_import_job(
                ImportJob(
                    job_id=f"old-{index}",
                    filename="tracking.xlsx",
                    state="ready",
                )
            )

        app._register_import_job(
            ImportJob(job_id="new", filename="tracking.xlsx")
        )

        self.assertEqual(len(app.import_jobs), MAX_IMPORT_JOBS)
        self.assertNotIn("old-0", app.import_jobs)
        self.assertIn("new", app.import_jobs)

    def test_active_jobs_are_never_evicted_to_meet_the_history_limit(self) -> None:
        app = bare_application()
        for index in range(MAX_IMPORT_JOBS + 1):
            app._register_import_job(
                ImportJob(job_id=f"active-{index}", filename="tracking.xlsx")
            )

        self.assertEqual(len(app.import_jobs), MAX_IMPORT_JOBS + 1)


class ImportPayloadLifecycleTests(unittest.TestCase):
    def test_persisted_workbook_can_be_restored_after_runtime_restart(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            import_id = "0123456789abcdef0123456789abcdef"
            app = bare_application()
            app.import_payload_path = Path(folder)
            app.import_payload_lock = threading.Lock()
            app.imports = {}
            columns = inventory.ImportColumns(
                username="User",
                deployment_serial="Device",
                pending_return="Old Device",
            )
            Application._persist_import_payload(
                app, import_id, "tracking.xlsx", b"workbook", columns
            )

            restored_workbook = WorkbookImport(
                "fresh-id", "tracking.xlsx", {"Sheet": []}
            )
            restarted = bare_application()
            restarted.import_payload_path = Path(folder)
            restarted.import_payload_lock = threading.Lock()
            restarted.imports = {}
            with mock.patch.object(
                WorkbookImport,
                "from_payload",
                return_value=restored_workbook,
            ) as from_payload:
                restored = restarted.get_import(import_id)

            self.assertIs(restored, restored_workbook)
            self.assertEqual(restored.import_id, import_id)
            from_payload.assert_called_once()
            self.assertEqual(from_payload.call_args.kwargs["columns"], columns)

    @mock.patch.object(
        WorkbookImport,
        "inspect_payload",
        return_value={"filename": "tracking.xlsx", "sheets": []},
    )
    @mock.patch.object(
        WorkbookImport,
        "decode_upload",
        return_value=b"decoded workbook",
    )
    def test_inspection_decodes_once_and_retains_bytes(
        self,
        decode_upload: mock.Mock,
        inspect_payload: mock.Mock,
    ) -> None:
        app = bare_application()
        job = ImportJob(job_id="inspect", filename="tracking.xlsx")

        app._inspect_import(job, "encoded workbook")

        decode_upload.assert_called_once()
        self.assertEqual(decode_upload.call_args.args, ("tracking.xlsx", "encoded workbook"))
        self.assertIn("debug", decode_upload.call_args.kwargs)
        inspect_payload.assert_called_once()
        self.assertEqual(inspect_payload.call_args.args, ("tracking.xlsx", b"decoded workbook"))
        self.assertIn("debug", inspect_payload.call_args.kwargs)
        self.assertEqual(job.state, "ready")
        self.assertEqual(len(app.pending_imports), 1)
        _, retained = next(iter(app.pending_imports.values()))
        self.assertEqual(retained, b"decoded workbook")

    @mock.patch.object(
        WorkbookImport,
        "inspect_payload",
        return_value={"filename": "tracking.xlsx", "sheets": []},
    )
    @mock.patch.object(WorkbookImport, "decode_upload")
    def test_pending_payload_cache_is_bounded(
        self,
        decode_upload: mock.Mock,
        _inspect_payload: mock.Mock,
    ) -> None:
        app = bare_application()
        decode_upload.side_effect = [
            f"payload-{index}".encode()
            for index in range(MAX_PENDING_IMPORTS + 1)
        ]

        for index in range(MAX_PENDING_IMPORTS + 1):
            app._inspect_import(
                ImportJob(job_id=f"inspect-{index}", filename="tracking.xlsx"),
                f"encoded-{index}",
            )

        self.assertEqual(len(app.pending_imports), MAX_PENDING_IMPORTS)
        retained_payloads = {
            payload for _, payload in app.pending_imports.values()
        }
        self.assertNotIn(b"payload-0", retained_payloads)

    @mock.patch("auto_eudm.web_runtime.threading.Thread")
    def test_mapping_passes_retained_bytes_to_the_reader(
        self, thread: mock.Mock
    ) -> None:
        app = bare_application()
        app.pending_imports["pending"] = (
            "tracking.xlsx",
            b"decoded workbook",
        )

        app.start_mapped_import("pending", {})

        _, payload, _, debug = thread.call_args.kwargs["args"]
        self.assertEqual(payload, b"decoded workbook")
        self.assertTrue(debug.path.exists())
        thread.return_value.start.assert_called_once_with()
        self.assertNotIn("pending", app.pending_imports)


class SubmissionJobStateTests(unittest.TestCase):
    def test_entry_timestamps_and_job_completion_use_snapshot_lock(self) -> None:
        job = submission_job("job")
        entry = JobEntry(spec=mock.Mock())

        job.update(entry, state="running", started_at=10.0)
        job.update(entry, state="succeeded", finished_at=12.5)
        job.set_state("finished", finished_at="2026-08-11T12:00:00")

        self.assertEqual(entry.state, "succeeded")
        self.assertEqual(entry.started_at, 10.0)
        self.assertEqual(entry.finished_at, 12.5)
        self.assertEqual(job.state, "finished")
        self.assertEqual(job.finished_at, "2026-08-11T12:00:00")

    @mock.patch(
        "auto_eudm.web_runtime.populate_spec",
        return_value=eudm.DeploymentResult("REQ-1", "ORDER-1", submitted=True),
    )
    def test_parallel_run_finishes_with_consistent_snapshots(
        self, _populate: mock.Mock
    ) -> None:
        store = bare_job_store(Path("history.json"))
        store.clients = SimpleNamespace(
            clients=lambda count: [object() for _ in range(count)]
        )
        job = SubmissionJob(
            job_id="job",
            entries=[
                JobEntry(valid_request("1")),
                JobEntry(valid_request("2")),
            ],
            request_for="simulated.user",
            concurrency=2,
            simulation=True,
        )

        with mock.patch.object(store, "_write_results") as write_results:
            store._run(job)

        snapshot = job.to_json()
        self.assertEqual(snapshot["state"], "finished")
        self.assertIsNotNone(snapshot["finished_at"])
        self.assertEqual(snapshot["counts"]["succeeded"], 2)
        self.assertTrue(
            all(entry["elapsed_seconds"] is not None for entry in snapshot["entries"])
        )
        write_results.assert_called_once_with(job)

    def test_client_setup_failure_finishes_every_entry(self) -> None:
        store = bare_job_store(Path("history.json"))

        def unavailable(_count: int) -> list[object]:
            raise eudm.EUDMError("Connect to EUDM")

        store.clients = SimpleNamespace(clients=unavailable)
        job = SubmissionJob(
            job_id="job",
            entries=[JobEntry(valid_request("1")), JobEntry(valid_request("2"))],
            request_for="valid.user",
            concurrency=2,
            simulation=False,
        )

        with mock.patch.object(store, "_write_results") as write_results:
            store._run(job)

        snapshot = job.to_json()
        self.assertEqual(snapshot["state"], "finished")
        self.assertEqual(snapshot["counts"]["failed"], 2)
        self.assertTrue(
            all(entry["message"] == "Connect to EUDM" for entry in snapshot["entries"])
        )
        write_results.assert_called_once_with(job)


class SubmissionJobRetentionTests(unittest.TestCase):
    def test_oldest_completed_live_job_is_pruned(self) -> None:
        store = bare_job_store(Path("history.json"))
        for index in range(MAX_LIVE_SUBMISSION_JOBS):
            store._register_job(submission_job(f"old-{index}", "finished"))

        store._register_job(submission_job("new"))

        self.assertEqual(len(store.jobs), MAX_LIVE_SUBMISSION_JOBS)
        self.assertNotIn("old-0", store.jobs)
        self.assertIn("new", store.jobs)

    def test_active_submission_jobs_are_not_pruned(self) -> None:
        store = bare_job_store(Path("history.json"))
        for index in range(MAX_LIVE_SUBMISSION_JOBS + 1):
            store._register_job(submission_job(f"active-{index}"))

        self.assertEqual(len(store.jobs), MAX_LIVE_SUBMISSION_JOBS + 1)


class SubmissionHistoryTests(unittest.TestCase):
    def test_legacy_history_is_migrated_to_the_canonical_file(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            canonical = root / "request-history.json"
            legacy = root / "web-request-history.json"
            history = [submission_job("legacy", "finished").to_json()]
            legacy.write_text(json.dumps(history), encoding="utf-8")
            store = bare_job_store(canonical)
            store.legacy_history_paths = (legacy,)

            loaded = store._load_history()

            self.assertEqual([item["job_id"] for item in loaded], ["legacy"])
            self.assertEqual(json.loads(canonical.read_text(encoding="utf-8")), history)

    def test_alm_drafts_expire_after_six_hours(self) -> None:
        now = datetime(2026, 8, 20, 12, 0, 0)
        current = {"saved_at": (now - timedelta(hours=5, minutes=59)).isoformat()}
        expired = {"saved_at": (now - timedelta(hours=6, minutes=1)).isoformat()}

        self.assertTrue(Application._draft_is_current(current, now=now))
        self.assertFalse(Application._draft_is_current(expired, now=now))

    def test_verification_cache_is_persisted_and_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            cache_path = Path(folder) / "web-verification-cache.json"
            app = Application.__new__(Application)
            app.verification_cache_path = cache_path
            app.verification_cache_lock = threading.Lock()
            app.verification_cache = {"serials": {}, "usernames": {}}
            app.record_verified_serial({
                "value": " ABC123 ",
                "columns": ["ABC123", "Laptop"],
                "device_type": "Laptop",
            })

            loaded = Application.__new__(Application)
            loaded.verification_cache_path = cache_path
            loaded.verification_cache_lock = threading.Lock()
            loaded.verification_cache = loaded._load_verification_cache()

            self.assertEqual(
                loaded.verification_cache_lookup("serial", "abc123"),
                {
                    "value": "ABC123",
                    "columns": ["ABC123", "Laptop"],
                    "device_type": "Laptop",
                },
            )

    def test_verification_cache_batches_updates_and_resolves_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            cache_path = Path(folder) / "web-verification-cache.json"
            app = Application.__new__(Application)
            app.verification_cache_path = cache_path
            app.verification_cache_lock = threading.Lock()
            app.verification_cache_write_lock = threading.Lock()
            app.verification_cache = {"serials": {}, "usernames": {}}
            app.record_verified_serial({
                "value": "ABC123",
                "columns": ["ABC123", "MacBook Air"],
                "device_type": "Laptop",
            })
            app.record_verified_serial({
                "value": "DEF456",
                "columns": ["DEF456", "ThinkPad"],
                "device_type": "Laptop",
            })

            app.flush_pending_state()
            persisted = json.loads(cache_path.read_text(encoding="utf-8"))

            self.assertEqual(set(persisted["serials"]), {"abc123", "def456"})
            self.assertEqual(
                app.verification_cache_lookup("serial", "MacBook Air")["value"],
                "ABC123",
            )

    def test_unsubmitted_request_queue_is_persisted_and_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            queue_path = Path(folder) / "web-request-queue.json"
            app = bare_application()
            app.request_queue_path = queue_path
            app.request_queue_lock = threading.Lock()
            app.request_queue = []
            requests = [{"id": "request-1", "serials": ["SERIAL123"], "kind": "user"}]

            self.assertEqual(app.save_request_queue(requests), requests)
            self.assertEqual(json.loads(queue_path.read_text(encoding="utf-8")), requests)

            loaded = bare_application()
            loaded.request_queue_path = queue_path
            loaded.request_queue_lock = threading.Lock()
            loaded.request_queue = loaded._load_request_queue()
            self.assertEqual(loaded.request_queue_json(), requests)

    def test_request_queue_merge_preserves_changes_from_both_windows(self) -> None:
        base = [
            {"id": "one", "status": "Pending Rebuild", "user": "alice"},
            {"id": "two", "status": "Used Stock", "user": "bob"},
        ]
        local = [
            {"id": "one", "status": "Pending Rebuild", "user": "alice.smith"},
            {"id": "two", "status": "Used Stock", "user": "bob"},
            {"id": "three", "status": "Pending Decom", "user": ""},
        ]
        remote = [
            {"id": "one", "status": "Pending Decom", "user": "alice"},
            {"id": "two", "status": "Used Stock", "user": "bob"},
            {"id": "four", "status": "Used Stock", "user": ""},
        ]

        merged = merge_request_queues(base, local, remote)

        self.assertEqual(
            {item["id"]: item for item in merged},
            {
                "one": {"id": "one", "status": "Pending Decom", "user": "alice.smith"},
                "two": {"id": "two", "status": "Used Stock", "user": "bob"},
                "three": {"id": "three", "status": "Pending Decom", "user": ""},
                "four": {"id": "four", "status": "Used Stock", "user": ""},
            },
        )

    def test_request_queue_merge_keeps_submission_progress_monotonic(self) -> None:
        base = [{"id": "one", "result_state": "running", "result_message": "Step 2"}]
        local = [{"id": "one", "result_state": "running", "result_message": "Step 2", "request_id": ""}]
        remote = [{"id": "one", "result_state": "succeeded", "result_message": "Submitted", "request_id": "163600"}]

        merged = merge_request_queues(base, local, remote)

        self.assertEqual(merged[0]["result_state"], "succeeded")
        self.assertEqual(merged[0]["result_message"], "Submitted")
        self.assertEqual(merged[0]["request_id"], "163600")

    def test_stale_window_queue_save_merges_against_current_server_state(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            app = bare_application()
            app.request_queue_path = Path(folder) / "queue.json"
            app.request_queue_lock = threading.Lock()
            base = [{"id": "one", "status": "Pending Rebuild", "user": "alice"}]
            app.request_queue = [
                {"id": "one", "status": "Pending Decom", "user": "alice"},
                {"id": "remote", "status": "Used Stock", "user": "bob"},
            ]
            local = [
                {"id": "one", "status": "Pending Rebuild", "user": "alice.smith"},
                {"id": "local", "status": "Used Stock", "user": "carol"},
            ]

            saved = app.save_request_queue(local, base)

        by_id = {request["id"]: request for request in saved}
        self.assertEqual(by_id["one"]["status"], "Pending Decom")
        self.assertEqual(by_id["one"]["user"], "alice.smith")
        self.assertIn("remote", by_id)
        self.assertIn("local", by_id)

    def test_shared_queue_keeps_duplicate_serials_only_once(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            app = bare_application()
            app.request_queue_path = Path(folder) / "queue.json"
            app.request_queue_lock = threading.Lock()
            app.request_queue = [{"id": "remote", "serials": ["SERIAL123"]}]
            incoming = [
                {"id": "duplicate", "serials": ["serial123"]},
                {"id": "unique", "serials": ["UNIQUE456", "unique456"]},
            ]

            saved, duplicates = app.save_request_queue_with_conflicts(incoming, [])

        by_id = {request["id"]: request for request in saved}
        self.assertNotIn("duplicate", by_id)
        self.assertEqual(by_id["remote"]["serials"], ["SERIAL123"])
        self.assertEqual(by_id["unique"]["serials"], ["UNIQUE456"])
        self.assertEqual(set(duplicates), {"serial123", "unique456"})

    def test_job_store_reuses_identical_active_run_and_rejects_overlap(self) -> None:
        store = bare_job_store(Path("history.json"))
        store.clients = SimpleNamespace(config=SimpleNamespace(simulate=True))

        with mock.patch.object(store, "_run") as run_job:
            active = store.create([valid_request("shared")], "requester", 1)
            duplicate = store.create([valid_request("shared")], "requester", 1)

            self.assertIs(duplicate, active)
            self.assertEqual(run_job.call_count, 1)
            self.assertEqual(store.active_jobs()[0]["job_id"], active.job_id)
            active.set_state("finished")
            self.assertEqual(store.active_jobs(), [])
            self.assertIs(
                store.create([valid_request("shared")], "requester", 1), active
            )
            self.assertEqual(run_job.call_count, 1)
            with self.assertRaises(SubmissionConflict):
                store.create(
                    [valid_request("shared"), valid_request("different")],
                    "requester",
                    1,
                )

    def test_job_store_rejects_request_ids_already_in_saved_history(self) -> None:
        store = bare_job_store(Path("history.json"))
        store.clients = SimpleNamespace(config=SimpleNamespace(simulate=True))
        store.persisted_history = [{"job_id": "old", "entries": [{"id": "already-sent"}]}]

        with self.assertRaises(SubmissionConflict):
            store.create([valid_request("already-sent")], "requester", 1)

    def test_parallel_completions_are_all_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            history_path = Path(folder) / "web-request-history.json"
            store = bare_job_store(history_path)
            jobs = [submission_job(f"job-{index}", "finished") for index in range(24)]

            with ThreadPoolExecutor(max_workers=8) as executor:
                list(executor.map(store._persist_history, jobs))

            persisted = json.loads(history_path.read_text(encoding="utf-8"))

        self.assertEqual(len(persisted), len(jobs))
        self.assertEqual(
            {item["job_id"] for item in persisted},
            {job.job_id for job in jobs},
        )

    @mock.patch.object(
        run_reporting,
        "write_result_file",
        side_effect=OSError("disk unavailable"),
    )
    def test_result_file_failure_does_not_skip_history(
        self, _write_result: mock.Mock
    ) -> None:
        store = bare_job_store(Path("history.json"))
        job = submission_job("job", "finished")

        with mock.patch.object(store, "_persist_history") as persist_history:
            store._write_results(job)

        persist_history.assert_called_once_with(job)


class BackgroundAuthenticationTests(unittest.TestCase):
    def test_visible_helix_retry_overrides_headless_default(self) -> None:
        manager = ClientManager(SimpleNamespace(
            simulate=False, request_for=None, browser_headless=True,
            base="https://example.invalid", browser_profile="chrome-profile", verbose=False,
        ))
        with mock.patch.object(eudm, "open_client", side_effect=eudm.EUDMError("offline")) as open_client:
            manager._connect(False)
        self.assertFalse(open_client.call_args.kwargs["headless"])

    def make_app(self) -> Application:
        app = bare_application()
        app.config = SimpleNamespace(simulate=False)
        app.preferences = {"headless_auth_enabled": True}
        app.clients = ClientManager(SimpleNamespace(
            simulate=False, request_for=None, browser_headless=False,
        ))
        app.auth_last_helix_health = 10_000.0
        app.auth_last_pc_health = 10_000.0
        app.auth_last_helix_retry = 0.0
        app.auth_last_pc_retry = 0.0
        app.auth_failure_counts = {"helix": 0, "pc_toolkit": 0}
        app.auth_attempt_active = {"helix": False, "pc_toolkit": False}
        return app

    def test_helix_stops_after_three_headless_failures_and_retries_visibly(self) -> None:
        app = self.make_app()
        app.pc_toolkit = mock.Mock()
        app.pc_toolkit.enabled.return_value = False
        attempts: list[bool] = []

        def fail(*, headless: bool) -> None:
            attempts.append(headless)
            app.clients.state = "error"

        app.clients.connect_async = fail
        for attempt in range(3):
            with mock.patch.object(eudm_runtime.time, "monotonic", return_value=100.0 + attempt * 100):
                app._auth_monitor_tick()
            with mock.patch.object(eudm_runtime.time, "monotonic", return_value=101.0 + attempt * 100):
                app._auth_monitor_tick()
        self.assertEqual(attempts, [True, True, True])
        self.assertTrue(app.clients.status()["background_auth_stopped"])
        with mock.patch.object(eudm_runtime.time, "monotonic", return_value=500.0):
            app._auth_monitor_tick()
        self.assertEqual(len(attempts), 3)

        app.retry_auth_visible("helix")
        self.assertEqual(attempts[-1], False)
        self.assertTrue(app.clients.background_auth_stopped)
        app.clients.state = "connected"
        with mock.patch.object(eudm_runtime.time, "monotonic", return_value=501.0):
            app._auth_monitor_tick()
        self.assertFalse(app.clients.background_auth_stopped)

    def test_pc_toolkit_has_independent_retry_limit(self) -> None:
        app = self.make_app()
        app.clients.state = "connected"
        toolkit = SimpleNamespace(state="error", background_auth_stopped=False)
        toolkit.enabled = lambda: True
        toolkit.status = lambda: {"state": toolkit.state}
        toolkit.check_connection = lambda: toolkit.status()
        attempts: list[bool] = []

        def fail(*, headless: bool) -> None:
            attempts.append(headless)
            toolkit.state = "error"

        toolkit.connect_async = fail
        app.pc_toolkit = toolkit
        for attempt in range(3):
            with mock.patch.object(eudm_runtime.time, "monotonic", return_value=100.0 + attempt * 100):
                app._auth_monitor_tick()
            with mock.patch.object(eudm_runtime.time, "monotonic", return_value=101.0 + attempt * 100):
                app._auth_monitor_tick()
        self.assertEqual(attempts, [True, True, True])
        self.assertTrue(toolkit.background_auth_stopped)
        with mock.patch.object(eudm_runtime.time, "monotonic", return_value=500.0):
            app._auth_monitor_tick()
        self.assertEqual(len(attempts), 3)
        app.retry_auth_visible("pc_toolkit")
        self.assertEqual(attempts[-1], False)


if __name__ == "__main__":
    unittest.main()
