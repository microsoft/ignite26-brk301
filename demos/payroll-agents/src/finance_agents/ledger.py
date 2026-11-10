"""Small JSON-snapshot ledger for controlled hotel payroll funding.

The runtime must authenticate humans before constructing Principal, and resolve
agent identities to AgentRole outside this module. Neither type is a credential.
StateStore implementations must atomically replace the ENTIRE snapshot, including
balances, approvals, idempotency bindings, transactions, tasks, and outbox.
Store reads must return isolated snapshots; versions are opaque strings.
There is deliberately no clock, approval expiry, real-money API, or message send.
"""

from __future__ import annotations

import copy
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Protocol, TypeVar


class DomainError(Exception):
    """Explicit failure. Integrations should branch on code, not message."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class AgentRole(str, Enum):
    HOTEL_FINANCE = "hotel_finance"
    BANKING_OPERATIONS = "banking_operations"


class Decision(str, Enum):
    APPROVE = "approve"
    DENY = "deny"
    CANCEL = "cancel"


@dataclass(frozen=True)
class Principal:
    """An authenticated human identity supplied by a trusted runtime."""

    tenant_id: str
    object_id: str


@dataclass(frozen=True)
class TransferAction:
    source_account_id: str
    destination_account_id: str
    amount_cents: int
    currency: str


@dataclass(frozen=True)
class LedgerConfig:
    tenant_id: str
    approver_object_ids: tuple[str, str]
    hotel_owner_id: str
    currency: str = "USD"
    approval_threshold_cents: int = 1_000_000
    delivery_max_attempts: int = 3


@dataclass(frozen=True)
class Snapshot:
    """An absent store returns Snapshot(None, None)."""

    version: str | None
    state: dict[str, Any] | None


class StateStore(Protocol):
    def read(self) -> Snapshot:
        """Read an isolated snapshot and its opaque concurrency token."""
        ...

    def compare_and_swap(
        self, expected_version: str | None, state: dict[str, Any]
    ) -> bool:
        """Atomically replace state iff version matches; False means contention.

        None means create-if-absent. Operational failures must raise, not return
        False. A successful replacement must produce a fresh concurrency token.
        """
        ...


class SQLiteStateStore:
    """Durable local provider; separate connections support multiple workers."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path == ":memory:":
            raise DomainError("INVALID_STORE", "Use a durable SQLite file.")
        try:
            with closing(sqlite3.connect(self.path, timeout=10)) as connection, connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS finance_state ("
                    "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), "
                    "version INTEGER NOT NULL, payload TEXT NOT NULL)"
                )
        except sqlite3.Error as exc:
            raise DomainError("STORE_ERROR", "Cannot initialize SQLite store.") from exc

    def read(self) -> Snapshot:
        try:
            with closing(sqlite3.connect(self.path, timeout=10)) as connection, connection:
                row = connection.execute(
                    "SELECT version, payload FROM finance_state WHERE singleton = 1"
                ).fetchone()
            if row is None:
                return Snapshot(None, None)
            return Snapshot(str(row[0]), json.loads(row[1]))
        except (sqlite3.Error, json.JSONDecodeError) as exc:
            raise DomainError("STORE_ERROR", "Cannot read SQLite state.") from exc

    def compare_and_swap(
        self, expected_version: str | None, state: dict[str, Any]
    ) -> bool:
        payload = json.dumps(state, sort_keys=True, separators=(",", ":"), allow_nan=False)
        try:
            with closing(sqlite3.connect(self.path, timeout=10)) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                if expected_version is None:
                    cursor = connection.execute(
                        "INSERT OR IGNORE INTO finance_state "
                        "(singleton, version, payload) VALUES (1, 1, ?)",
                        (payload,),
                    )
                else:
                    cursor = connection.execute(
                        "UPDATE finance_state SET version = version + 1, payload = ? "
                        "WHERE singleton = 1 AND CAST(version AS TEXT) = ?",
                        (payload, expected_version),
                    )
                return cursor.rowcount == 1
        except sqlite3.Error as exc:
            raise DomainError("STORE_ERROR", "Cannot replace SQLite state.") from exc


T = TypeVar("T")


def _text(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise DomainError("INVALID_ARGUMENT", f"{name} must be a nonempty string.")


def _positive_integer(value: Any, code: str, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DomainError(code, f"{name} must be a positive integer.")


class Ledger:
    """CAS-based domain service. All returned dictionaries are isolated copies.

    Constructor seed is create-if-absent, never a reset. Reopening requires the
    same policy configuration; policy drift fails closed. CAS contention retries
    at most max_cas_attempts; store exceptions propagate without silent retry.
    """

    RESERVE_ACCOUNT_ID = "hotel-reserve"
    PAYROLL_ACCOUNT_ID = "hotel-payroll"

    def __init__(
        self, store: StateStore, config: LedgerConfig, *, max_cas_attempts: int = 32
    ):
        if not isinstance(config, LedgerConfig):
            raise DomainError("INVALID_CONFIGURATION", "LedgerConfig is required.")
        _text(config.tenant_id, "tenant_id")
        _text(config.hotel_owner_id, "hotel_owner_id")
        _text(config.currency, "currency")
        if (
            not isinstance(config.approver_object_ids, tuple)
            or len(config.approver_object_ids) != 2
        ):
            raise DomainError("INVALID_CONFIGURATION", "Configure exactly two approvers.")
        for object_id in config.approver_object_ids:
            _text(object_id, "approver_object_id")
        if len(set(config.approver_object_ids)) != 2:
            raise DomainError("INVALID_CONFIGURATION", "Approver IDs must be distinct.")
        _positive_integer(
            config.approval_threshold_cents,
            "INVALID_CONFIGURATION",
            "approval_threshold_cents",
        )
        _positive_integer(
            config.delivery_max_attempts, "INVALID_CONFIGURATION", "delivery_max_attempts"
        )
        _positive_integer(max_cas_attempts, "INVALID_CONFIGURATION", "max_cas_attempts")
        self.store = store
        self.config = config
        self.max_cas_attempts = max_cas_attempts
        self._policy = {
            "tenant_id": config.tenant_id,
            "approver_object_ids": sorted(config.approver_object_ids),
            "hotel_owner_id": config.hotel_owner_id,
            "currency": config.currency,
            "approval_threshold_cents": config.approval_threshold_cents,
            "delivery_max_attempts": config.delivery_max_attempts,
        }
        for _ in range(self.max_cas_attempts):
            snapshot = store.read()
            if snapshot.state is not None:
                self._check_state(snapshot.state)
                return
            if snapshot.version is not None:
                raise DomainError("INVALID_STATE", "Absent state has a version.")
            if store.compare_and_swap(None, self._seed()):
                return
        raise DomainError("CAS_RETRIES_EXHAUSTED", "Cannot initialize ledger after contention.")

    def _seed(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "simulation": True,
            "policy": copy.deepcopy(self._policy),
            "accounts": {
                self.RESERVE_ACCOUNT_ID: {
                    "owner_id": self.config.hotel_owner_id,
                    "currency": self.config.currency,
                    "balance_cents": 5_000_000,
                },
                self.PAYROLL_ACCOUNT_ID: {
                    "owner_id": self.config.hotel_owner_id,
                    "currency": self.config.currency,
                    "balance_cents": 1_000_000,
                },
            },
            "tasks": {
                "payroll-funding": {
                    "status": "pending",
                    "obligation_cents": 2_500_000,
                    "currency": self.config.currency,
                    "source_account_id": self.RESERVE_ACCOUNT_ID,
                    "destination_account_id": self.PAYROLL_ACCOUNT_ID,
                    "last_transaction_id": None,
                }
            },
            "approvals": {},
            "idempotency": {},
            "transactions": [],
            "next_transaction_sequence": 1,
            "outbox": {},
        }

    def _check_state(self, state: dict[str, Any]) -> None:
        if (
            not isinstance(state, dict)
            or state.get("schema_version") != 1
            or state.get("simulation") is not True
        ):
            raise DomainError("INVALID_STATE", "Unsupported ledger snapshot.")
        if state.get("policy") != self._policy:
            raise DomainError("POLICY_CONFIGURATION_MISMATCH", "Stored policy differs from configuration.")
        for field in ("accounts", "tasks", "approvals", "idempotency", "outbox"):
            if not isinstance(state.get(field), dict):
                raise DomainError("INVALID_STATE", f"Invalid {field} collection.")
        if not isinstance(state.get("transactions"), list):
            raise DomainError("INVALID_STATE", "Invalid transaction collection.")
        sequence = state.get("next_transaction_sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
            raise DomainError("INVALID_STATE", "Invalid transaction sequence.")
        for account in state["accounts"].values():
            if not isinstance(account, dict):
                raise DomainError("INVALID_STATE", "Invalid account.")
            balance = account.get("balance_cents")
            if isinstance(balance, bool) or not isinstance(balance, int) or balance < 0:
                raise DomainError("INVALID_STATE", "Balances must be nonnegative integer cents.")

    def snapshot(self) -> dict[str, Any]:
        snapshot = self.store.read()
        if snapshot.state is None:
            raise DomainError("INVALID_STATE", "Initialized state is missing.")
        self._check_state(snapshot.state)
        return copy.deepcopy(snapshot.state)

    def _mutate(self, operation: Callable[[dict[str, Any]], T]) -> T:
        for _ in range(self.max_cas_attempts):
            snapshot = self.store.read()
            if snapshot.state is None:
                raise DomainError("INVALID_STATE", "Initialized state is missing.")
            self._check_state(snapshot.state)
            state = copy.deepcopy(snapshot.state)
            result = operation(state)
            self._check_state(state)
            # Read-only idempotent replays linearize at the snapshot read.
            if state == snapshot.state:
                return copy.deepcopy(result)
            if self.store.compare_and_swap(snapshot.version, state):
                return copy.deepcopy(result)
        raise DomainError("CAS_RETRIES_EXHAUSTED", "Ledger contention retry limit reached.")

    def _action(self, action: TransferAction) -> dict[str, Any]:
        if not isinstance(action, TransferAction):
            raise DomainError("INVALID_ACTION", "TransferAction is required.")
        _text(action.source_account_id, "source_account_id")
        _text(action.destination_account_id, "destination_account_id")
        _text(action.currency, "currency")
        _positive_integer(action.amount_cents, "INVALID_AMOUNT", "amount_cents")
        if action.source_account_id == action.destination_account_id:
            raise DomainError("SAME_ACCOUNT", "Source and destination must differ.")
        return asdict(action)

    def _validate_funds(self, state: dict[str, Any], action: dict[str, Any]) -> None:
        source = state["accounts"].get(action["source_account_id"])
        destination = state["accounts"].get(action["destination_account_id"])
        if source is None or destination is None:
            raise DomainError("ACCOUNT_NOT_FOUND", "Source or destination does not exist.")
        if any(
            account.get("owner_id") != state["policy"]["hotel_owner_id"]
            for account in (source, destination)
        ):
            raise DomainError("NON_HOTEL_ACCOUNT", "Both accounts must belong to this hotel.")
        if (
            action["currency"] != state["policy"]["currency"]
            or any(account.get("currency") != action["currency"] for account in (source, destination))
        ):
            raise DomainError("CURRENCY_MISMATCH", "Action and accounts must use policy currency.")
        if source["balance_cents"] < action["amount_cents"]:
            raise DomainError("INSUFFICIENT_FUNDS", "Source cannot fund the transfer.")

    def payroll_funding_action(self) -> TransferAction:
        state = self.snapshot()
        task = state["tasks"]["payroll-funding"]
        shortage = task["obligation_cents"] - state["accounts"][task["destination_account_id"]]["balance_cents"]
        if shortage <= 0:
            raise DomainError("PAYROLL_ALREADY_FUNDED", "Payroll obligation is already funded.")
        return TransferAction(
            task["source_account_id"], task["destination_account_id"], shortage, task["currency"]
        )

    def request_approval(
        self, action: TransferAction, *, request_id: str, idempotency_key: str
    ) -> dict[str, Any]:
        """One exact action/key and four delivery records, never four approvals."""
        action_data = self._action(action)
        _text(request_id, "request_id")
        _text(idempotency_key, "idempotency_key")

        def operation(state: dict[str, Any]) -> dict[str, Any]:
            existing = state["approvals"].get(request_id)
            if existing is not None:
                if existing["action"] != action_data or existing["idempotency_key"] != idempotency_key:
                    raise DomainError("REQUEST_CONFLICT", "Request ID is bound to another proposal.")
                return existing
            binding = state["idempotency"].get(idempotency_key)
            if binding is not None:
                raise DomainError("IDEMPOTENCY_CONFLICT", "Key is already bound to another action/request.")
            self._validate_funds(state, action_data)
            if action_data["amount_cents"] <= state["policy"]["approval_threshold_cents"]:
                raise DomainError("APPROVAL_NOT_REQUIRED", "Only above-threshold actions need a request.")
            request = {
                "request_id": request_id,
                "action": action_data,
                "idempotency_key": idempotency_key,
                "status": "pending",
                "decision": None,
                "decision_by": None,
                "executed_transaction_id": None,
            }
            state["approvals"][request_id] = request
            state["idempotency"][idempotency_key] = {
                "action": copy.deepcopy(action_data),
                "approval_request_id": request_id,
                "transaction_id": None,
            }
            for index, recipient in enumerate(state["policy"]["approver_object_ids"]):
                for channel in ("teams", "email"):
                    delivery_id = f"{request_id}/{index}/{channel}"
                    state["outbox"][delivery_id] = {
                        "delivery_id": delivery_id,
                        "request_id": request_id,
                        "request_status": "pending",
                        "recipient": {
                            "tenant_id": state["policy"]["tenant_id"],
                            "object_id": recipient,
                        },
                        "channel": channel,
                        "status": "pending",
                        "attempts": 0,
                        "attempt_id": None,
                        "last_error": None,
                        "last_result": None,
                    }
            return request

        return self._mutate(operation)

    def decide(
        self, request_id: str, principal: Principal, decision: Decision
    ) -> dict[str, Any]:
        """First eligible authenticated decision wins. No expiry or clock check."""
        _text(request_id, "request_id")
        if not isinstance(principal, Principal):
            raise DomainError("UNAUTHENTICATED_PRINCIPAL", "Typed authenticated Principal is required.")
        if not isinstance(decision, Decision):
            raise DomainError("INVALID_DECISION", "Use a Decision enum.")

        def operation(state: dict[str, Any]) -> dict[str, Any]:
            policy = state["policy"]
            if principal.tenant_id != policy["tenant_id"]:
                raise DomainError("WRONG_TENANT", "Principal is not in the configured tenant.")
            if principal.object_id not in policy["approver_object_ids"]:
                raise DomainError("UNAUTHORIZED_APPROVER", "Principal is not an eligible approver.")
            request = state["approvals"].get(request_id)
            if request is None:
                raise DomainError("REQUEST_NOT_FOUND", "Approval request does not exist.")
            identity = asdict(principal)
            if request["status"] != "pending":
                if request["decision"] == decision.value and request["decision_by"] == identity:
                    return request
                raise DomainError("REQUEST_NOT_PENDING", "Another decision already won.")
            request["status"] = {
                Decision.APPROVE: "approved",
                Decision.DENY: "denied",
                Decision.CANCEL: "cancelled",
            }[decision]
            request["decision"] = decision.value
            request["decision_by"] = identity
            for delivery in state["outbox"].values():
                if delivery["request_id"] == request_id:
                    delivery["request_status"] = request["status"]
                    if delivery["status"] in ("pending", "failed"):
                        delivery["status"] = "cancelled"
            return request

        return self._mutate(operation)

    def execute_transfer(
        self,
        action: TransferAction,
        *,
        idempotency_key: str,
        role: AgentRole,
        approval_request_id: str | None = None,
    ) -> dict[str, Any]:
        """Banking-only atomic transfer; exact action/key/request retry is safe."""
        if role is not AgentRole.BANKING_OPERATIONS:
            raise DomainError("BANKING_ROLE_REQUIRED", "Only Hotel Treasurer may execute transfers.")
        action_data = self._action(action)
        _text(idempotency_key, "idempotency_key")
        if approval_request_id is not None:
            _text(approval_request_id, "approval_request_id")

        def operation(state: dict[str, Any]) -> dict[str, Any]:
            binding = state["idempotency"].get(idempotency_key)
            if binding is not None:
                if (
                    binding["action"] != action_data
                    or binding["approval_request_id"] != approval_request_id
                ):
                    raise DomainError("IDEMPOTENCY_CONFLICT", "Key cannot be reused for changed action/request.")
                if binding["transaction_id"] is not None:
                    for transaction in state["transactions"]:
                        if transaction["transaction_id"] == binding["transaction_id"]:
                            return transaction
                    raise DomainError("INVALID_STATE", "Executed binding has no transaction.")
            self._validate_funds(state, action_data)
            request = None
            if action_data["amount_cents"] > state["policy"]["approval_threshold_cents"] or approval_request_id is not None:
                if approval_request_id is None:
                    raise DomainError("APPROVAL_REQUIRED", "Above-threshold transfer requires exact approval.")
                request = state["approvals"].get(approval_request_id)
                if request is None:
                    raise DomainError("REQUEST_NOT_FOUND", "Approval request does not exist.")
                if request["action"] != action_data or request["idempotency_key"] != idempotency_key:
                    raise DomainError("APPROVAL_BINDING_MISMATCH", "Approval is for another exact action/key.")
                if request["status"] != "approved":
                    raise DomainError("APPROVAL_NOT_APPROVED", f"Request is {request['status']}.")
                if request["executed_transaction_id"] is not None:
                    raise DomainError("APPROVAL_ALREADY_EXECUTED", "Approval has already been consumed.")
            transaction_id = f"tx-{state['next_transaction_sequence']}"
            source = state["accounts"][action_data["source_account_id"]]
            destination = state["accounts"][action_data["destination_account_id"]]
            source["balance_cents"] -= action_data["amount_cents"]
            destination["balance_cents"] += action_data["amount_cents"]
            transaction = {
                "transaction_id": transaction_id,
                "action": action_data,
                "idempotency_key": idempotency_key,
                "approval_request_id": approval_request_id,
                "source_balance_cents": source["balance_cents"],
                "destination_balance_cents": destination["balance_cents"],
            }
            state["transactions"].append(transaction)
            state["next_transaction_sequence"] += 1
            state["idempotency"][idempotency_key] = {
                "action": copy.deepcopy(action_data),
                "approval_request_id": approval_request_id,
                "transaction_id": transaction_id,
            }
            if request is not None:
                request["executed_transaction_id"] = transaction_id
            task = state["tasks"]["payroll-funding"]
            task["status"] = (
                "funded"
                if state["accounts"][task["destination_account_id"]]["balance_cents"] >= task["obligation_cents"]
                else "pending"
            )
            task["last_transaction_id"] = transaction_id
            return transaction

        return self._mutate(operation)

    def claim_delivery(self, delivery_id: str, *, attempt_id: str) -> dict[str, Any]:
        """Reserve one attempt; only pending requests may initiate deliveries.

        attempt_id is globally unique per attempt, supplied by the worker.
        An in-flight attempt must be completed (including explicit failure after
        worker recovery) before retry; there is no time-based lease stealing.
        """
        _text(delivery_id, "delivery_id")
        _text(attempt_id, "attempt_id")

        def operation(state: dict[str, Any]) -> dict[str, Any]:
            delivery = state["outbox"].get(delivery_id)
            if delivery is None:
                raise DomainError("DELIVERY_NOT_FOUND", "Delivery does not exist.")
            if delivery["attempt_id"] == attempt_id:
                return delivery
            if delivery["request_status"] != "pending":
                raise DomainError("REQUEST_NOT_PENDING", "Do not deliver a terminal approval request.")
            if delivery["status"] == "succeeded":
                raise DomainError("DELIVERY_ALREADY_SUCCEEDED", "Delivery is already complete.")
            if delivery["status"] == "in_flight":
                raise DomainError("DELIVERY_IN_FLIGHT", "Another attempt owns this delivery.")
            if delivery["attempts"] >= state["policy"]["delivery_max_attempts"]:
                raise DomainError("DELIVERY_RETRIES_EXHAUSTED", "Delivery attempt limit reached.")
            if any(item["attempt_id"] == attempt_id for item in state["outbox"].values()):
                raise DomainError("DELIVERY_ATTEMPT_CONFLICT", "Attempt ID belongs to another delivery.")
            delivery["attempts"] += 1
            delivery["attempt_id"] = attempt_id
            delivery["status"] = "in_flight"
            delivery["last_result"] = None
            return delivery

        return self._mutate(operation)

    def record_delivery_result(
        self,
        delivery_id: str,
        *,
        attempt_id: str,
        succeeded: bool,
        error: str | None = None,
        provider_result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record a send outcome, never execute/replay a financial action.

        Sending is external: exactly-once delivery across a crash cannot be
        guaranteed without provider-side deduplication using the delivery ID.
        """
        _text(delivery_id, "delivery_id")
        _text(attempt_id, "attempt_id")
        if not isinstance(succeeded, bool):
            raise DomainError("INVALID_ARGUMENT", "succeeded must be bool.")
        if error is not None and not isinstance(error, str):
            raise DomainError("INVALID_ARGUMENT", "error must be string or None.")
        if succeeded and error is not None:
            raise DomainError("INVALID_ARGUMENT", "Successful delivery cannot include an error.")
        if provider_result is not None and not isinstance(provider_result, dict):
            raise DomainError("INVALID_ARGUMENT", "provider_result must be a dictionary or None.")
        if not succeeded and provider_result is not None:
            raise DomainError("INVALID_ARGUMENT", "Failed delivery cannot include a provider result.")

        def operation(state: dict[str, Any]) -> dict[str, Any]:
            delivery = state["outbox"].get(delivery_id)
            if delivery is None:
                raise DomainError("DELIVERY_NOT_FOUND", "Delivery does not exist.")
            if delivery["attempt_id"] != attempt_id:
                raise DomainError("DELIVERY_ATTEMPT_CONFLICT", "Attempt does not own delivery.")
            outcome = {
                "succeeded": succeeded,
                "error": error,
                "provider_result": copy.deepcopy(provider_result),
            }
            if delivery["status"] != "in_flight":
                if delivery["last_result"] == outcome:
                    return delivery
                raise DomainError("DELIVERY_RESULT_CONFLICT", "Attempt already has a different result.")
            delivery["status"] = "succeeded" if succeeded else "failed"
            delivery["last_error"] = error
            delivery["last_result"] = outcome
            return delivery

        return self._mutate(operation)
