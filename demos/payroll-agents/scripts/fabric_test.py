"""Source-bound standard-library tests; no network, installations or ledger writes."""
import copy
import base64
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from payroll_agents.ledger import Ledger, LedgerConfig, Snapshot, Decision, Principal, AgentRole
from fabric_rest import FabricRest, parse_response, redact
from provision_fabric import (
    Provisioner, baseline_tables, dataagent_definition, decoded,
    notebook_definition, ontology_definition, parts, validate_sources, table_schemas,
    REFRESH_EXTRA_COLUMNS,
)
from fabric_refresh import (
    validate_snapshot, project_snapshot, refresh_notebook, token_identity, projection_agent_policy,
    REFRESH_SCHEMAS,
)
from fabric_graph import graph_agent_definition
from payroll_agents import projection


CONFIG = {"tenantId": "test-tenant", "subscriptionId": "test-subscription",
          "managerUpn": "operator@example.invalid", "hotelOwnerId": "hotel-001",
          "approverUpns": ["one@example.invalid", "two@example.invalid"]}
APPROVERS = [{"id": "one", "userPrincipalName": CONFIG["approverUpns"][0]},
             {"id": "two", "userPrincipalName": CONFIG["approverUpns"][1]}]


class MemoryStore:
    """Ephemeral test double only; cannot touch the runtime transactional store."""
    state = None

    def read(self):
        return Snapshot("1" if self.state else None, copy.deepcopy(self.state))

    def compare_and_swap(self, expected_version, state):
        self.state = copy.deepcopy(state)
        return True


class FinanceTests(unittest.TestCase):
    def setUp(self):
        self.tables = baseline_tables(CONFIG, "2026-10-13T03:00:00+00:00", APPROVERS)

    def test_fixture_matches_transactional_seed_without_fabricating_events(self):
        ledger = Ledger(MemoryStore(), LedgerConfig(CONFIG["tenantId"], ("one", "two"),
                                                    CONFIG["hotelOwnerId"]))
        authority = ledger.snapshot()
        actual = {r["account_id"]: r["balance_cents"] for r in self.tables["accounts"]}
        expected = {key: value["balance_cents"] for key, value in authority["accounts"].items()}
        self.assertEqual(actual, expected)
        payroll = self.tables["payroll_obligations"][0]
        self.assertEqual(payroll["obligation_cents"],
                         authority["tasks"]["payroll-funding"]["obligation_cents"])
        self.assertEqual(payroll["obligation_cents"] - actual[payroll["destination_account_id"]],
                         1500000)
        for table in ("transfers", "approval_requests", "notification_deliveries"):
            self.assertEqual(self.tables[table], [])
        self.assertEqual(authority["transactions"], [])
        for rows in self.tables.values():
            for row in rows:
                self.assertFalse(row["authoritative"])
                self.assertEqual(row["ledger_status"], "initial_baseline")
                self.assertEqual(row["ledger_version"], "initial-baseline-not-live-ledger")

    def test_policy_matches_ledger_source(self):
        policy = self.tables["transfer_policy"][0]
        self.assertEqual(policy["approval_threshold_cents"], 1000000)
        self.assertEqual(policy["comparison"], "strictly_greater_than")
        self.assertEqual(policy["notifications_per_request"], 4)
        self.assertEqual(policy["expiry"], "none")
        self.assertEqual(policy["execution_role"], "banking_operations")
        self.assertEqual(len(self.tables["eligible_approvers"]), 2)

    def test_definition_is_finance_only_and_published(self):
        definition = dataagent_definition(self.tables, "ws", "lh", "FinanceLedgerAnalytics",
                                         "onto", "FinanceLedgerOntology", "FinanceAnalyticalAgent")
        sources = validate_sources(definition, "ws", "lh", "onto")
        self.assertEqual(len(sources), 4)
        values = decoded(definition)
        self.assertIn("Files/Config/publish_info.json", values)
        self.assertEqual(values["Files/Config/data_agent.json"]["$schema"].split("/")[-2], "2.1.0")
        instructions = values["Files/Config/published/stage_config.json"]["aiInstructions"]
        self.assertIn("non-authoritative analytical projection", instructions)
        self.assertIn("NOT a current balance guarantee", instructions)
        self.assertIn("first authenticated decision", instructions)
        key = next(k for k in values if "/published/" in k and k.endswith("datasource.json"))
        values[key]["workspaceId"] = "unrelated-workspace"
        with self.assertRaises(ValueError):
            validate_sources(parts(values), "ws", "lh", "onto")

    def test_notebook_is_managed_create_if_absent(self):
        notebook = decoded(notebook_definition(self.tables, "ws", "lh", "FinanceLedgerAnalytics"))[
            "notebook-content.ipynb"]
        self.assertEqual(notebook["metadata"]["dependencies"]["lakehouse"]["default_lakehouse"], "lh")
        code = "".join(notebook["cells"][0]["source"])
        compile(code, "finance-baseline-notebook", "exec")
        self.assertIn(".saveAsTable(target)", code)
        self.assertIn("errorifexists", code)
        self.assertNotIn(".mode('overwrite')", code)
        self.assertTrue(all(line.endswith("\n") for line in notebook["cells"][0]["source"]))

    def test_selected_table_allowlist_is_enforced(self):
        definition = dataagent_definition(self.tables, "ws", "lh", "FinanceLedgerAnalytics",
                                         None, "FinanceLedgerOntology", "FinanceAnalyticalAgent")
        values = decoded(definition)
        key = next(k for k in values if "/published/" in k and k.endswith("datasource.json"))
        tables = values[key]["elements"][0]["children"][0]["children"][0]["children"]
        tables[0]["display_name"] = "unrelated_records"
        with self.assertRaisesRegex(ValueError, "allowlist"):
            validate_sources(parts(values), "ws", "lh", None)

    def test_baseline_capture_survives_checkpoint_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output.json"
            output.write_text(json.dumps({"tenant_id": CONFIG["tenantId"], "environment": "dev",
                "workspace_id": "ws", "projection_freshness": {"captured_at": "original-capture"}}))
            provisioner = Provisioner(CONFIG, "dev", output)
            self.assertEqual(provisioner.previous["projection_freshness"]["captured_at"],
                             "original-capture")
            with self.assertRaisesRegex(ValueError, "another tenant/environment"):
                Provisioner(CONFIG, "test", output)

    def test_ontology_has_stable_managed_finance_bindings(self):
        first = ontology_definition(self.tables, "ws", "lh", "FinanceLedgerOntology")
        self.assertEqual(first, ontology_definition(self.tables, "ws", "lh", "FinanceLedgerOntology"))
        bindings = [v["dataBindingConfiguration"]["sourceTableProperties"] for k, v in
                    decoded(first).items() if "/DataBindings/" in k]
        self.assertEqual(len(bindings), 3)
        self.assertTrue(all(v["workspaceId"] == "ws" and v["itemId"] == "lh"
                            and v["sourceSchema"] == "dbo" for v in bindings))

    def test_202_empty_stdout_and_204(self):
        status, headers, body = parse_response("", "Response status: 202\nResponse headers:\n"
            "{'Location': 'https://api.fabric.microsoft.com/v1/operations/operation', "
            "'x-ms-operation-id': 'operation', 'retry-after': '2'}")
        self.assertEqual(status, 202)
        self.assertEqual(headers["x-ms-operation-id"], "operation")
        self.assertEqual(body, {})
        self.assertEqual(parse_response("", "Response status: 204")[2], {})
        self.assertEqual(parse_response("Accepted\n", "Response status: 202")[2], {})

    def test_streamable_http_response(self):
        status, headers, value = parse_response(
            'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"tools":[]}}\n\n',
            "Response status: 200")
        self.assertEqual(value["result"]["tools"], [])

    def test_lro_poll_then_result_and_mutation_204(self):
        transport = FabricRest()
        with patch.object(transport, "raw", side_effect=[
            (202, {"x-ms-operation-id": "op"}, {}),
            (200, {}, {"status": "Succeeded"}),
            (204, {}, {}),
        ]) as raw:
            self.assertEqual(transport.request("post", "/test", {}), {})
            self.assertEqual(raw.call_args_list[-1].args[:2], ("get", "/operations/op/result"))
        with patch.object(transport, "raw", side_effect=[
            (202, {"x-ms-operation-id": "op"}, {}),
            (200, {}, {"status": "Failed", "error": {"message": "invalid definition"}}),
        ]):
            with self.assertRaisesRegex(RuntimeError, "invalid definition"):
                transport.request("post", "/test", {})

    def test_credentials_redacted(self):
        secret = "eyJhbGciOiJub25lIn0.eyJhdWQiOiJmYWJyaWMifQ.signature"
        value = redact("Authorization: Bearer " + secret + "\nERROR: bearer " + secret)
        self.assertNotIn(secret, value)
        self.assertNotIn("signature", value)

    def test_owned_names_never_adopt_unrelated_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            provisioner = Provisioner(CONFIG, "dev", Path(directory) / "output.json")
            with self.assertRaisesRegex(ValueError, "unowned"):
                provisioner.owned([{"displayName": "FinanceAnalytics-dev", "description": ""}],
                                  "FinanceAnalytics-dev")

    def test_bootstrap_preserves_refresh_and_graph_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "output.json"
            previous = {"tenant_id": CONFIG["tenantId"], "environment": "dev",
                        "workspace_id": "ws", "projection_refresh": {"notebook_id": "nb"},
                        "projection_freshness": {"ledger_version": "actual-source-etag",
                            "ledger_status": "captured_snapshot"},
                        "proof": {"ontology_relationship_query": {"verified": False}}}
            path.write_text(json.dumps(previous))
            provisioner = Provisioner(CONFIG, "dev", path)
            for key in ("projection_refresh", "projection_freshness", "proof"):
                self.assertEqual(provisioner.state[key], previous[key])

    def test_refresh_policy_preserves_finance_sources_and_selects_manifest_metadata(self):
        definition = dataagent_definition(self.tables, "ws", "lh", "FinanceLedgerAnalytics",
                                          None, "FinanceLedgerOntology", "FinanceAnalyticalAgent")
        class LocalDefinitionTransport:
            value = definition
            def request(self, method, url, body=None, **kwargs):
                if url.endswith("/updateDefinition"):
                    self.value = body["definition"]
                    return {}
                if url.endswith("/getDefinition"):
                    return self.value
                return {"displayName": "FinanceAnalyticalAgent", "type": "DataAgent",
                        "description": f"finance-analytics-bootstrap/v1/{CONFIG['tenantId']}/dev"}
        with tempfile.TemporaryDirectory() as directory:
            owner = Provisioner(CONFIG, "dev", Path(directory) / "output.json")
            actual = projection_agent_policy(LocalDefinitionTransport(), {
                "workspace_id": "ws", "lakehouse_id": "lh", "ontology_id": None,
                "data_agent_id": "agent"}, owner, refreshed=True)
        sources = validate_sources(actual, "ws", "lh", None)
        self.assertTrue(all(v["artifact_id"] == "lh" for v in sources))
        values = decoded(actual)
        stage = values["Files/Config/published/stage_config.json"]["aiInstructions"]
        self.assertIn("Query projection_status first", stage)
        self.assertIn("refreshing", stage)
        source = next(v for p, v in values.items() if "/published/" in p and p.endswith("/datasource.json"))
        serialized = json.dumps(source)
        self.assertIn("projection_state", serialized)
        self.assertIn("source_etag", serialized)

    def test_refresh_schema_evolution_matches_owned_column_allowlist(self):
        original = table_schemas(self.tables)
        common = {"captured_at", "ledger_version", "source_etag", "ledger_status", "authoritative"}
        for table, schema in REFRESH_SCHEMAS.items():
            refreshed = {column.strip().split()[0] for column in schema.split(",")} | common
            permitted = {name for name, _ in original[table]} | {
                "source_etag", *REFRESH_EXTRA_COLUMNS.get(table, {})}
            self.assertEqual(refreshed, permitted, table)

    def captured(self):
        ledger = Ledger(MemoryStore(), LedgerConfig(CONFIG["tenantId"], ("one", "two"),
                                                    CONFIG["hotelOwnerId"]))
        expected = {"tenant_id": CONFIG["tenantId"], "owner_id": CONFIG["hotelOwnerId"],
                    "approver_ids": ["one", "two"], "approver_upns": CONFIG["approverUpns"]}
        envelope = {"contract_version": 1, "source_etag": "opaque-local-test-source-v1",
                    "captured_at": "2026-10-13T03:00:00+00:00", "state": ledger.snapshot()}
        return ledger, envelope, expected

    def test_refresh_does_not_invent_events(self):
        ledger, envelope, expected = self.captured()
        projection = project_snapshot(envelope, expected)
        for table in ("transfers", "approval_requests", "notification_deliveries"):
            self.assertEqual(projection[table], [])
        for rows in projection.values():
            for row in rows:
                self.assertEqual(row["source_etag"], envelope["source_etag"])
                self.assertEqual(row["captured_at"], envelope["captured_at"])
                self.assertFalse(row["authoritative"])
        self.assertEqual({r["account_id"]: r["balance_cents"] for r in projection["accounts"]},
                         {key: r["balance_cents"] for key, r in envelope["state"]["accounts"].items()})

    def test_refresh_projects_actual_domain_events_only(self):
        ledger, envelope, expected = self.captured()
        action = ledger.payroll_funding_action()
        ledger.request_approval(action, request_id="local-test-request", idempotency_key="local-test-key")
        ledger.decide("local-test-request", Principal(CONFIG["tenantId"], "one"), Decision.APPROVE)
        ledger.execute_transfer(action, idempotency_key="local-test-key", role=AgentRole.BANKING_OPERATIONS,
                                approval_request_id="local-test-request")
        envelope["state"] = ledger.snapshot()
        projection = project_snapshot(envelope, expected)
        self.assertEqual(len(projection["transfers"]), 1)
        self.assertEqual(projection["transfers"][0]["transaction_id"], envelope["state"]["transactions"][0]["transaction_id"])
        self.assertEqual(len(projection["approval_requests"]), 1)
        self.assertEqual(len(projection["notification_deliveries"]), 4)
        self.assertTrue(all(row["status"] == "cancelled" for row in projection["notification_deliveries"]))

    def test_refresh_rejects_fixture_version_and_ineligible_decision(self):
        ledger, envelope, expected = self.captured()
        envelope["source_etag"] = "initial-baseline-not-live-ledger"
        with self.assertRaisesRegex(ValueError, "fixture label"):
            validate_snapshot(envelope, expected)
        envelope["source_etag"] = "opaque-local-test"
        action = ledger.payroll_funding_action()
        ledger.request_approval(action, request_id="local-test-request", idempotency_key="local-test-key")
        ledger.decide("local-test-request", Principal(CONFIG["tenantId"], "one"), Decision.APPROVE)
        envelope["state"] = ledger.snapshot()
        envelope["state"]["approvals"]["local-test-request"]["decision_by"]["object_id"] = "ineligible"
        with self.assertRaisesRegex(ValueError, "not eligible"):
            validate_snapshot(envelope, expected)

    def test_refresh_notebook_contains_bank_guard_and_versioned_commit(self):
        ledger, envelope, expected = self.captured()
        notebook = decoded(refresh_notebook({"workspace_id": "ws", "lakehouse_id": "lh"},
                                            expected, "bank-root-object-id"))["notebook-content.ipynb"]
        self.assertEqual(notebook["cells"][0]["metadata"]["tags"], ["parameters"])
        code = "".join(notebook["cells"][1]["source"])
        compile(code, "refresh-notebook", "exec")
        self.assertIn("notebookutils.runtime.context['userId']", code)
        self.assertIn("bank-root-object-id", code)
        self.assertIn("expected_projection_etag", code)
        self.assertIn("same", code.lower())
        self.assertIn("projection_state=\\'refreshing\\'", code)
        self.assertIn("source_etag IS NULL", code)
        self.assertIn("Files/finance_projection_manifest.json", code)

    def test_runtime_refresh_rejects_operator_fallback(self):
        with patch("fabric_refresh.cli", return_value={
            "tenantId": CONFIG["tenantId"], "user": {"type": "user"}}) as mocked:
            with self.assertRaisesRegex(PermissionError, "never operator fallback"):
                token_identity(CONFIG["tenantId"], "bank-root-object-id")
            self.assertEqual(mocked.call_count, 1)

    def test_graph_validation_agent_has_no_relational_fallback(self):
        state = {"workspace_id": "ws", "lakehouse_id": "lh", "ontology_id": "ontology",
                 "projection_freshness": {"captured_at": "2026-10-13T03:00:00+00:00"}}
        values = decoded(graph_agent_definition(state, CONFIG, APPROVERS))
        self.assertFalse(any(p.endswith("/datasource.json") for p in values))
        ontologies = [v for p, v in values.items() if p.endswith("/ontology.json")]
        self.assertEqual(len(ontologies), 2)
        self.assertTrue(all(v["artifactId"] == "ontology" for v in ontologies))


class ProjectionIntegrationTests(unittest.TestCase):
    """Offline transport simulation ONLY; never recorded as Fabric query/access proof."""
    def setUp(self):
        ids = [str(__import__("uuid").UUID(int=n)) for n in range(1, 7)]
        self.tenant, self.ws, self.lh, self.nb, self.bank, self.agent = ids
        expected = {"tenant_id": self.tenant, "owner_id": CONFIG["hotelOwnerId"],
                    "approver_ids": ["one", "two"], "approver_upns": CONFIG["approverUpns"]}
        self.output = {"tenant_id": self.tenant, "workspace_id": self.ws, "lakehouse_id": self.lh,
            "environment": "test", "data_agent_id": self.agent, "ontology_id": None,
            "projection_refresh": {"contract_version": 1, "notebook_id": self.nb,
                "bank_principal_id": self.bank, "expected_source_policy": expected}}
        self.config = projection.ProjectionConfig.from_output(self.output)
        ledger = Ledger(MemoryStore(), LedgerConfig(self.tenant, ("one", "two"), CONFIG["hotelOwnerId"]))
        self.envelope = {"contract_version": 1, "source_etag": "opaque-unit-capture-v1",
                         "captured_at": "2026-10-13T03:00:00+00:00", "state": ledger.snapshot()}
        digest = hashlib.sha256(json.dumps(self.envelope["state"], sort_keys=True,
                                           separators=(",", ":")).encode()).hexdigest()
        self.manifest = {"source_etag": self.envelope["source_etag"],
            "ledger_version": self.envelope["source_etag"], "captured_at": self.envelope["captured_at"],
            "ledger_status": "captured_snapshot", "projection_state": "complete",
            "authoritative": False, "source_authoritative": True, "snapshot_sha256": digest,
            "transaction_count": 0, "approval_count": 0, "notification_count": 0}
        tables = baseline_tables(CONFIG, self.envelope["captured_at"], APPROVERS)
        self.definition = dataagent_definition(tables, self.ws, self.lh, "FinanceLedgerAnalytics",
                                                None, "FinanceLedgerOntology", "FinanceAnalyticalAgent")
        self.calls, self.checkpoints, self.scopes = [], [], []
        self.job_url = (f"{projection.FABRIC}/v1/workspaces/{self.ws}/items/{self.nb}"
                        "/jobs/instances/00000000-0000-0000-0000-000000000099?beta=true")
        self.now = 0

    def token(self, scope, **overrides):
        self.scopes.append(scope)
        claims = {"tid": self.tenant, "oid": self.bank, "aud": scope.removesuffix("/.default"),
                  "idtyp": "app", **overrides}
        payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        return "unit-only." + payload + ".not-a-real-signature"

    def sleep(self, seconds):
        self.now += seconds

    def send(self, method, url, *, headers, body, timeout_seconds):
        self.calls.append((method, url, copy.deepcopy(body)))
        self.assertGreater(timeout_seconds, 0)
        self.assertEqual(headers["x-ms-fabric-skill"], "spark-cli")
        if url == self.config.manifest_url:
            return projection.HttpResponse(200, {}, copy.deepcopy(self.manifest))
        if url == self.job_url:
            self.assertTrue(any(v.get("job_url") == url for v in self.checkpoints))
            return projection.HttpResponse(200, {}, {"status": "Completed"})
        if url.endswith("/jobs/instances"):
            return projection.HttpResponse(200, {}, {"value": []})
        if url.endswith(f"/notebooks/{self.nb}/jobs/execute/instances?beta=false"):
            parameters = {parameter["name"]: parameter for parameter in body["parameters"]}
            self.assertEqual(parameters["snapshot_json"]["type"], "Text")
            self.assertEqual(json.loads(parameters["snapshot_json"]["value"]), self.envelope)
            return projection.HttpResponse(202, {"Location": self.job_url, "Retry-After": "0"})
        if url.endswith("/getDefinition"):
            return projection.HttpResponse(200, {}, copy.deepcopy(self.definition))
        if url.endswith("/updateDefinition"):
            self.definition = copy.deepcopy(body["definition"])
            return projection.HttpResponse(204, {}, None)
        return projection.HttpResponse(200, {}, {
            "type": "DataAgent", "displayName": "FinanceAnalyticalAgent",
            "description": f"finance-analytics-bootstrap/v1/{self.tenant}/test"})

    def run_refresh(self, **kwargs):
        options = {"config": self.config, "expected_projection_etag": "initial-baseline-not-live-ledger",
            "credential_kind": "agent_identity", "get_token": self.token, "send": self.send,
            "checkpoint": lambda v: self.checkpoints.append(copy.deepcopy(v)),
            "clock": lambda: self.now, "sleep": self.sleep, **kwargs}
        return projection.refresh_projection(self.envelope, **options)

    def test_cli_reuses_same_validator_and_installed_notebook_cells(self):
        import fabric_refresh
        self.assertIs(fabric_refresh.validate_snapshot, projection.validate_snapshot)
        self.assertIs(fabric_refresh.project_snapshot, projection.project_snapshot)
        evidence = Path(__file__).resolve().parents[1] / ".state/evidence/fabric-projection-install.json"
        if evidence.exists():
            installed = json.loads(evidence.read_text())
            contract = installed["installation"]
            state = json.loads((evidence.parents[1] / "fabric-output.json").read_text())
            desired = decoded(refresh_notebook(state, contract["expected_source_policy"],
                                               contract["bank_principal_id"]))["notebook-content.ipynb"]
            actual = decoded(installed["notebook_definition_readback"])["notebook-content.ipynb"]
            self.assertEqual([v["source"] for v in desired["cells"]], [v["source"] for v in actual["cells"]])

    def test_runtime_success_validates_manifest_and_syncs_metadata_without_graph(self):
        result = self.run_refresh()
        self.assertEqual(result["committed_manifest"], self.manifest)
        self.assertFalse(result["metadata_sync_pending"])
        self.assertTrue(result["graph_refresh_pending"])
        self.assertFalse(result["projection_freshness"]["authoritative"])
        self.assertIn(projection.STORAGE + "/.default", self.scopes)
        self.assertEqual(sum(method == "post" and "/jobs/" in url for method, url, _ in self.calls), 1)
        self.assertFalse(any("Graph" in url or "/Refresh/" in url for _, url, _ in self.calls))
        self.assertIn("source_etag", json.dumps(decoded(self.definition)))

    def test_human_delegated_and_wrong_principal_tokens_never_reach_http(self):
        for claims in ({"idtyp": "user"}, {"scp": "user_impersonation"}, {"oid": self.agent}):
            with self.assertRaises(projection.ProjectionRefreshError):
                self.run_refresh(get_token=lambda scope: self.token(scope, **claims))
            self.assertEqual(self.calls, [])
        with self.assertRaises(PermissionError):
            self.run_refresh(credential_kind="azure_cli_user")

    def test_active_job_and_hostile_location_cannot_submit_or_exfiltrate(self):
        with self.assertRaises(projection.ProjectionRefreshError):
            self.run_refresh(send=lambda *args, **kw: projection.HttpResponse(
                200, {}, {"value": [{"status": "InProgress"}]}))
        self.assertEqual(self.calls, [])
        self.job_url = "https://untrusted.example.invalid/job"
        with self.assertRaises(projection.ProjectionRefreshError) as caught:
            self.run_refresh()
        self.assertTrue(caught.exception.progress["submission_attempted"])
        self.assertFalse(any("untrusted" in url for _, url, _ in self.calls))

    def test_mismatched_manifest_rejects_and_metadata_failure_retains_commit(self):
        self.manifest["snapshot_sha256"] = "0" * 64
        with self.assertRaises(projection.ProjectionRefreshError):
            self.run_refresh()
        self.assertFalse(any(url.endswith("/updateDefinition") for _, url, _ in self.calls))
        self.setUp()
        good = self.send
        def failing(method, url, **kwargs):
            if url.endswith("/updateDefinition"):
                return projection.HttpResponse(403, {}, None)
            return good(method, url, **kwargs)
        with self.assertRaises(projection.ProjectionRefreshError) as caught:
            self.run_refresh(send=failing)
        self.assertEqual(caught.exception.progress["committed_manifest"], self.manifest)
        self.assertTrue(caught.exception.progress["metadata_sync_pending"])

    def test_empty_202_definition_operation_poll_result_and_204_mutation(self):
        good = self.send
        op = "00000000-0000-0000-0000-000000000088"
        first = True
        def async_definition(method, url, **kwargs):
            nonlocal first
            if url.endswith("/getDefinition") and first:
                first = False
                return projection.HttpResponse(202, {"x-ms-operation-id": op, "Retry-After": "0"})
            if url == projection.FABRIC + "/v1/operations/" + op:
                return projection.HttpResponse(200, {}, {"status": "Succeeded"})
            if url == projection.FABRIC + "/v1/operations/" + op + "/result":
                return projection.HttpResponse(200, {}, self.definition)
            return good(method, url, **kwargs)
        self.assertFalse(self.run_refresh(send=async_definition)["metadata_sync_pending"])

    def test_timeout_preserves_one_accepted_job_without_resubmission(self):
        good = self.send
        def still_running(method, url, **kwargs):
            if url == self.job_url:
                return projection.HttpResponse(200, {}, {"status": "InProgress"})
            return good(method, url, **kwargs)
        with self.assertRaises(projection.ProjectionRefreshError) as caught:
            self.run_refresh(send=still_running, timeout_seconds=10)
        self.assertEqual(caught.exception.progress["job_url"], self.job_url)
        self.assertEqual(sum(method == "post" and "/jobs/" in url for method, url, _ in self.calls), 1)

    def test_source_policy_drift_rejects_metadata_and_repeat_sync_is_noop(self):
        self.run_refresh()
        self.calls.clear()
        self.run_refresh()
        self.assertFalse(any(url.endswith("/updateDefinition") for _, url, _ in self.calls))
        values = decoded(self.definition)
        for path, value in values.items():
            if path.endswith("/datasource.json"):
                value["artifactId"] = self.agent
        self.definition = parts(values)
        with self.assertRaises(projection.ProjectionRefreshError) as caught:
            self.run_refresh()
        self.assertEqual(caught.exception.progress["committed_manifest"], self.manifest)

    def test_manifest_file_is_not_a_live_delta_gate(self):
        projection.assert_projection_current(self.manifest, current_source_etag=self.envelope["source_etag"],
            live_projection_status=self.manifest, row_versions=[self.envelope["source_etag"]])
        for status in ("refreshing", "failed"):
            with self.assertRaises(ValueError):
                projection.assert_projection_current(self.manifest,
                    current_source_etag=self.envelope["source_etag"],
                    live_projection_status={**self.manifest, "projection_state": status})
        with self.assertRaises(ValueError):
            projection.assert_projection_current(self.manifest, current_source_etag="new-ledger-version",
                                                  live_projection_status=self.manifest)
        with self.assertRaises(ValueError):
            projection.assert_projection_current(self.manifest, current_source_etag=self.envelope["source_etag"],
                live_projection_status=self.manifest, row_versions=["mixed"])

    def test_manifest_read_url_and_missing_file_are_explicit(self):
        self.assertEqual(projection.read_projection_manifest(self.config, credential_kind="agent_identity",
                         get_token=self.token, send=self.send), self.manifest)
        self.assertIsNone(projection.read_projection_manifest(self.config, credential_kind="agent_identity",
            get_token=self.token, send=lambda *args, **kwargs: projection.HttpResponse(404, {}, None)))
        self.assertIn(f"/{self.ws}/{self.lh}/Files/finance_projection_manifest.json", self.config.manifest_url)


if __name__ == "__main__":
    unittest.main(verbosity=2)
