import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from fabric_rest import parse_response
from provision_onelake_reader import grant_reader, merge_roles, reader_role

ID = "00000000-0000-4000-8000-000000000001"


class ReaderPermissionTests(unittest.TestCase):
    def setUp(self):
        self.desired = reader_role(ID, ID, ID, ID)
        self.current = {"value": [{
            "name": "ExistingRole", "id": ID, "etag": '"old"',
            "members": {}, "decisionRules": [],
        }]}

    def test_scope_is_only_eight_tables_and_one_manifest_without_writes(self):
        rules = self.desired["decisionRules"][0]["permission"]
        self.assertEqual(rules[0]["attributeValueIncludedIn"], ["Read"])
        paths = rules[1]["attributeValueIncludedIn"]
        self.assertEqual(len(paths), 9)
        self.assertNotIn("*", paths)
        self.assertIn("/Files/finance_projection_manifest.json", paths)
        merged = merge_roles(self.current, self.desired)
        self.assertEqual(merged["value"][0]["name"], "ExistingRole")
        self.assertIn("etag", self.current["value"][0])
        self.assertNotIn("etag", merged["value"][0])

    def test_conflicting_role_members_and_duplicates_are_not_adopted(self):
        other = copy.deepcopy(self.desired)
        other["members"] = {}
        with self.assertRaises(PermissionError):
            merge_roles({"value": [other]}, self.desired)
        with self.assertRaises(ValueError):
            merge_roles({"value": [self.desired, self.desired]}, self.desired)

    def test_quoted_collection_etag_is_not_replaced_by_a_body_etag(self):
        status, headers, _ = parse_response(
            '{"value":[{"etag":"body-only"}]}',
            """INFO: Response status: 200
INFO: Response headers:
INFO:     'ETag': '"collection-token"'
INFO: Response content:
INFO: {"value":[{"etag":"body-only"}]}
""",
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["etag"], '"collection-token"')

    def test_conditional_dry_run_write_preservation_and_readback(self):
        api = Mock()
        body = merge_roles(self.current, self.desired)
        readback = copy.deepcopy(body)
        readback["value"][-1]["id"] = ID
        readback["value"][-1]["members"]["microsoftEntraMembers"][0].pop("objectType")
        api.request.side_effect = [
            {"type": "Lakehouse", "description": f"finance-analytics-bootstrap/v1/{ID}/dev"}, readback,
        ]
        api.raw.side_effect = [
            (200, {"etag": '"collection-token"'}, self.current),
            (200, {}, {}), (200, {}, {}),
        ]
        result = grant_reader(api, tenant=ID, workspace=ID, lakehouse=ID, principal=ID, environment="dev")
        self.assertTrue(result["changed"])
        self.assertFalse(result["runtimeAccessVerified"])
        for call in api.raw.call_args_list[1:]:
            self.assertEqual(call.kwargs["headers"], {"If-Match": '"collection-token"'})
        self.assertTrue(api.raw.call_args_list[1].args[1].endswith("?dryRun=true"))

    def test_existing_correct_grant_is_noop_and_wrong_item_is_rejected(self):
        api = Mock()
        api.request.return_value = {"type": "Lakehouse", "description": f"finance-analytics-bootstrap/v1/{ID}/dev"}
        api.raw.return_value = (200, {"etag": '"token"'}, {"value": [{"id": ID, **self.desired}]})
        self.assertFalse(grant_reader(
            api, tenant=ID, workspace=ID, lakehouse=ID, principal=ID, environment="dev",
        )["changed"])
        api.raw.assert_called_once()
        api.reset_mock()
        api.request.return_value = {"type": "Lakehouse", "description": "Unrelated"}
        with self.assertRaises(PermissionError):
            grant_reader(api, tenant=ID, workspace=ID, lakehouse=ID, principal=ID, environment="dev")
        api.raw.assert_not_called()
