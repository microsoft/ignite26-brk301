#!/usr/bin/env python3
"""Provision finance-owned Fabric analytical assets using az only, not runtime auth."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid
from urllib.parse import quote, urlencode

from fabric_rest import FABRIC, FabricRest, cli, redact

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/dataAgent/definition"
FRESHNESS = (
    "Controlled initial baseline, NOT a live authoritative ledger snapshot. captured_at "
    "is fixture provisioning time, not a transactional ledger read. ledger_version is "
    "a baseline label, not a concurrency token. Balances may be stale after ANY ledger "
    "mutation. Consult the transactional ledger before approval or execution. No refresh "
    "worker is provisioned here."
)
PROJECTION_READ_POLICY = (
    "All finance data is a non-authoritative analytical projection. Query projection_status "
    "first. Refuse financial answers while ledger_status/projection_state is refreshing "
    "or failed, or when row versions/counts disagree with the manifest. initial_baseline "
    "means captured_at is fixture provisioning time and ledger_version is NOT a live "
    "concurrency token. captured_snapshot means captured_at/source_etag identify a "
    "previous consistent source read, NOT a current balance guarantee. Match ledger_version "
    "across all participating rows and report captured_at, ledger_version, ledger_status "
    "and authoritative=false. Actual freshness requires comparison with the transactional "
    "ledger's current ETag outside this tool. Ontology graph refresh is separate; a graph "
    "answer's source version must match the projection version or be explicitly rejected "
    "as stale. Never approve or execute from this projection."
)
REFRESH_EXTRA_COLUMNS = {
    "payroll_obligations": {"last_transaction_id": "string"},
    "transfers": {"idempotency_key": "string", "source_balance_cents": "bigint",
                  "destination_balance_cents": "bigint"},
    "approval_requests": {"source_account_id": "string", "destination_account_id": "string",
                          "currency": "string", "idempotency_key": "string"},
    "notification_deliveries": {"request_status": "string", "attempts": "bigint",
                               "attempt_id": "string"},
    "projection_status": {**{k: "string" for k in (
        "projection_state", "snapshot_sha256", "refresh_id", "requested_source_etag",
        "requested_snapshot_sha256", "previous_source_etag", "last_error")},
        "source_authoritative": "boolean"},
}


def dump(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def parts(values, format=None):
    definition = {"parts": [{"path": name, "payloadType": "InlineBase64",
                             "payload": base64.b64encode(json.dumps(value, separators=(",", ":"))
                                                        .encode()).decode()}
                            for name, value in values.items()]}
    if format:
        definition["format"] = format
    return definition


def decoded(response):
    return {p["path"]: json.loads(base64.b64decode(p["payload"]))
            for p in response.get("definition", response)["parts"]}


def stable_id(name):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "finance-analytics/" + name))


def numeric_id(name):
    return str(int.from_bytes(hashlib.sha256(("finance-ontology/" + name).encode()).digest()[:7],
                              "big") + 1)


def baseline_tables(config, captured_at, approvers):
    fixture = json.loads((ROOT / "datasets/finance/baseline.json").read_text())
    common = {"captured_at": captured_at, "ledger_version": fixture["ledger_version"],
              "ledger_status": fixture["ledger_status"], "authoritative": False}
    owner = config["hotelOwnerId"]
    accounts = [{**row, "owner_id": owner, **common} for row in fixture["accounts"]]
    payroll = [{**fixture["payroll"], "owner_id": owner, **common}]
    policy = [{"policy_id": "hotel-transfer-policy", **{
        k: v for k, v in fixture["approval_policy"].items() if not isinstance(v, list)},
        "notification_channels": "teams,email", **common}]
    people = [{"approver_id": p["id"], "configured_upn": p["userPrincipalName"],
               "policy_id": "hotel-transfer-policy", **common} for p in approvers]
    projection = [{"projection_id": "initial-baseline", "owner_id": owner,
                   "fixture_kind": fixture["fixture_kind"], "freshness_notice": FRESHNESS,
                   "transaction_count": 0, "approval_count": 0, "notification_count": 0,
                   **common}]
    return {"accounts": accounts, "payroll_obligations": payroll, "transfer_policy": policy,
            "eligible_approvers": people, "projection_status": projection,
            "transfers": [], "approval_requests": [], "notification_deliveries": []}


EMPTY_SCHEMAS = {
    "transfers": "transaction_id STRING, source_account_id STRING, destination_account_id STRING, amount_cents BIGINT, currency STRING, approval_request_id STRING",
    "approval_requests": "request_id STRING, status STRING, decision STRING, decision_by STRING, amount_cents BIGINT, executed_transaction_id STRING",
    "notification_deliveries": "delivery_id STRING, request_id STRING, recipient_id STRING, channel STRING, status STRING"
}


def table_schemas(tables):
    result = {}
    for table, rows in tables.items():
        if rows:
            result[table] = [(key, "boolean" if isinstance(value, bool) else
                             "bigint" if isinstance(value, int) else
                             "timestamp" if key == "captured_at" else "string")
                            for key, value in rows[0].items()]
        else:
            result[table] = [(pair.strip().split()[0], pair.strip().split()[1].lower())
                             for pair in EMPTY_SCHEMAS[table].split(",")]
            result[table] += [("captured_at", "timestamp"), ("ledger_version", "string"),
                              ("ledger_status", "string"), ("authoritative", "boolean")]
    return result


def notebook_definition(tables, ws, lh, lh_name):
    schemas = table_schemas(tables)
    code = (
        "import json\nfrom datetime import datetime\n"
        "from pyspark.sql.types import StructType, StructField, StringType, LongType, BooleanType, TimestampType\n"
        f"tables = json.loads({json.dumps(json.dumps(tables))})\n"
        f"schemas = json.loads({json.dumps(json.dumps(schemas))})\n"
        "types = {'string': StringType(), 'bigint': LongType(), 'boolean': BooleanType(), 'timestamp': TimestampType()}\n"
        "proof = {}\n"
        "for name, rows in tables.items():\n"
        "    target = 'dbo.' + name\n"
        "    if spark.catalog.tableExists(target):\n"
        "        # Never reset a projection, even if runtime has since refreshed it.\n"
        "        proof[name] = {'existing': True, 'count': spark.table(target).count()}\n"
        "        continue\n"
        "    schema = StructType([StructField(c, types[t], True) for c,t in schemas[name]])\n"
        "    converted = [{**r, 'captured_at': datetime.fromisoformat(r['captured_at'].replace('Z','+00:00'))} for r in rows]\n"
        "    df = spark.createDataFrame(converted, schema)\n"
        "    df.write.format('delta').mode('errorifexists').saveAsTable(target)\n"
        "    proof[name] = {'existing': False, 'count': spark.table(target).count()}\n"
        "a = {r.account_id: r.balance_cents for r in spark.table('dbo.accounts').collect()}\n"
        "assert a['hotel-reserve'] == 5000000 and a['hotel-payroll'] == 1000000\n"
        "assert spark.table('dbo.transfers').count() == 0\n"
        "assert spark.table('dbo.approval_requests').count() == 0\n"
        "print(json.dumps(proof))\n"
    )
    content = {"nbformat": 4, "nbformat_minor": 5, "metadata": {
        "kernelspec": {"name": "synapse_pyspark", "display_name": "Synapse PySpark"},
        "language_info": {"name": "python"},
        "dependencies": {"lakehouse": {"default_lakehouse": lh,
            "default_lakehouse_name": lh_name, "default_lakehouse_workspace_id": ws,
            "known_lakehouses": [{"id": lh}]}}},
        "cells": [{"cell_type": "code", "execution_count": None, "outputs": [],
                   "metadata": {}, "source": code.splitlines(keepends=True)}]}
    return parts({"notebook-content.ipynb": content}, "ipynb")


def ontology_definition(tables, ws, lh, name):
    """Stable concept IDs derived from versioned names; no copied reference IDs."""
    result = {"definition.json": {}, ".platform": {
        "metadata": {"type": "Ontology", "displayName": name}}}
    entities = {"FinanceAccount": ("accounts", "account_id"),
                "PayrollObligation": ("payroll_obligations", "task_id"),
                "TransferPolicy": ("transfer_policy", "policy_id")}
    schemas = table_schemas(tables)
    for entity, (table, key) in entities.items():
        eid = numeric_id(entity)
        props = [{"id": numeric_id(entity + "/" + column), "name": column,
                  "valueType": {"string": "String", "bigint": "BigInt", "boolean": "Boolean",
                                "timestamp": "DateTime"}[dtype]} for column, dtype in schemas[table]]
        result[f"EntityTypes/{eid}/definition.json"] = {
            "id": eid, "namespace": "usertypes", "namespaceType": "Custom",
            "name": entity, "visibility": "Visible",
            "entityIdParts": [numeric_id(entity + "/" + key)],
            "displayNamePropertyId": numeric_id(entity + "/" + key),
            "properties": props, "timeseriesProperties": []}
        bid = stable_id(entity + "/static")
        result[f"EntityTypes/{eid}/DataBindings/{bid}.json"] = {
            "id": bid, "dataBindingConfiguration": {
                "dataBindingType": "NonTimeSeries",
                "propertyBindings": [{"sourceColumnName": p["name"], "targetPropertyId": p["id"]}
                                     for p in props],
                "sourceTableProperties": {"sourceType": "LakehouseTable", "workspaceId": ws,
                    "itemId": lh, "sourceSchema": "dbo", "sourceTableName": table}}}
    rid, cid = numeric_id("PayrollFundedBy"), stable_id("PayrollFundedBy/link")
    result[f"RelationshipTypes/{rid}/definition.json"] = {
        "id": rid, "namespace": "usertypes", "namespaceType": "Custom", "name": "PayrollFundedBy",
        "source": {"entityTypeId": numeric_id("PayrollObligation")},
        "target": {"entityTypeId": numeric_id("FinanceAccount")}}
    result[f"RelationshipTypes/{rid}/Contextualizations/{cid}.json"] = {
        "id": cid, "dataBindingTable": {"sourceType": "LakehouseTable", "workspaceId": ws,
            "itemId": lh, "sourceSchema": "dbo", "sourceTableName": "payroll_obligations"},
        "sourceKeyRefBindings": [{"sourceColumnName": "task_id",
            "targetPropertyId": numeric_id("PayrollObligation/task_id")}],
        "targetKeyRefBindings": [{"sourceColumnName": "source_account_id",
            "targetPropertyId": numeric_id("FinanceAccount/account_id")}]}
    return parts(result)


def dataagent_definition(tables, ws, lh, lh_name, ontology, ontology_name, name):
    instructions = (
        "You provide read-only controlled hotel finance analysis. " + PROJECTION_READ_POLICY +
        " Use ONLY the configured finance lakehouse tables for exact amounts, payroll "
        "shortfall, policy and counts. Use the ontology only for modeled relationships. "
        "Compute shortfall by joining payroll_obligations.destination_account_id to "
        "accounts.account_id: MAX(0, obligation_cents - balance_cents). All money is "
        "integer USD cents; divide by 100 only for dollar display. Return captured_at, "
        "ledger_version, ledger_status and authoritative with financial answers. "
        "No approved/executed transfers exist unless actual rows say so; empty tables "
        "mean no recorded events at this baseline. Eligibility is NOT approval. "
        "Above threshold (strictly greater) requires either eligible approver, both "
        "channels to each approver (four notifications), first authenticated decision "
        "wins atomically, no expiry. Only Hotel Treasurer executes via ledger, "
        "never this read-only tool. Never query other workspaces or unselected sources. "
        "Refuse unrelated servicing, lending, credit, guest, or operational questions."
    )
    def node(path, display, type, children=None, dtype=None):
        n = {"id": stable_id(f"{ws}/{lh}/{path}"), "is_selected": True,
             "display_name": display, "type": type, "description": None,
             "children": children or []}
        if dtype:
            n["data_type"] = dtype
        return n
    elements = []
    for table, columns in table_schemas(tables).items():
        elements.append(node(table, table, "lakehouse_tables.table",
            [node(table + "/" + col, col, "lakehouse_tables.column", dtype=dt)
             for col, dt in columns]))
    source = {"$schema": SCHEMA + "/dataSource/1.0.0/schema.json", "artifactId": lh,
              "workspaceId": ws, "displayName": lh_name, "type": "lakehouse_tables",
              "dataSourceInstructions": instructions, "userDescription": PROJECTION_READ_POLICY,
              "metadata": None, "elements": [node("schemas", "Schemas", "schema_grouping", [
                  node("dbo", "dbo", "lakehouse_tables.schema", [
                      node("dbo/tables", "Tables", "table_grouping", elements)])])]}
    stage = {"$schema": SCHEMA + "/stageConfiguration/1.0.0/schema.json",
             "aiInstructions": instructions, "experimental": {"enableExperimentalFeatures": True}}
    result = {"Files/Config/data_agent.json": {"$schema": SCHEMA + "/dataAgent/2.1.0/schema.json"},
              "Files/Config/publish_info.json": {"$schema": SCHEMA + "/publishInfo/1.0.0/schema.json",
                  "description": "Read-only controlled hotel finance projection: account balances, payroll "
                  "funding shortfall, transfer policy, eligible approvers and projection freshness. "
                  "Not authoritative for execution or approval. " + PROJECTION_READ_POLICY},
              ".platform": {"$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
                  "metadata": {"type": "DataAgent", "displayName": name},
                  "config": {"version": "2.0", "logicalId": stable_id(name)}}}
    for version in ("draft", "published"):
        prefix = f"Files/Config/{version}"
        result[prefix + "/stage_config.json"] = stage
        result[prefix + "/lakehouse-tables-" + lh_name + "/datasource.json"] = source
        if ontology:
            result[prefix + "/ontologies/" + ontology_name + "/ontology.json"] = {
                "$schema": SCHEMA + "/ontology/1.0.0/schema.json", "artifactId": ontology,
                "workspaceId": ws, "displayName": ontology_name,
                "elements": [{"id": entity, "is_selected": True, "display_name": entity,
                              "type": "ontology.entity", "description": FRESHNESS, "children": []}
                             for entity in ("FinanceAccount", "PayrollObligation", "TransferPolicy")],
                "entitiesWithUserAuthoredDescriptions": ["FinanceAccount", "PayrollObligation", "TransferPolicy"]}
    return parts(result)


def validate_sources(definition, ws, lh, ontology):
    values = decoded(definition)
    sources = []
    for path, value in values.items():
        if path.endswith(("/datasource.json", "/ontology.json")):
            if value["workspaceId"] != ws or value["artifactId"] not in {lh, ontology}:
                raise ValueError("Non-finance source in definition")
            sources.append({"path": path, "workspace_id": value["workspaceId"],
                            "artifact_id": value["artifactId"]})
            if path.endswith("/datasource.json"):
                if value.get("type") != "lakehouse_tables":
                    raise ValueError("Only managed finance lakehouse tables are permitted")
                selected_tables = []
                def inspect(nodes):
                    for node in nodes:
                        if node.get("type") == "lakehouse_tables.table" and node.get("is_selected"):
                            selected_tables.append(node["display_name"])
                        inspect(node.get("children", []))
                inspect(value.get("elements", []))
                expected = {"accounts", "payroll_obligations", "transfer_policy",
                            "eligible_approvers", "projection_status", *EMPTY_SCHEMAS}
                if set(selected_tables) != expected or len(selected_tables) != len(expected):
                    raise ValueError("Selected table policy differs from the finance-only allowlist")
    published = [s for s in sources if "/published/" in s["path"]]
    if not published or not any(s["artifact_id"] == lh for s in published):
        raise ValueError("Published finance lakehouse source missing")
    return sources


class Provisioner:
    def __init__(self, config, environment, output):
        self.config, self.environment, self.output = config, environment, output
        self.previous = json.loads(output.read_text()) if output.exists() else {}
        if self.previous.get("workspace_id") and (
            self.previous.get("tenant_id") != config["tenantId"] or
            self.previous.get("environment") != environment
        ):
            raise ValueError("Existing output belongs to another tenant/environment; archive it first")
        self.api = FabricRest()
        evidence = ROOT / ".state/evidence/fabric-rest.json"
        if evidence.exists():
            self.api.events = json.loads(evidence.read_text()).get("requests", [])
        self.marker = f"finance-analytics-bootstrap/v1/{config['tenantId']}/{environment}"
        self.state = {**self.previous, "environment": environment, "tenant_id": config["tenantId"],
                      "status": "provisioning",
                      "validation_identity_kind": "bootstrap_operator_only",
                      "hired_agent_access": (
                          "pending_fabric_authorization_and_identity_query_test"
                          if self.previous.get("hired_agent_users") else "pending_actual_hire"), "blockers": [
                          b for b in self.previous.get("blockers", [])
                          if b.get("component") in ("ontology_relationship_query", "ontology_graph_refresh_api")]}

    def save(self):
        dump(self.output, self.state)
        dump(ROOT / ".state/evidence/fabric-rest.json",
             {"identity_kind": "bootstrap_operator_only", "requests": self.api.events})

    def owned(self, values, name, type=None):
        hits = [v for v in values if v["displayName"] == name and
                (type is None or v.get("type") == type)]
        if len(hits) > 1:
            raise ValueError(f"Ambiguous owned name {name}")
        if hits and hits[0].get("description") != self.marker:
            raise ValueError(f"Name collision with unowned resource {name}; preserving it")
        return hits[0] if hits else None

    def item(self, ws, name, type, definition=None, creation=None):
        url = f"/workspaces/{ws}/items"
        found = self.owned(self.api.listing(url), name, type)
        if found:
            return found
        payload = {"displayName": name, "type": type, "description": self.marker}
        if definition:
            payload["definition"] = definition
        if creation:
            payload["creationPayload"] = creation
        self.api.request("post", url, payload, result_required=False,
                         skill="fabriciq-ontology-cli" if type == "Ontology" else "spark-cli")
        found = self.owned(self.api.listing(url), name, type)
        if not found:
            raise RuntimeError(f"{type} creation succeeded but item not listed")
        return found

    def run_notebook(self, ws, nb):
        url = f"/workspaces/{ws}/items/{nb}/jobs/instances"
        history = self.api.listing(url)
        completed = [j for j in history if j.get("status") == "Completed"]
        if completed:
            return completed[0]
        recent_cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
        def started(job):
            value = datetime.fromisoformat(job["startTimeUtc"].replace("Z", "+00:00"))
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        recent = [j for j in history if started(j) >= recent_cutoff]
        if recent:
            job = recent[0]
            if job.get("status") in ("Failed", "Cancelled", "Canceled"):
                raise RuntimeError("Recent notebook job terminal failure: " + json.dumps(job))
            location = url + "/" + job["id"]
        else:
            status, headers, response = self.api.raw("post", url + "?jobType=RunNotebook", {})
            location = headers.get("location")
            if not location:
                raise RuntimeError("Job accepted without Location; inspect history, do not retry")
            self.state["notebook_job_id"] = location.rsplit("/", 1)[-1]
            self.save()
        deadline = time.monotonic() + 1200
        while time.monotonic() < deadline:
            job = self.api.request("get", location)
            self.state["notebook_job"] = job
            self.save()
            if job.get("status") == "Completed":
                return job
            if job.get("status") in ("Failed", "Cancelled", "Canceled"):
                raise RuntimeError("Notebook failed: " + json.dumps(job))
            time.sleep(15)
        raise TimeoutError("Notebook deadline; job ID persisted, no resubmit")

    def read_tables(self, ws, lh, name, expected):
        base = f"https://onelake.table.fabric.microsoft.com/delta/{ws}/{lh}/api/2.1/unity-catalog/tables"
        params = {"catalog_name": name + ".Lakehouse", "schema_name": "dbo"}
        tables = []
        while True:
            page = self.api.raw("get", base + "?" + urlencode(params),
                                resource="https://storage.azure.com")[2]
            tables.extend(page["tables"])
            if not page.get("next_page_token"):
                break
            params["page_token"] = page["next_page_token"]
        names = {v["name"] for v in tables}
        if set(expected) - names:
            raise RuntimeError("Managed table readback missing: " + str(set(expected) - names))
        details = []
        for table in tables:
            if table["name"] not in expected:
                raise RuntimeError("Unexpected table in owned finance lakehouse; refusing selection")
            value = self.api.raw("get", base + "/" + quote(
                name + ".Lakehouse.dbo." + table["name"], safe=""),
                resource="https://storage.azure.com")[2]
            if value["data_source_format"] != "DELTA" or (
                f"/{ws}/{lh}/Tables/dbo/" not in value["storage_location"]
            ):
                raise RuntimeError("Table is not managed finance Delta")
            columns = {c["name"] for c in value["columns"]}
            baseline_columns = {c for c, _ in table_schemas(expected)[table["name"]]}
            additions = {"source_etag", *REFRESH_EXTRA_COLUMNS.get(table["name"], {})}
            if not baseline_columns <= columns or columns - baseline_columns - additions:
                raise RuntimeError("Managed finance schema mismatch: " + table["name"])
            details.append(value)
        dump(ROOT / ".state/evidence/fabric-tables.json",
             {"identity_kind": "bootstrap_operator_only", "tables": details})
        return details

    def run(self):
        account = cli("account", "show")
        if account["tenantId"] != self.config["tenantId"] or account["id"] != self.config["subscriptionId"]:
            raise ValueError("CLI tenant/subscription mismatch")
        if account["user"]["type"] != "user" or account["user"]["name"].lower() != self.config["managerUpn"].lower():
            raise ValueError("Bootstrap requires the configured licensed human operator, never runtime fallback")
        approvers = [cli("ad", "user", "show", "--id", upn) for upn in self.config["approverUpns"]]
        if len(approvers) != 2 or len({p["id"] for p in approvers}) != 2:
            raise ValueError("Exactly two distinct configured approvers required")
        self.state["operator_upn"] = account["user"]["name"]
        self.state["resolved_approvers"] = [{"id": p["id"], "upn": p["userPrincipalName"]} for p in approvers]
        settings = self.config.get("fabric", {})
        capacities = self.api.listing("/capacities")
        desired = settings.get("capacityId") or os.getenv("FABRIC_CAPACITY_ID")
        candidates = [c for c in capacities if c["id"] == desired] if desired else [
            c for c in capacities if c["state"] == "Active" and
            (c["displayName"] == settings["capacityName"] if settings.get("capacityName") else c["sku"] == "F16")]
        if len(candidates) != 1 or candidates[0]["state"] != "Active":
            raise ValueError("Configure one existing active Fabric capacity; none will be created/resumed")
        capacity = candidates[0]
        self.state["capacity"] = capacity
        name = settings.get("workspaceName", f"FinanceAnalytics-{self.environment}")
        workspaces = self.api.listing("/workspaces")
        workspace = self.owned(workspaces, name)
        if not workspace:
            workspace = self.api.request("post", "/workspaces",
                {"displayName": name, "description": self.marker, "capacityId": capacity["id"]})
        ws = workspace["id"]
        workspace = self.api.request("get", f"/workspaces/{ws}")
        if workspace.get("capacityId") != capacity["id"]:
            raise ValueError("Owned workspace capacity mismatch; no reassignment")
        self.state["workspace_id"] = ws
        self.save()
        lh_name = "FinanceLedgerAnalytics"
        lh = self.item(ws, lh_name, "Lakehouse", creation={"enableSchemas": True})["id"]
        self.state["lakehouse_id"] = lh
        captured = self.previous.get("projection_freshness", {}).get("captured_at") or datetime.now(timezone.utc).isoformat()
        tables = baseline_tables(self.config, captured, approvers)
        self.state["projection_freshness"] = self.previous.get("projection_freshness") or {"captured_at": captured,
            "ledger_version": "initial-baseline-not-live-ledger", "ledger_status": "initial_baseline",
            "authoritative": False, "notice": FRESHNESS, "refresh": "parent_runtime_pending"}
        self.save()
        nb = self.item(ws, "FinanceInitialBaseline", "Notebook",
                       notebook_definition(tables, ws, lh, lh_name))["id"]
        self.state["notebook_id"] = nb
        self.save()
        self.state["notebook_job"] = self.run_notebook(ws, nb)
        self.state["lakehouse_tables"] = self.read_tables(ws, lh, lh_name, tables)
        ontology_name = "FinanceLedgerOntology"
        try:
            ontology = self.item(ws, ontology_name, "Ontology",
                ontology_definition(tables, ws, lh, ontology_name))["id"]
            self.state["ontology_id"] = ontology
            self.save()
            definition = self.api.request("post", f"/workspaces/{ws}/items/{ontology}/getDefinition", {},
                                          skill="fabriciq-ontology-cli")
            dump(ROOT / ".state/evidence/fabric-ontology.json",
                 {"identity_kind": "bootstrap_operator_only", "definition": definition})
        except RuntimeError as exc:
            ontology = None
            self.state["ontology_id"] = None
            self.state["blockers"].append({"component": "ontology", "error": str(exc)})
            self.save()
        agent_name = "FinanceAnalyticalAgent"
        expected = dataagent_definition(tables, ws, lh, lh_name, ontology, ontology_name, agent_name)
        validate_sources(expected, ws, lh, ontology)
        da = self.item(ws, agent_name, "DataAgent", expected)["id"]
        self.state["data_agent_id"] = da
        self.state["mcp_url"] = f"{FABRIC}/v1/mcp/workspaces/{ws}/dataagents/{da}/agent"
        self.save()
        actual = self.api.request("post", f"/workspaces/{ws}/items/{da}/getDefinition", {})
        sources = validate_sources(actual, ws, lh, ontology)
        dump(ROOT / ".state/evidence/fabric-dataagent-definition.json",
             {"identity_kind": "bootstrap_operator_only", "definition": actual, "sources": sources})
        self.state["selected_sources_verified"] = sources
        self.save()
        self.verify_mcp()
        self.state["status"] = "verified_operator_only"
        self.save()
        return self.state

    def verify_mcp(self):
        url = self.state["mcp_url"]
        headers = {"Accept": "application/json, text/event-stream"}
        message_id = 0
        def rpc(method, params=None, notification=False):
            nonlocal message_id
            message_id += 1
            body = {"jsonrpc": "2.0", "method": method}
            if not notification:
                body["id"] = message_id
            if params is not None:
                body["params"] = params
            status, returned, value = self.api.raw("post", url, body, headers)
            if returned.get("mcp-session-id"):
                headers["Mcp-Session-Id"] = returned["mcp-session-id"]
            if value.get("error"):
                raise RuntimeError("MCP error: " + json.dumps(value["error"]))
            return value
        init = rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "finance-bootstrap-operator-validation", "version": "1.0"}})
        headers["MCP-Protocol-Version"] = init["result"]["protocolVersion"]
        rpc("notifications/initialized", notification=True)
        tools = rpc("tools/list")
        advertised = tools["result"]["tools"]
        if len(advertised) != 1:
            raise ValueError("Expected one genuine DataAgent MCP tool")
        tool = advertised[0]
        properties = tool["inputSchema"]["properties"]
        required = tool["inputSchema"].get("required", [])
        question_field = next((p for p in required if properties[p].get("type") == "string"),
                              next(iter(properties)))
        queries = []
        for question in (
            "Using ONLY the configured finance lakehouse, list both account IDs and balance_cents, "
            "calculate payroll shortfall_cents by joining payroll_obligations to accounts, and return "
            "captured_at, ledger_version, ledger_status and authoritative. Also query the transfers "
            "table count. Do not assume or infer numbers from instructions.",
            "What finance data sources and tables are configured for this agent, and what are "
            "their freshness limitations? Are loan records or credit scores included in those "
            "configured sources?",
        ):
            answer = rpc("tools/call", {"name": tool["name"],
                                       "arguments": {question_field: question}})
            queries.append({"question": question, "response": answer})
            dump(ROOT / ".state/evidence/fabric-mcp.json", {
                "identity_kind": "bootstrap_operator_only", "hired_agent_access": "not_tested",
                "initialize": init, "tools": tools, "queries": queries})
            if answer.get("result", {}).get("isError"):
                raise RuntimeError("DataAgent tool returned error")
        text = json.dumps(queries[0]["response"])
        normalized = text.replace(",", "")
        freshness = self.state["projection_freshness"]
        expected_fields = ["hotel-reserve", "hotel-payroll", freshness["ledger_status"],
                           "captured_at", "ledger_version", "authoritative"]
        if freshness["ledger_status"] == "initial_baseline":
            expected_fields += ["5000000", "1000000", "1500000"]
        else:
            expected_fields += [freshness["ledger_version"]]
            normalized = normalized.replace('\\"', '"')
        for expected in expected_fields:
            if expected not in normalized:
                raise RuntimeError("MCP response missing source-bound projection field: " + expected)
        self.state["proof"] = {**self.state.get("proof", {}), "identity_kind": "bootstrap_operator_only",
            "mcp_handshake": True, "tool_schema_discovered": True,
            "queries_completed": len(queries), "evidence": ".state/evidence/fabric-mcp.json",
            "selected_source_policy": "finance_allowlist_verified_by_published_definition_readback",
            "hired_agent_authorization_verified": False}
        boundary_text = json.dumps(queries[1]["response"])
        self.state["proof"]["source_boundary_query"] = (
            "answered" if "FinanceLedgerAnalytics" in boundary_text else
            "no_configured_source_details_returned")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--environment", choices=("dev", "test", "prod"), default="dev",
                        help="Isolated owned namespace. Default is controlled dev.")
    parser.add_argument("--output", type=Path, default=ROOT / ".state/fabric-output.json")
    args = parser.parse_args()
    if args.output.resolve() != (ROOT / ".state/fabric-output.json").resolve():
        parser.error("Output must be the ignored .state/fabric-output.json")
    provisioner = Provisioner(json.loads(args.config.read_text()), args.environment, args.output)
    try:
        result = provisioner.run()
        print(json.dumps({key: result.get(key) for key in (
            "status", "workspace_id", "lakehouse_id", "ontology_id", "data_agent_id",
            "mcp_url", "projection_freshness", "proof", "blockers")}, indent=2))
    except Exception as exc:
        provisioner.state["status"] = "blocked"
        provisioner.state["blockers"].append({"component": "provisioning", "error": redact(str(exc))})
        provisioner.save()
        print(redact(str(exc)), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
