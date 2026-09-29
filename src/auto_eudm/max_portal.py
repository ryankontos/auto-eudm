"""Read-only Max portal device requests, based on the captured GraphQL API."""

from __future__ import annotations

from datetime import date
import re
import threading
import time
from typing import Any, Callable

from .eudm_request import EUDMError


LIST_QUERY = """query getDeviceRequests($types: [DeviceRequestTypeEnum!], $first: Int, $last: Int, $before: String, $after: String, $search: String, $filter: [DeviceRequestFilterInput!]) {
  allDeviceRequests(types: $types, first: $first, last: $last, before: $before, after: $after,
    where: {and: [{or: [{reference: {contains: $search}}, {requestedFor: {contains: $search}}]}, {or: $filter}]}) {
    pageInfo { startCursor endCursor hasNextPage }
    edges { node { __typename reference requestedFor requestedForLocation newDeviceType requestedBy helixId workflowInstanceId status } }
    totalCount
  }
}"""

DETAIL_FIELDS = """__typename reference workflowInstanceId oldManufacturer oldModel oldSerial oldDeviceType
  oldDeviceTypeDetails { title } newDeviceType newDeviceTypeDetails { title }
  helixId status requestedBy requestedByFullName requestedFor requestedForFullName
  createdAt updatedAt updatedBy"""
DETAIL_QUERIES = {
    "EarlyReplacementDeviceRequest": (
        "GetEarlyReplacementDeviceRequest",
        f"query GetEarlyReplacementDeviceRequest($ref: String) {{ allEarlyReplacementDeviceRequests(where: {{reference: {{eq: $ref}}}}) {{ nodes {{ {DETAIL_FIELDS} }} totalCount }} }}",
        "allEarlyReplacementDeviceRequests",
    ),
    "WarrantyExpiryReplacementDeviceRequest": (
        "GetWarrantyExpiryReplacementDeviceRequest",
        f"query GetWarrantyExpiryReplacementDeviceRequest($ref: String) {{ allWarrantyExpiryReplacementDeviceRequests(where: {{reference: {{eq: $ref}}}}) {{ nodes {{ {DETAIL_FIELDS} }} totalCount }} }}",
        "allWarrantyExpiryReplacementDeviceRequests",
    ),
    "AdditionalDeviceRequest": (
        "GetAdditionalDeviceRequest",
        """query GetAdditionalDeviceRequest($ref: String) {
          allAdditionalDeviceRequests(where: {reference: {eq: $ref}}) {
            nodes { __typename reference workflowInstanceId newDeviceType helixId status
              requestedBy requestedFor createdAt updatedAt updatedBy additionalComments }
            totalCount
          }
        }""",
        "allAdditionalDeviceRequests",
    ),
}

INACTIVE_STATUSES = {"TICKET_CLOSED", "CANCELLED_FOR_RETRY", "CANCELLED_PERMANENT"}
GENERIC_DETAIL_QUERY = """query getDeviceRequests($ref: String) {
  allDeviceRequests(where: {reference: {eq: $ref}}) {
    edges { node { __typename reference requestedBy requestedFor helixId workflowInstanceId status } }
    totalCount
  }
}"""

INC_PATTERN = re.compile(r"INC[0-9]+", re.IGNORECASE)
CATALOGUE_TTL_SECONDS = 90
CATALOGUE_MAX_PAGES = 40
NAME_DETAIL_SCAN_LIMIT = 150


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def _day(value: Any) -> date | None:
    try:
        return date.fromisoformat(_clean(value)[:10])
    except ValueError:
        return None


def rank_request(
    item: dict[str, Any], username: str, *, deployment_date: str = "",
    old_serials: tuple[str, ...] = (), device_hint: str = "",
) -> int:
    """Rank evidence without treating a loose name match as an exact user."""
    wanted = _clean(username).casefold()
    if not wanted or _clean(item.get("requestedFor")).casefold() != wanted:
        return -1
    score = 100
    if _clean(item.get("helixId")).upper().startswith("INC"):
        score += 25
    if _clean(item.get("status")).upper() not in INACTIVE_STATUSES:
        score += 35
    old = _clean(item.get("oldSerial")).casefold()
    if old and old in {_clean(value).casefold() for value in old_serials}:
        score += 70
    hint = _clean(device_hint).casefold()
    request_type = _clean(item.get("newDeviceTypeDetails", {}).get("title") if isinstance(item.get("newDeviceTypeDetails"), dict) else "")
    request_type = request_type or _clean(item.get("newDeviceType"))
    if hint and request_type and (hint in request_type.casefold() or request_type.casefold() in hint):
        score += 30
    requested = _day(item.get("createdAt"))
    deployed = _day(deployment_date)
    if requested and deployed:
        gap = (deployed - requested).days
        if -3 <= gap <= 60:
            score += max(0, 35 - abs(gap))
    return score


class MaxPortalService:
    def __init__(
        self,
        graphql: Callable[[str, str, dict[str, Any]], dict[str, Any]],
        name_logins: Callable[[str], list[str]] | None = None,
    ) -> None:
        self.graphql = graphql
        self.name_logins = name_logins
        self._catalogue_lock = threading.Lock()
        self._catalogue_rows: list[dict[str, Any]] = []
        self._catalogue_at = 0.0
        self._catalogue_truncated = False
        self._detail_lock = threading.Lock()
        self._detail_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}

    def _query(self, operation: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        response = self.graphql(operation, query, variables)
        if not isinstance(response, dict):
            raise EUDMError("Max portal returned an invalid response.")
        errors = response.get("errors")
        if errors:
            message = _clean(errors[0].get("message")) if isinstance(errors[0], dict) else ""
            raise EUDMError(f"Max portal request failed: {message or 'GraphQL error'}")
        data = response.get("data")
        if not isinstance(data, dict):
            raise EUDMError("Max portal did not return request data.")
        return data

    def _list_page(self, query: str, *, after: str = "", first: int = 50) -> dict[str, Any]:
        data = self._query("getDeviceRequests", LIST_QUERY, {
            "types": [], "first": min(50, max(1, int(first))), "last": None,
            "before": None, "after": after or None, "search": query, "filter": [],
        })
        connection = data.get("allDeviceRequests") or {}
        edges = connection.get("edges") or []
        return {
            "requests": [edge["node"] for edge in edges if isinstance(edge, dict) and isinstance(edge.get("node"), dict)],
            "total": int(connection.get("totalCount") or 0),
            "page_info": connection.get("pageInfo") or {},
        }

    def _all_pages(self, query: str, *, max_pages: int = CATALOGUE_MAX_PAGES) -> tuple[list[dict[str, Any]], bool]:
        rows: list[dict[str, Any]] = []
        cursor = ""
        seen_cursors: set[str] = set()
        for _ in range(max_pages):
            try:
                page = self._list_page(query, after=cursor)
            except EUDMError as exc:
                if rows and "unexpected execution error" in str(exc).casefold():
                    return rows, True
                raise
            rows.extend(page["requests"])
            info = page["page_info"]
            if not info.get("hasNextPage"):
                return rows, False
            next_cursor = _clean(info.get("endCursor"))
            if not next_cursor or next_cursor in seen_cursors:
                return rows, True
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return rows, True

    def _catalogue(self) -> tuple[list[dict[str, Any]], bool]:
        with self._catalogue_lock:
            if self._catalogue_at and time.monotonic() - self._catalogue_at < CATALOGUE_TTL_SECONDS:
                return list(self._catalogue_rows), self._catalogue_truncated
            rows, truncated = self._all_pages("")
            self._catalogue_rows = rows
            self._catalogue_truncated = truncated
            self._catalogue_at = time.monotonic()
            return list(rows), truncated

    def _username_rows(self, username: str) -> tuple[list[dict[str, Any]], bool]:
        try:
            rows, incomplete = self._all_pages(username, max_pages=4)
        except EUDMError as exc:
            if "unexpected execution error" not in str(exc).casefold():
                raise
            rows, incomplete = self._catalogue()
        return ([row for row in rows if _clean(row.get("requestedFor")).casefold() == username.casefold()],
                incomplete)

    @staticmethod
    def _unique(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        for row in rows:
            reference = _clean(row.get("reference"))
            if reference:
                found[reference] = {**found.get(reference, {}), **row}
        return list(found.values())

    def search(self, query: str = "", *, after: str = "", first: int = 50) -> dict[str, Any]:
        query = _clean(query)
        if len(query) > 120:
            raise EUDMError("Search text is too long.")
        if not query or after:
            return self._list_page(query, after=after, first=first)
        wanted = query.casefold()
        if INC_PATTERN.fullmatch(query):
            catalogue, incomplete = self._catalogue()
            rows = [row for row in self._unique(catalogue)
                    if _clean(row.get("helixId")).casefold() == wanted
                    or _clean(row.get("reference")).casefold() == wanted]
            return {"requests": [self._with_detail(row) for row in rows],
                    "total": len(rows), "page_info": {}, "truncated": incomplete}
        if " " in query:
            page = {"requests": [], "total": 0, "page_info": {}}
        else:
            try:
                page = self._list_page(query, first=first)
            except EUDMError as exc:
                if "unexpected execution error" not in str(exc).casefold():
                    raise
                catalogue, incomplete = self._catalogue()
                rows = [row for row in catalogue if any(wanted in _clean(row.get(field)).casefold()
                        for field in ("reference", "requestedFor"))]
                return {"requests": rows, "total": len(rows), "page_info": {}, "truncated": incomplete}
        if page["requests"] and " " not in query:
            return page

        rows = list(page["requests"])
        name_match_refs: set[str] = set()
        aliases: list[str] = []
        if self.name_logins is not None:
            try:
                aliases = self.name_logins(query)[:10]
            except EUDMError:
                pass  # Helix is optional for a Max portal name lookup.
        incomplete = False
        for username in aliases:
            matched_aliases, alias_incomplete = self._username_rows(username)
            name_match_refs.update(_clean(row.get("reference")) for row in matched_aliases)
            rows.extend(matched_aliases)
            incomplete = incomplete or alias_incomplete
        if not rows:
            catalogue, catalogue_incomplete = self._catalogue()
            rows = catalogue[:NAME_DETAIL_SCAN_LIMIT]
            incomplete = incomplete or catalogue_incomplete or len(catalogue) > NAME_DETAIL_SCAN_LIMIT
        detailed = [self._with_detail(row) for row in self._unique(rows)]
        incomplete = incomplete or any(row.get("detail_error") for row in detailed)
        for row in detailed:
            row["name_matched"] = _clean(row.get("reference")) in name_match_refs or (
                _clean(row.get("requestedForFullName")).casefold() == wanted
            )
        matched = [row for row in detailed if any(
            wanted in _clean(row.get(field)).casefold()
            for field in ("requestedForFullName", "requestedFor", "requestedByFullName", "requestedBy", "reference", "helixId")
        ) or row["name_matched"]]
        return {"requests": matched, "total": len(matched), "page_info": {}, "truncated": incomplete}

    def _with_detail(self, row: dict[str, Any]) -> dict[str, Any]:
        reference = _clean(row.get("reference"))
        if not reference:
            return row
        request_type = _clean(row.get("__typename"))
        key = (reference, request_type)
        with self._detail_lock:
            cached = self._detail_cache.get(key)
            if cached and time.monotonic() - cached[0] < CATALOGUE_TTL_SECONDS:
                return {**row, **cached[1], "detail_loaded": bool(cached[1])}
        try:
            detail = self.detail(reference, request_type)
        except EUDMError as exc:
            if "reconnect" in str(exc).casefold() or "rejected the session" in str(exc).casefold():
                raise
            return {**row, "detail_error": str(exc)}
        with self._detail_lock:
            self._detail_cache[key] = (time.monotonic(), detail)
        return {**row, **detail, "detail_loaded": bool(detail)}

    def bulk_incs(self, incs: list[str]) -> dict[str, Any]:
        if len(incs) > 200 or any(not INC_PATTERN.fullmatch(_clean(inc)) for inc in incs):
            raise EUDMError("Enter up to 200 valid INC numbers.")
        unique = list(dict.fromkeys(_clean(inc).upper() for inc in incs))
        if not unique:
            raise EUDMError("Enter at least one INC number.")
        catalogue, incomplete = self._catalogue()
        results = []
        for inc in unique:
            rows = [row for row in catalogue if _clean(row.get("helixId")).upper() == inc]
            results.append({"inc": inc, "requests": [self._with_detail(row) for row in self._unique(rows)]})
        return {"results": results, "truncated": incomplete}

    def detail(self, reference: str, request_type: str) -> dict[str, Any]:
        reference = _clean(reference)
        if not reference or len(reference) > 100:
            raise EUDMError("Choose a valid Max portal request reference.")
        if request_type not in DETAIL_QUERIES:
            # Some request types have no captured type-specific detail query;
            # the portal's generic reference lookup still returns core fields.
            data = self._query("getDeviceRequests", GENERIC_DETAIL_QUERY, {"ref": reference})
            edges = (data.get("allDeviceRequests") or {}).get("edges") or []
            return next((edge["node"] for edge in edges
                         if isinstance(edge, dict) and isinstance(edge.get("node"), dict)
                         and edge["node"].get("reference") == reference), {})
        operation, query, root = DETAIL_QUERIES[request_type]
        data = self._query(operation, query, {"ref": reference})
        nodes = (data.get(root) or {}).get("nodes") or []
        return next((node for node in nodes if isinstance(node, dict) and node.get("reference") == reference), {})

    def matches(
        self, username: str, *, deployment_date: str = "",
        old_serials: tuple[str, ...] = (), device_hint: str = "",
        name_hint: str = "",
    ) -> dict[str, Any]:
        username = _clean(username)
        if len(username) < 2:
            return {"candidates": [], "suggested_reference": ""}
        exact, incomplete = self._username_rows(username)
        exact = exact[:200]
        if not exact and _clean(name_hint):
            name = _clean(name_hint).casefold()
            by_name = self.search(name_hint)
            candidates = [row for row in by_name["requests"]
                          if row.get("name_matched") or _clean(row.get("requestedForFullName")).casefold() == name]
            return {"candidates": candidates, "suggested_reference": "",
                    "truncated": bool(by_name.get("truncated"))}
        ordered = sorted(exact, key=lambda row: (
            _clean(row.get("status")).upper() not in INACTIVE_STATUSES,
            _clean(row.get("helixId")).upper().startswith("INC"),
        ), reverse=True)
        enrich_refs = {_clean(row.get("reference")) for row in ordered[:6]}
        detailed = []
        for row in exact[:50]:
            detailed.append(self._with_detail(row) if _clean(row.get("reference")) in enrich_refs else row)
        detailed.sort(key=lambda item: (rank_request(
            item, username, deployment_date=deployment_date, old_serials=old_serials,
            device_hint=device_hint,
        ), _clean(item.get("createdAt"))), reverse=True)
        # Only choose automatically if the result is unambiguous. An INC still
        # needs human review before it can later be closed in Smart IT.
        active = [item for item in detailed if _clean(item.get("helixId")).upper().startswith("INC") and _clean(item.get("status")).upper() not in INACTIVE_STATUSES]
        scores = [rank_request(item, username, deployment_date=deployment_date,
                               old_serials=old_serials, device_hint=device_hint)
                  for item in active]
        clear_lead = len(scores) > 1 and scores[0] >= 190 and scores[0] - scores[1] >= 20
        suggested = (
            _clean(active[0].get("reference"))
            if len(exact) <= 50 and (len(active) == 1 or clear_lead)
            else ""
        )
        return {"candidates": detailed, "suggested_reference": suggested,
                "truncated": len(exact) > 50 or incomplete}
