import tempfile
import unittest
from pathlib import Path

from payroll_agents.demo_runs import DemoRuns
from payroll_agents.ledger import (
    AgentRole, Decision, DomainError, Ledger, LedgerConfig, Principal, SQLiteStateStore,
)


class DemoRunTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = SQLiteStateStore(Path(self.directory.name) / "runs.db")
        self.runs = DemoRuns(self.store)
        self.config = LedgerConfig("tenant", ("marco", "ayca"), "hotel")
        self.first = "ACTIVITY-" + "A" * 32
        self.second = "ACTIVITY-" + "B" * 32

    def test_runs_are_independent_persisted_and_never_reset_by_duplicate_start(self):
        first = Ledger(self.runs.start(self.first), self.config)
        reference = "PAYROLL-0123456789ABCDEF"
        action = first.payroll_funding_action()
        first.request_approval(action, request_id=reference, idempotency_key=reference)
        with self.assertRaises(DomainError):
            first.execute_transfer(
                action, role=AgentRole.BANKING_OPERATIONS,
                idempotency_key=reference, approval_request_id=reference,
            )
        first.decide(reference, Principal("tenant", "ayca"), Decision.APPROVE)
        transaction = first.execute_transfer(
            action, role=AgentRole.BANKING_OPERATIONS,
            idempotency_key=reference, approval_request_id=reference,
        )
        before = first.snapshot()
        second = Ledger(self.runs.start(self.second), self.config)
        self.assertEqual(second.snapshot()["transactions"], [])
        self.assertEqual(second.payroll_funding_action().amount_cents, 1_500_000)
        duplicate = Ledger(self.runs.start(self.first), self.config)
        self.assertEqual(duplicate.snapshot(), before)
        self.assertEqual(self.runs.current().run_id, self.second)
        self.assertEqual(
            duplicate.execute_transfer(
                action, role=AgentRole.BANKING_OPERATIONS,
                idempotency_key=reference, approval_request_id=reference,
            ), transaction,
        )
        self.assertEqual(len(duplicate.snapshot()["transactions"]), 1)
        restored = DemoRuns(self.store)
        self.assertEqual(Ledger(restored.resolve(reference), self.config).snapshot(), before)
        self.assertEqual(len(restored.ledgers()), 2)

    def test_stale_cross_run_versions_cannot_overwrite_financial_state(self):
        first_store = self.runs.start(self.first)
        first = Ledger(first_store, self.config)
        stale = first_store.read()
        Ledger(self.runs.start(self.second), self.config)
        self.assertFalse(first_store.compare_and_swap(stale.version, stale.state))
        self.assertEqual(first.snapshot(), stale.state)
        invalid = first.snapshot()
        invalid["simulation"] = False
        with self.assertRaises(PermissionError):
            first_store.compare_and_swap(first_store.read().version, invalid)

    def test_unbound_references_cannot_start_or_select_a_run(self):
        for value in ("arbitrary", "ACTIVITY-short", "PAYROLL-0123456789ABCDEF"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.runs.start(value)
        with self.assertRaises(DomainError):
            self.runs.resolve(self.first)
        Ledger(self.runs.start(self.first), self.config)
        with self.assertRaises(DomainError):
            self.runs.resolve(self.second)
