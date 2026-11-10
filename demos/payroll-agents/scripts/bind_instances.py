"""Bind only manager-owned, blueprint-verified hires; never reset financial state."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from urllib.parse import quote

from finance_agents.ledger import Ledger, LedgerConfig, SQLiteStateStore
from finance_agents.projection import ProjectionConfig
from provision_storage import az


def verify_hire(user, identity, manager, expected_blueprint, operator_id, default_identity):
    if (
        user.get("accountEnabled") is not True
        or not user.get("mail")
        or not user.get("identityParentId")
        or user["identityParentId"] != identity["id"]
        or identity.get("accountEnabled") is not True
        or identity.get("agentIdentityBlueprintId") != expected_blueprint
        or manager.get("id") != operator_id
        or identity["id"] == default_identity
        or not identity.get("appId")
    ):
        raise PermissionError("Hire is not a ready, manager-owned child of the expected blueprint")
    return {
        "instance_app_id": identity["appId"],
        "agent_user_object_id": user["id"],
        "identity_object_id": identity["id"],
        "mail": user["mail"],
        "manager_object_id": manager["id"],
    }


def validated_ledger(config, existing=None):
    with tempfile.TemporaryDirectory() as directory:
        store = SQLiteStateStore(Path(directory) / "validation.db")
        if existing is not None:
            if not store.compare_and_swap(None, existing):
                raise RuntimeError("Cannot stage existing ledger validation")
        return Ledger(store, config).snapshot()


def blob_json(storage, container, name, destination):
    exists = az(
        "storage", "blob", "exists", "--account-name", storage["storageAccountName"],
        "--container-name", container, "--name", name, "--auth-mode", "login",
    )
    if not exists["exists"]:
        return None
    az(
        "storage", "blob", "download", "--account-name", storage["storageAccountName"],
        "--container-name", container, "--name", name, "--auth-mode", "login",
        "--file", str(destination), "--overwrite", "true", "--no-progress",
    )
    return json.loads(destination.read_text())


def create_json(storage, container, name, value, destination):
    destination.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    az(
        "storage", "blob", "upload", "--account-name", storage["storageAccountName"],
        "--container-name", container, "--name", name, "--auth-mode", "login",
        "--file", str(destination), "--if-none-match", "*", "--overwrite", "false",
        "--content-type", "application/json", "--no-progress",
    )


def projection_binding(output, config, agents, approvers, mcp_url):
    projection = ProjectionConfig.from_output(output)
    root = agents["agents"]["banking_operations"]["instance_identity"]
    if (
        projection.tenant_id != config["tenantId"]
        or projection.bank_principal_id != root["principal_id"]
        or projection.expected_source_policy["owner_id"] != config["hotelOwnerId"]
        or set(projection.expected_source_policy["approver_ids"])
        != {person["object_id"] for person in approvers}
        or {upn.casefold() for upn in projection.expected_source_policy["approver_upns"]}
        != {upn.casefold() for upn in config["approverUpns"]}
        or mcp_url != (
            f"https://api.fabric.microsoft.com/v1/mcp/workspaces/{projection.workspace_id}"
            f"/dataagents/{projection.data_agent_id}/agent"
        )
    ):
        raise PermissionError("Projection output differs from the verified finance deployment")
    freshness = output["projection_freshness"]
    previous_etag = freshness.get("source_etag", freshness.get("ledger_version"))
    if not isinstance(previous_etag, str) or not previous_etag:
        raise ValueError("An explicit previous analytical version is required")
    return {
        key: output[key] for key in (
            "tenant_id", "environment", "workspace_id", "lakehouse_id",
            "data_agent_id", "ontology_id",
        )
    } | {
        "projection_refresh": {
            "contract_version": 1, "notebook_id": projection.notebook_id,
            "bank_principal_id": root["principal_id"], "bank_client_id": root["client_id"],
            "expected_source_policy": projection.expected_source_policy,
        },
        "projection_freshness": {"source_etag": previous_etag},
    }


def projection_only_upgrade(existing, desired):
    if existing.get("fabric", {}).get("projection") is not None:
        return False
    without_projection = {**desired, "fabric": {
        key: value for key, value in desired["fabric"].items() if key != "projection"
    }}
    return "projection" in desired["fabric"] and existing == without_projection


def preserve_projection_seed(existing, desired):
    original = existing.get("fabric", {}).get("projection")
    replacement = desired.get("fabric", {}).get("projection")
    if original is None or replacement is None:
        return
    static_original = {key: value for key, value in original.items() if key != "projection_freshness"}
    static_replacement = {
        key: value for key, value in replacement.items() if key != "projection_freshness"
    }
    if static_original != static_replacement:
        raise PermissionError("Existing projection authority or sources differ")
    replacement["projection_freshness"] = original["projection_freshness"]


def publish_projection_upgrade(storage, existing, desired, destination):
    properties = az(
        "storage", "blob", "show", "--account-name", storage["storageAccountName"],
        "--container-name", "runtime-bindings", "--name", "instances.json",
        "--auth-mode", "login",
    )
    if blob_json(storage, "runtime-bindings", "instances.json", destination) != existing:
        raise RuntimeError("Instance bindings changed before the projection upgrade")
    destination.write_text(json.dumps(desired, indent=2, allow_nan=False) + "\n")
    az(
        "storage", "blob", "upload", "--account-name", storage["storageAccountName"],
        "--container-name", "runtime-bindings", "--name", "instances.json",
        "--auth-mode", "login", "--file", str(destination),
        "--if-match", properties["properties"]["etag"], "--overwrite", "true",
        "--content-type", "application/json", "--no-progress",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--hotel-user-upn", required=True)
    parser.add_argument("--bank-user-upn", required=True)
    parser.add_argument("--fabric-mcp-url", required=True)
    parser.add_argument("--projection-output", type=Path)
    parser.add_argument("--state-dir", type=Path, default=Path(".state"))
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    storage = json.loads((args.state_dir / "storage-output.json").read_text())
    agents = json.loads((args.state_dir / "agents-output.json").read_text())
    search = json.loads((args.state_dir / "foundry-iq-output.json").read_text())
    account = az("account", "show")
    operator = az("ad", "signed-in-user", "show")
    if (
        account["tenantId"] != config["tenantId"]
        or account["id"] != config["subscriptionId"]
        or storage["tenantId"] != config["tenantId"]
        or storage["subscriptionId"] != config["subscriptionId"]
        or agents["tenant_id"] != config["tenantId"]
        or operator["userPrincipalName"].casefold() != config["managerUpn"].casefold()
        or operator["id"] != storage["operatorObjectId"]
    ):
        raise PermissionError("Manager, deployment records, and CLI target must match")
    if (
        not args.fabric_mcp_url.startswith("https://api.fabric.microsoft.com/v1/mcp/workspaces/")
        or not args.fabric_mcp_url.endswith("/agent")
    ):
        raise ValueError("Supply the published finance DataAgent MCP URL")
    graph = "https://graph.microsoft.com/v1.0"
    approvers = []
    for upn in config["approverUpns"]:
        person = az(
            "rest", "--method", "get",
            "--url", f"{graph}/users/{quote(upn, safe='')}"
            "?$select=id,mail,accountEnabled,identityParentId",
        )
        if (
            person.get("accountEnabled") is not True or not person.get("mail")
            or person.get("identityParentId")
        ):
            raise PermissionError("Approver must be an enabled directory user with a mailbox")
        approvers.append({"object_id": person["id"], "mail": person["mail"]})
    if len(approvers) != 2 or len({person["object_id"] for person in approvers}) != 2:
        raise ValueError("Exactly two distinct, re-resolved approvers are required")
    instances = {}
    for role, upn, container in (
        ("hotel_finance", args.hotel_user_upn, "hotel-tasks"),
        ("banking_operations", args.bank_user_upn, "bank-tasks"),
    ):
        user = az(
            "rest", "--method", "get",
            "--url", f"{graph}/users/{quote(upn, safe='')}/microsoft.graph.agentUser"
            "?$select=id,identityParentId,mail,accountEnabled",
        )
        identity = az(
            "rest", "--method", "get",
            "--url", f"{graph}/servicePrincipals/{user['identityParentId']}"
            "/microsoft.graph.agentIdentity"
            "?$select=id,appId,accountEnabled,agentIdentityBlueprintId",
        )
        manager = az(
            "rest", "--method", "get", "--url", f"{graph}/users/{user['id']}/manager?$select=id",
        )
        deployed = agents["agents"][role]
        own = verify_hire(
            user, identity, manager, deployed["blueprint"]["client_id"], operator["id"],
            deployed["instance_identity"]["principal_id"],
        )
        own["task_blob_url"] = storage["blobEndpoint"] + container + "/" + identity["id"] + ".json"
        instances[role] = own
    if len({own["agent_user_object_id"] for own in instances.values()}) != 2:
        raise PermissionError("The two personas must have independent agent users")
    bindings = {
        "tenant_id": config["tenantId"], "hotel_owner_id": config["hotelOwnerId"],
        "approvers": approvers, "instances": instances,
        "ledger_blob_url": storage["ledgerBlobUrl"],
        "foundry": {
            "project_endpoint": agents["project_endpoint"],
            "model_deployment": config["foundry"]["modelDeployment"],
        },
        "foundry_iq": {
            "endpoint": search["endpoint"], "knowledge_base": search["knowledgeBase"],
            "api_version": search["apiVersion"],
        },
        "fabric": {"mcp_url": args.fabric_mcp_url},
    }
    if args.projection_output is not None:
        bindings["fabric"]["projection"] = projection_binding(
            json.loads(args.projection_output.read_text()), config, agents, approvers,
            args.fabric_mcp_url,
        )
    ledger_config = LedgerConfig(
        config["tenantId"], tuple(person["object_id"] for person in approvers),
        config["hotelOwnerId"],
    )
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "blob.json"
        existing_bindings = blob_json(storage, "runtime-bindings", "instances.json", path)
        if existing_bindings is not None:
            preserve_projection_seed(existing_bindings, bindings)
        upgrade_projection = (
            existing_bindings is not None and args.projection_output is not None
            and projection_only_upgrade(existing_bindings, bindings)
        )
        if existing_bindings is not None and existing_bindings != bindings and not upgrade_projection:
            raise PermissionError("Existing project bindings differ; refusing to authorize another hire")
        existing_ledger = blob_json(storage, "finance-ledger", "state.json", path)
        state = validated_ledger(ledger_config, existing_ledger)
        if existing_ledger is None:
            create_json(storage, "finance-ledger", "state.json", state, path)
        for role, own in instances.items():
            container = "hotel-tasks" if role == "hotel_finance" else "bank-tasks"
            name = own["identity_object_id"] + ".json"
            existing_journal = blob_json(storage, container, name, path)
            if existing_journal is None:
                create_json(storage, container, name, {"schema_version": 1, "turns": {}}, path)
            elif existing_journal.get("schema_version") != 1 or not isinstance(
                existing_journal.get("turns"), dict
            ):
                raise ValueError("Existing hired-instance task journal is invalid")
        if existing_bindings is None:
            create_json(storage, "runtime-bindings", "instances.json", bindings, path)
        elif upgrade_projection:
            publish_projection_upgrade(storage, existing_bindings, bindings, path)
        if blob_json(storage, "runtime-bindings", "instances.json", path) != bindings:
            raise RuntimeError("Published bindings do not match the verified hires")
    (args.state_dir / "instance-bindings.json").write_text(json.dumps(bindings, indent=2) + "\n")
    print(json.dumps({
        "hiredAndBound": True, "instances": instances,
        "ledgerCreated": existing_ledger is None, "ledgerReset": False,
        "transactions": len(state["transactions"]),
    }, indent=2))


if __name__ == "__main__":
    main()
