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
from auto_eudm.web_models import RequestSpec, WorkbookImport
from auto_eudm.web_runtime import (
    Application,
    ImportJob,
    JobEntry,
    JobStore,
    MAX_IMPORT_JOBS,
    MAX_LIVE_SUBMISSION_JOBS,
    MAX_PENDING_IMPORTS,
    SubmissionJob,
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

        decode_upload.assert_called_once_with("tracking.xlsx", "encoded workbook")
        inspect_payload.assert_called_once_with(
            "tracking.xlsx", b"decoded workbook"
        )
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

        _, payload, _ = thread.call_args.kwargs["args"]
        self.assertEqual(payload, b"decoded workbook")
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


if __name__ == "__main__":
    unittest.main()
