import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from finance_autopilots.ledger import (
    AgentRole, Ledger, LedgerConfig, Snapshot, SQLiteStateStore,
)
from finance_autopilots.projection import ProjectionRefreshError
from finance_autopilots.projection_worker import drain_projection_queue, refresh_runtime_projection
from finance_autopilots.runtime import FinanceRuntime, authenticated_decision


class ProjectionWorkerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = Path(self.directory.name)
        self.journal = SQLiteStateStore(path / "journal.db")
        self.state = Ledger(
            SQLiteStateStore(path / "ledger.db"),
            LedgerConfig("tenant", ("marco", "ayca"), "hotel"),
        ).snapshot()
        self.store = Mock()
        self.store.read.return_value = Snapshot("actual-ledger-etag", copy.deepcopy(self.state))
        self.runtime = SimpleNamespace(
            role=AgentRole.BANKING_OPERATIONS, tenant="tenant", root_client_id="root-client",
            credential=Mock(), _claim_turn=FinanceRuntime._claim_turn,
            _complete_turn=FinanceRuntime._complete_turn,
            _record_turn_failure=FinanceRuntime._record_turn_failure,
        )
        self.config = SimpleNamespace(
            tenant_id="tenant", bank_principal_id="different-root-principal",
            expected_source_policy={
                "tenant_id": "tenant", "owner_id": "hotel", "approver_ids": ["marco", "ayca"],
            },
        )
        self.bindings = {
            "hotel_owner_id": "hotel", "approvers": [{"object_id": "marco"}, {"object_id": "ayca"}],
            "fabric": {"projection": {
                "projection_refresh": {"bank_client_id": "root-client"},
                "projection_freshness": {"source_etag": "initial-baseline-not-live-ledger"},
            }},
        }
        self.result = {
            "source_etag": "actual-ledger-etag", "job_status": "Completed",
            "committed_manifest": {"projection_state": "complete"},
        }
        self.config_patch = patch(
            "finance_autopilots.projection_worker.ProjectionConfig.from_output",
            return_value=self.config,
        )
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)

    def refresh(self):
        return refresh_runtime_projection(self.runtime, self.bindings, self.store, self.journal)

    def test_actual_capture_checkpoint_and_completed_refresh_are_not_replayed(self):
        def execute(envelope, **kwargs):
            self.assertEqual(envelope["state"], self.state)
            self.assertEqual(envelope["source_etag"], "actual-ledger-etag")
            self.assertTrue(envelope["captured_at"].endswith("+00:00"))
            self.assertEqual(kwargs["credential_kind"], "agent_identity")
            self.assertEqual(kwargs["expected_projection_etag"], "initial-baseline-not-live-ledger")
            kwargs["checkpoint"]({"job_url": "https://api.fabric.microsoft.com/job"})
            entry = next(iter(self.journal.read().state["turns"].values()))
            self.assertEqual(entry["projection_progress"]["capture"], envelope)
            self.assertIn("expected_projection_etag", entry["projection_progress"])
            return self.result

        with patch("finance_autopilots.projection_worker.read_projection_manifest",
                   return_value=None), patch(
            "finance_autopilots.projection_worker.refresh_projection", side_effect=execute,
        ) as run:
            self.assertTrue(self.refresh()["source_etag_current"])
            self.assertTrue(self.refresh()["source_etag_current"])
            run.assert_called_once()
        self.assertEqual(self.store.read.return_value.state, self.state)

    def test_accepted_job_failure_preserves_capture_and_refuses_resubmission(self):
        error = ProjectionRefreshError(
            "Job status unknown", {"job_url": "https://api.fabric.microsoft.com/job"},
        )
        with patch("finance_autopilots.projection_worker.read_projection_manifest",
                   return_value={"source_etag": "previous-real-etag"}), patch(
            "finance_autopilots.projection_worker.refresh_projection", side_effect=error,
        ) as run:
            with self.assertRaises(ProjectionRefreshError):
                self.refresh()
            with self.assertRaises(RuntimeError):
                self.refresh()
            run.assert_called_once()
        entry = next(iter(self.journal.read().state["turns"].values()))
        self.assertEqual(entry["status"], "running")
        self.assertEqual(entry["failure_type"], "ProjectionRefreshError")
        self.assertEqual(entry["projection_progress"]["job_url"], error.progress["job_url"])
        self.assertEqual(
            entry["projection_progress"]["expected_projection_etag"], "previous-real-etag",
        )

    def test_foreign_authority_or_hotel_writer_cannot_claim_a_refresh(self):
        for target, field, value in (
            (self.runtime, "role", AgentRole.HOTEL_FINANCE),
            (self.runtime, "root_client_id", "other-client"),
            (self.config, "tenant_id", "other-tenant"),
        ):
            previous = getattr(target, field)
            setattr(target, field, value)
            with self.subTest(field=field), self.assertRaises(PermissionError):
                self.refresh()
            setattr(target, field, previous)
        self.assertIsNone(self.journal.read().state)

    def test_committed_capture_does_not_claim_freshness_after_ledger_changes(self):
        self.store.read.side_effect = [
            Snapshot("actual-ledger-etag", self.state),
            Snapshot("new-ledger-etag", self.state),
        ]
        with patch("finance_autopilots.projection_worker.read_projection_manifest",
                   return_value=None), patch(
            "finance_autopilots.projection_worker.refresh_projection", return_value=self.result,
        ):
            self.assertFalse(self.refresh()["source_etag_current"])
        entry = next(iter(self.journal.read().state["turns"].values()))
        self.assertEqual(entry["status"], "complete")
        self.assertEqual(json.loads(entry["answer"]), self.result)

    def test_initial_onelake_xml_404_is_not_misreported_as_a_json_failure(self):
        http = httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(404, text="<Error>PathNotFound</Error>")
        ))
        self.addCleanup(http.close)

        def read_manifest(config, **kwargs):
            response = kwargs["send"](
                "GET", "https://onelake.dfs.fabric.microsoft.com/manifest",
                headers={}, body=None, timeout_seconds=30,
            )
            self.assertEqual(response.status, 404)
            self.assertIsNone(response.body)
            return None

        with patch("finance_autopilots.projection_worker.httpx.Client", return_value=http), patch(
            "finance_autopilots.projection_worker.read_projection_manifest",
            side_effect=read_manifest,
        ), patch(
            "finance_autopilots.projection_worker.refresh_projection", return_value=self.result,
        ):
            self.assertTrue(self.refresh()["source_etag_current"])

    def commit_transfer(self):
        ledger = Ledger(
            SQLiteStateStore(Path(self.directory.name) / "financial.db"),
            LedgerConfig("tenant", ("marco", "ayca"), "hotel"),
        )
        reference = "PAYROLL-0123456789ABCDEF"
        ledger.request_approval(
            ledger.payroll_funding_action(), request_id=reference, idempotency_key=reference,
        )
        authenticated_decision(ledger, "tenant", "marco", "approve " + reference)
        self.state = ledger.snapshot()
        self.store.read.return_value = Snapshot("actual-ledger-etag", copy.deepcopy(self.state))
        return ledger

    def complete_refresh(self, envelope, **kwargs):
        refresh_id = kwargs["refresh_id"]
        kwargs["checkpoint"]({"capture": envelope})
        return {
            **self.result, "refresh_id": refresh_id, "metadata_sync_pending": False,
            "committed_manifest": {"projection_state": "complete", "refresh_id": refresh_id},
        }

    def test_committed_transaction_is_a_durable_queue_without_a_second_ledger_write(self):
        ledger = self.commit_transfer()
        before = ledger.store.read()
        with patch("finance_autopilots.projection_worker.read_projection_manifest",
                   return_value=None), patch(
            "finance_autopilots.projection_worker.refresh_projection",
            side_effect=self.complete_refresh,
        ) as run:
            result = drain_projection_queue(self.runtime, self.bindings, self.store, self.journal)
            self.assertEqual(result["status"], "complete")
            self.assertEqual(
                drain_projection_queue(self.runtime, self.bindings, self.store, self.journal),
                {"status": "idle", "pending_transaction_ids": []},
            )
            run.assert_called_once()
        self.assertEqual(ledger.store.read(), before)
        self.store.compare_and_swap.assert_not_called()
        self.assertEqual(len(ledger.snapshot()["transactions"]), 1)
        self.assertEqual(
            self.journal.read().state["turns"]["projection-transfer/tx-1"]["status"], "complete",
        )

    def test_no_transfer_means_no_notebook_or_acknowledgement(self):
        with patch("finance_autopilots.projection_worker.refresh_projection") as run:
            result = drain_projection_queue(self.runtime, self.bindings, self.store, self.journal)
            self.assertEqual(result["status"], "idle")
            run.assert_not_called()
        self.assertIsNone(self.journal.read().state)

    def test_refresh_failure_cannot_replay_money_or_submit_for_a_changed_etag(self):
        ledger = self.commit_transfer()
        before = ledger.snapshot()
        error = ProjectionRefreshError("Unknown job", {"job_url": "https://api.fabric.microsoft.com/job"})
        with patch("finance_autopilots.projection_worker.read_projection_manifest",
                   return_value=None), patch(
            "finance_autopilots.projection_worker.refresh_projection", side_effect=error,
        ) as run:
            with self.assertRaises(ProjectionRefreshError):
                drain_projection_queue(self.runtime, self.bindings, self.store, self.journal)
            self.store.read.return_value = Snapshot("new-ledger-etag", copy.deepcopy(self.state))
            with self.assertRaises(RuntimeError):
                drain_projection_queue(self.runtime, self.bindings, self.store, self.journal)
            run.assert_called_once()
        self.assertEqual(ledger.snapshot(), before)
        self.assertNotIn("projection-transfer/tx-1", self.journal.read().state["turns"])

    def test_stale_receipt_leaves_transaction_pending(self):
        self.commit_transfer()
        self.store.read.side_effect = [
            Snapshot("actual-ledger-etag", self.state),
            Snapshot("actual-ledger-etag", self.state),
            Snapshot("changed-etag", self.state),
        ]
        with patch("finance_autopilots.projection_worker.read_projection_manifest",
                   return_value=None), patch(
            "finance_autopilots.projection_worker.refresh_projection",
            side_effect=self.complete_refresh,
        ):
            result = drain_projection_queue(self.runtime, self.bindings, self.store, self.journal)
        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["pending_transaction_ids"], ["tx-1"])
        self.assertNotIn("projection-transfer/tx-1", self.journal.read().state["turns"])

    def test_another_running_capture_excludes_a_new_refresh_atomically(self):
        FinanceRuntime._claim_turn(self.journal, "projection-refresh/other", "other")
        with self.assertRaises(RuntimeError):
            self.refresh()
        self.assertEqual(list(self.journal.read().state["turns"]), ["projection-refresh/other"])

    def test_queue_rejects_foreign_ledger_and_hotel_root_even_without_transactions(self):
        self.runtime.role = AgentRole.HOTEL_FINANCE
        with self.assertRaises(PermissionError):
            drain_projection_queue(self.runtime, self.bindings, self.store, self.journal)
        self.runtime.role = AgentRole.BANKING_OPERATIONS
        self.store.read.return_value.state["policy"]["tenant_id"] = "other-tenant"
        with self.assertRaises(ValueError):
            drain_projection_queue(self.runtime, self.bindings, self.store, self.journal)
        self.assertIsNone(self.journal.read().state)

    def test_completion_without_exact_transaction_capture_cannot_acknowledge_queue(self):
        self.commit_transfer()

        def omit_transaction(envelope, **kwargs):
            result = self.complete_refresh(envelope, **kwargs)
            snapshot = self.journal.read()
            entry = snapshot.state["turns"]["projection-refresh/" + kwargs["refresh_id"]]
            entry["projection_progress"]["capture"]["state"]["transactions"] = []
            self.assertTrue(self.journal.compare_and_swap(snapshot.version, snapshot.state))
            return result

        with patch("finance_autopilots.projection_worker.read_projection_manifest",
                   return_value=None), patch(
            "finance_autopilots.projection_worker.refresh_projection", side_effect=omit_transaction,
        ):
            with self.assertRaisesRegex(RuntimeError, "exact queued transaction"):
                drain_projection_queue(self.runtime, self.bindings, self.store, self.journal)
        self.assertNotIn("projection-transfer/tx-1", self.journal.read().state["turns"])

    def test_completed_job_with_pending_metadata_does_not_acknowledge_queue(self):
        self.commit_transfer()

        def incomplete_metadata(envelope, **kwargs):
            return {**self.complete_refresh(envelope, **kwargs), "metadata_sync_pending": True}

        with patch("finance_autopilots.projection_worker.read_projection_manifest",
                   return_value=None), patch(
            "finance_autopilots.projection_worker.refresh_projection", side_effect=incomplete_metadata,
        ):
            with self.assertRaisesRegex(RuntimeError, "verified complete capture"):
                drain_projection_queue(self.runtime, self.bindings, self.store, self.journal)
        self.assertNotIn("projection-transfer/tx-1", self.journal.read().state["turns"])


if __name__ == "__main__":
    unittest.main()
