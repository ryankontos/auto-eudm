import unittest

from auto_eudm.max_portal import MaxPortalService, rank_request
from auto_eudm.eudm_request import EUDMError
from auto_eudm.web_models import RequestSpec


class MaxPortalTests(unittest.TestCase):
    def test_matches_only_exact_user_and_keeps_multiple_choices(self):
        rows = [
            {"reference": "DR-1", "requestedFor": "rkontos", "helixId": "INC100", "status": "TICKET_CLOSED", "__typename": "AdditionalDeviceRequest"},
            {"reference": "DR-2", "requestedFor": "rkontos2", "helixId": "INC200", "status": "PENDING_APPROVAL", "__typename": "AdditionalDeviceRequest"},
            {"reference": "DR-3", "requestedFor": "rkontos", "helixId": "INC300", "status": "PENDING_APPROVAL", "__typename": "AdditionalDeviceRequest"},
            {"reference": "DR-4", "requestedFor": "rkontos", "helixId": "INC400", "status": "USER_NOTIFIED", "__typename": "AdditionalDeviceRequest"},
        ]

        def graphql(operation, query, variables):
            if operation == "getDeviceRequests":
                self.assertEqual(variables["search"], "rkontos")
                return {"data": {"allDeviceRequests": {"edges": [{"node": row} for row in rows], "totalCount": 4, "pageInfo": {"hasNextPage": False}}}}
            reference = variables["ref"]
            return {"data": {"allAdditionalDeviceRequests": {"nodes": [
                {"reference": reference, "createdAt": "2026-09-20T10:00:00Z"}
            ]}}}

        result = MaxPortalService(graphql).matches("rkontos", deployment_date="2026-09-24")
        self.assertEqual(len(result["candidates"]), 3)
        self.assertEqual(result["suggested_reference"], "")
        self.assertEqual({item["reference"] for item in result["candidates"][:2]}, {"DR-3", "DR-4"})

    def test_unique_open_inc_can_be_suggested(self):
        def graphql(operation, query, variables):
            if operation == "getDeviceRequests":
                return {"data": {"allDeviceRequests": {"edges": [{"node": {
                    "reference": "DR-9", "requestedFor": "User1", "helixId": "INC9",
                    "status": "PENDING_APPROVAL", "__typename": "NewStarterDeviceRequest",
                }}], "totalCount": 1, "pageInfo": {"hasNextPage": False}}}}
            raise AssertionError("An uncaptured detail operation should not be sent")

        result = MaxPortalService(graphql).matches("user1")
        self.assertEqual(result["suggested_reference"], "DR-9")

    def test_old_serial_is_stronger_evidence(self):
        a = {"requestedFor": "user1", "oldSerial": "OLD123", "helixId": "INC1", "status": "USER_NOTIFIED"}
        b = {"requestedFor": "user1", "oldSerial": "OTHER", "helixId": "INC2", "status": "USER_NOTIFIED"}
        self.assertGreater(rank_request(a, "user1", old_serials=("old123",)), rank_request(b, "user1", old_serials=("old123",)))
        self.assertEqual(rank_request(a, "another-user"), -1)

    def test_association_survives_queue_and_history_model(self):
        raw = {"id": "one", "kind": "user", "serials": ["SERIAL123"], "status": "Deployed - New Stock", "user": "user1",
               "max_portal": {"reference": "DR-9", "helix_id": "INC9", "status": "USER_NOTIFIED"}}
        self.assertEqual(RequestSpec.from_json(raw).to_json()["max_portal"]["helix_id"], "INC9")

    def test_inc_search_uses_captured_list_query_and_loads_details(self):
        row = {"reference": "DR-42", "helixId": "INC1234567", "requestedFor": "rkontos",
               "__typename": "AdditionalDeviceRequest", "status": "USER_NOTIFIED"}

        def graphql(operation, query, variables):
            if operation == "GetAdditionalDeviceRequest":
                return {"data": {"allAdditionalDeviceRequests": {"nodes": [
                    {"reference": "DR-42", "additionalComments": "Replacement requested"}
                ]}}}
            self.assertEqual(operation, "getDeviceRequests")
            self.assertEqual(variables["search"], "")
            self.assertNotIn("helixId: {contains:", query)
            edges = [{"node": row}]
            return {"data": {"allDeviceRequests": {"edges": edges,
                    "totalCount": len(edges), "pageInfo": {"hasNextPage": False}}}}

        result = MaxPortalService(graphql).search("INC1234567")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["requests"][0]["additionalComments"], "Replacement requested")

    def test_bulk_incs_never_uses_unsupported_filters(self):
        row = {"reference": "DR-42", "helixId": "INC1234567", "requestedFor": "rkontos",
               "__typename": "AdditionalDeviceRequest"}

        def graphql(operation, query, variables):
            if operation == "GetAdditionalDeviceRequest":
                return {"data": {"allAdditionalDeviceRequests": {"nodes": [row]}}}
            self.assertEqual(variables.get("search"), "")
            self.assertNotIn("helixId: {contains:", query)
            edges = [{"node": row}] if variables.get("search") == "" else []
            return {"data": {"allDeviceRequests": {"edges": edges,
                    "totalCount": len(edges), "pageInfo": {"hasNextPage": False}}}}

        service = MaxPortalService(graphql)
        self.assertEqual(service.search("INC1234567")["requests"][0]["reference"], "DR-42")
        bulk = service.bulk_incs(["inc1234567", "INC9999999", "INC1234567"])
        self.assertEqual([entry["inc"] for entry in bulk["results"]], ["INC1234567", "INC9999999"])
        self.assertEqual(len(bulk["results"][0]["requests"]), 1)
        self.assertEqual(bulk["results"][1]["requests"], [])

    def test_full_name_resolves_login_and_alm_never_auto_selects_name_only(self):
        row = {"reference": "DR-22", "helixId": "INC222", "requestedFor": "anotherlogin",
               "__typename": "AdditionalDeviceRequest", "status": "USER_NOTIFIED"}

        def graphql(operation, query, variables):
            if operation == "GetAdditionalDeviceRequest":
                return {"data": {"allAdditionalDeviceRequests": {"nodes": [
                    {**row, "requestedForFullName": "Jane Smith"}
                ]}}}
            self.assertNotIn("requestedForFullName: {contains:", query)
            edges = [{"node": row}] if variables.get("search") == "anotherlogin" else []
            return {"data": {"allDeviceRequests": {"edges": edges,
                    "totalCount": len(edges), "pageInfo": {"hasNextPage": False}}}}

        service = MaxPortalService(graphql, name_logins=lambda name: ["anotherlogin"] if name == "Jane Smith" else [])
        self.assertEqual(service.search("Jane Smith")["requests"][0]["reference"], "DR-22")
        result = service.matches("differentlogin", name_hint="Jane Smith")
        self.assertEqual(result["candidates"][0]["reference"], "DR-22")
        self.assertEqual(result["suggested_reference"], "")

    def test_unexpected_execution_error_on_login_query_uses_catalogue(self):
        row = {"reference": "DR-33", "requestedFor": "rkontos", "helixId": "INC33",
               "__typename": "AdditionalDeviceRequest", "status": "USER_NOTIFIED"}

        def graphql(operation, query, variables):
            if operation == "GetAdditionalDeviceRequest":
                return {"data": {"allAdditionalDeviceRequests": {"nodes": [row]}}}
            if variables.get("search") == "rkontos":
                return {"errors": [{"message": "Unexpected Execution Error"}]}
            self.assertEqual(variables.get("search"), "")
            return {"data": {"allDeviceRequests": {"edges": [{"node": row}],
                    "totalCount": 1, "pageInfo": {"hasNextPage": False}}}}

        service = MaxPortalService(graphql)
        self.assertEqual(service.search("rkontos")["requests"][0]["reference"], "DR-33")
        self.assertEqual(service.matches("rkontos")["candidates"][0]["reference"], "DR-33")

    def test_catalogue_keeps_partial_page_if_max_later_fails(self):
        row = {"reference": "DR-44", "requestedFor": "rkontos", "helixId": "INC44"}

        def graphql(operation, query, variables):
            if variables.get("after") == "next":
                return {"errors": [{"message": "Unexpected Execution Error"}]}
            return {"data": {"allDeviceRequests": {"edges": [{"node": row}],
                    "totalCount": 2, "pageInfo": {"hasNextPage": True, "endCursor": "next"}}}}

        result = MaxPortalService(graphql).bulk_incs(["INC44", "INC45"])
        self.assertTrue(result["truncated"])
        self.assertEqual(result["results"][0]["requests"][0]["reference"], "DR-44")

    def test_auth_rejection_is_not_reported_as_no_match(self):
        def graphql(operation, query, variables):
            raise EUDMError("Max portal rejected the session. Reconnect PC Toolkit and try again.")

        with self.assertRaisesRegex(EUDMError, "Reconnect"):
            MaxPortalService(graphql).matches("rkontos")


if __name__ == "__main__":
    unittest.main()
