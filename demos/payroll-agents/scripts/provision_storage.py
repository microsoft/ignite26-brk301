"""Provision only this project's storage, using the authenticated Azure CLI."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def az(*arguments: str) -> dict:
    try:
        result = subprocess.run(
            ["az", *arguments, "--only-show-errors", "--output", "json"],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as error:
        raise RuntimeError(error.stderr.strip() or "Azure CLI command failed") from error
    if not result.stdout.strip():
        return {}
    return json.loads(result.stdout)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path(".state/storage-output.json"))
    options = parser.parse_args()
    config = json.loads(options.config.read_text())
    account = az("account", "show")
    if (
        account["tenantId"] != config["tenantId"]
        or account["id"] != config["subscriptionId"]
    ):
        raise RuntimeError("Azure CLI tenant/subscription does not match the selected config")
    operator = az("ad", "signed-in-user", "show")
    if operator["userPrincipalName"].casefold() != config["managerUpn"].casefold():
        raise RuntimeError("Azure CLI is not signed in as the configured project manager")
    upns = config["approverUpns"]
    if len(upns) != 2 or len({upn.casefold() for upn in upns}) != 2:
        raise ValueError("Exactly two distinct approver UPNs are required")
    approvers = [az("ad", "user", "show", "--id", upn) for upn in upns]
    if len({person["id"] for person in approvers}) != 2:
        raise ValueError("Approver UPNs must resolve to two distinct directory objects")
    group = az("group", "show", "--name", config["resourceGroup"])
    if group["location"].casefold() != config["location"].casefold():
        raise RuntimeError("Resource group location does not match the selected config")
    deployment = az(
        "deployment", "group", "create",
        "--name", "finance-autopilots-storage",
        "--resource-group", config["resourceGroup"],
        "--template-file", str(Path(__file__).resolve().parents[1] / "infra/storage.bicep"),
        "--parameters", f"operatorObjectId={operator['id']}",
    )
    if deployment["properties"]["provisioningState"] != "Succeeded":
        raise RuntimeError("Storage deployment did not succeed")
    outputs = {
        key: value["value"] for key, value in deployment["properties"]["outputs"].items()
    }
    result = {
        "tenantId": config["tenantId"],
        "subscriptionId": config["subscriptionId"],
        "resourceGroup": config["resourceGroup"],
        "hotelOwnerId": config["hotelOwnerId"],
        "operatorObjectId": operator["id"],
        "approvers": [
            {
                "objectId": person["id"],
                "upn": person["userPrincipalName"],
                "mail": person.get("mail") or person["userPrincipalName"],
            }
            for person in approvers
        ],
        **outputs,
    }
    options.output.parent.mkdir(parents=True, exist_ok=True)
    options.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({
        "storageAccountName": result["storageAccountName"],
        "localBindings": str(options.output),
        "sharedKeys": "disabled",
    }, indent=2))


if __name__ == "__main__":
    main()
