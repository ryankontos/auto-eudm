"""Detailed, local diagnostics for ALM workbook load attempts.

The importer is deliberately conservative about copying workbook contents into
logs.  A load diagnostic records enough container/XML/parser detail to explain
an encryption, corruption, or compatibility failure without duplicating the
workbook's cell values or formulas.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import sys
import threading
import traceback
from io import BytesIO
from typing import Any
import zipfile
from xml.etree import ElementTree


MAX_XML_INSPECTION_BYTES = 8 * 1024 * 1024


def _text(value: Any) -> str:
    return str(value or "").replace("\r", "\\r").replace("\n", "\\n")


def _local_name(tag: Any) -> str:
    value = str(tag or "")
    return value.rsplit("}", 1)[-1]


class WorkbookLoadLog:
    """Append-only diagnostic log for one workbook load attempt."""

    def __init__(self, path: Path, attempt_id: str, filename: str) -> None:
        self.path = path
        self.attempt_id = attempt_id
        self.filename = filename
        self.lock = threading.Lock()
        self.started_at = datetime.now(timezone.utc)
        self._write_header()

    @classmethod
    def create(cls, root: Path, attempt_id: str, filename: str) -> "WorkbookLoadLog":
        directory = root / "results" / "alm-workbook-load-logs"
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        safe_id = "".join(character for character in str(attempt_id) if character.isalnum())[-48:]
        path = directory / f"{stamp}-{safe_id or 'attempt'}.log"
        return cls(path, str(attempt_id), filename)

    def _write(self, text: str) -> None:
        with self.lock:
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(text)
                stream.flush()

    def _write_header(self) -> None:
        self.path.write_text(
            "AutoEUDM ALM workbook load diagnostic\n"
            "====================================\n"
            f"Attempt ID: {self.attempt_id}\n"
            f"Started UTC: {self.started_at.isoformat()}\n"
            f"Filename: {_text(self.filename)}\n"
            f"Python: {_text(sys.version)}\n"
            f"Executable: {_text(sys.executable)}\n"
            f"Platform: {_text(platform.platform())}\n"
            f"Machine: {_text(platform.machine())}\n"
            f"Working directory: {_text(Path.cwd())}\n"
            "\n",
            encoding="utf-8",
        )
        for package in ("openpyxl", "et-xmlfile"):
            try:
                version = importlib.metadata.version(package)
            except importlib.metadata.PackageNotFoundError:
                version = "not installed"
            except Exception as exc:
                version = f"unknown ({type(exc).__name__}: {exc})"
            self.event("runtime package version", stage="environment", package=package, version=version)
        self.event("runtime ZIP implementation", stage="environment", zipfile_module=zipfile.__file__, zipfile_version=getattr(zipfile, "__version__", "stdlib"))
        self.event("diagnostic started", stage="lifecycle")

    def event(self, message: str, *, stage: str = "general", **details: Any) -> None:
        stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        line = f"[{stamp}] [{stage}] {message}"
        if details:
            encoded = json.dumps(details, ensure_ascii=False, sort_keys=True, default=str)
            line += f" | {encoded}"
        self._write(line + "\n")

    def exception(self, message: str, exc: BaseException, *, stage: str = "error") -> None:
        self.event(
            message,
            stage=stage,
            exception_type=type(exc).__name__,
            exception_message=str(exc),
        )
        self._write("--- traceback start ---\n")
        self._write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
        self._write("--- traceback end ---\n")

    def record_upload(self, encoded: str) -> None:
        value = str(encoded or "")
        self.event(
            "received browser upload payload",
            stage="upload",
            encoded_character_count=len(value),
            encoded_prefix=value[:24],
            encoded_suffix=value[-24:] if value else "",
            has_data_url_prefix=value.startswith("data:"),
        )

    def record_payload(self, payload: bytes) -> None:
        self.event(
            "decoded workbook payload",
            stage="container",
            byte_count=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            first_32_bytes_hex=payload[:32].hex(),
            first_16_bytes_ascii=repr(payload[:16]),
            zip_signature=payload[:4] in {b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"},
        )
        try:
            is_zip = zipfile.is_zipfile(BytesIO(payload))
        except Exception as exc:
            self.exception("zipfile.is_zipfile raised an exception", exc, stage="container")
            return
        self.event("checked ZIP container signature", stage="container", is_zipfile=is_zip)
        if not is_zip:
            return
        try:
            with zipfile.ZipFile(BytesIO(payload)) as archive:
                infos = archive.infolist()
                names = [info.filename for info in infos]
                self.event(
                    "opened ZIP central directory",
                    stage="zip",
                    member_count=len(infos),
                    comment_length=len(archive.comment),
                    duplicate_member_names=sorted({name for name in names if names.count(name) > 1}),
                    encrypted_member_count=sum(bool(info.flag_bits & 0x1) for info in infos),
                    zip_test_result="central directory readable",
                )
                for info in infos:
                    self._record_zip_member(archive, info)
        except Exception as exc:
            self.exception("could not open or inspect ZIP container", exc, stage="zip")

    def _record_zip_member(self, archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> None:
        encrypted = bool(info.flag_bits & 0x1)
        details: dict[str, Any] = {
            "name": info.filename,
            "date_time": info.date_time,
            "compress_type": info.compress_type,
            "compress_type_name": {
                zipfile.ZIP_STORED: "stored",
                zipfile.ZIP_DEFLATED: "deflated",
                zipfile.ZIP_BZIP2: "bzip2",
                zipfile.ZIP_LZMA: "lzma",
            }.get(info.compress_type, "unknown"),
            "flag_bits_hex": hex(info.flag_bits),
            "encrypted": encrypted,
            "file_size": info.file_size,
            "compressed_size": info.compress_size,
            "crc_hex": hex(info.CRC),
            "header_offset": info.header_offset,
            "extra_length": len(info.extra),
            "comment_length": len(info.comment),
            "create_system": info.create_system,
            "create_version": info.create_version,
            "extract_version": info.extract_version,
            "external_attr_hex": hex(info.external_attr),
        }
        try:
            digest = hashlib.sha256()
            read_bytes = 0
            xml_bytes = bytearray() if info.filename.lower().endswith(".xml") and info.file_size <= MAX_XML_INSPECTION_BYTES else None
            with archive.open(info, "r") as member:
                while True:
                    chunk = member.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    read_bytes += len(chunk)
                    if xml_bytes is not None:
                        xml_bytes.extend(chunk)
            details.update(read_result="read and CRC-checked", read_bytes=read_bytes, sha256=digest.hexdigest())
            if xml_bytes is not None:
                self._record_xml(info.filename, bytes(xml_bytes), details)
        except Exception as exc:
            details.update(read_result="failed", read_error=f"{type(exc).__name__}: {exc}")
            self.event("inspected ZIP member", stage="zip-member", **details)
            self.exception(f"failed while reading ZIP member {info.filename!r}", exc, stage="zip-member")
            return
        self.event("inspected ZIP member", stage="zip-member", **details)

    def _record_xml(self, name: str, payload: bytes, details: dict[str, Any]) -> None:
        try:
            root = ElementTree.fromstring(payload)
            tags: dict[str, int] = {}
            for element in root.iter():
                tag = _local_name(element.tag)
                tags[tag] = tags.get(tag, 0) + 1
            details.update(
                xml_parse_result="parsed",
                xml_root=_local_name(root.tag),
                xml_root_attributes=dict(root.attrib),
                xml_tag_counts=tags,
            )
            if name == "xl/workbook.xml":
                details["workbook_sheet_metadata"] = [
                    {key: value for key, value in element.attrib.items()}
                    for element in root.iter()
                    if _local_name(element.tag) == "sheet"
                ]
        except Exception as exc:
            details.update(xml_parse_result="failed", xml_parse_error=f"{type(exc).__name__}: {exc}")
            self.exception(f"XML parsing failed for {name!r}", exc, stage="xml")

    def close(self, outcome: str, *, error: str = "") -> None:
        self.event(
            "diagnostic finished",
            stage="lifecycle",
            outcome=outcome,
            error=error,
        )
