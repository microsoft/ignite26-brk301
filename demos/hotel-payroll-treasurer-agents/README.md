# Payroll funding: Hotel Payroll and Hotel Treasurer

**Act 2 of BRK301:** two independent agents with accounts, governed by Agent 365,
collaborate in a Microsoft Teams group chat. Hotel Payroll identifies the
shortfall; Hotel Treasurer coordinates human approval and executes the
authorized synthetic transfer. See the [walkthrough](../../docs/demo-walkthrough.md).

This folder is one shared payroll-funding demo with two agents, not an additional
agent. The `hotel_payroll_treasurer_agents` Python package hosts both roles under
separate agent identities. Hyphens in the `hotel-payroll-treasurer-agents` demo
name become underscores in Python imports.

| Agent | Responsibility | Internal role key |
| --- | --- | --- |
| Hotel Payroll | Identify the payroll shortfall and request funding. | `hotel_finance` |
| Hotel Treasurer | Coordinate human approval and execute the authorized synthetic transfer. | `banking_operations` |

The seed is payroll USD 10,000, reserve USD 50,000, and a recorded USD 25,000
obligation. An approved USD 15,000 transfer leaves payroll USD 25,000 and reserve
USD 35,000. This is a simulated ledger, not a connection to a real bank.

## Offline validation

Run from this directory in an environment separate from customer service:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[host]"
npm --prefix parquet-reader ci --ignore-scripts
$env:PYTHONPATH = "src;scripts"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe scripts\fabric_test.py
```

On Linux/macOS, use `.venv/bin/python` and
`PYTHONPATH=src:scripts`. The tests use doubles and temporary local stores; do
not supply live tenant bindings to offline checks.

Build the runtime from public package feeds and the committed npm lockfile:

```powershell
docker build -f infra/runtime.Dockerfile -t brk301-payroll:local .
```

The source build does not require the original private GitHub release or a
prebuilt dependency archive. It requires package-feed access and Docker; it
does not deploy the image or grant agent permissions.

## Tenant setup

Copy `config/environment.example.json` to ignored
`config/environment.local.json` and fill in your own directory, subscription,
resource group, manager, approvers, Fabric capacity, and Microsoft Foundry
resources. See [staged setup](docs/setup.md) before invoking provisioning scripts.

Keep `hotel_finance` and `banking_operations` as the internal role keys.
The public names are Hotel Payroll and Hotel Treasurer. Bind each to a distinct,
manager-verified hire; do not reuse an operator identity.

**Agents with accounts** is the customer-facing term. Local source paths and the
`hotel_payroll_treasurer_agents` package use agent terminology. External resource
names and publication API fields remain unchanged for compatibility; they are not
display names.

Default `three_iq` mode requires successful Foundry IQ, Fabric IQ, and Work IQ
evidence plus physical projection validation before governed model-driven
effects. The shared-chat flow selects `collaboration` explicitly and binds
`FINANCE_PAYROLL_TEAMS_CHAT_ID`. Optional replay is isolated and must be
explicitly enabled; it is not permission to reset a shared ledger.

Amounts are integer cents. Above USD 10,000, an authenticated configured human
must approve. Financial effects use durable idempotency and concurrent-write
guards. Fabric is only an analytical projection. A duplicate decision must not
cause another transfer.
