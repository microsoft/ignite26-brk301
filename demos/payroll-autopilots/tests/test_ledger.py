"""Stdlib-only domain tests; MemoryStateStore is deliberately test-only."""

import copy
import json
import shutil
import sqlite3
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from finance_autopilots import (
    AgentRole,
    Decision,
    DomainError,
    Ledger,
    LedgerConfig,
    Principal,
    Snapshot,
    SQLiteStateStore,
    TransferAction,
)


class MemoryStateStore:
    """Test-only isolated, thread-safe whole-snapshot CAS provider."""

    def __init__(self):
        self.state = None
        self.version = 0
        self.lock = threading.Lock()
        self.reject_next = 0
        self.cas_calls = 0

    def read(self):
        with self.lock:
            return Snapshot(
                str(self.version) if self.state is not None else None,
                copy.deepcopy(self.state),
            )

    def compare_and_swap(self, expected_version, state):
        with self.lock:
            self.cas_calls += 1
            if self.reject_next:
                self.reject_next -= 1
                return False
            actual = str(self.version) if self.state is not None else None
            if actual != expected_version:
                return False
            self.state = copy.deepcopy(state)
            self.version += 1
            return True


CONFIG = LedgerConfig("test-tenant", ("test-user-a", "test-user-b"), "test-hotel")
PRINCIPALS = tuple(Principal(CONFIG.tenant_id, user) for user in CONFIG.approver_object_ids)
BANK = AgentRole.BANKING_OPERATIONS


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStateStore()
        self.ledger = Ledger(self.store, CONFIG)
        self.action = self.ledger.payroll_funding_action()

    def assertCode(self, code, function, *args, **kwargs):
        with self.assertRaises(DomainError) as caught:
            function(*args, **kwargs)
        self.assertEqual(code, caught.exception.code)

    def request(self, request_id="req", key="fund", action=None):
        return self.ledger.request_approval(
            action or self.action, request_id=request_id, idempotency_key=key
        )

    def approve(self, principal=None):
        self.request()
        return self.ledger.decide("req", principal or PRINCIPALS[0], Decision.APPROVE)

    def execute(self, action=None, key="fund", request_id="req"):
        return self.ledger.execute_transfer(
            action or self.action,
            idempotency_key=key,
            role=BANK,
            approval_request_id=request_id,
        )

    def change_state(self, operation):
        snapshot = self.store.read()
        operation(snapshot.state)
        self.assertTrue(self.store.compare_and_swap(snapshot.version, snapshot.state))

    def test_baseline_and_seed_rerun_do_not_reset(self):
        state = self.ledger.snapshot()
        self.assertEqual(5_000_000, state["accounts"]["hotel-reserve"]["balance_cents"])
        self.assertEqual(1_000_000, state["accounts"]["hotel-payroll"]["balance_cents"])
        self.assertEqual(2_500_000, state["tasks"]["payroll-funding"]["obligation_cents"])
        self.assertEqual(1_500_000, self.action.amount_cents)
        self.approve()
        self.execute()
        before = self.ledger.snapshot()
        reopened = Ledger(self.store, CONFIG)
        self.assertEqual(before, reopened.snapshot())
        self.assertCode("PAYROLL_ALREADY_FUNDED", reopened.payroll_funding_action)

    def test_snapshot_is_json_serializable_and_isolated(self):
        state = self.ledger.snapshot()
        self.assertEqual(state, json.loads(json.dumps(state)))
        state["accounts"]["hotel-reserve"]["balance_cents"] = -1
        self.assertEqual(5_000_000, self.ledger.snapshot()["accounts"]["hotel-reserve"]["balance_cents"])

    def test_invalid_amounts_rejected_without_changes(self):
        before = self.ledger.snapshot()
        for amount in (True, False, 0, -1, 1.0, 1.5, "1500000", None):
            with self.subTest(amount=amount):
                action = replace(self.action, amount_cents=amount)
                self.assertCode("INVALID_AMOUNT", self.execute, action)
                self.assertCode("INVALID_AMOUNT", self.request, action=action)
        self.assertEqual(before, self.ledger.snapshot())

    def test_currency_same_account_missing_account_and_insufficient_funds(self):
        for code, action in (
            ("CURRENCY_MISMATCH", replace(self.action, currency="EUR")),
            ("SAME_ACCOUNT", replace(self.action, destination_account_id="hotel-reserve")),
            ("ACCOUNT_NOT_FOUND", replace(self.action, source_account_id="missing")),
            ("INSUFFICIENT_FUNDS", replace(self.action, amount_cents=5_000_001)),
        ):
            with self.subTest(code=code):
                self.assertCode(code, self.execute, action, request_id=None)
        self.assertEqual([], self.ledger.snapshot()["transactions"])

    def test_non_hotel_source_or_destination_rejected(self):
        for account_id in ("hotel-reserve", "hotel-payroll"):
            with self.subTest(account_id=account_id):
                store = MemoryStateStore()
                ledger = Ledger(store, CONFIG)
                snapshot = store.read()
                snapshot.state["accounts"][account_id]["owner_id"] = "other-hotel"
                store.compare_and_swap(snapshot.version, snapshot.state)
                self.assertCode(
                    "NON_HOTEL_ACCOUNT",
                    ledger.execute_transfer,
                    self.action,
                    idempotency_key="x",
                    role=BANK,
                )
                self.assertCode(
                    "NON_HOTEL_ACCOUNT",
                    ledger.request_approval,
                    self.action,
                    request_id="req",
                    idempotency_key="x",
                )

    def test_account_currency_mismatch_rejected(self):
        self.change_state(lambda state: state["accounts"]["hotel-payroll"].update(currency="EUR"))
        self.assertCode("CURRENCY_MISMATCH", self.execute, request_id=None)

    def test_below_and_exact_threshold_need_no_approval(self):
        for amount in (1, 999_999, 1_000_000):
            with self.subTest(amount=amount):
                action = replace(self.action, amount_cents=amount)
                self.assertCode("APPROVAL_NOT_REQUIRED", self.request, action=action)
                transaction = self.execute(action, key=f"amount-{amount}", request_id=None)
                self.assertIsNone(transaction["approval_request_id"])
        self.assertEqual({}, self.ledger.snapshot()["approvals"])

    def test_above_threshold_requires_approval(self):
        self.assertCode("APPROVAL_REQUIRED", self.execute, request_id=None)
        self.request()
        self.assertCode("APPROVAL_NOT_APPROVED", self.execute)
        self.assertEqual([], self.ledger.snapshot()["transactions"])

    def test_hotel_finance_cannot_write_or_retrieve_execution_success(self):
        action = replace(self.action, amount_cents=100)
        for role in (AgentRole.HOTEL_FINANCE, "banking_operations", None):
            self.assertCode(
                "BANKING_ROLE_REQUIRED", self.ledger.execute_transfer,
                action, idempotency_key="x", role=role,
            )
        self.execute(action, key="x", request_id=None)
        self.assertCode(
            "BANKING_ROLE_REQUIRED", self.ledger.execute_transfer,
            action, idempotency_key="x", role=AgentRole.HOTEL_FINANCE,
        )

    def test_either_approver_can_approve(self):
        for index, principal in enumerate(PRINCIPALS):
            action = replace(self.action, amount_cents=1_000_001)
            request_id, key = f"request-{index}", f"key-{index}"
            self.request(request_id, key, action)
            approved = self.ledger.decide(request_id, principal, Decision.APPROVE)
            self.assertEqual("approved", approved["status"])
            self.assertEqual(principal.object_id, approved["decision_by"]["object_id"])
            self.execute(action, key=key, request_id=request_id)

    def test_unauthorized_wrong_tenant_and_untyped_principal(self):
        self.request()
        for code, principal in (
            ("UNAUTHORIZED_APPROVER", Principal(CONFIG.tenant_id, "intruder")),
            ("WRONG_TENANT", Principal("other-tenant", PRINCIPALS[0].object_id)),
            ("UNAUTHENTICATED_PRINCIPAL", {"tenant_id": CONFIG.tenant_id, "object_id": PRINCIPALS[0].object_id}),
        ):
            self.assertCode(code, self.ledger.decide, "req", principal, Decision.APPROVE)
        self.assertCode("INVALID_DECISION", self.ledger.decide, "req", PRINCIPALS[0], "approve")
        self.assertEqual("pending", self.ledger.snapshot()["approvals"]["req"]["status"])

    def test_no_expiry_and_no_clock_dependency(self):
        self.request()
        with patch("time.time", side_effect=AssertionError("Clock must not be consulted")):
            reopened = Ledger(self.store, CONFIG)
            reopened.decide("req", PRINCIPALS[1], Decision.APPROVE)
            reopened.execute_transfer(
                self.action, idempotency_key="fund", role=BANK, approval_request_id="req"
            )
        self.assertEqual("funded", self.ledger.snapshot()["tasks"]["payroll-funding"]["status"])

    def test_denial_and_cancellation_are_terminal_for_either_user(self):
        for decision in (Decision.DENY, Decision.CANCEL):
            for index, principal in enumerate(PRINCIPALS):
                with self.subTest(decision=decision, index=index):
                    request_id, key = f"{decision.value}-{index}", f"{decision.value}-key-{index}"
                    self.request(request_id, key)
                    result = self.ledger.decide(request_id, principal, decision)
                    self.assertEqual(result, self.ledger.decide(request_id, principal, decision))
                    self.assertCode(
                        "REQUEST_NOT_PENDING", self.ledger.decide,
                        request_id, PRINCIPALS[1 - index], Decision.APPROVE,
                    )
                    self.assertCode(
                        "APPROVAL_NOT_APPROVED", self.execute,
                        key=key, request_id=request_id,
                    )
        self.assertEqual([], self.ledger.snapshot()["transactions"])

    def test_exact_action_and_idempotency_binding_cannot_change(self):
        self.approve()
        changed_actions = (
            replace(self.action, amount_cents=1_400_000),
            replace(self.action, source_account_id="hotel-payroll", destination_account_id="hotel-reserve"),
            replace(self.action, currency="EUR"),
        )
        for changed in changed_actions:
            self.assertCode("REQUEST_CONFLICT", self.request, action=changed)
            self.assertCode("IDEMPOTENCY_CONFLICT", self.execute, changed)
        self.assertCode("IDEMPOTENCY_CONFLICT", self.execute, request_id=None)
        self.assertCode("IDEMPOTENCY_CONFLICT", self.request, request_id="new")
        self.assertCode("REQUEST_CONFLICT", self.request, key="new")
        self.assertCode("APPROVAL_BINDING_MISMATCH", self.execute, key="new")
        changed = replace(self.action, amount_cents=1_400_000)
        self.request("new", "new", changed)
        self.assertCode("APPROVAL_NOT_APPROVED", self.execute, changed, key="new", request_id="new")
        self.assertEqual([], self.ledger.snapshot()["transactions"])

    def test_execution_rechecks_authoritative_balance_and_owner(self):
        self.approve()
        for index in range(4):
            self.execute(replace(self.action, amount_cents=1_000_000), key=f"spend-{index}", request_id=None)
        before = self.ledger.snapshot()
        self.assertCode("INSUFFICIENT_FUNDS", self.execute)
        self.assertEqual(before, self.ledger.snapshot())
        self.change_state(lambda state: state["accounts"]["hotel-reserve"].update(owner_id="not-hotel"))
        self.assertCode("NON_HOTEL_ACCOUNT", self.execute)
        self.assertIsNone(self.ledger.snapshot()["approvals"]["req"]["executed_transaction_id"])

    def test_policy_drift_fails_closed_at_execution_and_reopen(self):
        self.approve()
        self.change_state(lambda state: state["policy"].update(approval_threshold_cents=2_000_000))
        self.assertCode("POLICY_CONFIGURATION_MISMATCH", self.execute)
        self.assertCode("POLICY_CONFIGURATION_MISMATCH", Ledger, self.store, CONFIG)

    def test_accounting_atomicity_and_idempotent_execution(self):
        self.approve()
        transaction = self.execute()
        before = self.ledger.snapshot()
        self.assertEqual(transaction, self.execute())
        self.assertEqual(before, self.ledger.snapshot())
        balances = [account["balance_cents"] for account in before["accounts"].values()]
        self.assertEqual(6_000_000, sum(balances))
        self.assertTrue(all(balance >= 0 for balance in balances))
        self.assertEqual(3_500_000, before["accounts"]["hotel-reserve"]["balance_cents"])
        self.assertEqual(2_500_000, before["accounts"]["hotel-payroll"]["balance_cents"])
        self.assertEqual(1, len(before["transactions"]))
        self.assertEqual(transaction["transaction_id"], before["approvals"]["req"]["executed_transaction_id"])
        self.assertEqual(transaction["transaction_id"], before["idempotency"]["fund"]["transaction_id"])

    def test_idempotent_retry_works_after_source_no_longer_can_fund(self):
        action = replace(self.action, amount_cents=5_000_000)
        self.request(action=action)
        self.ledger.decide("req", PRINCIPALS[0], Decision.APPROVE)
        transaction = self.execute(action)
        self.assertEqual(0, self.ledger.snapshot()["accounts"]["hotel-reserve"]["balance_cents"])
        self.assertEqual(transaction, self.execute(action))
        self.assertCode("IDEMPOTENCY_CONFLICT", self.execute, replace(action, amount_cents=100))

    def test_conflicting_decisions_first_wins_and_same_retry_is_safe(self):
        self.request()
        approved = self.ledger.decide("req", PRINCIPALS[0], Decision.APPROVE)
        self.assertEqual(approved, self.ledger.decide("req", PRINCIPALS[0], Decision.APPROVE))
        self.assertCode("REQUEST_NOT_PENDING", self.ledger.decide, "req", PRINCIPALS[1], Decision.APPROVE)
        self.assertCode("REQUEST_NOT_PENDING", self.ledger.decide, "req", PRINCIPALS[0], Decision.CANCEL)

    def test_concurrent_conflicting_replies_have_one_winner(self):
        self.request()
        barrier = threading.Barrier(2)

        def respond(index):
            barrier.wait()
            try:
                return self.ledger.decide("req", PRINCIPALS[index], (Decision.APPROVE, Decision.DENY)[index])
            except DomainError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(respond, (0, 1)))
        self.assertEqual(1, sum(isinstance(result, dict) for result in results))
        self.assertIn("REQUEST_NOT_PENDING", results)
        winner = next(result for result in results if isinstance(result, dict))
        self.assertEqual(winner, self.ledger.snapshot()["approvals"]["req"])

    def test_concurrent_duplicate_execution_is_one_transfer(self):
        self.approve()
        barrier = threading.Barrier(12)

        def execute(_):
            barrier.wait()
            return self.execute()

        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(execute, range(12)))
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(1, len(self.ledger.snapshot()["transactions"]))
        self.assertEqual(3_500_000, self.ledger.snapshot()["accounts"]["hotel-reserve"]["balance_cents"])

    def test_concurrent_distinct_transfers_conserve_funds(self):
        barrier = threading.Barrier(10)
        action = replace(self.action, amount_cents=100_000)

        def execute(index):
            barrier.wait()
            return self.execute(action, key=f"parallel-{index}", request_id=None)

        with ThreadPoolExecutor(max_workers=10) as pool:
            transactions = list(pool.map(execute, range(10)))
        state = self.ledger.snapshot()
        self.assertEqual(10, len({item["transaction_id"] for item in transactions}))
        self.assertEqual(6_000_000, sum(item["balance_cents"] for item in state["accounts"].values()))
        self.assertEqual(4_000_000, state["accounts"]["hotel-reserve"]["balance_cents"])

    def test_cas_retries_are_bounded_and_revalidate_fresh_state(self):
        self.approve()
        self.store.reject_next = 2
        self.assertEqual("tx-1", self.execute()["transaction_id"])
        self.store.reject_next = self.ledger.max_cas_attempts
        before = self.ledger.snapshot()
        calls = self.store.cas_calls
        self.assertCode(
            "CAS_RETRIES_EXHAUSTED", self.execute,
            replace(self.action, amount_cents=100_000), key="new-transfer", request_id=None,
        )
        self.assertEqual(self.ledger.max_cas_attempts, self.store.cas_calls - calls)
        self.assertEqual(before, self.ledger.snapshot())

    def test_idempotent_replay_is_read_only_even_when_future_cas_would_contend(self):
        self.approve()
        original = self.execute()
        before = self.store.read()
        calls = self.store.cas_calls
        self.store.reject_next = self.ledger.max_cas_attempts
        self.assertEqual(self.execute(), original)
        self.assertEqual(self.ledger.decide("req", PRINCIPALS[0], Decision.APPROVE)["status"], "approved")
        self.assertEqual(self.store.read(), before)
        self.assertEqual(self.store.cas_calls, calls)

    def test_cas_loss_to_a_decision_is_revalidated(self):
        self.request()
        original = self.store.compare_and_swap
        raced = False

        def racing_cas(version, state):
            nonlocal raced
            if not raced:
                raced = True
                self.ledger.decide("req", PRINCIPALS[1], Decision.CANCEL)
            return original(version, state)

        with patch.object(self.store, "compare_and_swap", side_effect=racing_cas):
            self.assertCode("REQUEST_NOT_PENDING", self.ledger.decide, "req", PRINCIPALS[0], Decision.APPROVE)
        self.assertEqual("cancelled", self.ledger.snapshot()["approvals"]["req"]["status"])

    def test_cas_initialization_bounded_and_store_errors_not_suppressed(self):
        store = MemoryStateStore()
        store.reject_next = 3
        self.assertCode("CAS_RETRIES_EXHAUSTED", Ledger, store, CONFIG, max_cas_attempts=3)
        with patch.object(self.store, "read", side_effect=DomainError("STORE_ERROR", "Unavailable")):
            self.assertCode("STORE_ERROR", self.execute)

    def test_invalid_config_and_corrupt_balance_are_rejected(self):
        for config in (
            replace(CONFIG, approver_object_ids=("same", "same")),
            replace(CONFIG, approval_threshold_cents=True),
            replace(CONFIG, delivery_max_attempts=0),
        ):
            self.assertCode("INVALID_CONFIGURATION", Ledger, MemoryStateStore(), config)
        self.change_state(lambda state: state["accounts"]["hotel-reserve"].update(balance_cents=-1))
        self.assertCode("INVALID_STATE", self.ledger.snapshot)

    def test_one_request_creates_four_deliveries_once(self):
        request = self.request()
        before = self.ledger.snapshot()
        self.assertEqual(request, self.request())
        self.assertEqual(before, self.ledger.snapshot())
        self.assertEqual(1, len(before["approvals"]))
        self.assertEqual(4, len(before["outbox"]))
        self.assertEqual(
            {(user, channel) for user in CONFIG.approver_object_ids for channel in ("teams", "email")},
            {(row["recipient"]["object_id"], row["channel"]) for row in before["outbox"].values()},
        )

    def test_delivery_success_is_idempotent_and_never_replays_finance(self):
        self.request()
        delivery_id = next(iter(self.ledger.snapshot()["outbox"]))
        claimed = self.ledger.claim_delivery(delivery_id, attempt_id="attempt-1")
        self.assertEqual(claimed, self.ledger.claim_delivery(delivery_id, attempt_id="attempt-1"))
        self.assertCode("DELIVERY_IN_FLIGHT", self.ledger.claim_delivery, delivery_id, attempt_id="other")
        done = self.ledger.record_delivery_result(delivery_id, attempt_id="attempt-1", succeeded=True)
        self.assertEqual(done, self.ledger.record_delivery_result(delivery_id, attempt_id="attempt-1", succeeded=True))
        self.assertCode("DELIVERY_ALREADY_SUCCEEDED", self.ledger.claim_delivery, delivery_id, attempt_id="other")
        self.assertCode(
            "DELIVERY_RESULT_CONFLICT", self.ledger.record_delivery_result,
            delivery_id, attempt_id="attempt-1", succeeded=False,
        )
        self.assertEqual([], self.ledger.snapshot()["transactions"])

    def test_delivery_failure_has_bounded_retry_and_attempt_binding(self):
        self.request()
        ids = list(self.ledger.snapshot()["outbox"])
        for attempt in range(1, 4):
            attempt_id = f"attempt-{attempt}"
            self.ledger.claim_delivery(ids[0], attempt_id=attempt_id)
            self.assertCode(
                "DELIVERY_ATTEMPT_CONFLICT", self.ledger.claim_delivery,
                ids[1], attempt_id=attempt_id,
            )
            failed = self.ledger.record_delivery_result(
                ids[0], attempt_id=attempt_id, succeeded=False, error="controlled failure"
            )
            self.assertEqual(attempt, failed["attempts"])
        self.assertCode("DELIVERY_RETRIES_EXHAUSTED", self.ledger.claim_delivery, ids[0], attempt_id="attempt-4")
        self.assertCode(
            "DELIVERY_ATTEMPT_CONFLICT", self.ledger.record_delivery_result,
            ids[0], attempt_id="attempt-1", succeeded=True,
        )
        self.assertEqual([], self.ledger.snapshot()["transactions"])

    def test_terminal_request_cancels_unsent_outbox_and_allows_inflight_result(self):
        self.request()
        ids = list(self.ledger.snapshot()["outbox"])
        self.ledger.claim_delivery(ids[0], attempt_id="in-flight")
        self.ledger.decide("req", PRINCIPALS[1], Decision.CANCEL)
        self.assertCode("REQUEST_NOT_PENDING", self.ledger.claim_delivery, ids[1], attempt_id="late")
        self.ledger.record_delivery_result(ids[0], attempt_id="in-flight", succeeded=True)
        outbox = self.ledger.snapshot()["outbox"]
        self.assertEqual("succeeded", outbox[ids[0]]["status"])
        self.assertTrue(all(row["request_status"] == "cancelled" for row in outbox.values()))
        self.assertTrue(all(outbox[key]["status"] == "cancelled" for key in ids[1:]))


class SQLiteTests(unittest.TestCase):
    def setUp(self):
        # Explicit project-local scratch; never use the system temporary directory.
        self.directory = Path(".ledger-test-state") / uuid.uuid4().hex
        self.directory.mkdir(parents=True)
        self.path = self.directory / "state.sqlite"

    def tearDown(self):
        shutil.rmtree(self.directory)
        try:
            self.directory.parent.rmdir()
        except OSError:
            pass

    def assertCode(self, code, function, *args, **kwargs):
        with self.assertRaises(DomainError) as caught:
            function(*args, **kwargs)
        self.assertEqual(code, caught.exception.code)

    def test_store_cas_contract_and_read_isolation(self):
        first = SQLiteStateStore(self.path)
        second = SQLiteStateStore(self.path)
        self.assertEqual(Snapshot(None, None), first.read())
        self.assertTrue(first.compare_and_swap(None, {"value": 1}))
        self.assertFalse(second.compare_and_swap(None, {"value": 2}))
        read = first.read()
        read.state["value"] = 99
        self.assertEqual({"value": 1}, second.read().state)
        self.assertTrue(second.compare_and_swap(read.version, {"value": 2}))
        self.assertFalse(first.compare_and_swap(read.version, {"value": 3}))
        self.assertNotEqual(read.version, first.read().version)

    def test_persistence_includes_approval_outbox_and_execution(self):
        ledger = Ledger(SQLiteStateStore(self.path), CONFIG)
        action = ledger.payroll_funding_action()
        ledger.request_approval(action, request_id="req", idempotency_key="fund")
        restarted = Ledger(SQLiteStateStore(self.path), CONFIG)
        self.assertEqual(4, len(restarted.snapshot()["outbox"]))
        restarted.decide("req", PRINCIPALS[1], Decision.APPROVE)
        transaction = restarted.execute_transfer(
            action, idempotency_key="fund", role=BANK, approval_request_id="req"
        )
        restarted_again = Ledger(SQLiteStateStore(self.path), CONFIG)
        self.assertEqual(transaction, restarted_again.execute_transfer(
            action, idempotency_key="fund", role=BANK, approval_request_id="req"
        ))
        self.assertEqual(1, len(restarted_again.snapshot()["transactions"]))
        self.assertEqual(restarted.snapshot(), restarted_again.snapshot())

    def test_multiple_sqlite_workers_seed_and_execute_atomically(self):
        Ledger(SQLiteStateStore(self.path), CONFIG)
        barrier = threading.Barrier(6)

        def transfer(_):
            ledger = Ledger(SQLiteStateStore(self.path), CONFIG)
            action = TransferAction("hotel-reserve", "hotel-payroll", 1_000_000, "USD")
            barrier.wait()
            return ledger.execute_transfer(action, idempotency_key="shared", role=BANK)

        with ThreadPoolExecutor(max_workers=6) as pool:
            transactions = list(pool.map(transfer, range(6)))
        self.assertTrue(all(item == transactions[0] for item in transactions))
        state = Ledger(SQLiteStateStore(self.path), CONFIG).snapshot()
        self.assertEqual(1, len(state["transactions"]))
        self.assertEqual(4_000_000, state["accounts"]["hotel-reserve"]["balance_cents"])
        self.assertEqual(2_000_000, state["accounts"]["hotel-payroll"]["balance_cents"])

    def test_memory_path_and_sqlite_operational_failure_are_explicit(self):
        self.assertCode("INVALID_STORE", SQLiteStateStore, ":memory:")
        self.assertCode("STORE_ERROR", SQLiteStateStore, self.directory / "missing" / "state.sqlite")

    def test_connections_close_on_success_and_error_and_cas_rolls_back(self):
        connections = []
        original_connect = sqlite3.connect
        failing_sql = None

        class TrackedConnection(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                cursor = super().execute(sql, *args, **kwargs)
                if failing_sql is not None and sql.startswith(failing_sql):
                    cursor.close()
                    raise sqlite3.OperationalError("Injected failure after SQL execution")
                return cursor

        def connect(*args, **kwargs):
            connection = original_connect(*args, factory=TrackedConnection, **kwargs)
            connections.append(connection)
            return connection

        with patch("finance_autopilots.ledger.sqlite3.connect", side_effect=connect):
            store = SQLiteStateStore(self.path)
            self.assertEqual(Snapshot(None, None), store.read())
            self.assertTrue(store.compare_and_swap(None, {"value": 1}))
            baseline = store.read()
            self.assertFalse(store.compare_and_swap(None, {"value": 2}))
            self.assertTrue(store.compare_and_swap(baseline.version, {"value": 2}))
            baseline = store.read()
            for prefix, operation in (
                ("CREATE", lambda: SQLiteStateStore(self.path)),
                ("SELECT", store.read),
                ("UPDATE", lambda: store.compare_and_swap(baseline.version, {"value": 3})),
            ):
                with self.subTest(sql=prefix):
                    failing_sql = prefix
                    self.assertCode("STORE_ERROR", operation)
            failing_sql = None
            self.assertEqual(baseline, store.read())

        for connection in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
