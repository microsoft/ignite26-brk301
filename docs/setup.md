# Setup and validation

## Local checks first

Use Python 3.13 and Node.js 22 or later. Each demo needs its own virtual
environment because the voice proxy and payroll host pin different SDK versions.
Follow [customer-service agent setup](../demos/customer-service-agent/README.md)
and [payroll agents setup](../demos/hotel-payroll-treasurer-agents/README.md).

From the repository root, run the dependency-free publication checks:

```powershell
python scripts\check_public_sample.py
python -m unittest discover -s tests -v
```

These inspect source and sample configuration only. They are not a penetration
test, a license audit, or cloud acceptance.

## Configure your environment

Do not copy configuration from a live demo tenant. Create fresh local
configuration from the relevant templates:

| Project | Templates |
| --- | --- |
| Customer-service agent | `.env.template`, `config/azd.env.example`, `scripts/fabric/config.example.json`, `agent/a365.config.example.json`, `web/*/.env.example` |
| Payroll agents | `config/environment.example.json` |

All paths in the table are relative to the project directory. Example hostnames
beginning with `replace` and UUIDs beginning `00000000-0000-4000-8000-` are
non-deployment sample values, not supplied resources. Replace every such value
using actual outputs from your environment. Keep real environment files, generated
manifests, tokens, and deployment outputs ignored.

## Cloud preparation order

Start with [Deploy this demo](deploy.md) to provision the selected Azure
foundation, then follow its post-deployment checklist. This replaces manual
creation of the Azure account/project, model, Search, storage, and registry;
it does not replace the data-loading, runtime, or tenant configuration steps.

1. Verify current service availability, licenses, capacity, administrator consent,
   regional model quota, and access for your target tenant.
2. Create or select your own Microsoft Foundry project and model deployment.
3. Provision the demo-specific Fabric data and Foundry IQ corpus. Customer
   service uses synthetic hotel data; payroll uses its own isolated finance data.
4. Configure workload identities and scoped access. Operator access does not
   establish agent access.
5. Deploy the project-specific runtimes, publish the reviewed Agent 365 templates,
   and hire/bind the appropriate identities.
6. Test each IQ under the actual agent identity and test recipient-side
   communication. For payroll, verify an authenticated approval and one ledger
   effect in isolated synthetic state before using the demo live.

The Azure portal template provisions infrastructure, not a completed
fresh-tenant installation. The Fabric blueprints remain building blocks and
some helper scripts are explicitly placeholders. Both scenarios still require
the documented post-deployment steps, including staged provisioning and hiring.

For current platform concepts, consult the official
[Microsoft Foundry hosted-agent documentation](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents).
The source pins preserve the imported implementation; do not silently upgrade SDKs
or substitute a different model while diagnosing setup failures.

## Release status

Repository tests establish offline behavior only. This import does not deploy
or modify the existing conference demo, establish a successful customer-tenant
installation, or make the repository public. The README uses the supplied
illustrative session-slide images, not live tenant evidence.
