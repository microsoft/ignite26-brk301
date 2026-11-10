"""Repeatable demo ledgers, isolated per authenticated request and retained by CAS."""

from __future__ import annotations

import re

from .ledger import DomainError, Snapshot

RUN_ID = re.compile(r"ACTIVITY-[A-F0-9]{32}")
REQUEST_ID = re.compile(r"PAYROLL-[A-F0-9]{16}")


class DemoRuns:
    def __init__(self, store):
        self.store = store

    def _read(self):
        snapshot = self.store.read()
        state = snapshot.state
        if state is not None and (
            state.get("schema_version") != 1
            or state.get("kind") != "finance_demo_runs"
            or not isinstance(state.get("runs"), dict)
            or not isinstance(state.get("active_run"), str)
            or state.get("active_run") not in state["runs"]
            or any(not RUN_ID.fullmatch(key) for key in state["runs"])
            or any(value is not None and (
                not isinstance(value, dict) or value.get("simulation") is not True
            ) for value in state["runs"].values())
        ):
            raise RuntimeError("Stored demo run registry is invalid")
        return snapshot

    def start(self, run_id):
        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
            raise ValueError("A demo run requires the authenticated Hotel request reference")
        for _ in range(32):
            snapshot = self._read()
            state = snapshot.state
            if state is None:
                state = {"schema_version": 1, "kind": "finance_demo_runs",
                         "active_run": run_id, "runs": {run_id: None}}
            elif run_id in state["runs"]:
                return RunLedgerStore(self, run_id)
            else:
                state["runs"][run_id] = None
                state["active_run"] = run_id
            if self.store.compare_and_swap(snapshot.version, state):
                return RunLedgerStore(self, run_id)
        raise RuntimeError("Could not activate the new demo run")

    def current(self):
        snapshot = self._read()
        if snapshot.state is None:
            return None
        return RunLedgerStore(self, snapshot.state["active_run"])

    def resolve(self, reference):
        snapshot = self._read()
        if snapshot.state is None:
            raise DomainError("DEMO_RUN_NOT_FOUND", "No payroll demonstration has been started.")
        runs = snapshot.state["runs"]
        if isinstance(reference, str) and RUN_ID.fullmatch(reference) and reference in runs:
            return RunLedgerStore(self, reference)
        if isinstance(reference, str) and REQUEST_ID.fullmatch(reference):
            matches = [
                key for key, state in runs.items()
                if state is not None and reference in state["approvals"]
            ]
            if len(matches) == 1:
                return RunLedgerStore(self, matches[0])
        raise DomainError("DEMO_RUN_NOT_FOUND", "The request does not match a saved payroll demonstration.")

    def ledgers(self):
        snapshot = self._read()
        return [
            RunLedgerStore(self, key) for key, state in
            (snapshot.state["runs"].items() if snapshot.state is not None else ())
            if state is not None
        ]


class RunLedgerStore:
    def __init__(self, registry, run_id):
        self.registry = registry
        self.run_id = run_id

    def read(self):
        snapshot = self.registry._read()
        if snapshot.state is None or self.run_id not in snapshot.state["runs"]:
            raise RuntimeError("The saved demo run disappeared")
        state = snapshot.state["runs"][self.run_id]
        return Snapshot(snapshot.version if state is not None else None, state)

    def compare_and_swap(self, expected_version, state):
        if not isinstance(state, dict):
            raise TypeError("A demo ledger snapshot must be a dictionary")
        snapshot = self.registry._read()
        if snapshot.state is None or self.run_id not in snapshot.state["runs"]:
            raise RuntimeError("The saved demo run disappeared")
        existing = snapshot.state["runs"][self.run_id]
        if expected_version is None:
            if existing is not None:
                return False
        elif snapshot.version != expected_version or existing is None:
            return False
        if state.get("simulation") is not True:
            raise PermissionError("Replay storage accepts only an explicit demonstration ledger")
        snapshot.state["runs"][self.run_id] = state
        return self.registry.store.compare_and_swap(snapshot.version, snapshot.state)
