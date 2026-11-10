"""Bank-only Fabric projection integration. Standard library; no CLI or credentials.

Inject a trusted keyless root-token provider and a TLS-verifying, non-logging HTTP
sender. Token claims are identity guards, not signature/provenance verification.
Azure/Fabric validates signatures; the caller owns authentic ledger capture.
No graph refresh, ledger mutation, filesystem access or automatic POST retries.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import time
from typing import Any, Callable, Iterable, Mapping, Protocol
from urllib.parse import urlsplit, parse_qs
import uuid

logger = logging.getLogger(__name__)

FABRIC = "https://api.fabric.microsoft.com"
STORAGE = "https://storage.azure.com"
REFRESH_SCHEMAS = {
    "accounts": "account_id string,owner_id string,currency string,balance_cents bigint",
    "payroll_obligations": "task_id string,owner_id string,status string,obligation_cents bigint,currency string,source_account_id string,destination_account_id string,last_transaction_id string",
    "transfer_policy": "policy_id string,approval_threshold_cents bigint,comparison string,decision_rule string,expiry string,notifications_per_request bigint,notification_channels string,execution_role string",
    "eligible_approvers": "approver_id string,configured_upn string,policy_id string",
    "transfers": "transaction_id string,source_account_id string,destination_account_id string,amount_cents bigint,currency string,approval_request_id string,idempotency_key string,source_balance_cents bigint,destination_balance_cents bigint",
    "approval_requests": "request_id string,status string,decision string,decision_by string,source_account_id string,destination_account_id string,amount_cents bigint,currency string,idempotency_key string,executed_transaction_id string",
    "notification_deliveries": "delivery_id string,request_id string,recipient_id string,channel string,status string,request_status string,attempts bigint,attempt_id string",
}
REFRESH_COLUMNS = {
    "payroll_obligations": {"last_transaction_id": "string"},
    "transfers": {"idempotency_key": "string", "source_balance_cents": "bigint",
                  "destination_balance_cents": "bigint"},
    "approval_requests": {"source_account_id": "string", "destination_account_id": "string",
                          "currency": "string", "idempotency_key": "string"},
    "notification_deliveries": {"request_status": "string", "attempts": "bigint", "attempt_id": "string"},
    "projection_status": {**{k: "string" for k in (
        "projection_state", "snapshot_sha256", "refresh_id", "requested_source_etag",
        "requested_snapshot_sha256", "previous_source_etag", "last_error")},
        "source_authoritative": "boolean"},
}


def text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(name + " must be a nonempty string")


def integer(value, name, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(name + " must be an integer >= " + str(minimum))


def validate_snapshot(envelope, expected):
    """Validate shape and ledger invariants, not caller's provenance/authentication."""
    if set(envelope) != {"contract_version", "source_etag", "captured_at", "state"}:
        raise ValueError("Expected contract_version, source_etag, captured_at, state")
    if envelope["contract_version"] != 1:
        raise ValueError("Unsupported projection input contract")
    text(envelope["source_etag"], "source_etag")
    if envelope["source_etag"].startswith("initial-baseline"):
        raise ValueError("A provisioning fixture label is not an authoritative source ETag")
    if len(envelope["source_etag"]) > 256:
        raise ValueError("ETag too long")
    captured = datetime.fromisoformat(envelope["captured_at"].replace("Z", "+00:00"))
    if captured.tzinfo is None or captured.utcoffset().total_seconds() != 0:
        raise ValueError("captured_at must be a timezone-aware UTC source read timestamp")
    if captured > datetime.now(timezone.utc):
        raise ValueError("Future snapshot captures are invalid")
    state = envelope["state"]
    if state.get("schema_version") != 1 or state.get("simulation") is not True:
        raise ValueError("Only the supported controlled transactional ledger schema is accepted")
    policy = state["policy"]
    if policy["tenant_id"] != expected["tenant_id"] or policy["hotel_owner_id"] != expected["owner_id"]:
        raise ValueError("Source ledger tenant/owner mismatch")
    if sorted(policy["approver_object_ids"]) != sorted(expected["approver_ids"]):
        raise ValueError("Source ledger approver policy mismatch")
    if policy["approval_threshold_cents"] != 1000000 or policy["currency"] != "USD":
        raise ValueError("Source ledger currency/threshold mismatch")
    integer(policy["delivery_max_attempts"], "delivery_max_attempts", 1)
    if set(state["accounts"]) != {"hotel-reserve", "hotel-payroll"}:
        raise ValueError("Exactly the two finance-owned accounts are required")
    for account in state["accounts"].values():
        if account["owner_id"] != expected["owner_id"] or account["currency"] != "USD":
            raise ValueError("Non-hotel/non-USD account")
        integer(account["balance_cents"], "balance_cents")
    if set(state["tasks"]) != {"payroll-funding"}:
        raise ValueError("Unexpected finance task")
    task = state["tasks"]["payroll-funding"]
    integer(task["obligation_cents"], "obligation_cents", 1)
    if task["currency"] != "USD" or task["source_account_id"] != "hotel-reserve" or (
        task["destination_account_id"] != "hotel-payroll"
    ):
        raise ValueError("Payroll task references unexpected account/currency")
    if task["status"] not in ("pending", "funded"):
        raise ValueError("Unsupported payroll task status")
    transactions = state["transactions"]
    approvals = state["approvals"]
    outbox = state["outbox"]
    if not isinstance(transactions, list) or not isinstance(approvals, dict) or not isinstance(outbox, dict):
        raise ValueError("Malformed actual event collections")
    def action(a):
        if set(a) != {"source_account_id", "destination_account_id", "amount_cents", "currency"}:
            raise ValueError("Unexpected action schema")
        integer(a["amount_cents"], "amount_cents", 1)
        if a["currency"] != "USD" or a["source_account_id"] not in state["accounts"] or (
            a["destination_account_id"] not in state["accounts"] or
            a["source_account_id"] == a["destination_account_id"]
        ):
            raise ValueError("Invalid actual event action")
    ids = set()
    for tx in transactions:
        action(tx["action"])
        text(tx["transaction_id"], "transaction_id")
        if tx["transaction_id"] in ids:
            raise ValueError("Duplicate actual transaction ID")
        ids.add(tx["transaction_id"])
        integer(tx["source_balance_cents"], "source_balance_cents")
        integer(tx["destination_balance_cents"], "destination_balance_cents")
        rid = tx["approval_request_id"]
        if tx["action"]["amount_cents"] > policy["approval_threshold_cents"] and rid is None:
            raise ValueError("Above-threshold recorded transfer lacks actual approval reference")
        if rid is not None:
            approval = approvals[rid]
            if approval["status"] != "approved" or approval["action"] != tx["action"] or (
                approval["executed_transaction_id"] != tx["transaction_id"] or
                approval["idempotency_key"] != tx["idempotency_key"]
            ):
                raise ValueError("Actual executed approval/transaction binding mismatch")
    for rid, approval in approvals.items():
        if approval["request_id"] != rid:
            raise ValueError("Approval ID mismatch")
        action(approval["action"])
        if approval["status"] not in ("pending", "approved", "denied", "cancelled"):
            raise ValueError("Invalid actual approval status")
        if approval["status"] == "pending":
            if approval["decision"] is not None or approval["decision_by"] is not None:
                raise ValueError("Pending approval has fabricated decision fields")
        else:
            actor = approval["decision_by"]
            if not actor or actor["tenant_id"] != expected["tenant_id"] or (
                actor["object_id"] not in expected["approver_ids"]
            ):
                raise ValueError("Actual decision principal is not eligible")
            if approval["decision"] != {"approved": "approve", "denied": "deny",
                                       "cancelled": "cancel"}[approval["status"]]:
                raise ValueError("Actual approval decision/status mismatch")
        if approval["executed_transaction_id"] is not None and approval["executed_transaction_id"] not in ids:
            raise ValueError("Approval refers to missing actual transfer")
        deliveries = [v for v in outbox.values() if v["request_id"] == rid]
        if len(deliveries) != 4 or {
            (v["recipient"]["object_id"], v["channel"]) for v in deliveries
        } != {(p, c) for p in expected["approver_ids"] for c in ("teams", "email")}:
            raise ValueError("Actual request must have precisely four modeled notification records")
    for key, delivery in outbox.items():
        if delivery["delivery_id"] != key or delivery["request_id"] not in approvals:
            raise ValueError("Delivery references missing actual request")
        if delivery["recipient"]["tenant_id"] != expected["tenant_id"]:
            raise ValueError("Delivery tenant mismatch")
        if delivery["status"] not in ("pending", "in_flight", "succeeded", "failed", "cancelled"):
            raise ValueError("Invalid actual delivery status")
        integer(delivery["attempts"], "delivery attempts")
    integer(state["next_transaction_sequence"], "next_transaction_sequence", 1)
    return state


def project_snapshot(envelope, expected):
    state = validate_snapshot(envelope, expected)
    common = {"captured_at": envelope["captured_at"], "ledger_version": envelope["source_etag"],
              "source_etag": envelope["source_etag"], "ledger_status": "captured_snapshot",
              "authoritative": False}
    accounts = [{"account_id": key, **value, **common} for key, value in state["accounts"].items()]
    payroll = [{"task_id": key, "owner_id": expected["owner_id"], **value, **common}
               for key, value in state["tasks"].items()]
    policy = [{"policy_id": "hotel-transfer-policy", "approval_threshold_cents": 1000000,
               "comparison": "strictly_greater_than",
               "decision_rule": "first_authenticated_eligible_decision_wins_atomically",
               "expiry": "none", "notifications_per_request": 4,
               "notification_channels": "teams,email", "execution_role": "banking_operations", **common}]
    people = [{"approver_id": oid, "configured_upn": expected["approver_upns"][index],
               "policy_id": "hotel-transfer-policy", **common}
              for index, oid in enumerate(expected["approver_ids"])]
    transactions = [{**v["action"], **{k: val for k, val in v.items() if k != "action"}, **common}
                    for v in state["transactions"]]
    approvals = [{**v["action"], **{k: val for k, val in v.items() if k not in ("action", "decision_by")},
                  "decision_by": v["decision_by"]["object_id"] if v["decision_by"] else None, **common}
                 for v in state["approvals"].values()]
    notifications = [{k: v.get(k) for k in (
        "delivery_id", "request_id", "channel", "status", "request_status", "attempts",
        "attempt_id") } | {"recipient_id": v["recipient"]["object_id"], **common}
                     for v in state["outbox"].values()]
    return {"accounts": accounts, "payroll_obligations": payroll, "transfer_policy": policy,
            "eligible_approvers": people, "transfers": transactions, "approval_requests": approvals,
            "notification_deliveries": notifications}


@dataclass(frozen=True)
class ProjectionConfig:
    tenant_id: str
    workspace_id: str
    lakehouse_id: str
    notebook_id: str
    bank_principal_id: str
    expected_source_policy: Mapping[str, Any]
    data_agent_id: str
    ontology_id: str | None
    environment: str

    def __post_init__(self):
        for value in (self.tenant_id, self.workspace_id, self.lakehouse_id,
                      self.notebook_id, self.bank_principal_id, self.data_agent_id):
            uuid.UUID(value)
        if self.ontology_id:
            uuid.UUID(self.ontology_id)
        if self.environment not in ("dev", "test", "prod"):
            raise ValueError("Explicit dev/test/prod environment required")
        if self.expected_source_policy["tenant_id"] != self.tenant_id:
            raise ValueError("Installed source policy tenant mismatch")

    @classmethod
    def from_output(cls, output: Mapping[str, Any]) -> ProjectionConfig:
        refresh = output["projection_refresh"]
        if refresh["contract_version"] != 1:
            raise ValueError("Unsupported installed refresh contract")
        config = cls(output["tenant_id"], output["workspace_id"], output["lakehouse_id"],
                     refresh["notebook_id"], refresh["bank_principal_id"],
                     json.loads(json.dumps(refresh["expected_source_policy"])),
                     output["data_agent_id"], output.get("ontology_id"), output["environment"])
        return config

    @property
    def manifest_url(self) -> str:
        return (f"https://onelake.dfs.fabric.microsoft.com/{self.workspace_id}/{self.lakehouse_id}"
                "/Files/finance_projection_manifest.json")


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: Any = None  # Already parsed JSON; None for an empty response.


class HttpSend(Protocol):
    def __call__(self, method: str, url: str, *, headers: Mapping[str, str],
                 body: Mapping[str, Any] | None, timeout_seconds: float) -> HttpResponse: ...


class ProjectionRefreshError(RuntimeError):
    """Safe error carrying accepted job/commit progress; never resubmit blindly."""
    def __init__(self, message: str, progress: Mapping[str, Any]):
        super().__init__(message)
        self.progress = dict(progress)


def projection_job_id(config, value):
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https" or parsed.hostname != "api.fabric.microsoft.com"
        or parsed.username or parsed.password or parsed.port or parsed.fragment
        or (parsed.query and parse_qs(parsed.query, keep_blank_values=True) != {"beta": ["true"]})
    ):
        raise ValueError("Untrusted projection job URL")
    prefixes = (
        f"/v1/workspaces/{config.workspace_id}/items/{config.notebook_id}/jobs/instances/",
        f"/v1/workspaces/{config.workspace_id}/items/{config.notebook_id}/jobs/RunNotebook/instances/",
        f"/v1/workspaces/{config.workspace_id}/notebooks/{config.notebook_id}/jobs/execute/instances/",
    )
    for prefix in prefixes:
        if parsed.path.startswith(prefix):
            identifier = parsed.path[len(prefix):]
            if str(uuid.UUID(identifier)) != identifier.casefold():
                raise ValueError("Unexpected projection job identifier")
            return identifier
    raise ValueError("Projection job does not belong to the configured notebook")


class _Client:
    def __init__(self, config, credential_kind, get_token, send, timeout, sleep, clock):
        if credential_kind not in ("managed_identity", "agent_identity"):
            raise PermissionError("Only explicitly keyless managed/agent identity callbacks are allowed")
        if timeout <= 0:
            raise ValueError("Positive timeout required")
        self.config, self.get_token, self.send = config, get_token, send
        self.sleep, self.clock, self.deadline = sleep, clock, clock() + timeout
        self.operations = set()

    def wait(self, seconds):
        remaining = self.deadline - self.clock()
        if remaining <= seconds:
            raise TimeoutError("Projection deadline; inspect accepted job before any resume")
        self.sleep(seconds)

    def raw(self, method, url, body=None):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment or parsed.port:
            raise ValueError("Unsafe projection HTTP destination")
        resources = {"api.fabric.microsoft.com": FABRIC,
                     "onelake.dfs.fabric.microsoft.com": STORAGE}
        resource = resources.get(parsed.hostname)
        if not resource:
            raise ValueError("Projection destination is not allowlisted")
        notebook_execution = (
            f"/v1/workspaces/{self.config.workspace_id}"
            f"/notebooks/{self.config.notebook_id}/jobs/execute/instances"
        )
        extended_job = False
        if method.lower() == "get" and parsed.query:
            if parse_qs(parsed.query, keep_blank_values=True) == {"beta": ["true"]}:
                projection_job_id(self.config, url)
                extended_job = True
        if parsed.query and not (
            extended_job or
            (method.lower() == "post" and parsed.path == notebook_execution
             and parse_qs(parsed.query, keep_blank_values=True) == {"beta": ["false"]}) or
            (method.lower() == "get" and
            parsed.path == f"/v1/workspaces/{self.config.workspace_id}/items/{self.config.notebook_id}/jobs/instances"
            and set(parse_qs(parsed.query)) == {"continuationToken"})):
            raise ValueError("Unexpected projection query parameters")
        if resource == STORAGE and url != self.config.manifest_url:
            raise ValueError("Only the bound projection manifest may be read from OneLake")
        if resource == FABRIC:
            item_urls = [
                f"/v1/workspaces/{self.config.workspace_id}/items/{item}"
                for item in (self.config.notebook_id, self.config.data_agent_id)]
            notebook_call = method.lower() == "post" and parsed.path == notebook_execution
            if not notebook_call and not extended_job and not any(
                parsed.path == item or parsed.path.startswith(item + "/") for item in item_urls
            ) and (
                not any(parsed.path in (f"/v1/operations/{op}", f"/v1/operations/{op}/result")
                        for op in self.operations)):
                raise ValueError("Only bound finance items and their captured operations may be called")
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise TimeoutError("Projection request deadline")
        try:
            token = self.get_token(resource + "/.default")
        except Exception as error:
            logger.error("Projection root token acquisition failed: type=%s", type(error).__name__)
            raise PermissionError("Keyless root token acquisition failed; no human fallback") from None
        try:
            payload = token.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        except (ValueError, IndexError, TypeError):
            raise PermissionError("Workload token must be a valid JWT") from None
        audiences = (
            (FABRIC, "https://analysis.windows.net/powerbi/api",
             "00000009-0000-0000-c000-000000000000")
            if resource == FABRIC else (resource,)
        )
        audience = claims.get("aud")
        if (claims.get("tid") != self.config.tenant_id or
            claims.get("oid") != self.config.bank_principal_id or
            not isinstance(audience, str) or audience.rstrip("/") not in audiences or
            claims.get("idtyp") == "user" or claims.get("scp")):
            raise PermissionError("Human/delegated/wrong-principal token forbidden for projection runtime")
        if "exp" in claims and claims["exp"] <= time.time():
            raise PermissionError("Expired workload token")
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise TimeoutError("Projection token acquisition exceeded request deadline")
        headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json",
                   "x-ms-fabric-skill": "spark-cli"}
        if resource == STORAGE:
            headers["x-ms-version"] = "2021-06-08"
        try:
            response = self.send(method, url, headers=headers, body=body, timeout_seconds=remaining)
        except Exception:
            raise RuntimeError("Projection transport failed; request not retried") from None
        if not isinstance(response, HttpResponse):
            raise TypeError("HTTP callback must return HttpResponse")
        return HttpResponse(response.status, {k.lower(): v for k, v in response.headers.items()}, response.body)

    def json(self, method, url, body=None):
        response = self.raw(method, url, body)
        if response.status != 200:
            raise RuntimeError(f"Projection request returned HTTP {response.status}")
        return response.body

    def definition(self, url, body, *, mutation=False):
        response = self.raw("post", url, body)
        if response.status == 202:
            op = response.headers.get("x-ms-operation-id")
            if not op:
                op = response.headers.get("location", "").split("/operations/")[-1].split("/")[0]
            uuid.UUID(op)
            self.operations.add(op)
            poll_url = FABRIC + "/v1/operations/" + op
            while True:
                self.wait(max(0, float(response.headers.get("retry-after", "5"))))
                status = self.json("get", poll_url)["status"]
                if status == "Succeeded":
                    if mutation:
                        return None
                    response = self.raw("get", poll_url + "/result")
                    break
                if status in ("Failed", "Cancelled", "Canceled"):
                    raise RuntimeError("Finance definition operation failed; no POST retry")
        if response.status not in ((200, 204) if mutation else (200,)):
            raise RuntimeError("Finance definition request failed")
        return response.body

    def job_url(self, value):
        projection_job_id(self.config, value)
        return value.partition("?")[0] + "?beta=true"


def read_projection_manifest(config: ProjectionConfig, *, credential_kind: str,
                             get_token: Callable[[str], str], send: HttpSend,
                             timeout_seconds: float = 30) -> dict[str, Any] | None:
    """Read the last committed file (404 => no refresh yet), NOT the live Delta lock."""
    client = _Client(config, credential_kind, get_token, send, timeout_seconds, time.sleep, time.monotonic)
    response = client.raw("get", config.manifest_url)
    if response.status == 404:
        return None
    if response.status != 200 or not isinstance(response.body, dict):
        raise RuntimeError("Projection manifest read failed")
    return response.body


def assert_projection_current(manifest: Mapping[str, Any] | None, *, current_source_etag: str,
                              live_projection_status: Mapping[str, Any],
                              row_versions: Iterable[str] = ()) -> None:
    """Require actual live Delta status plus last commit, ledger ETag and query row versions.

    A file alone can be stale while Delta is refreshing/failed. This gate never
    substitutes analytical balances for the transactional ledger's authority.
    """
    if not manifest:
        raise ValueError("No committed projection")
    text(current_source_etag, "current_source_etag")
    digest = manifest.get("snapshot_sha256")
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("Missing/invalid committed source hash")
    captured = datetime.fromisoformat(manifest["captured_at"].replace("Z", "+00:00"))
    if captured.tzinfo is None or captured.utcoffset().total_seconds() != 0 or captured > datetime.now(timezone.utc):
        raise ValueError("Invalid committed capture timestamp")
    for field in ("transaction_count", "approval_count", "notification_count"):
        integer(manifest.get(field), field)
    for value in (manifest, live_projection_status):
        if (value.get("projection_state") != "complete" or
            value.get("ledger_status") != "captured_snapshot" or
            value.get("source_etag") != current_source_etag or
            value.get("ledger_version") != current_source_etag or
            value.get("authoritative") is not False or
            value.get("source_authoritative") is not True):
            raise ValueError("Projection stale, partial, failed or untrusted")
    for field in ("snapshot_sha256", "captured_at", "transaction_count", "approval_count", "notification_count"):
        if field not in manifest or live_projection_status.get(field) != manifest[field]:
            raise ValueError("Live projection status disagrees with committed manifest")
    if any(version != current_source_etag for version in row_versions):
        raise ValueError("Mixed-version analytical answer")


def _sync_metadata(client):
    config = client.config
    url = f"{FABRIC}/v1/workspaces/{config.workspace_id}/items/{config.data_agent_id}"
    item = client.json("get", url)
    marker = f"finance-analytics-bootstrap/v1/{config.tenant_id}/{config.environment}"
    if item.get("type") != "DataAgent" or item.get("displayName") != "FinanceAnalyticalAgent" or (
        item.get("description") != marker):
        raise ValueError("DataAgent metadata target is not the owned finance artifact")
    def decode(response):
        return {part["path"]: json.loads(base64.b64decode(part["payload"]))
                for part in response.get("definition", response)["parts"]}
    values = decode(client.definition(url + "/getDefinition", {}))
    original = json.loads(json.dumps(values))
    sources = [v for path, v in values.items() if path.endswith("/datasource.json")]
    ontologies = [v for path, v in values.items() if path.endswith("/ontology.json")]
    if any(isinstance(value, dict) and "artifactId" in value and
           not path.endswith(("/datasource.json", "/ontology.json"))
           for path, value in values.items()):
        raise ValueError("Unknown DataAgent source part; refusing metadata mutation")
    if len(sources) != 2 or any(
        v.get("workspaceId") != config.workspace_id or v.get("artifactId") != config.lakehouse_id
        or v.get("type") != "lakehouse_tables" for v in sources):
        raise ValueError("Finance DataAgent lakehouse source policy mismatch")
    if len(ontologies) != (2 if config.ontology_id else 0) or any(
        v.get("workspaceId") != config.workspace_id or v.get("artifactId") != config.ontology_id
        for v in ontologies):
        raise ValueError("Finance DataAgent ontology source policy mismatch")
    allowed = {"accounts", "payroll_obligations", "transfer_policy", "eligible_approvers",
               "projection_status", "transfers", "approval_requests", "notification_deliveries"}
    for source in sources:
        selected = []
        def columns(nodes):
            for node in nodes:
                if node.get("type") == "lakehouse_tables.schema" and (
                    node.get("display_name") != "dbo" or node.get("is_selected") is not True):
                    raise ValueError("Finance DataAgent schema allowlist mismatch")
                if node.get("type") == "lakehouse_tables.table":
                    table = node["display_name"]
                    if table not in allowed or node.get("is_selected") is not True:
                        raise ValueError("Unselected/unexpected finance table")
                    selected.append(table)
                    additions = {"source_etag": "string", **REFRESH_COLUMNS.get(table, {})}
                    for column in node["children"]:
                        if column["display_name"] in additions:
                            column["is_selected"] = True
                    existing = {v["display_name"] for v in node["children"]}
                    for name, dtype in additions.items():
                        if name not in existing:
                            node["children"].append({
                                "id": str(uuid.uuid5(uuid.NAMESPACE_URL,
                                    f"finance-analytics/{config.workspace_id}/{config.lakehouse_id}/{table}/{name}")),
                                "is_selected": True, "display_name": name,
                                "type": "lakehouse_tables.column", "data_type": dtype,
                                "description": None, "children": []})
                columns(node.get("children", []))
        columns(source["elements"])
        if set(selected) != allowed or len(selected) != len(allowed):
            raise ValueError("Finance DataAgent selected-table allowlist mismatch")
    desired = {"parts": [
        {"path": path, "payloadType": "InlineBase64",
         "payload": base64.b64encode(json.dumps(value, separators=(",", ":")).encode()).decode()}
        for path, value in values.items()]}
    # Instructions, both stages and all source IDs are preserved, never rebuilt from a fixture.
    if values != original:
        client.definition(url + "/updateDefinition", {"definition": desired}, mutation=True)
    if decode(client.definition(url + "/getDefinition", {})) != values:
        raise RuntimeError("Published finance metadata readback mismatch")


def refresh_projection(envelope: Mapping[str, Any], *, config: ProjectionConfig,
                       expected_projection_etag: str, credential_kind: str,
                       get_token: Callable[[str], str], send: HttpSend,
                       refresh_id: str | None = None,
                       checkpoint: Callable[[dict[str, Any]], None] | None = None,
                       timeout_seconds: float = 1200,
                       sleep: Callable[[float], None] = time.sleep,
                       clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Submit ONE bank-root notebook job and validate its actual committed manifest.

    No script imports, graph call or filesystem persistence. Updates/readbacks only
    the owned finance DataAgent's selected metadata after actual columns exist.
    On errors inspect ProjectionRefreshError.progress; a committed ledger transfer
    must never be rolled back/repeated because this analytical operation failed.
    """
    envelope = json.loads(json.dumps(envelope))  # Freeze the capture before callbacks can run.
    tables = project_snapshot(envelope, config.expected_source_policy)
    text(expected_projection_etag, "expected_projection_etag")
    payload = json.dumps(envelope, separators=(",", ":"))
    if len(payload.encode()) > 65536:
        raise ValueError("Inline projection capture exceeds 64 KiB")
    run_id = refresh_id or str(uuid.uuid4())
    text(run_id, "refresh_id")
    progress = {"refresh_id": run_id, "source_etag": envelope["source_etag"],
                "job_url": None, "live_refresh_executed": False, "graph_refresh_pending": True,
                "metadata_sync_pending": True}
    client = _Client(config, credential_kind, get_token, send, timeout_seconds, sleep, clock)
    base = f"{FABRIC}/v1/workspaces/{config.workspace_id}/items/{config.notebook_id}/jobs"
    def record():
        if checkpoint:
            checkpoint(dict(progress))
    try:
        url = base + "/instances"
        while url:
            page = client.json("get", url)
            if any(job["status"] not in ("Completed", "Failed", "Cancelled", "Canceled")
                   for job in page["value"]):
                raise RuntimeError("Projection job already active; refusing another submission")
            url = page.get("continuationUri")
            if page.get("continuationToken") and not url:
                raise RuntimeError("Incomplete job history; refusing submission")
        body = {"parameters": [
            {"name": name, "value": value, "type": "Text"}
            for name, value in (
                ("snapshot_json", payload),
                ("expected_projection_etag", expected_projection_etag),
                ("refresh_id", run_id),
            )
        ]}
        progress["submission_attempted"] = True
        record()
        response = client.raw(
            "post",
            f"{FABRIC}/v1/workspaces/{config.workspace_id}"
            f"/notebooks/{config.notebook_id}/jobs/execute/instances?beta=false",
            body,
        )
        if response.status != 202:
            raise RuntimeError(f"Notebook submission returned HTTP {response.status}; do not retry blindly")
        progress["live_refresh_executed"] = True
        progress["job_url"] = client.job_url(response.headers.get("location", ""))
        record()  # Persist accepted Location before any poll/sleep.
        client.wait(max(0, float(response.headers.get("retry-after", "5"))))
        while True:
            job = client.json("get", progress["job_url"])
            progress["job_status"] = job["status"]
            record()
            if job["status"] == "Completed":
                break
            if job["status"] in ("Failed", "Cancelled", "Canceled"):
                raise RuntimeError("Projection notebook failed; inspect saved job")
            client.wait(15)
        manifest_response = client.raw("get", config.manifest_url)
        manifest = manifest_response.body
        digest = hashlib.sha256(json.dumps(
            envelope["state"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if manifest_response.status != 200 or not isinstance(manifest, dict):
            raise RuntimeError("Completed notebook has no readable committed manifest")
        assert_projection_current(manifest, current_source_etag=envelope["source_etag"],
                                  live_projection_status=manifest)
        counts = {"transaction_count": len(tables["transfers"]),
                  "approval_count": len(tables["approval_requests"]),
                  "notification_count": len(tables["notification_deliveries"])}
        if manifest["snapshot_sha256"] != digest or any(manifest[k] != v for k, v in counts.items()):
            raise RuntimeError("Committed projection hash/counts differ from authoritative capture")
        progress["committed_manifest"] = manifest
        progress["projection_freshness"] = {
            "captured_at": manifest["captured_at"], "source_etag": manifest["source_etag"],
            "ledger_version": manifest["ledger_version"], "ledger_status": manifest["ledger_status"],
            "authoritative": False, "projection_state": "complete", "graph_refresh_pending": True,
            "notice": "Captured snapshot; compare ETag with current ledger; never assume graph freshness"}
        record()
        _sync_metadata(client)
        progress["metadata_sync_pending"] = False
        record()
        return progress
    except Exception as exc:
        # No arbitrary transport response/body/token/ledger data in exception text.
        safe = str(exc) if isinstance(exc, (ValueError, PermissionError, TimeoutError)) else (
            "Projection refresh failed; inspect accepted job and committed_manifest in progress")
        raise ProjectionRefreshError(safe, progress) from None
