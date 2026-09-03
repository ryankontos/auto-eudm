"""Small streaming XLSX reader for the web import path.

The web importer only needs cell values, row numbers, styles for the date
section fill, and the workbook's sheet names. Loading those directly from the
XLSX XML is substantially faster than constructing an openpyxl cell object
for every cell in a large ALM workbook. The normal openpyxl reader remains a
fallback for files with an XML layout this focused reader cannot understand.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
import posixpath
import re
from typing import Any, Iterator
from xml.etree import ElementTree
import zipfile


class FastWorkbookError(Exception):
    """The workbook needs the compatibility reader instead."""


@dataclass(frozen=True)
class FastRow:
    row_number: int
    # Column number -> (value, style index).
    cells: dict[int, tuple[Any, int]]


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _relationship_id(attributes: dict[str, str]) -> str | None:
    for key, value in attributes.items():
        if key == "r:id" or key.endswith("}id"):
            return value
    return None


def _column_number(reference: str) -> int:
    match = re.match(r"([A-Za-z]+)", reference)
    if not match:
        raise FastWorkbookError("A workbook cell had no column reference.")
    number = 0
    for character in match.group(1).upper():
        number = number * 26 + ord(character) - ord("A") + 1
    return number


def _row_number_from_reference(reference: str) -> int | None:
    match = re.search(r"(\d+)$", reference)
    return int(match.group(1)) if match else None


def _numeric_value(value: str) -> int | float | str:
    text = value.strip()
    if not text:
        return ""
    try:
        number = float(text)
    except ValueError:
        return value
    return int(number) if number.is_integer() else number


class FastWorkbook:
    """Read only the XML parts used by the ALM importer."""

    def __init__(self, payload: bytes) -> None:
        try:
            self.archive = zipfile.ZipFile(BytesIO(payload))
            workbook_root = ElementTree.fromstring(
                self.archive.read("xl/workbook.xml")
            )
            relationships_root = ElementTree.fromstring(
                self.archive.read("xl/_rels/workbook.xml.rels")
            )
        except (KeyError, OSError, ValueError, ElementTree.ParseError, zipfile.BadZipFile) as exc:
            archive = getattr(self, "archive", None)
            if archive is not None:
                archive.close()
            raise FastWorkbookError("The workbook XML could not be read.") from exc
        try:
            self.epoch = datetime(1904, 1, 1) if any(
                _local_name(element.tag) == "workbookPr"
                and str(element.attrib.get("date1904", "0")).casefold()
                in {"1", "true"}
                for element in workbook_root.iter()
            ) else datetime(1899, 12, 30)
            relationships = {
                str(element.attrib.get("Id")): str(element.attrib.get("Target"))
                for element in relationships_root.iter()
                if _local_name(element.tag) == "Relationship"
                and element.attrib.get("Id")
                and element.attrib.get("Target")
            }
            self._sheets: dict[str, str] = {}
            for element in workbook_root.iter():
                if _local_name(element.tag) != "sheet":
                    continue
                name = str(element.attrib.get("name", "")).strip()
                relation_id = _relationship_id(element.attrib)
                target = relationships.get(relation_id or "")
                if not name or not target:
                    continue
                self._sheets[name] = self._archive_path(target)
            if not self._sheets:
                raise FastWorkbookError("The workbook did not contain a worksheet.")
            self._style_fill_keys = self._read_style_fill_keys()
            self._shared_strings = self._read_shared_strings()
        except FastWorkbookError:
            self.close()
            raise
        except (KeyError, OSError, ValueError, ElementTree.ParseError) as exc:
            self.close()
            raise FastWorkbookError("The workbook metadata could not be read.") from exc

    @staticmethod
    def _archive_path(target: str) -> str:
        target = target.replace("\\", "/")
        if target.startswith("/"):
            return target.lstrip("/")
        return posixpath.normpath(posixpath.join("xl", target))

    @property
    def sheet_names(self) -> tuple[str, ...]:
        return tuple(self._sheets)

    def close(self) -> None:
        self.archive.close()

    def __enter__(self) -> "FastWorkbook":
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.close()

    def _read_shared_strings(self) -> list[str]:
        try:
            stream = self.archive.open("xl/sharedStrings.xml")
        except KeyError:
            return []
        values: list[str] = []
        with stream:
            try:
                for _event, element in ElementTree.iterparse(stream, events=("end",)):
                    if _local_name(element.tag) != "si":
                        continue
                    values.append(
                        "".join(
                            child.text or ""
                            for child in element.iter()
                            if _local_name(child.tag) == "t"
                        )
                    )
                    element.clear()
            except (OSError, ElementTree.ParseError) as exc:
                raise FastWorkbookError("The workbook shared strings could not be read.") from exc
        return values

    def _read_style_fill_keys(self) -> list[tuple[str, str | int]]:
        try:
            root = ElementTree.fromstring(self.archive.read("xl/styles.xml"))
        except KeyError:
            return [("none",)]
        except (OSError, ElementTree.ParseError) as exc:
            raise FastWorkbookError("The workbook styles could not be read.") from exc
        fills: list[str] = []
        cell_xfs: list[ElementTree.Element] = []
        for element in root.iter():
            name = _local_name(element.tag)
            if name == "fills":
                fills = [
                    ElementTree.tostring(child, encoding="unicode")
                    for child in element
                    if _local_name(child.tag) == "fill"
                ]
            elif name == "cellXfs":
                cell_xfs = [
                    child for child in element if _local_name(child.tag) == "xf"
                ]
        if not fills:
            fills = ["none"]
        result: list[tuple[str, str | int]] = []
        for xf in cell_xfs or [ElementTree.Element("xf")]:
            try:
                fill_id = int(xf.attrib.get("fillId", "0"))
            except ValueError:
                fill_id = 0
            result.append(
                ("fill", fills[fill_id] if 0 <= fill_id < len(fills) else fill_id)
            )
        return result

    def fill_key(self, style_index: int) -> tuple[str, str | int]:
        if 0 <= style_index < len(self._style_fill_keys):
            return self._style_fill_keys[style_index]
        return ("style", style_index)

    def sheet_max_row(self, name: str) -> int:
        path = self._sheets.get(name)
        if not path:
            raise FastWorkbookError("The workbook sheet was not found.")
        try:
            with self.archive.open(path) as stream:
                for event, element in ElementTree.iterparse(
                    stream, events=("start", "end")
                ):
                    if event == "start" and _local_name(element.tag) == "dimension":
                        reference = str(element.attrib.get("ref", ""))
                        ending = reference.split(":", 1)[-1]
                        row_number = _row_number_from_reference(ending)
                        if row_number:
                            return row_number
                        break
                    # A few generated workbooks omit <dimension>. Do not
                    # scan the whole sheet just to discover that; the import
                    # pass can still report progress without a total.
                    if event == "start" and _local_name(element.tag) == "sheetData":
                        break
        except (KeyError, OSError, ElementTree.ParseError) as exc:
            raise FastWorkbookError("The workbook sheet dimension could not be read.") from exc
        return 0

    def _cell_value(self, element: ElementTree.Element) -> Any:
        cell_type = str(element.attrib.get("t", ""))
        value_element = next(
            (
                child
                for child in element
                if _local_name(child.tag) == "v"
            ),
            None,
        )
        inline_element = next(
            (
                child
                for child in element
                if _local_name(child.tag) == "is"
            ),
            None,
        )
        if cell_type == "inlineStr" and inline_element is not None:
            return "".join(
                child.text or ""
                for child in inline_element.iter()
                if _local_name(child.tag) == "t"
            )
        raw = value_element.text if value_element is not None else None
        if raw is None:
            return None
        if cell_type == "s":
            try:
                return self._shared_strings[int(raw)]
            except (IndexError, ValueError):
                return raw
        if cell_type == "b":
            return raw == "1"
        if cell_type in {"str", "e", "d"}:
            return raw
        return _numeric_value(raw)

    def iter_rows(self, name: str) -> Iterator[FastRow]:
        path = self._sheets.get(name)
        if not path:
            raise FastWorkbookError("The workbook sheet was not found.")
        try:
            with self.archive.open(path) as stream:
                fallback_row = 0
                for event, element in ElementTree.iterparse(stream, events=("end",)):
                    if _local_name(element.tag) != "row":
                        continue
                    fallback_row += 1
                    row_number = int(element.attrib.get("r", fallback_row))
                    cells: dict[int, tuple[Any, int]] = {}
                    for cell in element:
                        if _local_name(cell.tag) != "c":
                            continue
                        reference = str(cell.attrib.get("r", ""))
                        if not reference:
                            continue
                        try:
                            column = _column_number(reference)
                            style = int(cell.attrib.get("s", "0"))
                        except ValueError as exc:
                            raise FastWorkbookError(
                                "The workbook contained an invalid cell reference."
                            ) from exc
                        cells[column] = (self._cell_value(cell), style)
                    yield FastRow(row_number, cells)
                    element.clear()
        except FastWorkbookError:
            raise
        except (KeyError, OSError, ValueError, ElementTree.ParseError) as exc:
            raise FastWorkbookError("The workbook sheet could not be read.") from exc
