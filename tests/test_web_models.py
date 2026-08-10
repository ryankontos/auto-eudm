from __future__ import annotations

import unittest

from auto_eudm.web_models import RequestSpec, validate_queue


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


if __name__ == "__main__":
    unittest.main()
