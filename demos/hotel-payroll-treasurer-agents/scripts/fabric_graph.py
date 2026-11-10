#!/usr/bin/env python3
"""Refresh owned ontology backing graph and prove relationship through graph-only MCP."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import time

from fabric_mcp import query_mcp, response_text
from fabric_rest import FABRIC, FabricRest, cli, redact
from provision_fabric import (
    ROOT, Provisioner, baseline_tables, dataagent_definition, decoded, dump, numeric_id, parts,
)

SKILL = "fabriciq-ontology-cli"

FUNDING_QUERY = (
    "MATCH (p:`PayrollObligation`)-[r:`PayrollFundedBy`]->(a:`FinanceAccount`) "
    "RETURN TO_JSON_STRING(p) AS payroll, TO_JSON_STRING(r) AS funding, "
    "TO_JSON_STRING(a) AS account LIMIT 20;"
)


def inspect_indexed_relationship(api, ws, graph_id, expected_ledger_sha256):
    """Read indexed relationships, without refreshing or asserting live-ledger freshness."""
    if not re.fullmatch(r"[0-9a-f]{64}", expected_ledger_sha256):
        raise ValueError("Expected ledger SHA256 must be the complete lowercase hash")
    base = f"/workspaces/{ws}/graphModels/{graph_id}"
    schema = api.request("get", base + "/getQueryableGraphType?beta=true", skill=SKILL)
    nodes = {n["alias"]: n for n in schema["nodeTypes"]}
    edges = [e for e in schema["edgeTypes"] if e["labels"] == ["PayrollFundedBy"]]
    if len(edges) != 1:
        raise ValueError("Queryable graph lacks one unambiguous PayrollFundedBy type")
    edge = edges[0]
    if (nodes[edge["sourceNodeType"]["alias"]]["labels"] != ["PayrollObligation"]
            or nodes[edge["destinationNodeType"]["alias"]]["labels"] != ["FinanceAccount"]):
        raise ValueError("Queryable funding edge has different endpoint types")
    response = api.request("post", base + "/executeQuery?beta=true",
                           {"query": FUNDING_QUERY}, skill=SKILL)
    if response.get("status", {}).get("code") != "00000":
        raise RuntimeError("Indexed GQL query did not complete successfully")
    result = response["result"]
    if result["kind"] != "TABLE" or {c["name"] for c in result["columns"]} != {
        "payroll", "funding", "account"
    }:
        raise ValueError("Indexed GQL query returned an unexpected result shape")
    if (not result["data"] or len(result["data"]) >= 20
            or response.get("continuationToken") or response.get("continuationUri")):
        raise ValueError("Indexed funding result is empty or not proven complete")
    relationships = []
    for row in result["data"]:
        if set(row) != {"payroll", "funding", "account"}:
            raise ValueError("Indexed funding result has unexpected columns")
        payroll, funding, account = (json.loads(row[k]) for k in ("payroll", "funding", "account"))
        if (payroll["labels"] != ["PayrollObligation"]
                or funding["labels"] != ["PayrollFundedBy"]
                or account["labels"] != ["FinanceAccount"]
                or funding["ends"] != [{"oid": payroll["oid"]}, {"oid": account["oid"]}]):
            raise ValueError("Indexed funding relationship endpoints do not match returned nodes")
        source, target = payroll["properties"], account["properties"]
        if (source["source_account_id"] != target["account_id"]
                or type(target["balance_cents"]) is not int
                or type(source["obligation_cents"]) is not int
                or source["ledger_version"] != target["ledger_version"]):
            raise ValueError("Indexed funding properties are inconsistent")
        relationships.append({
            "relationship": "PayrollFundedBy", "payroll": source, "account": target,
            "matches_requested_ledger_sha256": (
                source["ledger_version"] == expected_ledger_sha256
                and target["ledger_version"] == expected_ledger_sha256
            ),
        })
    return {
        "identity_kind": "bootstrap_operator_only", "hired_agent_access": "not_tested",
        "indexed_relationship_query_verified": True,
        "expected_ledger_sha256": expected_ledger_sha256,
        "all_relationships_match_requested_hash": all(
            r["matches_requested_ledger_sha256"] for r in relationships
        ),
        "current_ledger_revalidated": False, "graph_freshness_verified": False,
        "relationships": relationships,
    }


def persist_graph_state(path, evidence):
    """Merge independently captured graph proof without losing a refresh contract."""
    state = json.loads(path.read_text())
    if evidence.get("graph_model_id"):
        state["graph_model_id"] = evidence["graph_model_id"]
    if evidence.get("data_agent_id"):
        state["ontology_query_agent_id"] = evidence["data_agent_id"]
        state["ontology_query_mcp_url"] = evidence["mcp_url"]
    proof = state.setdefault("proof", {})
    if evidence.get("refresh_job", {}).get("status") == "Completed":
        proof["ontology_materialization"] = {
            "status": "completed", "identity_kind": "bootstrap_operator_only",
            "refresh_job_id": evidence["refresh_job"]["id"],
            "evidence": ".state/evidence/fabric-graph.json"}
    proof["ontology_relationship_query"] = {
        "status": evidence["status"],
        "verified": evidence["status"] == "graph_relationship_verified_operator_only",
        "identity_kind": "bootstrap_operator_only",
        "error": evidence.get("error"), "evidence": ".state/evidence/fabric-graph.json"}
    state["blockers"] = [b for b in state.get("blockers", [])
                         if b.get("component") != "ontology_relationship_query"]
    if evidence["status"] == "blocked":
        state["blockers"].append({
            "component": "ontology_relationship_query", "error": evidence.get("error"),
            "identity_kind": "bootstrap_operator_only",
            "evidence": ".state/evidence/fabric-graph.json"})
    dump(path, state)


def validate_graph(definition, ws, lh):
    values = decoded(definition)
    sources = values["dataSources.json"]["dataSources"]
    tables = []
    for source in sources:
        matches = [
            table for table in ("accounts", "payroll_obligations", "transfer_policy")
            if source.get("type") == "DeltaTable" and source["properties"]["path"] in {
                f"abfss://{ws}@{host}/{lh}/Tables/dbo/{table}" for host in (
                    "onelake.pbidedicated.windows.net", "onelake.dfs.fabric.microsoft.com"
                )
            }
        ]
        if len(matches) != 1:
            raise ValueError("Graph references a non-finance source")
        tables.extend(matches)
    if len(tables) != 3 or set(tables) != {"accounts", "payroll_obligations", "transfer_policy"}:
        raise ValueError("Graph references a non-finance source")
    graph = values["graphType.json"]
    edges = graph["edgeTypes"]
    if len(edges) != 1 or edges[0]["labels"] != ["PayrollFundedBy"]:
        raise ValueError("Expected exactly the finance PayrollFundedBy edge")
    if edges[0]["alias"] != numeric_id("PayrollFundedBy"):
        raise ValueError("Graph relationship alias is not the authored ontology alias")
    return values


def resolve_graph(api, state):
    ws, ontology, lh = state["workspace_id"], state["ontology_id"], state["lakehouse_id"]
    name = f"FinanceLedgerOntology_graph_{ontology.replace('-', '')}"
    hits = [v for v in api.listing(f"/workspaces/{ws}/items", skill=SKILL)
            if v["type"] == "GraphModel" and v["displayName"] == name]
    if len(hits) != 1:
        raise RuntimeError("Expected one ontology-owned backing GraphModel; materialization not available")
    graph = hits[0]
    definition = api.request("post", f"/workspaces/{ws}/items/{graph['id']}/getDefinition", {},
                             skill=SKILL)
    values = validate_graph(definition, ws, lh)
    return graph, definition, values


def refresh_graph(api, ws, graph_id, *, force=False, timeout=900, checkpoint=None):
    base = f"/workspaces/{ws}/items/{graph_id}/jobs/instances"
    history = api.listing(base, skill=SKILL)
    running = [j for j in history if j["status"] not in ("Completed", "Failed", "Cancelled", "Canceled")]
    completed = [j for j in history if j["status"] == "Completed" and j["jobType"] == "Refresh"]
    if len(running) > 1:
        raise RuntimeError("Multiple graph refreshes already running; refusing another submission")
    if running:
        location = base + "/" + running[0]["id"]
    elif completed and not force:
        return completed[0]
    else:
        status, headers, body = api.raw("post",
            f"/workspaces/{ws}/items/{graph_id}/jobs/Refresh/instances", {}, skill=SKILL)
        location = headers.get("location")
        if status != 202 or not location:
            raise RuntimeError("Graph refresh lacks a captured 202 Location; do not resubmit")
        if checkpoint:
            checkpoint({"identity_kind": "bootstrap_operator_only", "job_url": location,
                        "status": "submitted"})
        time.sleep(min(max(int(headers.get("retry-after", "5")), 2), 60))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = api.request("get", location, skill=SKILL)
        if checkpoint:
            checkpoint({"identity_kind": "bootstrap_operator_only", "job_url": location, "job": job})
        if job["status"] == "Completed":
            return job
        if job["status"] in ("Failed", "Cancelled", "Canceled"):
            raise RuntimeError("Graph refresh failed: " + json.dumps(job))
        time.sleep(15)
    raise TimeoutError("Graph refresh deadline exceeded; original job URL captured, do not resubmit")


def graph_agent_definition(state, config, approvers):
    ws, lh, ontology = state["workspace_id"], state["lakehouse_id"], state["ontology_id"]
    tables = baseline_tables(config, state["projection_freshness"]["captured_at"], approvers)
    values = decoded(dataagent_definition(tables, ws, lh, "FinanceLedgerAnalytics", ontology,
        "FinanceLedgerOntology", "FinanceOntologyRelationshipAgent"))
    # No relational source can answer the validation question: ontology is exclusive.
    for path in list(values):
        if path.endswith("/datasource.json"):
            del values[path]
    instruction = (
        "Only FinanceLedgerOntology is configured. Use its materialized graph for relationship "
        "questions. Traverse PayrollFundedBy from PayrollObligation to FinanceAccount. Do not "
        "fall back to SQL/lakehouse tables or infer a relationship from names. Return entity "
        "IDs, relationship name, stored properties, captured_at, ledger_version, ledger_status "
        "and authoritative. A PayrollFundedBy edge is a modeled funding-source relationship, "
        "NOT evidence that a transfer was approved or executed. An initial_baseline is controlled "
        "and non-authoritative. Refuse if the ontology cannot be queried."
    )
    for version in ("draft", "published"):
        values[f"Files/Config/{version}/stage_config.json"]["aiInstructions"] = instruction
    values["Files/Config/publish_info.json"]["description"] = (
        "Read-only finance ontology relationship analysis using only FinanceLedgerOntology's "
        "PayrollObligation, FinanceAccount, TransferPolicy and PayrollFundedBy relationship. "
        "No independent lakehouse/SQL source is configured. No authority to approve or execute."
    )
    return parts(values)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--refresh", action="store_true", help="Explicitly request one fresh graph job")
    parser.add_argument("--inspect-only", action="store_true",
                        help="Read the public indexed GQL API without refresh, publication or state changes")
    parser.add_argument("--expected-ledger-sha256",
                        help="Complete captured ledger hash to compare, not proof of current ledger state")
    args = parser.parse_args()
    if args.inspect_only and (args.refresh or not args.expected_ledger_sha256):
        parser.error("--inspect-only requires --expected-ledger-sha256 and forbids --refresh")
    if args.expected_ledger_sha256 and not args.inspect_only:
        parser.error("--expected-ledger-sha256 is only supported with --inspect-only")
    state_path = ROOT / ".state/fabric-output.json"
    state = json.loads(state_path.read_text())
    config = json.loads(args.config.read_text())
    api = FabricRest()
    evidence = {"identity_kind": "bootstrap_operator_only", "hired_agent_access": "not_tested",
                "started_at": datetime.now(timezone.utc).isoformat()}
    try:
        account = cli("account", "show")
        if account["tenantId"] != config["tenantId"] or account["id"] != config["subscriptionId"] or (
            account["user"]["type"] != "user" or account["user"]["name"].lower() != config["managerUpn"].lower()
        ):
            raise ValueError("Graph validation requires configured bootstrap operator/tenant/subscription")
        graph, definition, graph_values = resolve_graph(api, state)
        if args.inspect_only:
            evidence.update(inspect_indexed_relationship(
                api, state["workspace_id"], graph["id"], args.expected_ledger_sha256))
            evidence["graph_model_id"] = graph["id"]
            dump(ROOT / ".state/evidence/fabric-graph-inspection.json", evidence)
            print(json.dumps(evidence, indent=2))
            return 0
        evidence.update({"graph_model_id": graph["id"], "definition": definition})
        dump(ROOT / ".state/evidence/fabric-graph.json", evidence)
        job = refresh_graph(api, state["workspace_id"], graph["id"], force=args.refresh,
            checkpoint=lambda v: dump(ROOT / ".state/evidence/fabric-graph-job.json", v))
        evidence["refresh_job"] = job
        dump(ROOT / ".state/evidence/fabric-graph.json", evidence)
        provisioner = Provisioner(config, state["environment"], state_path)
        provisioner.api = api
        approvers = [cli("ad", "user", "show", "--id", upn) for upn in config["approverUpns"]]
        desired = graph_agent_definition(state, config, approvers)
        item = provisioner.item(state["workspace_id"], "FinanceOntologyRelationshipAgent",
                                "DataAgent", desired)
        agent_id = item["id"]
        actual = api.request("post",
            f"/workspaces/{state['workspace_id']}/items/{agent_id}/getDefinition", {}, skill=SKILL)
        values = decoded(actual)
        if any(p.endswith("/datasource.json") for p in values):
            raise RuntimeError("Graph-only validation DataAgent has a SQL/lakehouse fallback")
        ontology_parts = [v for p, v in values.items() if p.endswith("/ontology.json")]
        if len(ontology_parts) != 2 or any(v["artifactId"] != state["ontology_id"]
                                         or v["workspaceId"] != state["workspace_id"] for v in ontology_parts):
            raise RuntimeError("Graph-only DataAgent source readback mismatch")
        url = f"{FABRIC}/v1/mcp/workspaces/{state['workspace_id']}/dataagents/{agent_id}/agent"
        question = (
            "Using only FinanceLedgerOntology, start at PayrollObligation with task_id "
            "'payroll-funding' and traverse the outgoing PayrollFundedBy relationship. "
            "Which FinanceAccount does it reach? Return the source task_id, edge name, "
            "target account_id, stored balance_cents, captured_at, ledger_version, ledger_status "
            "and authoritative. This asks for an actual relationship traversal, not a SQL join "
            "or an inference about whether payroll was transferred."
        )
        proof = query_mcp(api, url, [question])
        evidence.update({"data_agent_id": agent_id, "mcp_url": url,
                         "source_definition": actual, "mcp": proof})
        dump(ROOT / ".state/evidence/fabric-graph.json", evidence)
        response = proof["queries"][0]["response"]
        text = response_text(response)
        expected_fields = ["payroll-funding", "PayrollFundedBy", "hotel-reserve",
                           "captured_at", "ledger_version", "authoritative",
                           state["projection_freshness"]["ledger_version"]]
        if state["projection_freshness"]["ledger_status"] == "initial_baseline":
            expected_fields.append("5000000")
        if response.get("result", {}).get("isError") or any(
            expected not in text.replace(",", "") for expected in expected_fields):
            raise RuntimeError("Graph-only MCP did not return the genuine expected relationship")
        evidence["status"] = "graph_relationship_verified_operator_only"
        evidence["verified_at"] = datetime.now(timezone.utc).isoformat()
        dump(ROOT / ".state/evidence/fabric-graph.json", evidence)
        persist_graph_state(state_path, evidence)
        print(json.dumps({"status": evidence["status"], "graph_model_id": graph["id"],
                          "refresh_job": job, "data_agent_id": agent_id,
                          "mcp_url": url, "answer": text}, indent=2))
        return 0
    except Exception as exc:
        evidence["status"], evidence["error"] = "blocked", redact(str(exc))
        dump(ROOT / ".state/evidence" / (
            "fabric-graph-inspection.json" if args.inspect_only else "fabric-graph.json"), evidence)
        if not args.inspect_only:
            persist_graph_state(state_path, evidence)
        print(redact(str(exc)), file=sys.stderr)
        return 1
    finally:
        dump(ROOT / ".state/evidence" / (
            "fabric-graph-inspection-rest.json" if args.inspect_only else "fabric-graph-rest.json"),
             {"identity_kind": "bootstrap_operator_only", "requests": api.events})


if __name__ == "__main__":
    sys.exit(main())
