from __future__ import annotations

from dataclasses import dataclass
import unittest

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


if __name__ == "__main__":
    unittest.main()
