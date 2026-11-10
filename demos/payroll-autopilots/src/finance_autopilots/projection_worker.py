"""Root-only analytical refreshes with durable capture and job checkpoints."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from uuid import UUID

import httpx

from .ledger import AgentRole
from .projection import (
    HttpResponse, ProjectionConfig, ProjectionRefreshError, read_projection_manifest,
    projection_job_id, refresh_projection, validate_snapshot,
)


def persist_refresh_progress(journal, key, fingerprint, progress):
    for _ in range(32):
        snapshot = journal.read()
        if snapshot.state is None:
            raise RuntimeError("Projection task journal disappeared")
        entry = snapshot.state["turns"][key]
        if entry["input_hash"] != fingerprint or entry["status"] != "running":
            raise RuntimeError("Projection task ownership changed")
        entry["projection_progress"] = {**entry.get("projection_progress", {}), **progress}
        if journal.compare_and_swap(snapshot.version, snapshot.state):
            return
    raise RuntimeError("Could not checkpoint the analytical refresh")


def projection_config(runtime, bindings):
    if runtime.role is not AgentRole.BANKING_OPERATIONS:
        raise PermissionError("Only Hotel Treasurer may refresh analytical projections")
    configured = bindings["fabric"].get("projection")
    if configured is None:
        raise RuntimeError("Manager-controlled projection configuration has not been published")
    config = ProjectionConfig.from_output(configured)
    if (
        config.tenant_id != runtime.tenant
        or config.expected_source_policy["owner_id"] != bindings["hotel_owner_id"]
        or set(config.expected_source_policy["approver_ids"])
        != {person["object_id"] for person in bindings["approvers"]}
        or configured["projection_refresh"]["bank_client_id"] != runtime.root_client_id
    ):
        raise PermissionError("Projection configuration differs from the bound finance authority")
    return config


def authorize_projection_retry(journal, ledger_snapshot, config, *, job_url, job,
                               job_history, manifest_status, manager_id, confirmed=False):
    """Administrative reconciliation only; never submits a job or writes the ledger."""
    if confirmed is not True:
        raise PermissionError("An explicit administrative retry decision is required")
    UUID(manager_id)
    validate_snapshot({
        "contract_version": 1, "state": ledger_snapshot.state,
        "source_etag": ledger_snapshot.version,
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }, config.expected_source_policy)
    fingerprint = hashlib.sha256(json.dumps(
        {"source_etag": ledger_snapshot.version, "state": ledger_snapshot.state},
        sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()
    key = "projection-refresh/" + fingerprint
    try:
        observed_job_id = projection_job_id(config, job_url)
    except ValueError as error:
        raise RuntimeError("Retry requires the configured notebook's verified job URL") from error
    def stable_job(observed):
        return {
            **observed,
            "failureReason": {
                field: value for field, value in observed.get("failureReason", {}).items()
                if field != "requestId"
            },
        }
    if (
        observed_job_id != str(UUID(job["id"])) or job.get("itemId") != config.notebook_id
        or job.get("jobType") != "RunNotebook" or job.get("status") != "Failed"
        or job.get("failureReason", {}).get("errorCode") != "UserAccessTokenException"
        or job.get("failureReason", {}).get("message")
        != "Job failed to start: unable to acquire user token"
        or job.get("failureReason", {}).get("isRetriable") is not False
        or manifest_status != 404 or not isinstance(job_history, list)
        or not any(stable_job(observed) == stable_job(job) for observed in job_history)
        or any(observed.get("status") not in ("Completed", "Failed", "Cancelled", "Canceled")
               for observed in job_history)
    ):
        raise RuntimeError("Retry requires a verified pre-start failure, no active job and no manifest")
    for _ in range(32):
        snapshot = journal.read()
        if snapshot.state is None:
            raise RuntimeError("Original projection checkpoint disappeared")
        entry = snapshot.state["turns"].get(key)
        progress = entry.get("projection_progress", {}) if entry else {}
        previous_progress = [
            attempt.get("projection_progress", {})
            for attempt in entry.get("projection_attempts", [])
        ] if entry else []
        pre_submission = (
            progress.get("phase") == "prepared"
            and progress.get("job_url") is None
            and progress.get("job_status") is None
            and progress.get("live_refresh_executed") is False
            and progress.get("submission_attempted") is not True
            and any(previous.get("job_url") == job_url
                    and previous.get("job_status") == "Failed"
                    for previous in previous_progress)
        )
        accepted_failure = (
            progress.get("job_url") == job_url and progress.get("job_status") == "Failed"
            and progress.get("live_refresh_executed") is True
            and progress.get("submission_attempted") is True
        )
        if (
            not entry or entry["input_hash"] != fingerprint or entry["status"] != "running"
            or entry.get("failure_type") != "ProjectionRefreshError"
            or not (accepted_failure or pre_submission)
            or progress.get("capture", {}).get("source_etag") != ledger_snapshot.version
            or progress.get("capture", {}).get("state") != ledger_snapshot.state
            or "committed_manifest" in progress
            or entry.get("projection_recovery", {}).get("status") == "authorized"
            or any(other != key and other.startswith("projection-refresh/")
                   and value["status"] == "running"
                   for other, value in snapshot.state["turns"].items())
        ):
            raise RuntimeError("Original failed capture changed or conflicts with another refresh")
        entry["projection_recovery"] = {
            "status": "authorized", "manager_id": manager_id,
            "job_url": progress.get("job_url"), "original_job_url": job_url,
            "pre_submission": pre_submission,
            "source_etag": ledger_snapshot.version, "verified_job": job,
            "authorized_at": datetime.now(timezone.utc).isoformat(),
        }
        if journal.compare_and_swap(snapshot.version, snapshot.state):
            return key
    raise RuntimeError("Could not persist explicit projection reconciliation")


def refresh_runtime_projection(runtime, bindings, store, journal):
    config = projection_config(runtime, bindings)
    configured = bindings["fabric"]["projection"]
    snapshot = store.read()
    captured_at = datetime.now(timezone.utc).isoformat()
    if snapshot.state is None or not isinstance(snapshot.version, str) or not snapshot.version:
        raise RuntimeError("An actual versioned ledger capture is required")
    envelope = {
        "contract_version": 1, "state": snapshot.state,
        "source_etag": snapshot.version, "captured_at": captured_at,
    }
    fingerprint = hashlib.sha256(json.dumps(
        {"source_etag": snapshot.version, "state": snapshot.state},
        sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()
    key = "projection-refresh/" + fingerprint
    claimed = runtime._claim_turn(
        journal, key, fingerprint, exclusive_prefix="projection-refresh/",
    )
    if claimed["status"] == "complete":
        result = json.loads(claimed["answer"])
        return {**result, "source_etag_current": store.read().version == result["source_etag"]}

    def get_token(scope):
        if scope == "https://api.fabric.microsoft.com/.default":
            return asyncio.run(runtime._root_fabric_token(config.bank_principal_id))
        return runtime.credential.get_token(scope).token
    with httpx.Client(timeout=120, follow_redirects=False) as http:
        def send(method, url, *, headers, body, timeout_seconds):
            response = http.request(
                method, url, headers=headers, json=body, timeout=timeout_seconds,
            )
            if response.content:
                try:
                    content = response.json()
                except ValueError:
                    if response.is_error or response.status_code in (202, 204):
                        content = None
                    else:
                        raise RuntimeError("Projection endpoint returned a non-JSON response") from None
            else:
                content = None
            return HttpResponse(response.status_code, dict(response.headers), content)

        def checkpoint(progress):
            persist_refresh_progress(journal, key, fingerprint, {
                "capture": envelope, **progress,
            })

        checkpoint({"phase": "captured"})
        try:
            manifest = read_projection_manifest(
                config, credential_kind="agent_identity", get_token=get_token,
                send=send, timeout_seconds=30,
            )
            freshness = configured["projection_freshness"]
            previous_etag = manifest["source_etag"] if manifest is not None else (
                freshness["source_etag"] if "source_etag" in freshness
                else freshness["ledger_version"]
            )
            checkpoint({"phase": "prepared", "expected_projection_etag": previous_etag})
            result = refresh_projection(
                envelope, config=config, expected_projection_etag=previous_etag,
                credential_kind="agent_identity", get_token=get_token, send=send,
                refresh_id=fingerprint, checkpoint=checkpoint, timeout_seconds=600,
            )
        except ProjectionRefreshError as error:
            checkpoint(error.progress)
            runtime._record_turn_failure(journal, key, fingerprint, type(error).__name__)
            raise
        except (httpx.HTTPError, RuntimeError, ValueError, PermissionError) as error:
            runtime._record_turn_failure(journal, key, fingerprint, type(error).__name__)
            raise
    runtime._complete_turn(journal, key, fingerprint, json.dumps(result, allow_nan=False))
    return {**result, "source_etag_current": store.read().version == result["source_etag"]}


def transaction_fingerprint(transaction):
    return hashlib.sha256(json.dumps(
        transaction, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


def drain_projection_queue(runtime, bindings, store, journal):
    """The atomically committed transaction log is the durable refresh queue."""
    config = projection_config(runtime, bindings)
    snapshot = store.read()
    if snapshot.state is None:
        raise RuntimeError("Projection queue requires an initialized ledger")
    validate_snapshot({
        "contract_version": 1, "state": snapshot.state,
        "source_etag": snapshot.version, "captured_at": datetime.now(timezone.utc).isoformat(),
    }, config.expected_source_policy)
    journal_snapshot = journal.read()
    if journal_snapshot.state is not None and (
        journal_snapshot.state.get("schema_version") != 1
        or not isinstance(journal_snapshot.state.get("turns"), dict)
    ):
        raise RuntimeError("Unsupported projection queue journal")
    turns = journal_snapshot.state["turns"] if journal_snapshot.state is not None else {}
    pending = []
    for transaction in snapshot.state["transactions"]:
        key = "projection-transfer/" + transaction["transaction_id"]
        acknowledged = turns.get(key)
        if acknowledged is not None:
            if (
                acknowledged["status"] != "complete"
                or acknowledged["input_hash"] != transaction_fingerprint(transaction)
            ):
                raise RuntimeError("Projected transaction acknowledgement changed")
        else:
            pending.append(transaction)
    if not pending:
        return {"status": "idle", "pending_transaction_ids": []}

    result = refresh_runtime_projection(runtime, bindings, store, journal)
    ids = [transaction["transaction_id"] for transaction in pending]
    if result["source_etag_current"] is not True:
        return {"status": "stale", "pending_transaction_ids": ids, "refresh": result}
    refresh_id = result["refresh_id"]
    for _ in range(32):
        snapshot = journal.read()
        if snapshot.state is None:
            raise RuntimeError("Projection receipt journal disappeared")
        completed = snapshot.state["turns"]["projection-refresh/" + refresh_id]
        capture = completed["projection_progress"]["capture"]
        manifest = result["committed_manifest"]
        if (
            completed["status"] != "complete"
            or result["job_status"] != "Completed"
            or result["metadata_sync_pending"] is not False
            or manifest["projection_state"] != "complete"
            or manifest["refresh_id"] != refresh_id
            or capture["source_etag"] != result["source_etag"]
        ):
            raise RuntimeError("Post-transfer refresh has no verified complete capture")
        captured = {
            transaction["transaction_id"]: transaction
            for transaction in capture["state"]["transactions"]
        }
        for transaction in pending:
            transaction_id = transaction["transaction_id"]
            if captured.get(transaction_id) != transaction:
                raise RuntimeError("Refresh did not capture the exact queued transaction")
            key = "projection-transfer/" + transaction_id
            entry = {
                "status": "complete", "input_hash": transaction_fingerprint(transaction),
                "answer": json.dumps({
                    "refresh_id": refresh_id, "source_etag": result["source_etag"],
                }, sort_keys=True),
            }
            prior = snapshot.state["turns"].get(key)
            if prior is not None and (
                prior["status"] != "complete" or prior["input_hash"] != entry["input_hash"]
            ):
                raise RuntimeError("Projected transaction acknowledgement conflicts")
            if prior is None:
                snapshot.state["turns"][key] = entry
        if journal.compare_and_swap(snapshot.version, snapshot.state):
            return {"status": "complete", "pending_transaction_ids": [], "refresh": result}
    raise RuntimeError("Could not acknowledge projected transactions")
