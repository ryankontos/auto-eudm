from __future__ import annotations

import base64
from pathlib import Path
import unittest
from unittest import mock

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

    def test_returning_user_details_must_match_the_selected_login(self) -> None:
        errors = location_request(
            returning=True,
            returning_user="selected.user",
            returning_user_info={"login": "different.user", "columns": []},
        ).validate()
        self.assertTrue(any("do not match" in error for error in errors))

    def test_returning_user_match_is_case_insensitive(self) -> None:
        errors = location_request(
            returning=True,
            returning_user="Selected.User",
            returning_user_info={"login": "selected.user", "columns": []},
        ).validate()
        self.assertEqual(errors, [])

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

    def test_returning_toggle_must_be_a_json_boolean(self) -> None:
        with self.assertRaisesRegex(eudm.EUDMError, "true or false"):
            location_request(returning="false")

    def test_returning_user_columns_must_be_a_json_list(self) -> None:
        with self.assertRaisesRegex(eudm.EUDMError, "columns must be a list"):
            location_request(
                returning=True,
                returning_user="valid.user",
                returning_user_info={
                    "login": "valid.user",
                    "columns": "Valid User",
                },
            )


class WorkbookUploadTests(unittest.TestCase):
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

    def test_sample_workbook_can_be_inspected_and_read_from_one_payload(self) -> None:
        path = (
            Path(__file__).resolve().parents[1]
            / "samples"
            / "Inventory Tracking - Sydney - Test Data.xlsx"
        )
        payload = path.read_bytes()

        inspection = WorkbookImport.inspect_payload(path.name, payload)
        workbook = WorkbookImport.from_payload(path.name, payload)

        self.assertEqual(inspection["default_sheet"], "Bookings 2026")
        self.assertIn("Bookings 2026", workbook.sheets)
        self.assertTrue(workbook.summary()["sheets"])


if __name__ == "__main__":
    unittest.main()
