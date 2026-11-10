# Customer-service agent

**Act 1 of BRK301:** Hotel Guest Services combines Foundry IQ, Fabric IQ, and
Work IQ. Start with read-only grounded answers, then explain the guarded action
tools. The [session walkthrough](../../docs/demo-walkthrough.md) supplies the
presenter flow.

## Local development

Run from this directory. Use Python 3.13 and a separate environment from payroll:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e "./src/hotel-servicing-agent[dev]" -e "./web/backend[dev]"
.\.venv\Scripts\python.exe -m pytest --ignore=tests/unit/test_bridge_readiness.py --ignore=tests/unit/test_private_vault_bootstrap.py
.\.venv\Scripts\python.exe -m ruff check src web/backend scripts tests
.\.venv\Scripts\python.exe -m ruff format --check src web/backend scripts tests
.\.venv\Scripts\python.exe -m mypy src/hotel-servicing-agent/hotel_servicing_agent web/backend/hotel_voice_proxy scripts
.\.venv\Scripts\python.exe scripts\validate_technical_naming.py
.\.venv\Scripts\python.exe scripts\validate_configuration.py
```

On Linux/macOS, `make setup` installs the Python and frontend dependencies and
`make check` runs the core checks. Agent 365 bridge dependencies are deliberately
separate; the repository workflow tests them in their own job.

For the optional voice frontend:

```powershell
npm --prefix web/frontend ci
npm --prefix web/frontend test
npm --prefix web/frontend run lint
npm --prefix web/frontend run typecheck
npm --prefix web/frontend run build
```

The optional [ASSERT evaluation project](evaluation/assert/README.md) has its own
environment and upstream source lock; live evaluation requires actual configured
identity and tracing infrastructure.

## Assets and configuration

| Directory | Purpose |
| --- | --- |
| `src/hotel-servicing-agent` | Foundry Hosted Agent, property-scoped tools, orchestration, and voice adapter |
| `agent` | Agent 365 messaging bridge |
| `data`, `foundryiq-docs` | Deterministic synthetic data generation contracts and knowledge corpus |
| `fabric` | Lakehouses, data-agent draft, ontology, notebooks, model, and report definitions |
| `infra`, `manifest` | Parameterized deployment building blocks and sample publication manifests |
| `web` | Optional authenticated browser voice client and proxy |

The committed configuration is a coherent sample snapshot, not a deployed
environment. Copy `.env.template` to an ignored `.env`, replace all sample
bindings, and follow the [setup guide](../../docs/setup.md). Preserve the two
instruction copies and hosted SDK pins. Do not upload sample publication IDs as
if they were registered identities.

Generate and check a small synthetic dataset without cloud access:

```powershell
.\.venv\Scripts\python.exe scripts\generate_hotel_data.py --scale demo
.\.venv\Scripts\python.exe scripts\validate_hotel_data.py
.\.venv\Scripts\python.exe scripts\setup_foundry_iq.py --dry-run
```

Cloud provisioning remains explicit. The import does not verify a new tenant,
deploy all blueprints automatically, or enable arbitrary Work IQ mutations.
