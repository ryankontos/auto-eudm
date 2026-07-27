"""Pure request and spreadsheet models used by the AutoEUDM web interface."""

from __future__ import annotations

import base64
from collections import Counter
from dataclasses import dataclass
from datetime import date
from io import BytesIO
import re
from typing import Any
import uuid

from . import eudm_inventory_import as inventory
from . import eudm_request as eudm


USER_STATUSES: tuple[tuple[str, str], ...] = (
    ("Deployed - Existing Stock", "Deployed - Existing Stock"),
    ("Deployed - New Stock", "Deployed - New Stock"),
    ("Deployed - Loan", "Loan"),
    ("Deployed - Pending Return", "Pending Return"),
    ("Deployed - Unmanaged", "Unmanaged"),
)

LOCATION_STATUSES: tuple[tuple[str, str], ...] = (
    ("Donated", "Donated"),
    ("Hold", "Hold"),
    ("New Stock", "New Stock"),
    ("Loan Stock", "Loan Stock"),
    ("Used Stock", "Used Stock"),
    ("Pending Decom", "Pending Decom"),
    ("Pending Disposal", "Pending Disposal"),
    ("Pending Pickup", "Pending Pickup"),
    ("Pending Repair", "Pending Repair"),
    ("Pending Rebuild", "Pending Rebuild"),
    ("Under Repair", "Under Repair"),
    ("Vendor Collected", "Vendor Collected"),
    ("Stolen/Lost", "Stolen/Lost"),
)

CITIES: tuple[str, ...] = (
    "Bangkok, TH",
    "Beijing, CN",
    "Brisbane, AU",
    "Calgary, CA",
    "Chicago, US",
    "Dubai, AE",
    "Dublin, IE",
    "Frankfurt, DE",
    "Geneva, CH",
    "Gurugram, IN",
    "Hong Kong, CN",
    "Houston, US",
    "Hyderabad, IN",
    "Jacksonville, US",
    "Jakarta, ID",
    "Kansas City, US",
    "Kuala Lumpur, MY",
    "London, UK",
    "Los Angeles, US",
    "Luxembourg, LU",
    "Manila, PH",
    "Melbourne, AU",
    "Mexico City, MX",
    "Minneapolis, US",
    "Mumbai, IN",
    "New York, US",
    "Paris, FR",
    "Perth, AU",
    "Philadelphia, US",
    "San Diego, US",
    "San Jose, US",
    "Santiago, Chile",
    "Sao Paulo, BR",
    "Seoul, KR",
    "Shanghai, CN",
    "Singapore, SG",
    "Sydney, AU",
    "Taipei, TW",
    "Tokyo, JP",
    "Toronto, CA",
)

SERIAL_PATTERN = re.compile(r"[A-Za-z0-9._-]+")


def clean(value: Any) -> str:
    return str(value or "").strip()


def parse_serials(value: Any) -> list[str]:
    if isinstance(value, list):
        raw = [clean(item) for item in value]
    else:
        raw = [
            item.strip()
            for item in re.split(r"[\s,;]+", clean(value))
        ]
    return [item for item in raw if item]


@dataclass(frozen=True)
class Location:
    city: str
    building: str
    floor: str
    room: str
    cabinet: str | None = None

    @classmethod
    def from_json(cls, raw: Any) -> "Location":
        source = raw if isinstance(raw, dict) else {}
        return cls(
            city=clean(source.get("city")),
            building=clean(source.get("building")),
            floor=clean(source.get("floor")),
            room=clean(source.get("room")),
            cabinet=clean(source.get("cabinet")) or None,
        )

    def display(self) -> str:
        return " → ".join(
            value
            for value in (
                self.city,
                self.building,
                self.floor,
                self.room,
                self.cabinet,
            )
            if value
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "city": self.city,
            "building": self.building,
            "floor": self.floor,
            "room": self.room,
            "cabinet": self.cabinet or "",
            "display": self.display(),
        }


@dataclass(frozen=True)
class RequestSpec:
    client_id: str
    kind: str
    serials: tuple[str, ...]
    status: str
    user: str | None
    returning_requested: bool
    returning_user: str | None
    return_confirmed: bool
    returning_user_info: dict[str, Any] | None
    location: Location | None
    group: str
    source: str | None = None

    @classmethod
    def from_json(cls, raw: Any) -> "RequestSpec":
        if not isinstance(raw, dict):
            raise eudm.EUDMError("Each queue entry must be an object.")
        kind = clean(raw.get("kind"))
        location = (
            Location.from_json(raw.get("location"))
            if kind in {"location", "bulk_location"}
            else None
        )
        return cls(
            client_id=clean(raw.get("id")) or uuid.uuid4().hex,
            kind=kind,
            serials=tuple(parse_serials(raw.get("serials"))),
            status=clean(raw.get("status")),
            user=clean(raw.get("user")) or None,
            returning_requested=bool(
                raw.get("returning") or clean(raw.get("returning_user"))
            ),
            returning_user=clean(raw.get("returning_user")) or None,
            # The web UI shows return details in the request editor and again
            # in final review. That visible review replaces the old per-row
            # confirmation checkbox while the core API still receives the
            # explicit confirmation flag it requires.
            return_confirmed=True,
            returning_user_info=(
                {
                    "login": clean(raw.get("returning_user_info", {}).get("login")),
                    "columns": [clean(value) for value in raw.get("returning_user_info", {}).get("columns", []) if clean(value)],
                }
                if isinstance(raw.get("returning_user_info"), dict)
                else None
            ),
            location=location,
            group=clean(raw.get("group")) or "Requests",
            source=clean(raw.get("source")) or None,
        )

    def validate(
        self,
        *,
        user_statuses: set[str] | None = None,
        location_statuses: set[str] | None = None,
    ) -> list[str]:
        errors: list[str] = []
        if self.kind not in {"user", "location", "bulk_location"}:
            return ["Choose Deploy to user, Add to location stock, or Bulk add to location stock."]
        if not self.serials:
            errors.append("Enter at least one serial number.")
        if self.kind != "bulk_location" and len(self.serials) != 1:
            errors.append("This request must contain exactly one serial number.")
        duplicate_serials = [
            serial
            for serial, count in Counter(
                item.casefold() for item in self.serials
            ).items()
            if count > 1
        ]
        if duplicate_serials:
            errors.append("Remove duplicate serial numbers from this request.")
        for serial in self.serials:
            if not SERIAL_PATTERN.fullmatch(serial):
                errors.append(
                    f"Serial {serial!r} can contain only letters, numbers, dot, dash, and underscore."
                )
                break
            if len(serial) < 6:
                errors.append(f"Serial {serial!r} must contain at least six characters.")
                break

        if self.kind == "user":
            allowed = user_statuses or {value for _, value in USER_STATUSES}
            if self.status not in allowed:
                errors.append("Choose a valid status for Deploy to user.")
            if not self.user:
                errors.append("Choose the user receiving the device.")
            elif not re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]*", self.user):
                errors.append("The receiving user must be a login ID, not a display name or email address.")
            if self.returning_requested:
                errors.append("Deploy to user cannot have a returning user.")
            if self.location:
                errors.append("Deploy to user cannot include a location.")
        else:
            allowed = location_statuses or {
                value for _, value in LOCATION_STATUSES
            }
            if self.status not in allowed:
                errors.append("Choose a valid status for Add to location stock.")
            if self.user:
                errors.append("Add to location stock cannot include a deployed-to user.")
            if not self.location:
                errors.append("Choose a location.")
            elif not all(
                (
                    self.location.city,
                    self.location.building,
                    self.location.floor,
                    self.location.room,
                )
            ):
                errors.append("Choose both the city and the location.")
            if self.returning_requested and not self.returning_user:
                errors.append("Choose the returning user or turn off the return option.")
            elif self.returning_user and not re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]*", self.returning_user):
                errors.append("The returning user must be a login ID, not a display name or email address.")
            if self.returning_user and not self.returning_user_info:
                errors.append("Search and verify the returning user's details before submitting; an email will be sent to them.")
            if self.kind == "bulk_location" and self.returning_requested:
                errors.append(
                    "Bulk add to location stock cannot include a returning user."
                )
        return errors

    def device_count(self) -> int:
        return len(self.serials)

    def destination(self) -> str:
        if self.kind == "user":
            return self.user or ""
        return self.location.display() if self.location else ""

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.client_id,
            "kind": self.kind,
            "serials": list(self.serials),
            "status": self.status,
            "user": self.user or "",
            "returning": self.returning_requested,
            "returning_user": self.returning_user or "",
            "return_confirmed": self.return_confirmed,
            "returning_user_info": self.returning_user_info or None,
            "location": self.location.to_json() if self.location else None,
            "group": self.group,
            "source": self.source or "",
            "errors": self.validate(),
            "destination": self.destination(),
            "device_count": self.device_count(),
        }


def validate_queue(
    specs: list[RequestSpec],
    request_for: str,
    *,
    user_statuses: set[str] | None = None,
    location_statuses: set[str] | None = None,
) -> dict[str, list[str]]:
    errors: dict[str, list[str]] = {}
    for spec in specs:
        spec_errors = spec.validate(
            user_statuses=user_statuses,
            location_statuses=location_statuses,
        )
        if spec_errors:
            errors[spec.client_id] = spec_errors
    requester = clean(request_for)
    if not requester:
        errors["_queue"] = ["Set EUDM_REQUEST_FOR or enter a requesting login ID."]
    elif any(character.isspace() for character in requester):
        errors["_queue"] = ["The requesting login ID cannot contain whitespace."]
    if not specs:
        errors.setdefault("_queue", []).append("Add at least one request.")

    owners: dict[str, list[str]] = {}
    spellings: dict[str, str] = {}
    for spec in specs:
        for serial in spec.serials:
            key = serial.casefold()
            owners.setdefault(key, []).append(spec.client_id)
            spellings.setdefault(key, serial)
    for key, client_ids in owners.items():
        if len(client_ids) > 1:
            message = (
                f"Serial {spellings[key]} appears in more than one queued request."
            )
            for client_id in client_ids:
                errors.setdefault(client_id, []).append(message)
    return errors


@dataclass
class WorkbookImport:
    import_id: str
    filename: str
    sheets: dict[str, list[inventory.SheetRow]]

    @classmethod
    def from_upload(cls, filename: str, encoded: str) -> "WorkbookImport":
        if not filename.lower().endswith((".xlsx", ".xlsm")):
            raise eudm.EUDMError("Choose an .xlsx or .xlsm workbook.")
        try:
            payload = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise eudm.EUDMError("The uploaded workbook data was invalid.") from exc
        if not payload:
            raise eudm.EUDMError("The uploaded workbook was empty.")
        if len(payload) > 30 * 1024 * 1024:
            raise eudm.EUDMError("The workbook is larger than the 30 MB local limit.")
        try:
            from openpyxl import load_workbook
            workbook = load_workbook(
                BytesIO(payload), data_only=True, read_only=False
            )
        except Exception as exc:
            raise eudm.EUDMError(
                "Could not read the workbook. Use an unencrypted .xlsx or .xlsm file."
            ) from exc
        sheets: dict[str, list[inventory.SheetRow]] = {}
        try:
            for sheet in workbook.worksheets:
                rows: list[inventory.SheetRow] = []
                for values in sheet.iter_rows(
                    min_row=2, min_col=1, max_col=12
                ):
                    deployment_date = inventory.normalize_date(
                        values[0].value, workbook.epoch
                    )
                    if deployment_date is None:
                        continue
                    rows.append(
                        inventory.SheetRow(
                            row_number=values[0].row,
                            deployment_date=deployment_date,
                            username=inventory.username_for(values),
                            new_serial=inventory.clean_text(values[9].value),
                            old_serial=inventory.clean_text(values[11].value),
                            marked_red=any(
                                inventory.cell_is_red(cell) for cell in values
                            ),
                        )
                    )
                if rows:
                    sheets[sheet.title] = rows
        finally:
            workbook.close()
        if not sheets:
            raise eudm.EUDMError(
                "No dated data rows were found in columns A-L of any sheet."
            )
        return cls(uuid.uuid4().hex, filename, sheets)

    def summary(self) -> dict[str, Any]:
        sheet_summaries = []
        for name, rows in self.sheets.items():
            dates = []
            for selected in sorted(
                {row.deployment_date for row in rows}, reverse=True
            ):
                new_count, return_count = inventory.eligible_counts(
                    row for row in rows if row.deployment_date == selected
                )
                dates.append(
                    {
                        "value": selected.isoformat(),
                        "label": selected.strftime("%A %-d %B %Y"),
                        "new_count": new_count,
                        "return_count": return_count,
                    }
                )
            sheet_summaries.append({"name": name, "dates": dates})
        default_sheet = (
            "Bookings 2026"
            if "Bookings 2026" in self.sheets
            else next(iter(self.sheets))
        )
        return {
            "import_id": self.import_id,
            "filename": self.filename,
            "default_sheet": default_sheet,
            "sheets": sheet_summaries,
        }

    def prepare(
        self,
        sheet_name: str,
        selected_date: str,
        mode: str,
        default_location: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if sheet_name not in self.sheets:
            raise eudm.EUDMError("Choose one of the workbook's available sheets.")
        if mode not in {"new", "returns", "both"}:
            raise eudm.EUDMError("Choose new devices, returns, or both.")
        try:
            chosen_date = date.fromisoformat(selected_date)
        except ValueError as exc:
            raise eudm.EUDMError("Choose a valid deployment date.") from exc
        actions, ignored = inventory.build_actions(
            self.sheets[sheet_name], chosen_date, mode
        )
        if not actions:
            raise eudm.EUDMError(
                "No deployable rows remain for that date and selection."
            )
        requests = []
        for action in actions:
            is_return = action.group == "Pending returns"
            requests.append(RequestSpec(
                client_id=uuid.uuid4().hex,
                kind="location" if is_return else "user",
                serials=(action.serial,),
                status=("Used Stock" if is_return else action.status),
                user=None if is_return else action.username,
                returning_requested=is_return and bool(action.username),
                returning_user=action.username if is_return else None,
                return_confirmed=True,
                returning_user_info=None,
                location=Location.from_json(default_location) if is_return and default_location else None,
                group=action.group,
                source=f"{self.filename} · {sheet_name}",
            ).to_json())
        requests.sort(
            key=lambda request: 0
            if request["group"] == "New deployments"
            else 1
        )
        return {
            "requests": requests,
            "ignored": [
                {"reason": reason, "count": count}
                for reason, count in sorted(ignored.items())
            ],
            "counts": {
                "requests": len(requests),
                "new": sum(
                    request["group"] == "New deployments"
                    for request in requests
                ),
                "returns": sum(
                    request["group"] == "Pending returns"
                    for request in requests
                ),
            },
        }
