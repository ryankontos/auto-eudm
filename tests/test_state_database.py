from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

from auto_eudm.pc_toolkit import PCToolkitService
from auto_eudm.state_database import AppDatabase, SCHEMA_VERSION, _SCHEMA
from auto_eudm.web_runtime import Application


def preferences() -> dict[str, object]:
    return {
        "concurrency": 60,
        "request_statuses": ["Deployed - Existing Stock", "Pending Rebuild"],
        "import_columns": {
            "username": "Username",
            "deployment_serial": "Serial",
            "returned_device": "Returned",
            "pending_return": "Pending",
            "enabled": "Attended",
            "device_allocation": "Device(s) Allocation",
            "new_asset_status": "New Asset Status",
            "first_name": "First Name",
            "last_name": "Last Name",
        },
        "pc_toolkit_model_mappings": [
            {
                "model": "MacBook Air (M2, 2022)",
                "user_status": "Deployed - New Stock",
                "location_status": "Pending Rebuild",
            }
        ],
        "update_channel": "development",
        "pc_toolkit_transport": "browser",
        "validate_editor_serials": True,
        "validate_editor_users": True,
        "validate_bulk_serials": True,
        "validate_quick_import": True,
        "validate_workbook_import": True,
        "save_alm_import_drafts": True,
        "show_returned_serials_on_hand": False,
        "start_at_login": False,
        "headless_auth_enabled": False,
        "pc_toolkit_enabled": True,
        "pc_toolkit_auto_connect": True,
    }


class RelationalStateDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.database = AppDatabase(root / "state.sqlite3", instance_id="test")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_preferences_are_normalized_and_constrained(self) -> None:
        saved = preferences()
        self.database.save_preferences(saved)
        loaded = self.database.load_preferences({"default_only": "fallback"})

        self.assertEqual(loaded["concurrency"], 60)
        self.assertEqual(loaded["update_channel"], "development")
        self.assertEqual(loaded["import_columns"]["device_allocation"], "Device(s) Allocation")
        self.assertEqual(loaded["pc_toolkit_model_mappings"][0]["location_status"], "Pending Rebuild")
        self.assertFalse(loaded["show_returned_serials_on_hand"])
        self.assertEqual(loaded["default_only"], "fallback")

        with self.assertRaises(sqlite3.IntegrityError):
            with self.database._write() as connection:
                connection.execute("UPDATE preferences SET concurrency=201 WHERE singleton=1")

    def test_v1_database_upgrades_transactionally_without_replacing_data(self) -> None:
        path = Path(self.temporary.name) / "v1.sqlite3"
        schema_v1 = _SCHEMA.replace(
            "    summary_signature TEXT NOT NULL DEFAULT '',\n", ""
        ).replace(
            "    content_signature TEXT NOT NULL DEFAULT '',\n", ""
        )
        connection = sqlite3.connect(path)
        try:
            connection.executescript(schema_v1 + "\nPRAGMA user_version = 1;")
            connection.execute(
                "INSERT INTO alm_imports(import_id, filename, content, sha256, byte_count, source_format, "
                "has_sheets, supports_formatting, default_sheet, needs_mapping, saved_at) "
                "VALUES ('legacy', 'legacy.csv', X'', ?, 0, 'csv', 0, 0, '', 0, '2026-09-27')",
                ("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",),
            )
            connection.commit()
        finally:
            connection.close()

        upgraded = AppDatabase(path, instance_id="upgrade")
        connection = sqlite3.connect(path)
        try:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(
                connection.execute("SELECT filename, byte_count FROM alm_imports WHERE import_id='legacy'").fetchone(),
                ("legacy.csv", 0),
            )
            self.assertIn(
                "summary_signature",
                {row[1] for row in connection.execute("PRAGMA table_info(alm_imports)")},
            )
            self.assertIn(
                "content_signature",
                {row[1] for row in connection.execute("PRAGMA table_info(request_records)")},
            )
        finally:
            connection.close()
        self.assertEqual(upgraded.load_alm_workbook("legacy")[0], "legacy.csv")

    def test_queue_is_relational_atomic_and_duplicate_serial_safe(self) -> None:
        requests = [
            {
                "id": "first",
                "kind": "user",
                "status": "Deployed - New Stock",
                "serials": ["SERIAL001", "SERIAL002"],
                "user": "alice.smith",
                "user_info": {"login": "alice.smith", "columns": ["Alice Smith", "alice.smith"]},
                "location": None,
                "bulk_validation": "valid",
                "bulk_serial_states": {"SERIAL001": "valid", "SERIAL002": "valid"},
                "max_portal": {"reference": "INC12345", "status": "Open", "matched_username": "alice.smith"},
            },
            {
                "id": "duplicate",
                "kind": "user",
                "status": "Deployed - New Stock",
                "serials": ["serial001"],
                "user": "bob.jones",
            },
        ]

        queue = self.database.save_request_queue(requests)

        self.assertEqual([request["id"] for request in queue], ["first"])
        self.assertEqual(queue[0]["serials"], ["SERIAL001", "SERIAL002"])
        self.assertEqual(queue[0]["user_info"]["columns"], ["Alice Smith", "alice.smith"])
        self.assertEqual(queue[0]["max_portal"]["reference"], "INC12345")
        self.assertEqual(queue[0]["bulk_serial_states"]["SERIAL002"], "valid")

        with mock.patch.object(self.database, "_store_pc_links", wraps=self.database._store_pc_links) as store_links:
            self.database.save_request_queue(requests)
        self.assertEqual(store_links.call_count, 0, "an unchanged queue row should retain its relationships")

        # A second process/window can mutate the latest committed queue without
        # replacing it with a stale in-memory copy.
        second_connection = AppDatabase(self.database.path, instance_id="test")
        second_connection.mutate_request_queue(
            lambda current: current + [{
                "id": "second", "kind": "location", "status": "Pending Rebuild",
                "serials": ["SERIAL003"], "location": {"city": "Sydney", "building": "1 Elizabeth Street"},
            }]
        )
        self.assertEqual(
            [request["id"] for request in self.database.load_request_queue()],
            ["first", "second"],
        )

        connection = sqlite3.connect(self.database.path)
        try:
            self.assertEqual(connection.execute("SELECT count(*) FROM request_records").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT count(*) FROM request_serials").fetchone()[0], 3)
            self.assertEqual(connection.execute("SELECT count(*) FROM max_portal_associations").fetchone()[0], 1)
        finally:
            connection.close()

    def test_verification_alias_lookup_uses_indexed_rows_and_refreshes_lru(self) -> None:
        self.database.merge_verification_cache({
            "serials": {
                "abc123": {"value": "ABC123", "columns": ["MacBook Air"], "device_type": "Laptop"}
            },
            "usernames": {
                "alice.smith": {"value": "alice.smith", "columns": ["Alice Smith"]}
            },
        })

        self.assertEqual(
            {
                key: value
                for key, value in self.database.lookup_verification("serials", "  macbook   air ").items()
                if key != "verified_at"
            },
            {"value": "ABC123", "columns": ["MacBook Air"], "device_type": "Laptop"},
        )
        self.assertEqual(
            self.database.lookup_verification("usernames", "ALICE SMITH")["value"],
            "alice.smith",
        )
        self.assertIsNone(self.database.lookup_verification("serials", "unknown"))
        connection = sqlite3.connect(self.database.path)
        try:
            plan = connection.execute(
                "EXPLAIN QUERY PLAN SELECT canonical_key FROM verification_aliases "
                "WHERE category='serials' AND alias_key='macbook air'"
            ).fetchone()[3]
        finally:
            connection.close()
        self.assertIn("USING INDEX", plan)

    def test_runtime_uses_database_alias_index_and_keeps_pending_cache_writes_visible(self) -> None:
        app = Application.__new__(Application)
        app.database = self.database
        app.verification_cache = {"serials": {}, "usernames": {}}
        app.verification_cache_lock = threading.Lock()
        app.verification_cache_write_lock = threading.Lock()
        app.verification_cache_dirty_entries = {"serials": {}, "usernames": {}}
        app.verification_cache_alias_index = None
        app.verification_cache_write_timer = None
        app.verification_cache_dirty = False
        app.verification_cache_last_write = time.monotonic()

        app.record_verified_serial({
            "value": "SERIAL002", "columns": ["Second Mac"], "device_type": "MacBook Air"
        })
        self.assertEqual(app.verification_cache_lookup("serial", "Second Mac")["value"], "SERIAL002")

        app._flush_verification_cache()
        self.assertEqual(app.verification_cache_lookup("serial", "second mac")["value"], "SERIAL002")

    def test_pc_toolkit_results_and_models_use_domain_tables(self) -> None:
        self.database.save_pc_toolkit_cache({
            "serial001": {
                "fetched_at": 100.0,
                "result": {
                    "query": "SERIAL001",
                    "lookup_kind": "device",
                    "found": True,
                    "active_count": 1,
                    "record_count": 1,
                    "devices": [{
                        "serial": "SERIAL001",
                        "model": "MacBook Air (M2, 2022)",
                        "status": "Deployed",
                        "active": True,
                        "assigned_user": {"login": "alice.smith", "name": "Alice Smith"},
                        "hardware": {"memory_gb": 16, "operating_system": "macOS"},
                    }],
                },
            }
        }, {"MacBook Air (M2, 2022)"}, max_entries=100)

        loaded = self.database.load_pc_toolkit_cache()
        device = loaded["entries"]["serial001"]["result"]["devices"][0]
        self.assertEqual(device["model"], "MacBook Air (M2, 2022)")
        self.assertEqual(device["assigned_user"]["login"], "alice.smith")
        self.assertEqual(device["hardware"]["memory_gb"], 16.0)
        self.assertEqual(self.database.load_pc_toolkit_models(), ["MacBook Air (M2, 2022)"])
        connection = sqlite3.connect(self.database.path)
        try:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            connection.close()
        self.assertIn("pc_toolkit_lookup_devices", tables)
        self.assertIn("pc_toolkit_lookup_people", tables)
        self.assertIn("pc_toolkit_lookup_hardware", tables)
        self.assertNotIn("pc_toolkit_json", tables)

    def test_pc_toolkit_cache_flush_only_upserts_changed_lookups(self) -> None:
        entries = {
            key: {
                "fetched_at": fetched_at,
                "result": {
                    "query": key, "found": True,
                    "devices": [{"serial": key.upper(), "model": model, "active": True}],
                },
            }
            for key, fetched_at, model in (
                ("serial001", 100.0, "MacBook Air"),
                ("serial002", 101.0, "MacBook Pro"),
            )
        }
        self.database.save_pc_toolkit_cache(entries, {"MacBook Air", "MacBook Pro"}, max_entries=100)
        service = PCToolkitService(
            self.database.path,
            state_store=self.database,
            simulate=True,
            preferences=lambda: {"pc_toolkit_enabled": True},
        )
        changed = {
            "fetched_at": 200.0,
            "result": {
                "query": "SERIAL001", "found": True,
                "devices": [{"serial": "SERIAL001", "model": "MacBook Air (M2)", "active": True}],
            },
        }
        service.cache["serial001"] = changed
        service.cache_dirty["serial001"] = changed

        with mock.patch.object(self.database, "_save_pc_lookup", wraps=self.database._save_pc_lookup) as save_lookup:
            service._write_cache(reason="scheduled_update")

        self.assertEqual(save_lookup.call_count, 1)
        restored = self.database.load_pc_toolkit_cache()["entries"]
        self.assertEqual(restored["serial001"]["result"]["devices"][0]["model"], "MacBook Air (M2)")
        self.assertEqual(restored["serial002"]["result"]["devices"][0]["model"], "MacBook Pro")
        self.assertFalse(service.cache_dirty)

    def test_pc_toolkit_service_uses_indexed_cache_on_demand(self) -> None:
        self.database.save_pc_toolkit_cache({
            "serial001": {
                "fetched_at": time.time(),
                "result": {
                    "query": "SERIAL001", "found": True, "record_count": 1,
                    "devices": [{"serial": "SERIAL001", "model": "MacBook Air", "active": True}],
                },
            }
        }, {"MacBook Air"}, max_entries=100)
        service = PCToolkitService(
            self.database.path,
            state_store=self.database,
            simulate=True,
            preferences=lambda: {"pc_toolkit_enabled": True},
        )

        self.assertEqual(service.cache, {}, "SQLite-backed service should not preload the entire cache")
        with mock.patch.object(
            self.database,
            "load_pc_toolkit_cache",
            side_effect=AssertionError("a lookup should use the indexed cache query"),
        ):
            result = service.lookup("SERIAL001")
        self.assertTrue(result["cached"])
        self.assertEqual(result["devices"][0]["model"], "MacBook Air")
        self.assertEqual(service.status()["cached_queries"], 1)
        self.assertEqual(len(service.cache), 1)

    def test_workbook_bytes_and_resumable_review_are_related(self) -> None:
        workbook = {
            "import_id": "import-1",
            "filename": "alm.csv",
            "format": "csv",
            "has_sheets": False,
            "supports_formatting": False,
            "default_sheet": "",
            "sheets": [{
                "name": "CSV",
                "headings": ["Username", "Serial", "New Asset Status"],
                "dates": [{
                    "value": "2026-09-27", "deployment_count": 1, "row_count": 1,
                    "groups": [], "warnings": [],
                }],
            }],
        }
        payload = b"Username,Serial\nalice.smith,SERIAL001\n"
        self.database.save_alm_workbook("import-1", "alm.csv", payload, {"username": "Username"})
        draft = {
            "id": "draft-1", "import_id": "import-1", "filename": "alm.csv",
            "phase": "review", "workbook": workbook, "saved_at": "2026-09-27T10:00:00",
            "settings": {"mode": "deploy", "sheet": "CSV", "dates": ["2026-09-27"], "columns": {"username": "Username"}},
            "preview": {
                "mode": "deploy", "sheet": "CSV", "dates": ["2026-09-27"],
                "counts": {"requests": 1, "deployments": 1},
                "requests": [{
                    "id": "alm-row-1", "kind": "user", "username": "alice.smith",
                    "serial": "SERIAL001", "status": "Deployed - New Stock",
                    "deployment_date": "2026-09-27", "alm_row_number": 2,
                    "included": True,
                }],
            },
        }
        self.database.save_alm_draft(draft)

        with mock.patch.object(self.database, "_store_pc_links", wraps=self.database._store_pc_links) as store_links:
            self.database.save_alm_draft(draft)
        self.assertEqual(store_links.call_count, 0, "an unchanged review row should not be rebuilt")

        restored_workbook = self.database.load_alm_workbook("import-1")
        self.assertEqual(restored_workbook[:2], ("alm.csv", payload))
        self.assertEqual(restored_workbook[2]["username"], "Username")
        restored = self.database.load_alm_drafts()[0]
        self.assertEqual(restored["phase"], "review")
        self.assertEqual(restored["workbook"]["sheets"][0]["headings"], ["Username", "Serial", "New Asset Status"])
        self.assertEqual(restored["preview"]["requests"][0]["username"], "alice.smith")
        connection = sqlite3.connect(self.database.path)
        try:
            self.assertEqual(connection.execute("SELECT byte_count FROM alm_imports WHERE import_id='import-1'").fetchone()[0], len(payload))
            self.assertEqual(connection.execute("SELECT count(*) FROM alm_draft_preview").fetchone()[0], 1)
        finally:
            connection.close()

    def test_mapping_draft_preserves_inspection_headings_and_import_identifier(self) -> None:
        workbook = {
            "import_id": "mapping-import", "filename": "alm.csv", "format": "csv",
            "has_sheets": False, "supports_formatting": False, "default_sheet": "Inventory",
            "sheets": [{"name": "Inventory", "dates": []}],
            "inspection": {
                "default_sheet": "Inventory",
                "sheets": [{"name": "Inventory", "headings": ["Username", "SN", "Old serial", "Returned"]}],
            },
        }
        self.database.save_alm_workbook("mapping-import", "alm.csv", b"Username,SN\n", {})
        self.database.save_alm_draft({
            "id": "mapping-draft", "import_id": "mapping-import", "phase": "mapping",
            "workbook": workbook,
            "settings": {"columns": {"old_device_serial": "Old serial", "return_checkbox": "Returned"}},
        })
        restored = self.database.load_alm_drafts()[0]
        self.assertEqual(restored["phase"], "mapping")
        self.assertEqual(restored["workbook"]["inspection"]["import_id"], "mapping-import")
        self.assertEqual(restored["workbook"]["inspection"]["sheets"][0]["headings"],
                         ["Username", "SN", "Old serial", "Returned"])
        self.assertEqual(restored["settings"]["columns"]["old_device_serial"], "Old serial")

    def test_history_is_a_job_with_relational_request_entries(self) -> None:
        job = {
            "job_id": "job-1", "state": "finished", "created_at": "2026-09-27T10:00:00",
            "finished_at": "2026-09-27T10:00:05", "request_for": "alice.smith", "simulation": True,
            "entries": [{
                "id": "request-1", "kind": "user", "status": "Deployed - New Stock",
                "user": "alice.smith", "serials": ["SERIAL001"], "state": "succeeded",
                "message": "submitted", "step": 3, "step_count": 3,
                "request_id": "1636000", "order_id": "ORD-1", "elapsed_seconds": 5,
            }],
        }

        history = self.database.upsert_request_history(job)

        self.assertEqual(history[0]["entries"][0]["serials"], ["SERIAL001"])
        self.assertEqual(history[0]["entries"][0]["request_id"], "1636000")
        connection = sqlite3.connect(self.database.path)
        try:
            self.assertEqual(connection.execute("SELECT count(*) FROM submission_jobs").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM submission_entries").fetchone()[0], 1)
        finally:
            connection.close()

    def test_legacy_migration_is_idempotent_and_keeps_source_files(self) -> None:
        root = Path(self.temporary.name) / "results"
        root.mkdir()
        queue_file = root / "web-request-queue.json"
        queue_file.write_text(json.dumps([{
            "id": "legacy-request", "kind": "user", "status": "Deployed - New Stock",
            "user": "legacy.user", "serials": ["LEGACY001"],
        }]), encoding="utf-8")
        cache_file = root / "web-verification-cache.json"
        cache_file.write_text(json.dumps({
            "serials": {"legacy001": {"value": "LEGACY001", "columns": ["LEGACY Laptop"], "device_type": "Laptop"}},
            "usernames": {},
        }), encoding="utf-8")
        pc_file = root / "pc-toolkit-cache.json"
        pc_file.write_text(json.dumps({
            "entries": {"legacy001": {"fetched_at": 100.0, "result": {
                "query": "LEGACY001", "found": True,
                "devices": [{"serial": "LEGACY001", "model": "Legacy MacBook", "active": True}],
            }}},
            "models": ["Legacy MacBook"],
        }), encoding="utf-8")

        database_path = Path(self.temporary.name) / "migrated.sqlite3"
        migrated = AppDatabase(database_path, legacy_results_dir=root)
        # Construct a second handle to model a restart. The migration marker
        # prevents re-importing or duplicating state.
        restarted = AppDatabase(database_path, legacy_results_dir=root)

        self.assertEqual([row["id"] for row in restarted.load_request_queue()], ["legacy-request"])
        self.assertEqual(restarted.lookup_verification("serials", "legacy laptop")["value"], "LEGACY001")
        self.assertEqual(restarted.load_pc_toolkit_models(), ["Legacy MacBook"])
        self.assertEqual(restarted.load_pc_toolkit_cache()["entries"]["legacy001"]["result"]["devices"][0]["model"], "Legacy MacBook")
        self.assertTrue(queue_file.exists())
        self.assertTrue(cache_file.exists())
        self.assertTrue(pc_file.exists())
        connection = sqlite3.connect(database_path)
        try:
            migrations = connection.execute("SELECT count(*) FROM app_migrations").fetchone()[0]
            issues = connection.execute("SELECT count(*) FROM migration_issues").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(migrations, 1)
        self.assertEqual(issues, 0)


if __name__ == "__main__":
    unittest.main()
