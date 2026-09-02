from __future__ import annotations

import base64
from datetime import date
from io import BytesIO
import unittest
from unittest import mock

from auto_eudm import eudm_inventory_import as inventory
from auto_eudm import eudm_request as eudm
from auto_eudm import web_models
from auto_eudm.web_models import RequestSpec, WorkbookImport, validate_queue


def user_request(**overrides: object) -> RequestSpec:
    raw: dict[str, object] = {
        "id": "request-1",
        "kind": "user",
        "serials": ["SERIAL123"],
        "status": "Deployed - New Stock",
        "user": "valid.user",
        "group": "Deployments",
    }
    raw.update(overrides)
    return RequestSpec.from_json(raw)


def location_request(**overrides: object) -> RequestSpec:
    raw: dict[str, object] = {
        "id": "request-2",
        "kind": "location",
        "serials": ["SERIAL456"],
        "status": "Pending Rebuild",
        "location": {
            "city": "Sydney, AU",
            "building": "Building",
            "floor": "1",
            "room": "Store",
        },
        "group": "Returned devices",
    }
    raw.update(overrides)
    return RequestSpec.from_json(raw)


class RequestValidationTests(unittest.TestCase):
    def test_valid_user_request(self) -> None:
        self.assertEqual(user_request().validate(), [])

    def test_invalid_serial_is_rejected_server_side(self) -> None:
        errors = user_request(serials=["bad serial"]).validate()
        self.assertTrue(any("Serial numbers must" in error for error in errors))

    def test_user_deployment_requires_a_receiving_user(self) -> None:
        self.assertIn("Choose the receiving user.", user_request(user="").validate())

    def test_location_return_preserves_the_returning_user(self) -> None:
        request = location_request(
            returning=True,
            returning_user="returning.user",
            returning_user_info={
                "login": "returning.user",
                "columns": ["Returning User", "returning.user"],
            },
        )

        restored = RequestSpec.from_json(request.to_json())

        self.assertTrue(restored.returning_requested)
        self.assertEqual(restored.returning_user, "returning.user")
        self.assertEqual(restored.returning_user_info, {
            "login": "returning.user",
            "columns": ["Returning User", "returning.user"],
        })
        self.assertEqual(restored.validate(), [])

    def test_location_return_requires_verified_returning_user_details(self) -> None:
        request = location_request(
            returning=True,
            returning_user="returning.user",
        )

        self.assertTrue(any("Search and verify the returning user's details" in error for error in request.validate()))

    def test_bulk_location_return_is_rejected(self) -> None:
        request = location_request(
            kind="bulk_location",
            returning=True,
            returning_user="returning.user",
            returning_user_info={
                "login": "returning.user",
                "columns": ["Returning User", "returning.user"],
            },
        )

        self.assertIn(
            "Bulk add to location stock cannot include a returning user.",
            request.validate(),
        )

    def test_requesting_user_must_be_a_login_id(self) -> None:
        errors = validate_queue([user_request()], "person@example.com")
        self.assertIn("_queue", errors)
        self.assertTrue(any("login ID" in error for error in errors["_queue"]))

    def test_duplicate_within_bulk_request_is_not_reported_as_cross_request(self) -> None:
        request = location_request(
            kind="bulk_location",
            serials=["SERIAL456", "serial456"],
        )

        errors = validate_queue([request], "valid.user")

        self.assertEqual(
            errors[request.client_id].count(
                "Remove duplicate serial numbers from this request."
            ),
            1,
        )
        self.assertFalse(
            any("more than one queued request" in message for message in errors[request.client_id])
        )

    def test_cross_request_duplicate_is_reported_once_per_request(self) -> None:
        first = user_request(id="request-1", serials=["SERIAL123"])
        second = user_request(id="request-2", serials=["serial123"])

        errors = validate_queue([first, second], "valid.user")

        for request in (first, second):
            matching = [
                message
                for message in errors[request.client_id]
                if "more than one queued request" in message
            ]
            self.assertEqual(len(matching), 1)

    def test_duplicate_request_ids_merge_errors_and_fail_the_queue(self) -> None:
        first = user_request(id="duplicate", serials=["bad serial"])
        second = user_request(id="duplicate", user="")

        errors = validate_queue([first, second], "valid.user")

        self.assertTrue(any("Serial numbers must" in message for message in errors["duplicate"]))
        self.assertIn("Choose the receiving user.", errors["duplicate"])
        self.assertTrue(any("duplicate internal IDs" in message for message in errors["_queue"]))

    def test_reserved_queue_id_is_rejected(self) -> None:
        errors = validate_queue([user_request(id="_queue")], "valid.user")
        self.assertTrue(any("reserved internal ID" in message for message in errors["_queue"]))


class WorkbookUploadTests(unittest.TestCase):
    def test_workbook_prepare_accepts_multiple_dates(self) -> None:
        rows = [
            inventory.SheetRow(
                row_number=2,
                deployment_date=date(2025, 2, 3),
                username="first.user",
                deployment_serial="SERIAL123",
                returned_device_serial=None,
                pending_return_serial=None,
                marked_red=False,
                enabled=True,
            ),
            inventory.SheetRow(
                row_number=3,
                deployment_date=date(2025, 2, 4),
                username="second.user",
                deployment_serial="SERIAL456",
                returned_device_serial=None,
                pending_return_serial=None,
                marked_red=False,
                enabled=True,
            ),
        ]
        workbook = WorkbookImport("import-multiple", "tracking.xlsx", {"Sheet": rows})

        payload = workbook.prepare(
            "Sheet",
            ["2025-02-03", "2025-02-04"],
            "deployments",
        )

        self.assertEqual(
            [request["serials"][0] for request in payload["requests"]],
            ["SERIAL123", "SERIAL456"],
        )
        self.assertEqual(payload["dates"], ["2025-02-03", "2025-02-04"])

    def test_workbook_prepare_rejects_duplicates_across_dates(self) -> None:
        rows = [
            inventory.SheetRow(
                row_number=2,
                deployment_date=date(2025, 2, 3),
                username="first.user",
                deployment_serial="SERIAL123",
                returned_device_serial=None,
                pending_return_serial=None,
                marked_red=False,
                enabled=True,
            ),
            inventory.SheetRow(
                row_number=3,
                deployment_date=date(2025, 2, 4),
                username="second.user",
                deployment_serial="SERIAL123",
                returned_device_serial=None,
                pending_return_serial=None,
                marked_red=False,
                enabled=True,
            ),
        ]
        workbook = WorkbookImport("import-duplicate-dates", "tracking.xlsx", {"Sheet": rows})

        with self.assertRaisesRegex(eudm.EUDMError, "selected dates/sections"):
            workbook.prepare(
                "Sheet",
                ["2025-02-03", "2025-02-04"],
                "deployments",
            )

    def test_workbook_prepare_persists_device_allocation(self) -> None:
        row = inventory.SheetRow(
            row_number=2,
            deployment_date=date(2025, 2, 3),
            username="valid.user",
            deployment_serial="SERIAL123",
            returned_device_serial=None,
            pending_return_serial=None,
            marked_red=False,
            enabled=True,
            device_allocation="MacBookPro18,3",
        )
        workbook = WorkbookImport("import-1", "tracking.xlsx", {"Sheet": [row]})

        payload = workbook.prepare("Sheet", "2025-02-03", "deployments")

        self.assertEqual(
            payload["requests"][0]["device_allocation"], "MacBookPro18,3"
        )

    def test_workbook_prepare_marks_missing_return_serials_on_deployments(self) -> None:
        row = inventory.SheetRow(
            row_number=2,
            deployment_date=date(2025, 2, 3),
            username="valid.user",
            deployment_serial="SERIAL123",
            returned_device_serial=None,
            pending_return_serial=None,
            marked_red=False,
            enabled=True,
        )
        workbook = WorkbookImport("import-returns", "tracking.xlsx", {"Sheet": [row]})

        request = workbook.prepare("Sheet", "2025-02-03", "deployments")["requests"][0]

        self.assertFalse(request["has_returned_device_serial"])
        self.assertFalse(request["has_pending_return_serial"])

    def test_workbook_prepare_reports_deployment_serial_without_username(self) -> None:
        row = inventory.SheetRow(
            row_number=2,
            deployment_date=date(2025, 2, 3),
            username=None,
            deployment_serial="SERIAL123",
            returned_device_serial=None,
            pending_return_serial=None,
            marked_red=False,
            enabled=True,
        )
        workbook = WorkbookImport("import-missing-user", "tracking.xlsx", {"Sheet": [row]})

        payload = workbook.prepare("Sheet", "2025-02-03", "deployments")

        self.assertEqual(payload["requests"], [])
        self.assertEqual(
            payload["warnings"]["missing_username_deployments"],
            [{"row_number": 2, "date": "2025-02-03", "serial": "SERIAL123"}],
        )

    def test_backlog_filters_rows_and_labels_duplicate_username_occurrences(self) -> None:
        rows = [
            inventory.SheetRow(
                row_number=2,
                deployment_date=date(2025, 2, 10),
                username="valid.user",
                deployment_serial="SERIAL123",
                returned_device_serial=None,
                pending_return_serial=None,
                marked_red=False,
                enabled=True,
                new_asset_status="In Inventory",
            ),
            inventory.SheetRow(
                row_number=3,
                deployment_date=date(2025, 2, 9),
                username="deployed.user",
                deployment_serial="SERIAL456",
                returned_device_serial=None,
                pending_return_serial=None,
                marked_red=False,
                enabled=True,
                new_asset_status="Deployed",
            ),
            inventory.SheetRow(
                row_number=4,
                deployment_date=date(2025, 1, 1),
                username="old.user",
                deployment_serial="SERIAL789",
                returned_device_serial=None,
                pending_return_serial=None,
                marked_red=False,
                enabled=True,
                new_asset_status="In Inventory",
            ),
            inventory.SheetRow(
                row_number=5,
                deployment_date=date(2025, 2, 9),
                username="VALID.USER",
                deployment_serial="SERIAL999",
                returned_device_serial=None,
                pending_return_serial=None,
                marked_red=False,
                enabled=False,
                new_asset_status="In Inventory",
            ),
            inventory.SheetRow(
                row_number=6,
                deployment_date=date(2025, 2, 9),
                username="review.user",
                deployment_serial="SERIAL321",
                returned_device_serial=None,
                pending_return_serial=None,
                marked_red=False,
                enabled=True,
                new_asset_status="Deployed - awaiting review",
            ),
            inventory.SheetRow(
                row_number=7,
                deployment_date=date(2025, 2, 9),
                username="Full Name",
                deployment_serial="SERIAL654",
                returned_device_serial=None,
                pending_return_serial=None,
                marked_red=False,
                enabled=True,
                new_asset_status="In Inventory",
            ),
        ]
        workbook = WorkbookImport("import-backlog", "tracking.xlsx", {"Sheet": rows})
        ignored = {WorkbookImport.backlog_key("SERIAL999", "valid.user")}

        payload = workbook.prepare_backlog(
            "Sheet", 5, True, ignored, today=date(2025, 2, 10)
        )

        self.assertEqual(
            [item["serial"] for item in payload["candidates"]],
            ["SERIAL123", "SERIAL321", "SERIAL654"],
        )
        self.assertEqual(payload["candidates"][0]["username_occurrence"], 1)
        self.assertEqual(payload["ignored_count"], 1)
        self.assertEqual(payload["counts"]["ignored"], 1)

        payload = workbook.prepare_backlog(
            "Sheet", 5, True, set(), today=date(2025, 2, 10)
        )
        candidates = payload["candidates"]
        self.assertEqual(
            [item["serial"] for item in candidates],
            ["SERIAL123", "SERIAL999", "SERIAL321", "SERIAL654"],
        )
        self.assertEqual([item["row_number"] for item in candidates], [2, 5, 6, 7])
        self.assertEqual([item["username_occurrence"] for item in candidates], [1, 2, 1, 1])
        self.assertEqual([item["username_occurrence_total"] for item in candidates], [2, 2, 1, 1])
        self.assertEqual(len({item["id"] for item in candidates}), 4)
        self.assertEqual(payload["counts"]["candidates"], 4)
        self.assertTrue(candidates[0]["attending"])
        self.assertTrue(candidates[0]["included"])
        self.assertFalse(candidates[1]["attending"])
        self.assertFalse(candidates[1]["included"])
        self.assertTrue(candidates[1]["default_excluded"])

        with self.assertRaisesRegex(eudm.EUDMError, "between 1 and 3650"):
            workbook.prepare_backlog("Sheet", 0, True, set(), today=date(2025, 2, 10))

    def test_workbook_summary_counts_selected_date_and_valid_user_rows(self) -> None:
        selected = inventory.SheetRow(
            row_number=2,
            deployment_date=date(2025, 2, 3),
            username="valid.user",
            deployment_serial="SERIAL123",
            returned_device_serial=None,
            pending_return_serial=None,
            marked_red=False,
            enabled=True,
        )
        other_date = inventory.SheetRow(
            row_number=3,
            deployment_date=date(2025, 2, 4),
            username="other.user",
            deployment_serial="SERIAL456",
            returned_device_serial="RETURN456",
            pending_return_serial="PENDING456",
            marked_red=False,
            enabled=True,
        )
        display_name = inventory.SheetRow(
            row_number=4,
            deployment_date=date(2025, 2, 3),
            username="Jane Doe",
            deployment_serial="SERIAL789",
            returned_device_serial="RETURN789",
            pending_return_serial="PENDING789",
            marked_red=False,
            enabled=True,
        )
        workbook = WorkbookImport(
            "import-2", "tracking.xlsx", {"Sheet": [selected, other_date, display_name]}
        )

        dates = workbook.summary()["sheets"][0]["dates"]
        selected_summary = next(item for item in dates if item["value"] == "2025-02-03")

        self.assertEqual(selected_summary["deployment_count"], 1)
        self.assertEqual(selected_summary["returned_device_count"], 0)
        self.assertEqual(selected_summary["pending_return_count"], 0)
        self.assertEqual(selected_summary["eligible_row_count"], 1)

    def test_request_spec_preserves_device_allocation(self) -> None:
        request = user_request(device_allocation="MacBookPro18,3")

        restored = RequestSpec.from_json(request.to_json())

        self.assertEqual(restored.device_allocation, "MacBookPro18,3")
        self.assertEqual(restored.to_json()["device_allocation"], "MacBookPro18,3")

    def test_request_spec_preserves_selected_user_details_for_history(self) -> None:
        request = user_request(user_info={
            "login": "valid.user",
            "columns": ["Valid User", "valid.user"],
        })

        restored = RequestSpec.from_json(request.to_json())

        self.assertEqual(restored.user_info, {
            "login": "valid.user",
            "columns": ["Valid User", "valid.user"],
        })

    def test_upload_is_decoded_to_bytes(self) -> None:
        encoded = base64.b64encode(b"workbook bytes").decode("ascii")
        self.assertEqual(
            WorkbookImport.decode_upload("tracking.xlsx", encoded),
            b"workbook bytes",
        )

    def test_invalid_or_empty_upload_is_rejected_before_parsing(self) -> None:
        with self.assertRaisesRegex(eudm.EUDMError, "invalid"):
            WorkbookImport.decode_upload("tracking.xlsx", "not base64!")
        with self.assertRaisesRegex(eudm.EUDMError, "empty"):
            WorkbookImport.decode_upload("tracking.xlsx", "")
        with self.assertRaisesRegex(eudm.EUDMError, "xlsx"):
            WorkbookImport.decode_upload("tracking.csv", "anything")

    def test_oversized_upload_is_rejected_before_parsing(self) -> None:
        encoded = base64.b64encode(b"1234").decode("ascii")
        with mock.patch.object(web_models, "MAX_WORKBOOK_BYTES", 3):
            with self.assertRaisesRegex(eudm.EUDMError, "100 MB"):
                WorkbookImport.decode_upload("tracking.xlsx", encoded)

    def test_workbook_can_be_inspected_and_read_from_one_payload(self) -> None:
        from openpyxl import Workbook as OpenPyXLWorkbook

        source = OpenPyXLWorkbook()
        source.active.title = "Bookings 2026"
        sheet = source.active
        sheet.append(["Date", "Username", "SN", "OLD Device SN", "Attend"])
        sheet.append([date(2025, 2, 3), "valid.user", "SERIAL123", "PENDING123", True])
        buffer = BytesIO()
        source.save(buffer)
        payload = buffer.getvalue()

        inspection = WorkbookImport.inspect_payload("tracking.xlsx", payload)
        workbook = WorkbookImport.from_payload("tracking.xlsx", payload)

        self.assertEqual(inspection["default_sheet"], "Bookings 2026")
        self.assertIn("Bookings 2026", workbook.sheets)
        self.assertTrue(workbook.summary()["sheets"])


if __name__ == "__main__":
    unittest.main()
