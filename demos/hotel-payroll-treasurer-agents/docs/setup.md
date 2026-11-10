# Staged payroll setup

This is an operator-driven build and deployment, not `azd up`. Offline tests do
not prove licensing, live identity access, message delivery, or fresh-tenant
acceptance. Start with the repository [setup guide](../../../docs/setup.md).

1. Prepare your own Microsoft Foundry account/project, model deployment, private
   registry, two human approvers, and Agent 365 prerequisites. Copy
   `config/environment.example.json` to ignored `config/environment.local.json`.
2. Build `infra/runtime.Dockerfile` from the demo directory. Push it to your
   registry and record its immutable digest. Grant the appropriate pulling
   identity access; do not enable registry admin credentials.
3. Inspect and run `scripts/provision_storage.py` and
   `scripts/provision_foundry_iq.py` with `--config`. They create the isolated
   ledger and finance policy retrieval resources. Retain outputs under ignored
   `.state/`; never commit them.
4. Register the two hosted versions with `scripts/deploy_agents.py`, passing
   `--config`, `--image` with an immutable ACR digest, and the explicit
   `--project-endpoint`. The script verifies operator and target identity.
5. Inspect `scripts/publish_agents.py`, supply your reviewed publication URLs,
   and publish. Hire both agents through the supported Agent 365 process.
   Resolve the actual hired users and identities; registration is not hiring.
6. Use `scripts/provision_fabric.py` and
   `scripts/provision_onelake_reader.py` for isolated finance analytical assets
   and scoped access. Check each script's `--help` for required outputs and
   arguments; the steps are state-dependent, not a blind batch command.
7. Use `scripts/bind_instances.py` to bind the manager-verified hires, approval
   identities, policy knowledge base, and Fabric projection. It must preserve
   existing financial state. Grant runtime access under actual agent identities,
   not an operator-token fallback.
8. For the final shared-chat demo, create a dedicated Microsoft Teams group with
   the two hires and approvers, resolve its native ID, and explicitly select
   `--workflow-mode collaboration --payroll-teams-chat-id` when registering
   the intended version. Use `--update-existing` only with the verified prior
   deployment output. Preserve those bindings privately.
9. Verify individual tool access, authenticated chat ingress, approval
   correlation, exactly one ledger transfer, and recipient-side outcomes in
   isolated synthetic state. Verify physical projection freshness separately
   if presenting Fabric analytics.

Use the scripts' `--help` and inspect their target checks before execution.
They change cloud state and can send real messages in your configured tenant.
Do not run them against the conference environment merely to validate this PR.
Do not reuse the original source repository's private dependency artifacts.
