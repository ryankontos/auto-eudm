import unittest

from auto_eudm.max_portal import MaxPortalService, rank_request
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


if __name__ == "__main__":
    unittest.main()
