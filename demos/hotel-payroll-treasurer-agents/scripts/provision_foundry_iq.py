"""Create only finance-specific policy sources using Entra-authenticated Azure CLI."""

import argparse
import json
from pathlib import Path

from provision_storage import az


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path(".state/foundry-iq-output.json"))
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    account = az("account", "show")
    operator = az("ad", "signed-in-user", "show")
    if account["tenantId"] != config["tenantId"] or account["id"] != config["subscriptionId"]:
        raise RuntimeError("CLI target differs from the selected environment")
    if operator["userPrincipalName"].casefold() != config["managerUpn"].casefold():
        raise RuntimeError("CLI operator differs from the configured manager")
    root = Path(__file__).resolve().parents[1]
    deployment = az(
        "deployment", "group", "create", "--name", "finance-autopilots-search",
        "--resource-group", config["resourceGroup"],
        "--template-file", str(root / "infra/search.bicep"),
        "--parameters", f"operatorObjectId={operator['id']}",
        f"modelAccountName={config['foundry']['account']}",
        f"location={config.get('searchLocation', config['location'])}",
    )
    if deployment["properties"]["provisioningState"] != "Succeeded":
        raise RuntimeError("Finance policy infrastructure did not deploy")
    search = {name: value["value"] for name, value in deployment["properties"]["outputs"].items()}
    endpoint = search["endpoint"]
    version = "2025-11-01-preview"

    def put(path, body):
        return az("rest", "--method", "put", "--url", f"{endpoint}/{path}?api-version={version}",
                  "--resource", "https://search.azure.com", "--body", json.dumps(body))

    index_name = "finance-autopilot-policies"
    index = {
        "name": index_name,
        "fields": [
            {"name": "id", "type": "Edm.String", "key": True, "filterable": True},
            {"name": "title", "type": "Edm.String", "searchable": True},
            {"name": "content", "type": "Edm.String", "searchable": True},
            {"name": "role", "type": "Edm.String", "filterable": True},
        ],
        "semantic": {
            "defaultConfiguration": "finance-semantic",
            "configurations": [{
                "name": "finance-semantic",
                "prioritizedFields": {
                    "titleField": {"fieldName": "title"},
                    "prioritizedContentFields": [{"fieldName": "content"}],
                },
            }],
        },
    }
    put(f"indexes/{index_name}", index)
    documents = json.loads((root / "policies/finance-policies.json").read_text())
    uploaded = az(
        "rest", "--method", "post",
        "--url", f"{endpoint}/indexes/{index_name}/docs/index?api-version={version}",
        "--resource", "https://search.azure.com",
        "--body", json.dumps({"value": [{"@search.action": "mergeOrUpload", **doc}
                                      for doc in documents]}),
    )
    if len(uploaded["value"]) != len(documents) or not all(item["status"] for item in uploaded["value"]):
        raise RuntimeError("One or more policy documents failed indexing")
    source_name = "finance-autopilot-policies-source"
    put(f"knowledgesources/{source_name}", {
        "name": source_name,
        "kind": "searchIndex",
        "searchIndexParameters": {
            "searchIndexName": index_name,
            "semanticConfigurationName": "finance-semantic",
            "sourceDataFields": [{"name": "id"}, {"name": "title"}, {"name": "role"}],
        },
    })
    kb_name = "finance-autopilot-knowledge"
    put(f"knowledgebases/{kb_name}", {
        "name": kb_name,
        "description": "Cited controlled hotel finance and banking operational policies.",
        "outputMode": "answerSynthesis",
        "knowledgeSources": [{"name": source_name}],
        "retrievalReasoningEffort": {"kind": "low"},
        "models": [{
            "kind": "azureOpenAI",
            "azureOpenAIParameters": {
                "resourceUri": f"https://{config['foundry']['account']}.openai.azure.com",
                "deploymentId": config["foundry"]["modelDeployment"],
                "modelName": config["foundry"].get("modelName", "gpt-5.4-mini"),
            },
        }],
        "retrievalInstructions": "Retrieve only the cited finance policies. Do not invent approval rules.",
        "answerInstructions": "Cite policy sources and explicitly state that all financial actions are controlled.",
    })
    result = {
        "searchResourceId": search["id"],
        "endpoint": endpoint,
        "knowledgeBase": kb_name,
        "apiVersion": version,
        "policyDocuments": len(documents),
        "authentication": "Entra-managed-identities",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
