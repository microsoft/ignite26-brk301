"""Private peer correlation, separate from visible messages and financial state."""

from __future__ import annotations

import hashlib
import json
import re
from uuid import UUID

REFERENCE = re.compile(r"(?:payroll-funding|PAYROLL-[A-F0-9]{16}|ACTIVITY-[A-F0-9]{32})")
REFERENCE_HEADER = "X-Finance-Peer-Reference"
HOP_HEADER = "X-Finance-Peer-Hop"


def validate_route(reference, hop):
    if (
        not isinstance(reference, str) or not REFERENCE.fullmatch(reference)
        or isinstance(hop, bool) or not isinstance(hop, int) or not 1 <= hop <= 3
    ):
        raise ValueError("Peer routing requires a bounded reference and conversation step")


def email_peer_route(headers):
    values = {}
    for header in headers:
        name = header["name"].casefold()
        if name not in (REFERENCE_HEADER.casefold(), HOP_HEADER.casefold()):
            continue
        if name in values:
            raise PermissionError("Duplicate peer routing headers are not supported")
        values[name] = header["value"]
    if not values:
        return None
    reference = values.get(REFERENCE_HEADER.casefold())
    hop = values.get(HOP_HEADER.casefold())
    if hop not in ("1", "2", "3"):
        raise PermissionError("Peer email has incomplete routing metadata")
    validate_route(reference, int(hop))
    return reference, int(hop)


class PeerRoutes:
    def __init__(self, store, tenant):
        self.store = store
        self.tenant = tenant

    def _read(self):
        snapshot = self.store.read()
        if snapshot.state is not None and (
            snapshot.state.get("schema_version") != 1
            or snapshot.state.get("tenant_id") != self.tenant
            or not isinstance(snapshot.state.get("messages"), dict)
            or not isinstance(snapshot.state.get("demo_runs", {}), dict)
        ):
            raise RuntimeError("Stored peer routing registry is invalid")
        return snapshot

    def bind_demo_run(self, reference, run_id):
        validate_route(reference, 1)
        if not isinstance(run_id, str) or not re.fullmatch(r"ACTIVITY-[A-F0-9]{32}", run_id):
            raise ValueError("Peer scope requires an exact saved demo run")
        for _ in range(32):
            snapshot = self._read()
            state = snapshot.state or {
                "schema_version": 1, "tenant_id": self.tenant, "messages": {},
            }
            scopes = state.setdefault("demo_runs", {})
            if reference in scopes:
                if scopes[reference] != run_id:
                    raise PermissionError("A peer exchange cannot be rebound to another demo run")
                return
            scopes[reference] = run_id
            if self.store.compare_and_swap(snapshot.version, state):
                return
        raise RuntimeError("Could not persist the peer exchange demo scope")

    def demo_run(self, reference):
        validate_route(reference, 1)
        snapshot = self._read()
        run_id = (
            snapshot.state.get("demo_runs", {}).get(reference)
            if snapshot.state is not None else None
        )
        if run_id is not None and (
            not isinstance(run_id, str) or not re.fullmatch(r"ACTIVITY-[A-F0-9]{32}", run_id)
        ):
            raise RuntimeError("Stored peer demo scope is invalid")
        return run_id

    def _key(self, sender, recipient, chat_id, message_id):
        for identifier in (sender, recipient):
            UUID(identifier)
        if sender == recipient:
            raise PermissionError("Peer routing requires distinct bound identities")
        for value in (chat_id, message_id):
            if (
                not isinstance(value, str) or not value or len(value) > 1024
                or any(ord(character) < 32 for character in value)
            ):
                raise ValueError("Peer routing requires native chat and message identities")
        return hashlib.sha256(json.dumps(
            [self.tenant, sender, recipient, chat_id, message_id],
            separators=(",", ":"),
        ).encode()).hexdigest()

    @staticmethod
    def _content_hash(text):
        return hashlib.sha256(" ".join(text.split()).encode()).hexdigest()

    def record(self, sender, recipient, chat_id, message_id, text, reference, hop):
        validate_route(reference, hop)
        key = self._key(sender, recipient, chat_id, message_id)
        entry = {
            "reference": reference, "hop": hop, "content_hash": self._content_hash(text),
        }
        for _ in range(32):
            snapshot = self._read()
            state = snapshot.state or {
                "schema_version": 1, "tenant_id": self.tenant, "messages": {},
            }
            existing = state["messages"].get(key)
            if existing is not None:
                if existing != entry:
                    raise PermissionError("A native peer message cannot be rebound to another task")
                return
            state["messages"][key] = entry
            if self.store.compare_and_swap(snapshot.version, state):
                return
        raise RuntimeError("Could not persist accepted peer message routing")

    def resolve(self, sender, recipient, chat_id, message_id, text):
        key = self._key(sender, recipient, chat_id, message_id)
        snapshot = self._read()
        if snapshot.state is None:
            return None
        entry = snapshot.state["messages"].get(key)
        if entry is None:
            return None
        validate_route(entry["reference"], entry["hop"])
        if entry["content_hash"] != self._content_hash(text):
            raise PermissionError("Peer message content differs from its accepted delivery")
        return entry["reference"], entry["hop"]
