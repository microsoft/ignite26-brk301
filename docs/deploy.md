# Deploy the demo in your environment

The README button opens a GitHub Codespace with this repository, Python 3.13,
Node.js 24, Docker, Azure CLI, and Azure Developer CLI. Use your own GitHub,
Azure, Microsoft Fabric, Microsoft 365, and Agent 365 access.
Codespaces and cloud resources use your account's quotas and billing.

**This is guided setup, not an unattended full-tenant deployment.** Creating
the workspace does not provision agents or grant tenant permissions. Agent 365
hiring, human approver selection, Fabric capacity, and native chat bindings
still need your environment's administrator/operator steps.
The repository must be accessible to your GitHub account; public customer
access starts only after the repository is made public.

## 1. Choose a scenario

| Scenario | Folder | Setup |
| --- | --- | --- |
| Hotel Guest Services | `demos/customer-service-agent` | [Customer-service setup](../demos/customer-service-agent/README.md) |
| Hotel Payroll + Hotel Treasurer | `demos/hotel-payroll-treasurer-agents` | [Staged payroll/treasurer setup](../demos/hotel-payroll-treasurer-agents/docs/setup.md) |

You can deploy one or both. Keep their Python environments separate; there is
no combined root `azd up` command. Start with the shared
[environment requirements](setup.md), then follow the selected project's setup.

## 2. Sign in to your environment

In the Codespace terminal, sign in explicitly to the tenant and subscription
you intend to use. Check the active Azure account before any provisioning.
Do not use the conference tenant or copy someone else's deployed bindings.
Replace the sample IDs with your deployment outputs, not guessed values.

The customer-service project has its own `azure.yaml`. The payroll/treasurer
project uses the staged scripts and immutable container digest described in its
setup guide; its configuration is not a one-command substitute for hiring.

## 3. Configure and verify

Follow the selected setup guide in order. Keep generated configuration,
deployment outputs, and tokens in ignored local files.
For payroll, hire and bind **two distinct agents**, select authorized human
approvers, and use an isolated synthetic ledger and Microsoft Teams chat.
Only select `collaboration` mode deliberately; it is not the default
`three_iq` evidence workflow.

Run the project's offline checks first, then verify actual identity access,
tool evidence, approval correlation, and results in your environment.
Local tests or a clicked button are not deployment-success evidence.
