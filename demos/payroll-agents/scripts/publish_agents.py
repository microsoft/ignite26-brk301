"""Publish the independent blueprints for tenant-admin activation and all-user hiring."""

import argparse
import json
from pathlib import Path

from provision_storage import az


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--app-version", default="1.0.0")
    parser.add_argument("--agents", type=Path, default=Path(".state/agents-output.json"))
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    deployed = json.loads(args.agents.read_text())
    account = az("account", "show")
    operator = az("ad", "signed-in-user", "show")
    if (
        account["tenantId"] != config["tenantId"] or account["id"] != config["subscriptionId"]
        or deployed["tenant_id"] != config["tenantId"]
        or operator["userPrincipalName"].casefold() != config["managerUpn"].casefold()
    ):
        raise RuntimeError("Operator, agent deployment, and environment must match")
    endpoint = deployed["project_endpoint"]
    api_version = deployed["api_version"]
    results = {}
    for role, agent in deployed["agents"].items():
        name = agent["name"]
        current = az(
            "rest", "--method", "get", "--resource", "https://ai.azure.com",
            "--url", f"{endpoint}/agents/{name}/versions/{agent['version']}?api-version={api_version}",
        )
        if current["status"] != "active":
            raise RuntimeError(f"{name} must have an active hosted version before publication")
        az(
            "rest", "--method", "patch", "--resource", "https://ai.azure.com",
            "--url", f"{endpoint}/agents/{name}?api-version={api_version}",
            "--body", json.dumps({"agent_endpoint": {
                "protocols": ["activity"],
                "protocol_configuration": {"activity": {"enable_m365_public_endpoint": True}},
                "authorization_schemes": [{"type": "BotServiceTenant"}],
            }}),
        )
        title = "Hotel Payroll" if role == "hotel_finance" else "Hotel Treasurer"
        body = {
            "agentDisplayName": title, "publishAsAutopilot": True, "publishScope": "Tenant",
            "appVersion": args.app_version, "canRespondWithoutMention": True,
            "shortDescription": "Independent controlled finance workflow with three-IQ grounding.",
            "fullDescription": (
                f"{title} collaborates through its own Teams identity and mailbox, "
                "uses Foundry IQ policies, Fabric IQ analytics, and Work IQ context, "
                "and enforces authenticated approvals for controlled payroll funding. "
                "New hires require manager-configured resource bindings."
            ),
            "developerName": "Microsoft Corporation",
            "developerWebsiteUrl": "https://github.com/microsoft/ignite26-brk301",
            "privacyUrl": "https://github.com/microsoft/ignite26-brk301/blob/main/demos/payroll-agents/docs/privacy.md",
            "termsOfUseUrl": "https://github.com/microsoft/ignite26-brk301/blob/main/demos/payroll-agents/docs/terms.md",
            "useAgenticUserTemplate": True,
            "agenticUserTemplate": {
                "Id": "digitalWorkerTemplate", "File": "agenticUserTemplateManifest.json",
                "SchemaVersion": "0.1.0-preview",
                "AgentIdentityBlueprintId": current["blueprint"]["client_id"],
                "CommunicationProtocol": "activityProtocol",
            },
            "optionalPermissionScopes": [
                {"resourceAppId": "ea9ffc3e-8a23-4a7d-836d-234d7c7565c1",
                 "scopes": ["McpServers.Mail.All", "McpServers.Teams.All"]},
                {"resourceAppId": "00000003-0000-0000-c000-000000000000",
                 "scopes": ["User.Read", "Mail.Read", "Mail.Send", "Chat.Create", "ChatMessage.Send"]},
                {"resourceAppId": "00000009-0000-0000-c000-000000000000",
                 "scopes": ["DataAgent.Execute.All", "Lakehouse.Read.All"]},
            ],
        }
        result = az(
            "rest", "--method", "post", "--resource", "https://ai.azure.com",
            "--url", f"{endpoint}/agents/{name}/microsoft365/publish?api-version={api_version}",
            "--body", json.dumps(body),
        )
        results[role] = {"name": name, "app_version": args.app_version, "response": result}
        Path(".state/publish-output.json").write_text(json.dumps(results, indent=2) + "\n")
        print(json.dumps({"name": name, "publishedAsAutopilot": True,
                          "adminActivationAndHirePending": True, "response": result}))


if __name__ == "__main__":
    main()
