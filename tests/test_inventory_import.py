from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import unittest
from unittest import mock

from auto_eudm import eudm_inventory_import as inventory
from auto_eudm import eudm_request as eudm


@dataclass
class FakeCell:
    value: object
    column: int
    row: int = 1


class FakeSheet:
    max_column = 3
    max_row = 1

    def iter_rows(self, **_kwargs: object) -> list[tuple[FakeCell, ...]]:
        return [
            (
                FakeCell("Unrelated", 1),
                FakeCell("Headings", 2),
                FakeCell("Only", 3),
            )
        ]


class ImportColumnTests(unittest.TestCase):
    def test_no_mapping_uses_all_import_column_defaults(self) -> None:
        self.assertEqual(inventory.columns_from_mapping(), inventory.ImportColumns())

    def test_explicit_blank_optional_columns_remain_disabled(self) -> None:
        columns = inventory.columns_from_mapping(
            {
                "username": "Username",
                "deployment_serial": "SN",
                "pending_return": "OLD Device SN",
                "returned_device": "",
                "enabled": "",
                "device_allocation": "",
            }
        )
        self.assertEqual(columns.returned_device, "")
        self.assertEqual(columns.device_allocation, "")

    def test_missing_header_error_lists_only_required_columns(self) -> None:
        with self.assertRaises(eudm.EUDMError) as raised:
            inventory.find_column_indexes(FakeSheet(), inventory.ImportColumns())
        message = str(raised.exception)
        self.assertIn("'Username', 'SN', 'OLD Device SN', 'Date'", message)
        self.assertNotIn("Device(s) Allocation", message)
        self.assertNotIn("Returned Device SN", message)


class ImportActionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.selected_date = date(2025, 2, 3)
        self.row = inventory.SheetRow(
            row_number=2,
            deployment_date=self.selected_date,
            username="valid.user",
            deployment_serial="DEPLOY123",
            returned_device_serial="RETURN123",
            pending_return_serial="PENDING123",
            marked_red=False,
            enabled=True,
        )

    def test_cli_modes_create_deployments_and_pending_returns(self) -> None:
        actions, ignored = inventory.build_actions(
            [self.row],
            self.selected_date,
            "deployments,pending_returns",
        )

        self.assertEqual(
            [(action.group, action.serial) for action in actions],
            [
                ("Deployments", "DEPLOY123"),
                ("Pending returns", "PENDING123"),
            ],
        )
        self.assertFalse(ignored)

    @mock.patch("builtins.print")
    @mock.patch("builtins.input", return_value="1")
    def test_cli_can_override_a_deployment_to_existing_stock(
        self, _input: mock.Mock, _print: mock.Mock
    ) -> None:
        actions, _ = inventory.build_actions(
            [self.row], self.selected_date, "deployments"
        )

        updated = inventory.override_new_statuses(actions)

        self.assertEqual(updated[0].status, inventory.EXISTING_STOCK)

    def test_date_label_is_portable_and_has_no_zero_padding(self) -> None:
        self.assertEqual(
            inventory.format_date_label(self.selected_date),
            "Monday 3 February 2025",
        )


if __name__ == "__main__":
    unittest.main()
