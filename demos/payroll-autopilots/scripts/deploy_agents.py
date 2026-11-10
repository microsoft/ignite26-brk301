"""Register the two isolated hosted agent versions using the authenticated Azure CLI."""

import argparse
import json
import re
from pathlib import Path
from urllib.parse import quote, urlsplit

from provision_storage import az


def account_model_endpoint(account):
    endpoint = account["properties"]["endpoints"]["OpenAI Language Model Instance API"]
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "https" or not parsed.hostname
        or not parsed.hostname.endswith(".openai.azure.com")
        or parsed.username is not None or parsed.password is not None
        or parsed.port not in (None, 443) or parsed.path not in ("", "/")
        or parsed.query or parsed.fragment
    ):
        raise ValueError("The selected account must advertise a credential-free Azure OpenAI endpoint")
    return endpoint.rstrip("/")


def validate_tenant_endpoint(endpoint):
    if (
        endpoint.get("authorization_schemes") != [{"type": "BotServiceTenant"}]
        or endpoint.get("protocols") != ["activity"]
        or endpoint.get("protocol_configuration", {}).get("activity", {}).get(
            "enable_m365_public_endpoint"
        ) is not True
    ):
        raise RuntimeError("Foundry gateway mode requires only the authenticated Tenant Activity endpoint")


def workflow_environment(previous, *, mode=None, replay=None, chat_id=None):
    settings = {
        key: previous[key]
        for key in ("FINANCE_WORKFLOW_MODE", "FINANCE_DEMO_REPLAY", "FINANCE_PAYROLL_TEAMS_CHAT_ID")
        if key in previous
    }
    if mode is not None:
        settings["FINANCE_WORKFLOW_MODE"] = mode
    if replay is not None:
        settings["FINANCE_DEMO_REPLAY"] = "true" if replay else "false"
    if chat_id is not None:
        settings["FINANCE_PAYROLL_TEAMS_CHAT_ID"] = chat_id
    selected_mode = settings.get("FINANCE_WORKFLOW_MODE", "three_iq")
    selected_replay = settings.get("FINANCE_DEMO_REPLAY", "false")
    if selected_mode not in ("three_iq", "collaboration") or selected_replay not in ("true", "false"):
        raise ValueError("Invalid finance workflow configuration")
    if selected_replay == "true" and selected_mode != "collaboration":
        raise ValueError("Repeatable demonstrations require collaboration mode")
    selected_chat = settings.get("FINANCE_PAYROLL_TEAMS_CHAT_ID")
    if selected_chat is not None and not re.fullmatch(r"19:[A-Za-z0-9_-]+@thread\.v2", selected_chat):
        raise ValueError("The coordination chat must be an explicit native group chat identity")
    return settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--project-endpoint", required=True)
    parser.add_argument("--update-existing", action="store_true")
    parser.add_argument("--workflow-mode", choices=("three_iq", "collaboration"))
    parser.add_argument("--demo-replay", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--payroll-teams-chat-id")
    parser.add_argument("--output", type=Path, default=Path(".state/agents-output.json"))
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    account = az("account", "show")
    operator = az("ad", "signed-in-user", "show")
    if account["id"] != config["subscriptionId"] or account["tenantId"] != config["tenantId"]:
        raise RuntimeError("CLI environment differs from the configured deployment target")
    if operator["userPrincipalName"].casefold() != config["managerUpn"].casefold():
        raise RuntimeError("CLI operator differs from the configured manager")
    image = args.image
    if ".azurecr.io/" not in image or "@sha256:" not in image:
        raise ValueError("Deploy an immutable private ACR image digest, not a mutable tag")
    endpoint = args.project_endpoint.rstrip("/")
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "https" or not parsed.hostname
        or not parsed.hostname.endswith(".services.ai.azure.com")
        or parsed.username is not None or parsed.password is not None
        or parsed.query or parsed.fragment
    ):
        raise ValueError("An explicit credential-free Foundry project endpoint is required")
    storage = json.loads(Path(".state/storage-output.json").read_text())
    if storage["tenantId"] != config["tenantId"] or storage["subscriptionId"] != config["subscriptionId"]:
        raise RuntimeError("Storage bindings belong to another environment")
    version = "2025-11-15-preview"
    previous = None
    if args.update_existing:
        previous = json.loads(args.output.read_text())
        if previous["tenant_id"] != config["tenantId"] or previous["project_endpoint"] != endpoint:
            raise RuntimeError("Existing deployment record belongs to another target")
    results = dict(previous["agents"]) if previous is not None else {}
    model = az("cognitiveservices", "account", "show",
               "--name", config["foundry"]["account"], "--resource-group", config["resourceGroup"])
    project = az(
        "rest", "--method", "get",
        "--url", f"https://management.azure.com{model['id']}"
        f"/projects/{quote(config['foundry']['project'], safe='')}?api-version=2025-04-01-preview",
    )
    if project["properties"]["endpoints"]["AI Foundry API"].rstrip("/") != endpoint:
        raise PermissionError("Project endpoint does not match the configured Foundry ARM resource")
    model_endpoint = account_model_endpoint(model)
    for role, name, title in (
        ("hotel_finance", "hotel-finance-autopilot", "Hotel Payroll"),
        ("banking_operations", "banking-operations-autopilot", "Hotel Treasurer"),
    ):
        body = {
            "description": f"{title}: independent governed finance workflow and three-IQ grounding.",
            "definition": {
                "kind": "hosted", "image": image, "cpu": "1", "memory": "2Gi",
                "container_protocol_versions": [{"protocol": "activity_protocol", "version": "v1"}],
                "environment_variables": {
                    "TENANT_ID": config["tenantId"],
                    "FINANCE_ROLE": role,
                    "FINANCE_BINDINGS_URL": storage["runtimeBindingsBlobUrl"],
                    "FINANCE_ACTIVITY_AUTHENTICATION": "foundry_gateway",
                    "FINANCE_MODEL_ENDPOINT": model_endpoint,
                },
            },
            "agent_endpoint": {
                "protocols": ["activity"],
                "protocol_configuration": {"activity": {"enable_m365_public_endpoint": True}},
                "authorization_schemes": [{"type": "BotServiceTenant"}],
            },
            "digital_worker_type": "m365",
            "metadata": {"application": "hotel-bank-finance-autopilots", "enableVnextExperience": "true"},
        }
        current_environment = {}
        if previous is not None:
            existing = az(
                "rest", "--method", "get", "--resource", "https://ai.azure.com",
                "--url", f"{endpoint}/agents/{name}?api-version={version}",
            )
            expected = previous["agents"][role]
            if (
                existing["digital_worker_type"] != "m365"
                or existing["blueprint"] != expected["blueprint"]
            ):
                raise RuntimeError("Existing agent does not match this finance deployment's blueprint")
            validate_tenant_endpoint(existing["agent_endpoint"])
            current_environment = existing["versions"]["latest"]["definition"]["environment_variables"]
            if (
                current_environment["TENANT_ID"] != config["tenantId"]
                or current_environment["FINANCE_ROLE"] != role
            ):
                raise RuntimeError("Existing runtime identity configuration differs from the target")
            body["agent_endpoint"] = {
                key: existing["agent_endpoint"][key]
                for key in (
                    "protocols", "protocol_configuration", "authorization_schemes", "version_selector",
                )
                if key in existing["agent_endpoint"]
            }
            del body["digital_worker_type"]
        body["definition"]["environment_variables"].update(workflow_environment(
            current_environment, mode=args.workflow_mode,
            replay=args.demo_replay, chat_id=args.payroll_teams_chat_id,
        ))
        result = az(
            "rest", "--method", "post",
            "--url", f"{endpoint}/agents/{name}/versions?api-version={version}",
            "--resource", "https://ai.azure.com",
            "--headers", "Foundry-Features=DigitalWorker=V1Preview",
            "--body", json.dumps(body),
        )
        if result["name"] != name or not result.get("blueprint") or not result.get("instance_identity"):
            raise RuntimeError("Hosted version did not return independent managed identities")
        if previous is not None and (
            result["blueprint"] != existing["blueprint"]
            or result["instance_identity"] != existing["instance_identity"]
        ):
            raise RuntimeError("Updated version changed the existing agent identities")
        results[role] = result
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({
            "tenant_id": config["tenantId"], "project_endpoint": endpoint,
            "image": image, "api_version": version, "agents": results,
            "lifecycleVerification": "not_performed",
        }, indent=2) + "\n")
        principal = result["instance_identity"]["principal_id"]
        resources = [
            (storage["runtimeBindingsContainerId"], "Storage Blob Data Reader"),
            (storage["ledgerContainerId"], "Storage Blob Data Contributor"),
            (storage["bankTasksContainerId"] if role == "banking_operations"
             else storage["hotelTasksContainerId"], "Storage Blob Data Contributor"),
        ]
        search = json.loads(Path(".state/foundry-iq-output.json").read_text())
        resources.append((search["searchResourceId"], "Search Index Data Reader"))
        resources.append((model["id"], "Cognitive Services OpenAI User"))
        for scope, role_name in resources:
            az("role", "assignment", "create", "--assignee-object-id", principal,
               "--assignee-principal-type", "ServicePrincipal", "--role", role_name, "--scope", scope)
        print(json.dumps({"role": role, "name": name, "version": result["version"],
                          "status": result.get("status"),
                          "lifecycleVerification": "not_performed"}))


if __name__ == "__main__":
    main()
