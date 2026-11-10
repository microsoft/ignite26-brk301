#!/usr/bin/env python3
"""Install/execute a bank-only projection notebook from a captured real ledger snapshot.

No snapshot is synthesized. Installation is operator-only; execution rejects human
CLI credentials and independently checks the executing principal in the notebook.
"""
from __future__ import annotations

import argparse
import base64
import inspect
import json
from pathlib import Path
import sys

from fabric_rest import FABRIC, FabricRest, cli, redact
from provision_fabric import (
    ROOT, FRESHNESS, PROJECTION_READ_POLICY, REFRESH_EXTRA_COLUMNS, Provisioner, decoded, dump, parts,
    stable_id, validate_sources,
)
sys.path.insert(0, str(ROOT / "src"))
from payroll_agents.projection import (
    text, integer, validate_snapshot, project_snapshot, ProjectionConfig, HttpResponse,
    ProjectionRefreshError, refresh_projection, REFRESH_SCHEMAS,
)


def projection_agent_policy(api, state, provisioner, *, refreshed=False):
    """Update only owned agent policy/selected metadata, never source IDs or rows."""
    ws, agent = state["workspace_id"], state["data_agent_id"]
    item = api.request("get", f"/workspaces/{ws}/items/{agent}")
    if not provisioner.owned([item], "FinanceAnalyticalAgent", "DataAgent"):
        raise ValueError("Refresh policy target is not the owned finance DataAgent")
    url = f"/workspaces/{ws}/items/{agent}"
    original = api.request("post", url + "/getDefinition", {})
    validate_sources(original, ws, state["lakehouse_id"], state["ontology_id"])

    def policy(value):
        if isinstance(value, dict):
            return {k: policy(v) for k, v in value.items()}
        if isinstance(value, list):
            return [policy(v) for v in value]
        if isinstance(value, str):
            return value.replace(FRESHNESS, PROJECTION_READ_POLICY)
        return value

    values = policy(decoded(original))
    if refreshed:
        def columns(nodes):
            for node in nodes:
                if node.get("type") == "lakehouse_tables.table":
                    table = node["display_name"]
                    additions = {"source_etag": "string", **REFRESH_EXTRA_COLUMNS.get(table, {})}
                    existing = {v["display_name"] for v in node["children"]}
                    for name, dtype in additions.items():
                        if name not in existing:
                            node["children"].append({
                                "id": stable_id(f"{ws}/{state['lakehouse_id']}/{table}/{name}"),
                                "is_selected": True, "display_name": name,
                                "type": "lakehouse_tables.column", "data_type": dtype,
                                "description": None, "children": []})
                columns(node.get("children", []))
        for path, value in values.items():
            if path.endswith("/datasource.json"):
                columns(value["elements"])
    desired = parts(values)
    validate_sources(desired, ws, state["lakehouse_id"], state["ontology_id"])
    if values != decoded(original):
        api.request("post", url + "/updateDefinition", {"definition": desired}, result_required=False)
    actual = api.request("post", url + "/getDefinition", {})
    validate_sources(actual, ws, state["lakehouse_id"], state["ontology_id"])
    if decoded(actual) != values:
        raise RuntimeError("Published projection policy/metadata readback mismatch")
    return actual


def refresh_notebook(state, expected, bank_principal_id):
    # The same pure validation/projection functions run at the trust boundary in Spark.
    validators = "\n".join(inspect.getsource(f) for f in (text, integer, validate_snapshot, project_snapshot))
    code = (
        "import json, hashlib\nfrom datetime import datetime, timezone\n"
        "import notebookutils\n"
        f"EXPECTED = json.loads({json.dumps(json.dumps(expected))})\n"
        f"BANK_PRINCIPAL_ID = {bank_principal_id!r}\n"
        "if notebookutils.runtime.context['userId'].lower() != BANK_PRINCIPAL_ID.lower():\n"
        "    raise PermissionError('Only the installed bank root principal may refresh finance projections')\n"
        + validators +
        f"\nSCHEMAS = json.loads({json.dumps(json.dumps(REFRESH_SCHEMAS))})\n"
        "if not snapshot_json or not expected_projection_etag or not refresh_id:\n"
        "    raise ValueError('Captured snapshot, expected projection ETag and refresh ID are required')\n"
        "envelope = json.loads(snapshot_json)\n"
        "tables = project_snapshot(envelope, EXPECTED)\n"
        "digest = hashlib.sha256(json.dumps(envelope['state'], sort_keys=True, separators=(',',':')).encode()).hexdigest()\n"
        "def literal(value):\n"
        "    return \"'\" + str(value).replace(\"'\", \"''\") + \"'\"\n"
        "manifest = 'dbo.projection_status'\n"
        "additions = {'source_etag':'STRING','projection_state':'STRING','snapshot_sha256':'STRING',\n"
        "    'refresh_id':'STRING','requested_source_etag':'STRING','requested_snapshot_sha256':'STRING',\n"
        "    'previous_source_etag':'STRING','last_error':'STRING','source_authoritative':'BOOLEAN'}\n"
        "existing_columns = set(spark.table(manifest).columns)\n"
        "missing = [f'{k} {v}' for k,v in additions.items() if k not in existing_columns]\n"
        "if missing:\n"
        "    spark.sql('ALTER TABLE ' + manifest + ' ADD COLUMNS (' + ','.join(missing) + ')')\n"
        "rows = spark.table(manifest).collect()\n"
        "if len(rows) != 1:\n"
        "    raise ValueError('Expected one projection manifest; refusing ambiguous state')\n"
        "previous = rows[0].asDict()\n"
        "prior_etag = previous['source_etag'] or previous['ledger_version']\n"
        "if previous['source_etag'] == envelope['source_etag'] and previous['projection_state'] == 'complete':\n"
        "    if previous['snapshot_sha256'] != digest:\n"
        "        raise ValueError('Same source ETag with different state; refusing tampering')\n"
        "    print(json.dumps({'status':'unchanged','source_etag':prior_etag}))\n"
        "else:\n"
        "    incoming = datetime.fromisoformat(envelope['captured_at'].replace('Z','+00:00')).replace(tzinfo=None)\n"
        "    if incoming < previous['captured_at']:\n"
        "        raise ValueError('Refusing an older captured snapshot')\n"
        "    resume = previous['projection_state'] in ('refreshing','failed') and previous['refresh_id'] == refresh_id\n"
        "    if resume:\n"
        "        if previous['requested_source_etag'] != envelope['source_etag'] or previous['requested_snapshot_sha256'] != digest:\n"
        "            raise ValueError('Resume must use identical captured source')\n"
        "    else:\n"
        "        if prior_etag != expected_projection_etag or previous['projection_state'] not in (None,'complete'):\n"
        "            raise ValueError('Projection version conflict or another refresh owns the manifest')\n"
        "        sql = 'UPDATE ' + manifest + ' SET projection_state=\\'refreshing\\', ledger_status=\\'refreshing\\', refresh_id=' + literal(refresh_id)\n"
        "        sql += ', requested_source_etag=' + literal(envelope['source_etag']) + ', requested_snapshot_sha256=' + literal(digest)\n"
        "        sql += ', previous_source_etag=' + literal(prior_etag) + ', last_error=NULL'\n"
        "        sql += ' WHERE COALESCE(source_etag,ledger_version)=' + literal(expected_projection_etag)\n"
        "        sql += ' AND COALESCE(projection_state,\\'complete\\')=\\'complete\\''\n"
        "        spark.sql(sql).collect()\n"
        "        acquired = spark.table(manifest).first().asDict()\n"
        "        if acquired['refresh_id'] != refresh_id or acquired['projection_state'] != 'refreshing':\n"
        "            raise ValueError('Could not acquire projection refresh ownership')\n"
        "    try:\n"
        "        for name, records in tables.items():\n"
        "            schema = SCHEMAS[name] + ',captured_at timestamp,ledger_version string,source_etag string,ledger_status string,authoritative boolean'\n"
        "            converted = [{**r,'captured_at':datetime.fromisoformat(r['captured_at'].replace('Z','+00:00'))} for r in records]\n"
        "            frame = spark.createDataFrame(converted,schema)\n"
        "            frame.write.format('delta').mode('overwrite').option('overwriteSchema','true').saveAsTable('dbo.'+name)\n"
        "            loaded = spark.table('dbo.'+name)\n"
        "            if loaded.count() != len(records) or loaded.where('source_etag IS NULL OR source_etag <> ' + literal(envelope['source_etag'])).count():\n"
        "                raise ValueError('Projection quality gate failed: ' + name)\n"
        "        sql = 'UPDATE ' + manifest + ' SET source_etag=' + literal(envelope['source_etag'])\n"
        "        sql += ', ledger_version=' + literal(envelope['source_etag'])\n"
        "        sql += ', captured_at=CAST(' + literal(envelope['captured_at']) + ' AS TIMESTAMP)'\n"
        "        sql += ', ledger_status=\\'captured_snapshot\\', projection_state=\\'complete\\', source_authoritative=true, authoritative=false'\n"
        "        sql += ', fixture_kind=\\'transactional_ledger_capture\\', snapshot_sha256=' + literal(digest)\n"
        "        sql += ', transaction_count=' + str(len(envelope['state']['transactions']))\n"
        "        sql += ', approval_count=' + str(len(envelope['state']['approvals']))\n"
        "        sql += ', notification_count=' + str(len(envelope['state']['outbox']))\n"
        "        sql += ', freshness_notice=\\'Captured authoritative source snapshot, not a live balance guarantee. Compare source_etag to current ledger before acting; block while projection_state is not complete.\\''\n"
        "        sql += ', last_error=NULL WHERE refresh_id=' + literal(refresh_id)\n"
        "        spark.sql(sql).collect()\n"
        "        result = spark.table(manifest).first().asDict()\n"
        "        if result['projection_state'] != 'complete' or result['source_etag'] != envelope['source_etag']:\n"
        "            raise ValueError('Projection manifest commit verification failed')\n"
        "        print(json.dumps({'status':'complete','source_etag':result['source_etag'],'snapshot_sha256':digest}))\n"
        "    except Exception:\n"
        "        spark.sql('UPDATE '+manifest+' SET projection_state=\\'failed\\',ledger_status=\\'failed\\',last_error=\\'Refresh failed; do not consume mixed tables\\' WHERE refresh_id='+literal(refresh_id)).collect()\n"
        "        raise\n"
        "final = spark.table(manifest).first().asDict()\n"
        "if final['projection_state'] != 'complete':\n"
        "    raise ValueError('Projection manifest is not complete')\n"
        "proof = {k:final[k] for k in ('source_etag','ledger_version','ledger_status','projection_state',\n"
        "    'authoritative','source_authoritative','snapshot_sha256','refresh_id','transaction_count',\n"
        "    'approval_count','notification_count')}\n"
        "proof['captured_at'] = final['captured_at'].replace(tzinfo=timezone.utc).isoformat()\n"
        "notebookutils.fs.put('Files/finance_projection_manifest.json',json.dumps(proof),True)\n"
        "print(json.dumps(proof))\n"
    )
    notebook = {"nbformat": 4, "nbformat_minor": 5, "metadata": {
        "kernelspec": {"name": "synapse_pyspark", "display_name": "Synapse PySpark"},
        "language_info": {"name": "python"}, "dependencies": {"lakehouse": {
            "default_lakehouse": state["lakehouse_id"], "default_lakehouse_name": "FinanceLedgerAnalytics",
            "default_lakehouse_workspace_id": state["workspace_id"],
            "known_lakehouses": [{"id": state["lakehouse_id"]}]}}},
        "cells": [
            {"cell_type": "code", "metadata": {"tags": ["parameters"]}, "execution_count": None, "outputs": [],
             "source": ["snapshot_json = ''\n", "expected_projection_etag = ''\n", "refresh_id = ''\n"]},
            {"cell_type": "code", "metadata": {}, "execution_count": None, "outputs": [],
             "source": code.splitlines(keepends=True)}]}
    compile(code, "finance-runtimeion-refresh", "exec")
    return parts({"notebook-content.ipynb": notebook}, "ipynb")


def token_identity(expected_tenant, expected_principal):
    """Tokens stay in memory. No human CLI credential can pass runtime preflight."""
    account = cli("account", "show")
    if account["tenantId"] != expected_tenant or account["user"]["type"].lower() == "user":
        raise PermissionError("Runtime refresh requires keyless workload CLI context, never operator fallback")
    token = cli("account", "get-access-token", "--resource", FABRIC)["accessToken"]
    payload = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    if claims.get("tid") != expected_tenant or claims.get("oid") != expected_principal:
        raise PermissionError("Workload token tenant/object ID mismatch")
    if claims.get("aud", "").rstrip("/") != FABRIC:
        raise PermissionError("Workload token Fabric audience mismatch")
    return {"tenant_id": claims["tid"], "principal_id": claims["oid"],
            "managed_identity_claim_present": bool(claims.get("xms_mirid"))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--install", action="store_true", help="Operator-only create-if-absent notebook installation")
    parser.add_argument("--principal-id", required=True, help="Exact bank root principal object ID, not a hired placeholder")
    parser.add_argument("--snapshot", type=Path, help="Captured real source envelope; no fixture accepted as runtime fallback")
    parser.add_argument("--expected-projection-etag", help="Last complete projection source_etag; baseline uses its ledger_version label")
    parser.add_argument("--refresh-id", help="Unique caller ID; explicitly reuse only to resume the identical failed capture")
    parser.add_argument("--validate-only", action="store_true", help="Local contract validation, no cloud write")
    parser.add_argument("--refresh-graph", action="store_true",
                        help="Also request GraphModel refresh; may fail explicitly in preview")
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    state_path = ROOT / ".state/fabric-output.json"
    state = json.loads(state_path.read_text())
    api = FabricRest()
    evidence = {"identity_kind": "local_validation" if args.validate_only else
                "bootstrap_operator_only" if args.install else "bank_workload",
                "live_refresh_executed": False}
    try:
        if args.install:
            if args.snapshot or args.validate_only:
                raise ValueError("--install cannot execute or fabricate a snapshot")
            account = cli("account", "show")
            if account["tenantId"] != config["tenantId"] or account["id"] != config["subscriptionId"] or (
                account["user"]["type"] != "user" or account["user"]["name"].lower() != config["managerUpn"].lower()
            ):
                raise PermissionError("Installation requires configured operator")
            principal = cli("ad", "sp", "show", "--id", args.principal_id)
            if principal["id"] != args.principal_id:
                raise ValueError("Use the supplied bank principal object ID, not an app alias")
            approvers = [cli("ad", "user", "show", "--id", upn)["id"] for upn in config["approverUpns"]]
            expected = {"tenant_id": config["tenantId"], "owner_id": config["hotelOwnerId"],
                        "approver_ids": approvers, "approver_upns": config["approverUpns"]}
            provisioner = Provisioner(config, state["environment"], state_path)
            provisioner.api = api
            desired = refresh_notebook(state, expected, args.principal_id)
            notebook = provisioner.item(state["workspace_id"], "FinanceLedgerProjectionRefresh",
                "Notebook", desired)
            actual = api.request("post",
                f"/workspaces/{state['workspace_id']}/items/{notebook['id']}/getDefinition?format=ipynb", {})
            deployed = decoded(actual)["notebook-content.ipynb"]
            authored = decoded(desired)["notebook-content.ipynb"]
            if [v["source"] for v in deployed["cells"]] != [v["source"] for v in authored["cells"]]:
                raise RuntimeError("Existing refresh notebook definition drift; explicit migration required")
            contract = {"contract_version": 1, "notebook_id": notebook["id"],
                        "bank_principal_id": args.principal_id, "expected_source_policy": expected,
                        "principal_type_observed": principal["servicePrincipalType"],
                        "execution_verified": False,
                        "identity_support": "ServiceIdentity/AgentIdentity not explicitly supported by Fabric docs; live test pending"}
            evidence["data_agent_policy_readback"] = projection_agent_policy(api, state, provisioner)
            state["projection_refresh"] = contract
            dump(state_path, state)
            evidence["installation"] = contract
            evidence["notebook_definition_readback"] = actual
            dump(ROOT / ".state/evidence/fabric-projection-install.json", evidence)
            print(json.dumps(contract, indent=2))
            return 0
        if not args.snapshot:
            raise ValueError("A real captured source --snapshot is required; no fixture is synthesized")
        contract = state.get("projection_refresh")
        if not contract or contract["bank_principal_id"] != args.principal_id:
            raise ValueError("Install a bank-bound notebook first; principal must match its binding")
        envelope = json.loads(args.snapshot.read_text())
        tables = project_snapshot(envelope, contract["expected_source_policy"])
        payload = json.dumps(envelope, separators=(",", ":"))
        if len(payload.encode()) > 65536:
            raise ValueError("Bounded inline contract is limited to 64 KiB; no silent truncation")
        evidence["source_etag"], evidence["captured_at"] = envelope["source_etag"], envelope["captured_at"]
        evidence["projected_row_counts"] = {k: len(v) for k, v in tables.items()}
        if args.validate_only:
            print(json.dumps(evidence, indent=2))
            return 0
        if not args.expected_projection_etag:
            raise ValueError("--expected-projection-etag is required for explicit optimistic concurrency")
        evidence["identity"] = token_identity(config["tenantId"], args.principal_id)
        def get_token(scope):
            token_identity(config["tenantId"], args.principal_id)
            return cli("account", "get-access-token", "--resource",
                       scope.removesuffix("/.default"))["accessToken"]
        def send(method, url, *, headers, body, timeout_seconds):
            # Do not put tokens in az argv. Recheck the keyless CLI identity for
            # every call, then let az acquire its own token for the same audience.
            token_identity(config["tenantId"], args.principal_id)
            resource = FABRIC if url.startswith(FABRIC + "/") else "https://storage.azure.com"
            safe_headers = {k: v for k, v in headers.items()
                            if k.lower() not in ("authorization", "x-ms-fabric-skill")}
            status, response_headers, response = api.raw(
                method, url, body, headers=safe_headers, resource=resource,
                timeout_seconds=timeout_seconds)
            return HttpResponse(status, response_headers, response)
        def checkpoint(progress):
            evidence.update(progress)
            if progress.get("committed_manifest"):
                state["projection_freshness"] = progress["projection_freshness"]
                dump(state_path, state)
            dump(ROOT / ".state/evidence/fabric-projection-refresh.json", evidence)
        result = refresh_projection(envelope, config=ProjectionConfig.from_output(state),
            expected_projection_etag=args.expected_projection_etag,
            credential_kind="agent_identity", get_token=get_token, send=send,
            refresh_id=args.refresh_id, checkpoint=checkpoint)
        evidence.update(result)
        if args.refresh_graph:
            from fabric_graph import refresh_graph, resolve_graph
            graph, _, _ = resolve_graph(api, state)
            evidence["graph_refresh_job"] = refresh_graph(api, state["workspace_id"], graph["id"], force=True,
                checkpoint=lambda v: dump(ROOT / ".state/evidence/fabric-projection-graph-job.json",
                                           {**v, "identity_kind": "bank_workload"}))
            state["projection_freshness"]["graph_refresh_pending"] = False
            dump(state_path, state)
        print(json.dumps(evidence, indent=2))
        return 0
    except Exception as exc:
        if isinstance(exc, ProjectionRefreshError):
            evidence.update(exc.progress)
        evidence["error"] = redact(str(exc))
        print(redact(str(exc)), file=sys.stderr)
        return 1
    finally:
        evidence["requests"] = api.events
        dump(ROOT / ".state/evidence/fabric-projection-refresh.json", evidence)


if __name__ == "__main__":
    sys.exit(main())
