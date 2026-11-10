import tempfile
import unittest
from pathlib import Path

from finance_autopilots.ledger import SQLiteStateStore
from finance_autopilots.peer_routes import (
    HOP_HEADER, REFERENCE_HEADER, PeerRoutes, email_peer_route,
)

SENDER = "00000000-0000-0000-0000-000000000001"
RECIPIENT = "00000000-0000-0000-0000-000000000002"
REFERENCE = "ACTIVITY-" + "A" * 32


class PeerRouteTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = SQLiteStateStore(Path(self.directory.name) / "routes.db")
        self.routes = PeerRoutes(self.store, "tenant")
        self.address = SENDER, RECIPIENT, "native-chat", "native-message"
        self.text = "Hotel Treasurer, please confirm payroll funding."

    def test_private_route_binds_exact_native_message_and_preserves_duplicate_receipts(self):
        self.assertIsNone(self.routes.resolve(*self.address, self.text))
        self.routes.record(*self.address, self.text, REFERENCE, 1)
        before = self.store.read()
        self.routes.record(*self.address, self.text, REFERENCE, 1)
        self.assertEqual(self.store.read(), before)
        self.assertEqual(self.routes.resolve(*self.address, self.text), (REFERENCE, 1))
        self.assertEqual(
            self.routes.resolve(*self.address, self.text.replace(" ", "\n")), (REFERENCE, 1),
        )
        self.assertNotIn(self.text, str(before.state))
        self.assertEqual(
            PeerRoutes(self.store, "tenant").resolve(*self.address, self.text), (REFERENCE, 1),
        )

    def test_identical_messages_in_concurrent_runs_remain_separately_addressed(self):
        other = "ACTIVITY-" + "B" * 32
        self.routes.record(*self.address, self.text, REFERENCE, 1)
        second = SENDER, RECIPIENT, "native-chat", "another-message"
        self.routes.record(*second, self.text, other, 2)
        self.assertEqual(self.routes.resolve(*self.address, self.text), (REFERENCE, 1))
        self.assertEqual(self.routes.resolve(*second, self.text), (other, 2))
        for address in (
            (RECIPIENT, SENDER, *self.address[2:]),
            (SENDER, RECIPIENT, "different-chat", "native-message"),
        ):
            self.assertIsNone(self.routes.resolve(*address, self.text))
        with self.assertRaises(PermissionError):
            self.routes.resolve(*self.address, "changed content")
        with self.assertRaises(PermissionError):
            self.routes.record(*self.address, self.text, other, 1)
        with self.assertRaises(RuntimeError):
            PeerRoutes(self.store, "different-tenant").resolve(*self.address, self.text)

    def test_invalid_routes_cannot_be_registered(self):
        for reference, hop in (("arbitrary", 1), (REFERENCE, 0), (REFERENCE, 4), (REFERENCE, True)):
            with self.subTest(reference=reference, hop=hop), self.assertRaises(ValueError):
                self.routes.record(*self.address, self.text, reference, hop)
        self.assertIsNone(self.store.read().state)

    def test_read_only_exchanges_bind_exact_saved_demo_without_changing_message_routes(self):
        run_id = "ACTIVITY-" + "C" * 32
        other = "ACTIVITY-" + "B" * 32
        self.assertIsNone(self.routes.demo_run(REFERENCE))
        self.routes.bind_demo_run(REFERENCE, run_id)
        before = self.store.read()
        self.routes.bind_demo_run(REFERENCE, run_id)
        self.assertEqual(self.store.read(), before)
        self.routes.record(*self.address, self.text, REFERENCE, 1)
        self.assertEqual(self.routes.resolve(*self.address, self.text), (REFERENCE, 1))
        self.assertEqual(PeerRoutes(self.store, "tenant").demo_run(REFERENCE), run_id)
        self.assertIsNone(self.routes.demo_run(other))
        with self.assertRaises(PermissionError):
            self.routes.bind_demo_run(REFERENCE, other)
        with self.assertRaises(ValueError):
            self.routes.bind_demo_run(other, "latest")
        with self.assertRaises(RuntimeError):
            PeerRoutes(self.store, "other-tenant").demo_run(REFERENCE)

    def test_private_email_headers_are_bounded_complete_and_unambiguous(self):
        headers = [
            {"name": REFERENCE_HEADER.lower(), "value": REFERENCE},
            {"name": HOP_HEADER.upper(), "value": "2"},
            {"name": "Subject", "value": "Payroll update"},
        ]
        self.assertEqual(email_peer_route(headers), (REFERENCE, 2))
        self.assertIsNone(email_peer_route([]))
        for invalid in (
            headers[:1], headers[1:2], [*headers, headers[0]],
            [{"name": REFERENCE_HEADER, "value": REFERENCE},
             {"name": HOP_HEADER, "value": "4"}],
        ):
            with self.subTest(headers=invalid), self.assertRaises((PermissionError, ValueError)):
                email_peer_route(invalid)
