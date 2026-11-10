"""Exercise the real Blob provider in a separate, disposable validation blob."""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from azure.identity import AzureCliCredential

from finance_autopilots.blob_store import BlobStateStore
from finance_autopilots.ledger import (
    AgentRole,
    Decision,
    Ledger,
    LedgerConfig,
    Principal,
)
from provision_storage import az


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bindings", type=Path, default=Path(".state/storage-output.json"))
    parser.add_argument("--output", type=Path, default=Path(".state/evidence/cloud-ledger.json"))
    options = parser.parse_args()
    bindings = json.loads(options.bindings.read_text())
    account = az("account", "show")
    operator = az("ad", "signed-in-user", "show")
    if (
        account["tenantId"] != bindings["tenantId"]
        or account["id"] != bindings["subscriptionId"]
        or operator["id"] != bindings["operatorObjectId"]
    ):
        raise RuntimeError("CLI tenant, subscription, or actual operator differs from the bindings")
    credential = AzureCliCredential(tenant_id=bindings["tenantId"])
    blob_url = bindings["blobEndpoint"] + f"validation/cas-{uuid4()}.json"
    store = BlobStateStore.from_url(blob_url, credential)
    if store.read().state is not None:
        raise RuntimeError("Validation must start in a new, isolated blob")
    config = LedgerConfig(
        tenant_id=bindings["tenantId"],
        approver_object_ids=tuple(person["objectId"] for person in bindings["approvers"]),
        hotel_owner_id=bindings["hotelOwnerId"],
    )
    ledger = Ledger(store, config)
    before = ledger.snapshot()
    original = store.read()
    if original.state is None:
        raise AssertionError("Ledger initialization did not persist the cloud snapshot")
    if store.compare_and_swap(None, original.state):
        raise AssertionError("Create-if-absent overwrote an existing cloud snapshot")
    if not store.compare_and_swap(original.version, original.state):
        raise AssertionError("Fresh ETag update failed")
    if store.compare_and_swap(original.version, original.state):
        raise AssertionError("Stale ETag unexpectedly replaced cloud state")

    action = ledger.payroll_funding_action()
    request_id = f"validation-{uuid4()}"
    ledger.request_approval(
        action, request_id=request_id, idempotency_key="same-logical-transfer"
    )
    if bindings["operatorObjectId"] not in config.approver_object_ids:
        raise RuntimeError("Authenticated operator must be one of the eligible project approvers")
    ledger.decide(
        request_id,
        Principal(bindings["tenantId"], bindings["operatorObjectId"]),
        Decision.APPROVE,
    )

    def execute(_: int) -> dict:
        return ledger.execute_transfer(
            action,
            idempotency_key="same-logical-transfer",
            role=AgentRole.BANKING_OPERATIONS,
            approval_request_id=request_id,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(execute, range(8)))
    after = ledger.snapshot()
    if len(after["transactions"]) != 1 or any(result != results[0] for result in results):
        raise AssertionError("Concurrent duplicate requests did not produce one transaction")
    reserve = Ledger.RESERVE_ACCOUNT_ID
    payroll = Ledger.PAYROLL_ACCOUNT_ID
    if (
        before["accounts"][reserve]["balance_cents"]
        - after["accounts"][reserve]["balance_cents"] != 1_500_000
        or after["accounts"][payroll]["balance_cents"]
        - before["accounts"][payroll]["balance_cents"] != 1_500_000
    ):
        raise AssertionError("Cloud debit and credit do not match the approved $15,000 transfer")
    restarted = Ledger(BlobStateStore.from_url(blob_url, credential), config).snapshot()
    if restarted != after:
        raise AssertionError("A new provider did not recover exactly the persisted cloud state")
    evidence = {
        "verifiedAt": datetime.now(timezone.utc).isoformat(),
        "kind": "live-azure-blob-with-authenticated-operator",
        "controlledData": True,
        "agentDeploymentOrMessagingEvidence": False,
        "blobUrl": blob_url,
        "checks": {
            "createIfAbsent": True,
            "staleEtagRejected": True,
            "concurrentIdenticalRequests": 8,
            "committedTransactions": len(after["transactions"]),
            "debitCents": 1_500_000,
            "creditCents": 1_500_000,
            "restartPersistence": True,
        },
    }
    options.output.parent.mkdir(parents=True, exist_ok=True)
    options.output.write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    main()
