import copy
import base64
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from fabric_graph import inspect_indexed_relationship, numeric_id, validate_graph

HASH = "a" * 64


class GraphInspectionTests(unittest.TestCase):
    def setUp(self):
        self.schema = {
            "nodeTypes": [
                {"alias": "1", "labels": ["PayrollObligation"]},
                {"alias": "2", "labels": ["FinanceAccount"]},
            ],
            "edgeTypes": [{
                "labels": ["PayrollFundedBy"], "sourceNodeType": {"alias": "1"},
                "destinationNodeType": {"alias": "2"},
            }],
        }
        self.payroll = {
            "labels": ["PayrollObligation"], "oid": "10",
            "properties": {"task_id": "payroll-funding", "source_account_id": "hotel-reserve",
                           "obligation_cents": 2500000, "ledger_version": HASH},
        }
        self.account = {
            "labels": ["FinanceAccount"], "oid": "20",
            "properties": {"account_id": "hotel-reserve", "balance_cents": 3500000,
                           "ledger_version": HASH},
        }
        self.funding = {
            "labels": ["PayrollFundedBy"], "oid": "30", "properties": {},
            "ends": [{"oid": "10"}, {"oid": "20"}],
        }
        self.response = {
            "status": {"code": "00000"},
            "result": {"kind": "TABLE", "columns": [{"name": n} for n in (
                "payroll", "funding", "account"
            )], "data": []},
        }

    def invoke(self, response=None):
        if response is None:
            response = copy.deepcopy(self.response)
            response["result"]["data"] = [{
                "payroll": json.dumps(self.payroll), "funding": json.dumps(self.funding),
                "account": json.dumps(self.account),
            }]
        api = Mock()
        api.request.side_effect = [self.schema, response]
        result = inspect_indexed_relationship(api, "workspace", "graph", HASH)
        return api, result

    def test_actual_relationship_read_is_not_live_freshness_or_runtime_proof(self):
        api, result = self.invoke()
        self.assertTrue(result["indexed_relationship_query_verified"])
        self.assertTrue(result["all_relationships_match_requested_hash"])
        self.assertFalse(result["graph_freshness_verified"])
        self.assertFalse(result["current_ledger_revalidated"])
        self.assertEqual(result["hired_agent_access"], "not_tested")
        self.assertEqual(len(api.request.call_args_list), 2)
        self.assertTrue(api.request.call_args_list[0].args[1].endswith("?beta=true"))
        self.assertIn("/executeQuery?beta=true", api.request.call_args_list[1].args[1])
        for call in api.request.call_args_list:
            self.assertEqual(call.kwargs["skill"], "fabriciq-ontology-cli")
            self.assertNotIn("/jobs/", call.args[1])

    def test_initial_baseline_is_reported_as_not_matching(self):
        self.payroll["properties"]["ledger_version"] = "initial-baseline-not-live-ledger"
        self.account["properties"]["ledger_version"] = "initial-baseline-not-live-ledger"
        _, result = self.invoke()
        self.assertFalse(result["all_relationships_match_requested_hash"])

    def test_failed_empty_truncated_and_paginated_queries_are_rejected(self):
        for response in (
            {**self.response, "status": {"code": "42000"}},
            self.response,
            {**self.response, "continuationToken": "more"},
            {**self.response, "result": {**self.response["result"], "data": [{}] * 20}},
        ):
            with self.subTest(response=response), self.assertRaises((ValueError, RuntimeError)):
                self.invoke(response)

    def test_wrong_edge_or_account_and_inexact_money_are_rejected(self):
        self.funding["ends"].reverse()
        with self.assertRaises(ValueError):
            self.invoke()
        self.funding["ends"].reverse()
        self.account["properties"]["account_id"] = "other-account"
        with self.assertRaises(ValueError):
            self.invoke()
        self.account["properties"]["account_id"] = "hotel-reserve"
        self.account["properties"]["balance_cents"] = 3500000.0
        with self.assertRaises(ValueError):
            self.invoke()

    def test_ambiguous_schema_and_incomplete_hash_are_rejected(self):
        self.schema["edgeTypes"].append(copy.deepcopy(self.schema["edgeTypes"][0]))
        with self.assertRaises(ValueError):
            self.invoke()
        api = Mock()
        with self.assertRaises(ValueError):
            inspect_indexed_relationship(api, "workspace", "graph", HASH[:32])
        api.request.assert_not_called()

    def test_source_paths_cannot_spoof_finance_identifiers(self):
        sources = [{
            "type": "DeltaTable", "properties": {
                "path": f"abfss://workspace@onelake.pbidedicated.windows.net/lake/Tables/dbo/{table}"
            },
        } for table in ("accounts", "payroll_obligations", "transfer_policy")]
        values = {
            "dataSources.json": {"dataSources": sources},
            "graphType.json": {"edgeTypes": [{
                "labels": ["PayrollFundedBy"], "alias": numeric_id("PayrollFundedBy"),
            }]},
        }

        def definition():
            return {"definition": {"parts": [{
                "path": path, "payloadType": "InlineBase64",
                "payload": base64.b64encode(json.dumps(value).encode()).decode(),
            } for path, value in values.items()]}}

        validate_graph(definition(), "workspace", "lake")
        for path in (
            "abfss://workspace@other.example/lake/Tables/dbo/accounts",
            "abfss://workspace@onelake.pbidedicated.windows.net/lake/Tables/dbo/accounts/extra",
            "abfss://workspace@onelake.pbidedicated.windows.net/lake/Tables/dbo/transfers",
        ):
            sources[0]["properties"]["path"] = path
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate_graph(definition(), "workspace", "lake")


if __name__ == "__main__":
    unittest.main()
