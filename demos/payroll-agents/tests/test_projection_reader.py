import base64
import copy
import hashlib
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs

import httpx

from payroll_agents.ledger import AgentRole, Ledger, LedgerConfig, Snapshot, SQLiteStateStore
from payroll_agents.projection import REFRESH_SCHEMAS, project_snapshot
from payroll_agents.projection_reader import (
    DeltaReader, decode_parquet, read_current_projection, relative_path, revalidate_projection,
)

ROOT = Path(__file__).resolve().parents[1]
ID = "00000000-0000-4000-8000-000000000001"
COMMON = {"captured_at", "ledger_version", "source_etag", "ledger_status", "authoritative"}


def parquet(columns):
    return subprocess.run(
        ["node", str(ROOT / "parquet-reader" / "fixture.mjs")],
        input=json.dumps({"columns": columns}).encode(),
        capture_output=True, check=True, timeout=30,
    ).stdout


class ParquetTests(unittest.TestCase):
    def test_exact_int64_boolean_hash_and_microsecond_roundtrip(self):
        rows = decode_parquet(parquet([
            {"name": "amount", "type": "INT64", "data": ["9007199254740993", "-9007199254740993"]},
            {"name": "authoritative", "type": "BOOLEAN", "data": [False, True]},
            {"name": "snapshot_sha256", "type": "STRING", "data": ["a" * 64, None]},
            {"name": "captured_at", "type": "INT64", "timestamp_unit": "MICROS",
             "data": ["1234567", "-1"]},
        ]))
        self.assertEqual(rows, [
            {"amount": 9007199254740993, "authoritative": False, "snapshot_sha256": "a" * 64,
             "captured_at": "1970-01-01T00:00:01.234567+00:00"},
            {"amount": -9007199254740993, "authoritative": True, "snapshot_sha256": None,
             "captured_at": "1969-12-31T23:59:59.999999+00:00"},
        ])

    def test_timestamp_units_do_not_silently_truncate(self):
        for unit, value, expected in (
            ("MILLIS", "1234", "1970-01-01T00:00:01.234000+00:00"),
            ("NANOS", "1234567000", "1970-01-01T00:00:01.234567+00:00"),
        ):
            self.assertEqual(decode_parquet(parquet([{
                "name": "time", "type": "INT64", "timestamp_unit": unit, "data": [value],
            }]))[0]["time"], expected)
        with self.assertRaisesRegex(RuntimeError, "decoding failed"):
            decode_parquet(parquet([{
                "name": "time", "type": "INT64", "timestamp_unit": "NANOS", "data": ["1001"],
            }]))

    def test_invalid_oversized_and_overrow_files_fail(self):
        for content in (b"", b"x" * (16 * 1024 * 1024 + 1), b"SECRET_BAD_FILE"):
            with self.subTest(size=len(content)), self.assertRaises((ValueError, RuntimeError)):
                decode_parquet(content)
        with self.assertRaises(RuntimeError):
            decode_parquet(parquet([{"name": "value", "type": "INT64", "data": ["1"] * 10001}]))
        result = subprocess.run(
            ["node", str(ROOT / "parquet-reader" / "reader.mjs")],
            input='{"parquet_base64":"SECRET$"}', text=True, capture_output=True, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("SECRET", result.stderr)


class DeltaTests(unittest.TestCase):
    def setUp(self):
        self.claims = {"tid": ID, "appid": ID, "oid": "00000000-0000-4000-8000-000000000002",
                       "aud": "https://storage.azure.com/", "idtyp": "app", "exp": time.time() + 600}
        self.responses = {}
        self.requests = []
        self.http = httpx.Client(transport=httpx.MockTransport(self.respond))
        self.addCleanup(self.http.close)
        self.reader = DeltaReader(ID, ID, self.token, ID, ID, http=self.http)
        self.file = parquet([
            {"name": "balance_cents", "type": "INT64", "data": ["3500000"]},
        ])

    def token(self, scope):
        self.assertEqual(scope, "https://storage.azure.com/.default")
        return "header." + base64.urlsafe_b64encode(json.dumps(self.claims).encode()).decode().rstrip("=") + ".sig"

    def respond(self, request):
        self.requests.append(request)
        if request.url.query:
            directory = parse_qs(request.url.query.decode())["directory"][0]
            names = [name for name in self.responses if name.startswith(directory + "/")]
            return httpx.Response(200, json={"paths": [{"name": name} for name in names]})
        key = request.url.path[len("/" + ID + "/"):]
        if key not in self.responses:
            return httpx.Response(404)
        return httpx.Response(200, content=self.responses[key])

    def install(self, table="projection_status", versions=None, columns=None):
        metadata = {"format": {"provider": "parquet"}, "partitionColumns": [],
                    "configuration": {"delta.enableDeletionVectors": "true"},
                    "schemaString": json.dumps({"fields": [
                        {"name": name} for name in (columns or ["balance_cents"])
                    ]})}
        actions = [{"protocol": {"minReaderVersion": 3, "readerFeatures": ["deletionVectors"]}},
                   {"metaData": metadata},
                   {"add": {"path": "original.parquet", "size": len(self.file),
                            "stats": '{"minValues":{"balance_cents":99999999}}'}}]
        for version, values in (versions or {0: actions}).items():
            self.responses[self.reader.path(table, f"_delta_log/{version:020d}.json")] = (
                "\n".join(json.dumps(value) for value in values).encode()
            )
        self.responses[self.reader.path(table, "original.parquet")] = self.file
        return actions

    def test_reads_actual_rows_not_delta_statistics_and_applies_removes(self):
        actions = self.install()
        replacement = parquet([{"name": "balance_cents", "type": "INT64", "data": ["2500000"]}])
        self.responses[self.reader.path("projection_status", "new.parquet")] = replacement
        self.install(versions={0: actions, 1: [
            {"remove": {"path": "original.parquet"}},
            {"add": {"path": "new.parquet", "size": len(replacement)}},
        ]})
        actual = self.reader.table("projection_status")
        self.assertEqual(actual["version"], 1)
        self.assertEqual(actual["rows"], [{"balance_cents": 2500000}])
        self.assertTrue(all(r.url.host == "onelake.dfs.fabric.microsoft.com" for r in self.requests))

    def test_delegated_wrong_root_wrong_tenant_wrong_audience_and_expired_tokens_fail(self):
        self.install()
        for field, value in (("scp", "read"), ("idtyp", "user"), ("appid", "other"),
                             ("tid", "other"), ("aud", "https://graph.microsoft.com"), ("exp", 0)):
            original = copy.deepcopy(self.claims)
            self.claims[field] = value
            with self.subTest(field=field), self.assertRaises(PermissionError):
                self.reader.table("projection_status")
            self.claims = original
        self.assertEqual(self.requests, [])

    def test_paths_unknown_features_missing_history_and_deletion_vectors_fail_closed(self):
        for path in ("../foreign.parquet", "/other", "%2e%2e/other", "https://evil.test", "a\\b"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                relative_path(path)
        with self.assertRaises(PermissionError):
            self.reader.path("foreign")
        actions = self.install()
        for modify in (
            lambda values: values[0]["protocol"].update(readerFeatures=["columnMapping"]),
            lambda values: values[1]["metaData"].update(partitionColumns=["owner_id"]),
            lambda values: values[2]["add"].update(deletionVector={"pathOrInlineDv": "ignored"}),
            lambda values: values[2]["add"].update(path="../foreign.parquet"),
            lambda values: values[2]["add"].update(size=1),
        ):
            values = copy.deepcopy(actions)
            modify(values)
            self.install(versions={0: values})
            with self.assertRaises(ValueError):
                self.reader.table("projection_status")
        self.responses.pop(self.reader.path("projection_status", "_delta_log/00000000000000000000.json"))
        self.install(versions={1: actions})
        with self.assertRaisesRegex(ValueError, "Incomplete Delta history"):
            self.reader.table("projection_status")

    def test_classic_multipart_checkpoint_state_and_v2_rejection(self):
        actions = self.install()
        self.responses.pop(self.reader.path("projection_status", "_delta_log/00000000000000000000.json"))
        self.responses[self.reader.path("projection_status", "_delta_log/_last_checkpoint")] = json.dumps({
            "version": 10, "parts": 2,
        }).encode()
        for part in (1, 2):
            self.responses[self.reader.path(
                "projection_status", f"_delta_log/{10:020d}.checkpoint.{part:010d}.{2:010d}.parquet",
            )] = b"checkpoint" + bytes([part])
        self.install(versions={11: []})
        with patch("payroll_agents.projection_reader.decode_parquet", side_effect=[
            actions[:2], actions[2:], [{"balance_cents": 3500000}],
        ]):
            self.assertEqual(self.reader.table("projection_status")["version"], 11)
        self.responses[self.reader.path("projection_status", "_delta_log/_last_checkpoint")] = json.dumps({
            "version": 10, "v2Checkpoint": {"path": "sidecar"},
        }).encode()
        with self.assertRaisesRegex(ValueError, "V2"):
            self.reader.table("projection_status")

    def test_detects_changed_delta_versions_and_refuses_redirects(self):
        self.install()
        inventory = self.reader.inventory
        calls = 0

        def changing(table):
            nonlocal calls
            calls += 1
            actual = inventory(table)
            return (1, *actual[1:]) if calls > 1 else actual

        with patch.object(self.reader, "inventory", side_effect=changing):
            with self.assertRaisesRegex(ValueError, "changed during"):
                self.reader.table("projection_status")
        self.http = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
            302, headers={"Location": "https://evil.test"},
        )))
        self.addCleanup(self.http.close)
        self.reader.http = self.http
        with self.assertRaises(httpx.HTTPStatusError):
            self.reader.get(self.reader.path("projection_status", "original.parquet"))


class ProjectionProofTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        state = Ledger(SQLiteStateStore(Path(self.directory.name) / "ledger.db"),
                       LedgerConfig(ID, ("marco", "ayca"), "hotel")).snapshot()
        self.store = Mock()
        self.store.read.return_value = Snapshot("actual-etag", state)
        self.runtime = SimpleNamespace(role=AgentRole.HOTEL_FINANCE, tenant=ID,
                                       root_client_id=ID, credential=Mock())
        self.expected = {"tenant_id": ID, "owner_id": "hotel",
                         "approver_ids": ["marco", "ayca"], "approver_upns": ["marco@test", "ayca@test"]}
        self.config = SimpleNamespace(tenant_id=ID, workspace_id=ID, lakehouse_id=ID,
                                      expected_source_policy=self.expected)
        self.bindings = {"tenant_id": ID, "hotel_owner_id": "hotel",
                         "approvers": [{"object_id": "marco"}, {"object_id": "ayca"}],
                         "fabric": {"projection": {}}}
        captured = "2020-01-01T00:00:01.234567+00:00"
        self.digest = hashlib.sha256(json.dumps(state, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.manifest = {"snapshot_sha256": self.digest, "captured_at": captured,
                         "projection_state": "complete", "ledger_status": "captured_snapshot",
                         "source_etag": "actual-etag", "ledger_version": "actual-etag",
                         "authoritative": False, "source_authoritative": True,
                         "transaction_count": 0, "approval_count": 0, "notification_count": 0}
        self.status = {**self.manifest, "owner_id": "hotel", "requested_source_etag": "actual-etag",
                       "requested_snapshot_sha256": self.digest, "last_error": None}
        self.tables = {"projection_status": {"rows": [self.status], "version": 5}}
        for name, rows in project_snapshot({
            "contract_version": 1, "state": state, "source_etag": "actual-etag", "captured_at": captured,
        }, self.expected).items():
            columns = {field.split()[0] for field in REFRESH_SCHEMAS[name].split(",")} | COMMON
            self.tables[name] = {"version": 2, "columns": columns,
                                 "rows": [{key: row[key] for key in columns} for row in rows]}
        self.reader = Mock()
        self.reader.get.side_effect = lambda *args, **kwargs: (json.dumps(self.manifest).encode(), {})
        self.reader.table.side_effect = lambda name: copy.deepcopy(self.tables[name])
        self.reader.inventory.side_effect = lambda name: (self.tables[name]["version"], set(), None)
        for target, value in (("ProjectionConfig.from_output", self.config), ("DeltaReader", self.reader)):
            mocked = patch("payroll_agents.projection_reader." + target, return_value=value)
            mocked.start()
            self.addCleanup(mocked.stop)

    def read(self):
        return read_current_projection(self.runtime, self.bindings, self.store)

    def test_verifies_all_actual_answer_rows_and_rechecks_before_publication(self):
        proof = self.read()
        self.assertTrue(proof["freshness_verified"])
        self.assertFalse(proof["ontology_freshness_verified"])
        self.assertEqual(len(proof["delta_versions"]), 8)
        self.assertEqual({row["account_id"]: row["balance_cents"]
                          for row in proof["analytical_rows"]["accounts"]},
                         {"hotel-reserve": 5000000, "hotel-payroll": 1000000})
        revalidate_projection(self.runtime, self.bindings, self.store, proof)
        self.status["projection_state"] = "refreshing"
        with self.assertRaises(ValueError):
            revalidate_projection(self.runtime, self.bindings, self.store, proof)

    def test_rejects_wrong_rows_counts_hash_status_or_source_changes(self):
        originals = copy.deepcopy((self.tables, self.manifest, self.status))
        for change in (
            lambda: self.tables["accounts"]["rows"][0].update(balance_cents=1),
            lambda: self.tables["accounts"]["rows"][0].update(ledger_version="old-etag"),
            lambda: self.manifest.update(snapshot_sha256="b" * 64),
            lambda: self.status.update(requested_snapshot_sha256="b" * 64),
            lambda: self.status.update(source_authoritative=False),
            lambda: (self.manifest.update(transaction_count=1), self.status.update(transaction_count=1)),
            lambda: self.tables["transfers"]["rows"].append({"invented": True}),
        ):
            self.tables, self.manifest, self.status = copy.deepcopy(originals)
            self.tables["projection_status"]["rows"] = [self.status]
            self.store.read.return_value = Snapshot("actual-etag", self.store.read.return_value.state)
            change()
            with self.assertRaises(ValueError):
                self.read()
        self.store.read.side_effect = [
            Snapshot("actual-etag", self.store.read.return_value.state),
            Snapshot("changed-etag", self.store.read.return_value.state),
        ]
        self.tables, self.manifest, self.status = copy.deepcopy(originals)
        self.tables["projection_status"]["rows"] = [self.status]
        with self.assertRaisesRegex(ValueError, "ledger changed"):
            self.read()
