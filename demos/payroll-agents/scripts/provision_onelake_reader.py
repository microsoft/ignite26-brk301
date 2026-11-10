"""Grant only the Hotel root's finance table/manifest reads, preserving existing roles."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import sys
from uuid import UUID

from fabric_rest import FabricRest, cli

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from payroll_agents.projection import REFRESH_SCHEMAS

ROLE = "FinanceHotelReadOnly"


def reader_role(tenant, workspace, lakehouse, principal):
    for value in (tenant, workspace, lakehouse, principal):
        UUID(value)
    return {
        "name": ROLE, "kind": "Policy",
        "decisionRules": [{
            "effect": "Permit",
            "permission": [
                {"attributeName": "Action", "attributeValueIncludedIn": ["Read"]},
                {"attributeName": "Path", "attributeValueIncludedIn": [
                    *[f"/Tables/dbo/{table}" for table in ("projection_status", *REFRESH_SCHEMAS)],
                    "/Files/finance_projection_manifest.json",
                ]},
            ],
        }],
        "members": {"microsoftEntraMembers": [{
            "tenantId": tenant, "objectId": principal, "objectType": "ServicePrincipal",
        }]},
    }


def normalized_role(role):
    value = copy.deepcopy(role)
    value.pop("etag", None)
    value.pop("id", None)
    for member in value.get("members", {}).get("microsoftEntraMembers", []):
        member.pop("objectType", None)
    return value


def merge_roles(current, desired):
    roles = copy.deepcopy(current["value"])
    matching = [role for role in roles if role["name"] == ROLE]
    if len(matching) > 1:
        raise ValueError("Duplicate finance reader role; reconcile before changing permissions")
    if matching and normalized_role(matching[0])["members"] != normalized_role(desired)["members"]:
        raise PermissionError("Existing named reader role is not this deployment's managed role")
    if matching:
        UUID(matching[0]["id"])
        roles[roles.index(matching[0])] = {**desired, "id": matching[0]["id"]}
    else:
        roles.append(desired)
    for role in roles:
        role.pop("etag", None)
    return {"value": roles}


def grant_reader(api, *, tenant, workspace, lakehouse, principal, environment):
    item_url = f"/workspaces/{workspace}/items/{lakehouse}"
    item = api.request("get", item_url)
    if (item.get("type") != "Lakehouse"
            or item.get("description") != f"finance-analytics-bootstrap/v1/{tenant}/{environment}"):
        raise PermissionError("Reader grant target is not the owned finance lakehouse")
    roles_url = item_url + "/dataAccessRoles"
    status, headers, current = api.raw("get", roles_url)
    if status != 200 or not headers.get("etag"):
        raise RuntimeError("Current OneLake role collection and exact ETag required")
    desired = reader_role(tenant, workspace, lakehouse, principal)
    body = merge_roles(current, desired)
    actual = next((role for role in current["value"] if role["name"] == ROLE), None)
    if actual and normalized_role(actual) == normalized_role(desired):
        return {"role": ROLE, "roleId": actual["id"], "changed": False, "runtimeAccessVerified": False}
    conditional = {"If-Match": headers["etag"]}
    status, _, _ = api.raw("put", roles_url + "?dryRun=true", body, headers=conditional)
    if status != 200:
        raise RuntimeError("OneLake reader role dry run did not succeed")
    status, _, _ = api.raw("put", roles_url, body, headers=conditional)
    if status != 200:
        raise RuntimeError("OneLake reader grant requires explicit reconciliation")
    readback = api.request("get", roles_url)
    canonical = lambda roles: {role["name"]: normalized_role(role) for role in roles}
    if canonical(readback["value"]) != canonical(body["value"]):
        raise RuntimeError("OneLake reader grant readback differs; do not overwrite other roles")
    role = next(role for role in readback["value"] if role["name"] == ROLE)
    UUID(role["id"])
    return {"role": ROLE, "roleId": role["id"], "changed": True, "runtimeAccessVerified": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--fabric-output", type=Path, default=Path(".state/fabric-output.json"))
    parser.add_argument("--agents-output", type=Path, default=Path(".state/agents-output.json"))
    parser.add_argument("--output", type=Path, default=Path(".state/onelake-reader-output.json"))
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    fabric = json.loads(args.fabric_output.read_text())
    agents = json.loads(args.agents_output.read_text())
    account = cli("account", "show")
    operator = cli("ad", "signed-in-user", "show")
    if (account["tenantId"] != config["tenantId"] or account["id"] != config["subscriptionId"]
            or fabric["tenant_id"] != config["tenantId"] or agents["tenant_id"] != config["tenantId"]
            or operator["userPrincipalName"].casefold() != config["managerUpn"].casefold()):
        raise PermissionError("CLI, manager and deployment outputs must identify the same target")
    result = grant_reader(
        FabricRest(), tenant=config["tenantId"], workspace=fabric["workspace_id"],
        lakehouse=fabric["lakehouse_id"], environment=fabric["environment"],
        principal=agents["agents"]["hotel_finance"]["instance_identity"]["principal_id"],
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"tenant_id": config["tenantId"], **result}, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
