"""Read actual, version-stable finance Delta rows under the bound managed root."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, unquote, urlencode
from uuid import UUID

import httpx

from .ledger import AgentRole, DomainError
from .projection import REFRESH_SCHEMAS, ProjectionConfig, assert_projection_current, project_snapshot

STORAGE_SCOPE = "https://storage.azure.com/.default"
TABLES = (
    "projection_status", "accounts", "payroll_obligations", "transfer_policy",
    "eligible_approvers", "transfers", "approval_requests", "notification_deliveries",
)
READER = Path(__file__).resolve().parents[2] / "parquet-reader" / "reader.mjs"


def decode_parquet(content):
    if not content or len(content) > 16 * 1024 * 1024:
        raise ValueError("Finance Parquet file exceeds its supported bound")
    result = subprocess.run(
        ["node", "--max-old-space-size=128", str(READER)], input=json.dumps({
            "parquet_base64": base64.b64encode(content).decode(),
        }), text=True, capture_output=True, timeout=30, check=False,
    )
    if result.returncode or len(result.stdout) > 16 * 1024 * 1024:
        raise RuntimeError("Finance Parquet decoding failed; no analytical fallback")

    def timestamp(value):
        if set(value) == {"__timestamp_micros"}:
            micros = value["__timestamp_micros"]
            if not isinstance(micros, str) or not re.fullmatch(r"-?\d{1,20}", micros):
                raise ValueError("Invalid exact Parquet timestamp")
            return (datetime(1970, 1, 1, tzinfo=timezone.utc)
                    + timedelta(microseconds=int(micros))).isoformat()
        return value

    decoded = json.loads(result.stdout, object_hook=timestamp)
    rows = decoded.get("rows")
    if not isinstance(rows, list) or len(rows) > 10000 or any(not isinstance(r, dict) for r in rows):
        raise ValueError("Invalid finance Parquet row result")
    return rows


def relative_path(value):
    if not isinstance(value, str):
        raise ValueError("Delta file path is not text")
    value = unquote(value)
    if (not value or value.startswith("/") or "\\" in value or ":" in value
            or any(part in ("", ".", "..") for part in value.split("/"))
            or "%" in value or any(ord(c) < 32 for c in value)):
        raise ValueError("Delta file path escapes its bound finance table")
    return value


class DeltaReader:
    def __init__(self, workspace, lakehouse, get_token, tenant, root_client, *, http=None):
        UUID(workspace)
        UUID(lakehouse)
        self.workspace, self.lakehouse = workspace, lakehouse
        self.base = f"https://onelake.dfs.fabric.microsoft.com/{workspace}"
        self.get_token, self.tenant, self.root_client = get_token, tenant, root_client
        self.http = http or httpx.Client(follow_redirects=False)
        self.deadline = time.monotonic() + 180
        self.byte_budget = 128 * 1024 * 1024

    def path(self, table, suffix=""):
        if table not in TABLES:
            raise PermissionError("Only installed finance tables may be read")
        return f"{self.lakehouse}/Tables/dbo/{table}" + ("/" + relative_path(suffix) if suffix else "")

    def get(self, path="", *, query=None, optional=False, limit=16 * 1024 * 1024):
        if path and not (path == f"{self.lakehouse}/Files/finance_projection_manifest.json" or
                         any(path.startswith(self.path(table) + "/") for table in TABLES)):
            raise PermissionError("Only the bound finance projection may be read")
        token = self.get_token(STORAGE_SCOPE)
        claims = json.loads(base64.urlsafe_b64decode(
            token.split(".")[1] + "=" * (-len(token.split(".")[1]) % 4)))
        if (claims.get("tid") != self.tenant
                or claims.get("appid", claims.get("azp")) != self.root_client
                or claims.get("aud", "").rstrip("/") != "https://storage.azure.com"
                or claims.get("scp") or claims.get("idtyp") == "user"
                or claims.get("exp", 0) <= time.time()):
            raise PermissionError("Only the bound, non-delegated managed root may read Delta")
        UUID(claims["oid"])
        url = self.base + ("/" + quote(path, safe="/") if path else "")
        if query:
            url += "?" + urlencode(query)
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Finance Delta read deadline exceeded")
        with self.http.stream("GET", url, headers={
            "Authorization": "Bearer " + token, "x-ms-version": "2021-06-08",
        }, timeout=min(remaining, 45)) as response:
            if optional and response.status_code == 404:
                return None, dict(response.headers)
            response.raise_for_status()
            if response.status_code != 200:
                raise RuntimeError("Unexpected finance Delta read response")
            chunks, count = [], 0
            for chunk in response.iter_bytes():
                count += len(chunk)
                self.byte_budget -= len(chunk)
                if time.monotonic() >= self.deadline:
                    raise TimeoutError("Finance Delta read deadline exceeded")
                if count > limit or self.byte_budget < 0:
                    raise ValueError("Finance Delta response exceeds its supported bound")
                chunks.append(chunk)
            return b"".join(chunks), dict(response.headers)

    def inventory(self, table):
        directory = self.path(table, "_delta_log")
        params = {"resource": "filesystem", "directory": directory, "recursive": "false"}
        versions = set()
        for _ in range(100):
            content, headers = self.get(query=params, limit=4 * 1024 * 1024)
            page = json.loads(content)
            for entry in page["paths"]:
                match = re.fullmatch(re.escape(directory) + r"/(\d{20})\.json", entry["name"])
                if match:
                    versions.add(int(match[1]))
            continuation = headers.get("x-ms-continuation") or page.get("ContinuationToken")
            if not continuation:
                break
            params["continuation"] = continuation
        else:
            raise ValueError("Finance Delta inventory exceeded its page bound")
        checkpoint, _ = self.get(self.path(table, "_delta_log/_last_checkpoint"), optional=True)
        checkpoint = json.loads(checkpoint) if checkpoint else None
        if checkpoint and ("v2Checkpoint" in checkpoint or "checkpointSchema" in checkpoint):
            raise ValueError("V2 Delta checkpoints require an explicitly supported reader")
        latest = max(versions | ({checkpoint["version"]} if checkpoint else set()), default=-1)
        if latest < 0:
            raise ValueError("No committed finance Delta snapshot")
        return latest, versions, checkpoint

    def table(self, table):
        latest, versions, checkpoint = self.inventory(table)
        active, metadata, protocol = {}, None, None

        def apply(action):
            nonlocal metadata, protocol
            if action.get("metaData") is not None:
                metadata = action["metaData"]
            if action.get("protocol") is not None:
                protocol = action["protocol"]
            if action.get("remove") is not None:
                active.pop(relative_path(action["remove"]["path"]), None)
            if action.get("add") is not None:
                add = action["add"]
                active[relative_path(add["path"])] = add

        start = 0
        if checkpoint:
            version, parts = checkpoint["version"], checkpoint.get("parts", 1)
            if (type(version) is not int or type(parts) is not int or
                    version < 0 or not 1 <= parts <= 100):
                raise ValueError("Invalid Delta checkpoint")
            for part in range(1, parts + 1):
                suffix = (f"{version:020d}.checkpoint.parquet" if "parts" not in checkpoint else
                          f"{version:020d}.checkpoint.{part:010d}.{parts:010d}.parquet")
                content, _ = self.get(self.path(table, "_delta_log/" + suffix))
                for action in decode_parquet(content):
                    apply(action)
            start = version + 1
        if latest - start + 1 > 256:
            raise ValueError("Finance Delta history requires a retained checkpoint")
        for version in range(start, latest + 1):
            if version not in versions:
                raise ValueError("Incomplete Delta history; no stale snapshot fallback")
            content, _ = self.get(self.path(table, f"_delta_log/{version:020d}.json"))
            for line in content.decode().splitlines():
                apply(json.loads(line))
        if not metadata or not protocol or metadata["format"]["provider"] != "parquet":
            raise ValueError("Finance Delta metadata or protocol is unavailable")
        if (type(protocol["minReaderVersion"]) is not int or
                not 1 <= protocol["minReaderVersion"] <= 3 or
                set(protocol.get("readerFeatures", [])) - {"deletionVectors", "timestampNtz"} or
                metadata.get("configuration", {}).get("delta.columnMapping.mode", "none") != "none" or
                metadata.get("partitionColumns")):
            raise ValueError("Unsupported Delta feature; analytics consumption refused")
        columns = {field["name"] for field in json.loads(metadata["schemaString"])["fields"]}
        if table != "projection_status":
            required = {column.split()[0] for column in REFRESH_SCHEMAS[table].split(",")} | {
                "captured_at", "ledger_version", "source_etag", "ledger_status", "authoritative",
            }
            if columns != required:
                raise ValueError("Delta schema differs from the installed finance projection contract")
        rows = []
        for path, add in sorted(active.items()):
            if add.get("deletionVector"):
                raise ValueError("Active Delta deletion vectors require an explicitly supported reader")
            content, _ = self.get(self.path(table, path))
            if len(content) != add["size"]:
                raise ValueError("Delta file size differs from the committed add action")
            rows.extend(decode_parquet(content))
            if len(rows) > 10000:
                raise ValueError("Finance Delta table exceeds its row bound")
        if any(set(row) != columns for row in rows):
            raise ValueError("Parquet rows disagree with the committed Delta schema")
        if self.inventory(table)[0] != latest:
            raise ValueError("Finance Delta changed during its read")
        return {"version": latest, "columns": columns, "rows": rows}


def read_current_projection(runtime, bindings, store):
    if runtime.role not in (AgentRole.HOTEL_FINANCE, AgentRole.BANKING_OPERATIONS):
        raise PermissionError("Only the two installed finance roots may consume analytics")
    configured = bindings["fabric"].get("projection")
    if configured is None:
        raise DomainError("IQ_ANALYTICS_UNAVAILABLE", "No installed projection reader configuration.")
    config = ProjectionConfig.from_output(configured)
    if (config.tenant_id != runtime.tenant or bindings["tenant_id"] != runtime.tenant
            or config.expected_source_policy["owner_id"] != bindings["hotel_owner_id"]
            or set(config.expected_source_policy["approver_ids"])
            != {person["object_id"] for person in bindings["approvers"]}):
        raise PermissionError("Analytical configuration differs from the bound finance authority")
    snapshot = store.read()
    if snapshot.state is None:
        raise ValueError("Actual authoritative ledger snapshot required")
    with httpx.Client(follow_redirects=False) as http:
        reader = DeltaReader(config.workspace_id, config.lakehouse_id,
                             lambda scope: runtime.credential.get_token(scope).token,
                             runtime.tenant, runtime.root_client_id, http=http)
        content, _ = reader.get(f"{config.lakehouse_id}/Files/finance_projection_manifest.json")
        manifest = json.loads(content)
        status = reader.table("projection_status")
        if len(status["rows"]) != 1:
            raise ValueError("Exactly one live Delta projection status row required")
        assert_projection_current(manifest, current_source_etag=snapshot.version,
                                  live_projection_status=status["rows"][0])
        digest = hashlib.sha256(json.dumps(
            snapshot.state, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode()).hexdigest()
        if digest != manifest["snapshot_sha256"]:
            raise ValueError("Committed projection hash differs from the actual ledger")
        for field, collection in (
            ("transaction_count", "transactions"), ("approval_count", "approvals"),
            ("notification_count", "outbox"),
        ):
            if manifest[field] != len(snapshot.state[collection]):
                raise ValueError("Projection counts differ from the actual ledger")
        live_status = status["rows"][0]
        if (live_status.get("owner_id") != config.expected_source_policy["owner_id"]
                or live_status.get("requested_source_etag") != snapshot.version
                or live_status.get("requested_snapshot_sha256") != digest
                or live_status.get("last_error") is not None):
            raise ValueError("Live analytical status differs from the completed source capture")
        expected = project_snapshot({
            "contract_version": 1, "state": snapshot.state, "source_etag": snapshot.version,
            "captured_at": datetime.fromisoformat(
                manifest["captured_at"].replace("Z", "+00:00")).isoformat(),
        }, config.expected_source_policy)
        tables = {"projection_status": status}
        for name, records in expected.items():
            table = reader.table(name)
            if not records:
                if table["rows"]:
                    raise ValueError("Analytical table invents events absent from the actual ledger")
            else:
                if not table["columns"] <= set(records[0]):
                    raise ValueError("Unknown analytical finance columns")
                selected = [{key: row[key] for key in table["columns"]} for row in records]
                canonical = lambda values: sorted(json.dumps(r, sort_keys=True, allow_nan=False) for r in values)
                if canonical(selected) != canonical(table["rows"]):
                    raise ValueError("Actual Delta rows disagree with the captured ledger")
            assert_projection_current(
                manifest, current_source_etag=snapshot.version,
                live_projection_status=status["rows"][0],
                row_versions=(r.get("ledger_version") for r in table["rows"]),
            )
            tables[name] = table
        for name, table in tables.items():
            if reader.inventory(name)[0] != table["version"]:
                raise ValueError("Finance Delta changed before the read could commit")
        if store.read().version != snapshot.version:
            raise ValueError("Authoritative ledger changed during the analytical read")
    return {
        "freshness_verified": True, "source_etag": snapshot.version,
        "projection_status": status["rows"][0],
        "delta_versions": {name: table["version"] for name, table in tables.items()},
        "analytical_rows": {name: table["rows"] for name, table in tables.items()},
        "ontology_freshness_verified": False, "_source_state": snapshot.state,
    }


def revalidate_projection(runtime, bindings, store, proof):
    config = ProjectionConfig.from_output(bindings["fabric"]["projection"])
    with httpx.Client(follow_redirects=False) as http:
        reader = DeltaReader(config.workspace_id, config.lakehouse_id,
                             lambda scope: runtime.credential.get_token(scope).token,
                             runtime.tenant, runtime.root_client_id, http=http)
        content, _ = reader.get(f"{config.lakehouse_id}/Files/finance_projection_manifest.json")
        status = reader.table("projection_status")
        if len(status["rows"]) != 1 or status["rows"][0] != proof["projection_status"]:
            raise ValueError("Live analytical status changed before publication")
        assert_projection_current(json.loads(content), current_source_etag=proof["source_etag"],
                                  live_projection_status=status["rows"][0])
        for name, version in proof["delta_versions"].items():
            if reader.inventory(name)[0] != version:
                raise ValueError("Analytical answer rows changed before publication")
        if store.read().version != proof["source_etag"]:
            raise ValueError("Authoritative ledger changed before publication")
