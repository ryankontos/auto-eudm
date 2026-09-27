"""Read-only Max portal device requests, based on the captured GraphQL API."""

from __future__ import annotations

from datetime import date
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
    def __init__(self, graphql: Callable[[str, str, dict[str, Any]], dict[str, Any]]) -> None:
        self.graphql = graphql

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

    def search(self, query: str = "", *, after: str = "", first: int = 50) -> dict[str, Any]:
        query = _clean(query)
        if len(query) > 120:
            raise EUDMError("Search text is too long.")
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
    ) -> dict[str, Any]:
        username = _clean(username)
        if len(username) < 2:
            return {"candidates": [], "suggested_reference": ""}
        page = self.search(username)
        rows = list(page["requests"])
        after = (page.get("page_info") or {}).get("endCursor")
        while (page.get("page_info") or {}).get("hasNextPage") and after and len(rows) < 200:
            page = self.search(username, after=after)
            rows.extend(page["requests"])
            next_after = (page.get("page_info") or {}).get("endCursor")
            if next_after == after:
                break
            after = next_after
        exact = [row for row in rows if _clean(row.get("requestedFor")).casefold() == username.casefold()]
        exact = exact[:200]
        ordered = sorted(exact, key=lambda row: (
            _clean(row.get("status")).upper() not in INACTIVE_STATUSES,
            _clean(row.get("helixId")).upper().startswith("INC"),
        ), reverse=True)
        enrich_refs = {_clean(row.get("reference")) for row in ordered[:6]}
        detailed = []
        for row in exact[:50]:
            try:
                detail = self.detail(_clean(row.get("reference")), _clean(row.get("__typename"))) if _clean(row.get("reference")) in enrich_refs else {}
                detailed.append({**row, **detail, "detail_loaded": bool(detail)})
            except EUDMError:
                detailed.append(row)
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
        return {"candidates": detailed, "suggested_reference": suggested, "truncated": len(exact) > 50}
