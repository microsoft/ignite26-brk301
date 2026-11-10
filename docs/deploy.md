# Deploy the demo in your environment

The README **Deploy this demo** button opens an Azure portal custom deployment,
not Codespaces. It provisions Azure resources in your subscription. Afterward,
complete the scenario-specific data, agent, and Microsoft 365 configuration below.
**A successful Azure deployment is not yet a ready-to-chat demo.**

## 1. Deploy Azure resources

Sign in to the Azure portal with your own **human operator account**. Use a
subscription and a new, dedicated resource group for this sample.

Before deploying:

- In **Subscriptions > your subscription > Access control (IAM)**, verify
  resource creation and role-assignment permissions: for example, Owner, or
  Contributor plus User Access Administrator at the deployment scope.
- In **Resource providers**, register `Microsoft.CognitiveServices`,
  `Microsoft.Search`, `Microsoft.Storage`, and `Microsoft.ContainerRegistry`.
- Verify Foundry Hosted Agent availability and model quota in your region.
  The template preserves `gpt-5.4-mini` version `2026-03-17` (GlobalStandard).
  Customer service also uses `text-embedding-3-small` version `1` (Standard).
- Have Microsoft Fabric capacity and the necessary Microsoft 365 / Agent 365
  licenses and administrator access for the later steps.

Click the README button, choose your directory, subscription, and resource group,
then review the parameters:

| Parameter | What to select |
| --- | --- |
| `scenario` | `both` (default), `customer-service`, or `payroll` |
| `location` | Region supporting the pinned models and Hosted Agents. For payroll, use the same region as the resource group. |
| `searchLocation` | Search region; defaults to `location` |
| `operatorObjectId` | Defaults to the identity starting the deployment; use the human account that will perform setup |
| `modelCapacity` | GPT model capacity; default 10, subject to your subscription quota |
| `embeddingCapacity` | Customer-service embedding capacity; default 10, unused for payroll only |

Select **Review + create**, inspect the resources and permissions, then **Create**.
The deployment provisions:

| Scope | Azure resources |
| --- | --- |
| Shared | Microsoft Foundry account and project, pinned GPT model, Entra-authenticated Azure Container Registry, operator access, and registry pull access for the account/project managed identities |
| Customer service | Embedding model, dedicated Azure AI Search, private document container, Entra-based Search connection, and scoped data access |
| Payroll + treasurer | Separate Azure AI Search, isolated Blob Storage containers for ledger, tasks, validation, and runtime bindings, plus bootstrap access |

The project is shared infrastructure, **not a shared agent identity**. Later
hiring still creates separate identities. The template creates no hired agents,
Fabric workspace, business data, financial ledger contents, or Microsoft Teams
chat, and sends no messages. Storage shared keys and registry admin credentials
are disabled.

These are billable Azure resources. Search and registry resources can incur
charges before any agent is used. Delete your dedicated demo resources when
finished; do not use a resource group containing other workloads.

### Before the repository is public

The portal button needs a publicly readable ARM JSON template. Its main-branch
URL becomes usable after this PR is merged and the repository is published.
Until then, use your authenticated GitHub access to download
[`infra/azuredeploy.json`](../infra/azuredeploy.json). In the Azure portal,
open **Deploy a custom template > Build your own template in the editor >
Load file**, load that JSON, save, and continue with the same parameters.
No extra hosting or tracker is required.

## 2. Continue after Azure deployment

In the Azure portal, open **Resource group > Deployments > your deployment >
Outputs**. Open `NEXT_STEPS_URL` to return to this checklist. Retain the other
outputs privately; they are the actual bindings for your environment.

| Outputs | Use |
| --- | --- |
| `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `AZURE_RESOURCE_GROUP`, `AZURE_LOCATION` | Verify your selected deployment target |
| `AZURE_AI_ACCOUNT_NAME`, `AZURE_AI_PROJECT_NAME`, `AZURE_AI_PROJECT_ID`, `AZURE_AI_PROJECT_ENDPOINT` | Open the project in Microsoft Foundry and configure the agent tools |
| `AZURE_AI_MODEL_DEPLOYMENT_NAME`, `AZURE_OPENAI_ENDPOINT` | Model and Foundry IQ setup |
| `AZURE_CONTAINER_REGISTRY_NAME`, `AZURE_CONTAINER_REGISTRY_ENDPOINT` | Push your own reviewed runtime images |
| `customerService` | Customer-service Search, archive, and knowledge-base environment settings; empty if not selected |
| `payroll` | Payroll configuration fields, isolated storage URLs, and Search endpoint; empty if not selected |

Clone/download this repository to your development machine. Install Python 3.13,
Azure CLI, Azure Developer CLI with the project's agent extension, and Docker
for container builds. Follow each demo's isolated dependency instructions;
do not install both Python environments together. Node.js is needed for the
optional customer-service frontend and the payroll parquet reader.

Sign in to the directory and subscription shown in the outputs. Wait for new
role assignments to propagate before testing data access. Do not copy
configuration from the conference tenant or guess resource IDs.

### Customer-service agent

1. Follow [local setup](../demos/customer-service-agent/README.md#local-development).
   Copy `.env.template` to an ignored `.env`. Use the shared Azure outputs plus
   the `customerService` output fields, following the names in
   [`config/azd.env.example`](../demos/customer-service-agent/config/azd.env.example).
   Use the same values for the azd environment; do not provision another project.
2. From the customer-service directory, generate the synthetic corpus with
   `scripts/generate_hotel_data.py` and load it with
   [`scripts/setup_foundry_iq.py`](../demos/customer-service-agent/scripts/setup_foundry_iq.py).
   The [README commands](../demos/customer-service-agent/README.md#assets-and-configuration)
   include a dry run. Run live setup only after setting the output-based Search,
   model, and archive environment variables. Verify with `--verify-only`.
3. In **Microsoft Fabric**, create/select your capacity-backed workspace and
   deploy the [Fabric artifacts](../demos/customer-service-agent/fabric/README.md):
   lakehouses, synthetic data, notebooks, and data agent. Complete the IDs in
   the local configuration and publish the data agent; its committed definition
   is a draft. These blueprints are not a finished Fabric installer.
4. In **Microsoft Foundry**, verify the project and model outputs, then deploy
   `hotel-servicing-agent` using the existing
   [`azure.yaml`](../demos/customer-service-agent/azure.yaml).
   Configure the remaining Fabric, Work IQ, and Agent 365 values first.
   The existing project binding must be reused, not replaced by a second
   provisioning run.
5. Follow the [Agent 365 bridge setup](../demos/customer-service-agent/agent/README.md):
   provide a reviewed image, dedicated identity, Key Vault secret URIs,
   environment, and scoped permissions. Those tenant-dependent bridge resources
   are not created by this button. Publish/hire through your supported Agent 365
   flow and complete the actual instance bindings and administrator consent.
6. In **Microsoft Teams**, verify the hired Hotel Guest Services agent can
   retrieve the right property's policy and data under its own identity.
   Start with read-only questions from the [walkthrough](demo-walkthrough.md).

### Hotel Payroll and Hotel Treasurer

1. Follow the [staged setup](../demos/hotel-payroll-treasurer-agents/docs/setup.md).
   Copy `config/environment.example.json` to ignored
   `config/environment.local.json`. Merge the `payroll.config` output values,
   then set your manager UPN, **two distinct human approvers**, and Fabric
   capacity. The operator must be the configured manager.
2. Build the [runtime Dockerfile](../demos/hotel-payroll-treasurer-agents/infra/runtime.Dockerfile),
   push to the output registry using Entra authentication, and retain its
   immutable `@sha256:` digest. Do not enable registry admin credentials.
3. Run `scripts/provision_storage.py --config` and
   `scripts/provision_foundry_iq.py --config` with your local configuration.
   These reuse the button's deterministic storage and Search resource names
   in the same resource group. Keep their required `.state/` outputs:
   later scripts need the verified operator, approvers, and policy bindings,
   not just the portal's resource URLs.
4. Follow the staged guide to register **both** hosted versions, publish and
   hire both agents, and resolve their actual users and service identities.
   Registration is not hiring. Use the output project endpoint and image digest.
5. Provision the isolated Fabric analytical assets and OneLake reader using
   the staged scripts. Bind the verified hires and approvers, grant runtime
   access under the actual identities, and preserve existing ledger state.
6. In **Microsoft Teams**, create a dedicated Payroll Funding group chat with
   both hires and the approvers. Resolve its native chat ID. Select
   `collaboration` mode explicitly for the shared-chat demo; the default
   `three_iq` evidence workflow is different.
7. Verify identity access, authenticated approval correlation, exactly one
   ledger effect, and recipient-side outcomes in isolated synthetic state.
   Verify Fabric projection freshness separately; Fabric is not the ledger.

## 3. Validation and maintenance

Offline checks and template validation do not prove a full customer-tenant
installation. Complete the [setup checks](setup.md) and scenario walkthrough
before presenting the demo.

[`infra/main.bicep`](../infra/main.bicep) is the source for the committed,
self-contained portal template. It reuses the demo-specific infrastructure;
there are no private linked templates or runtime secrets in the artifact.
After editing Bicep, regenerate and check it:

```powershell
az bicep build --file infra\main.bicep --outfile infra\azuredeploy.json
python scripts\check_public_sample.py
python -m unittest discover -s tests -v
```

CI rebuilds the template and rejects drift. Maintainers can replace the README
button destination with an aka redirect to the same Azure portal URL for click
tracking; the repository contains no tracking service.

The button uses the documented
[Deploy to Azure template URL format](https://learn.microsoft.com/azure/azure-resource-manager/templates/deploy-to-azure-button).
