import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import jwt
import httpx
from microsoft_agents.activity.channel_id import ChannelId

from hotel_payroll_treasurer_agents.ledger import (
    AgentRole, Decision, DomainError, Ledger, LedgerConfig, Snapshot, SQLiteStateStore,
)
from hotel_payroll_treasurer_agents.communications import DeliveryRejected, DeliveryUncertain
from hotel_payroll_treasurer_agents.demo_runs import DemoRuns
from hotel_payroll_treasurer_agents.mcp_client import McpToolError
from hotel_payroll_treasurer_agents.peer_routes import PeerRoutes
from hotel_payroll_treasurer_agents.runtime import (
    APPROVAL_EXECUTE_MESSAGE, FABRIC_SCOPE, M365_SCOPE, PEER_TASK_REFERENCE,
    FinanceRuntime, approval_context_reference,
    authenticated_decision, authenticated_email_sender, execute_approved_request,
    execute_pending_approvals, parse_decision,
    cited_policy_result, email_reply_text, https_endpoint, own_token_claims, require_service_success,
    currency_units_valid, display_money, human_message_style_issue, ledger_money,
    parse_peer_wire_message, peer_wire_message, prohibited_effects,
    requests_peer_check,
)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = SQLiteStateStore(Path(self.directory.name) / "state.db")

    def test_runtime_names_preserve_internal_role_bindings(self):
        environment = {
            "FOUNDRY_AGENT_DEFAULT_INSTANCE_CLIENT_ID": "root-client",
            "FINANCE_BINDINGS_URL": "https://storage.blob.core.windows.net/bindings/runtime.json",
            "FINANCE_MODEL_ENDPOINT": "https://finance.openai.azure.com",
        }
        with patch.dict("os.environ", environment), patch(
            "hotel_payroll_treasurer_agents.runtime.ManagedIdentityCredential"
        ), patch("hotel_payroll_treasurer_agents.runtime.httpx.AsyncClient"), patch(
            "hotel_payroll_treasurer_agents.runtime.BlobStateStore.from_url"
        ):
            for role, name in (
                ("hotel_finance", "Hotel Payroll"),
                ("banking_operations", "Hotel Treasurer"),
            ):
                with self.subTest(role=role):
                    runtime = FinanceRuntime(role, "tenant", Mock())
                    self.assertEqual(runtime.display_name, name)
                    self.assertEqual(runtime.role.value, role)

    def test_fabric_application_tokens_reject_delegated_or_wrong_identity_claims(self):
        runtime = FinanceRuntime.__new__(FinanceRuntime)
        runtime.tenant = "tenant"
        claims = {
            "tid": "tenant", "oid": "hired-principal", "appid": "hired-app",
            "aud": FABRIC_SCOPE.removesuffix("/.default"), "idtyp": "app",
        }
        signing_key = "unit-test-only-not-a-real-credential"
        token = jwt.encode(claims, signing_key, algorithm="HS256")
        self.assertEqual(
            runtime._validate_application_token(token, "hired-app", "hired-principal"), token,
        )
        for override in (
            {"tid": "other-tenant"}, {"oid": "other-principal"}, {"appid": "root-app"},
            {"aud": "https://graph.microsoft.com"}, {"idtyp": "user"},
            {"scp": "user_impersonation"},
        ):
            with self.subTest(override=override), self.assertLogs(
                "hotel_payroll_treasurer_agents.runtime", level="ERROR"
            ), self.assertRaises(PermissionError):
                runtime._validate_application_token(
                    jwt.encode({**claims, **override}, signing_key, algorithm="HS256"),
                    "hired-app", "hired-principal",
                )

    def test_peer_references_support_independent_activity_threads_without_arbitrary_routes(self):
        for reference in (
            "payroll-funding", "PAYROLL-0123456789ABCDEF",
            "ACTIVITY-0123456789ABCDEF0123456789ABCDEF",
        ):
            with self.subTest(reference=reference):
                self.assertIsNotNone(PEER_TASK_REFERENCE.fullmatch(reference))
        for reference in (
            "", "arbitrary-task", "ACTIVITY-0123456789ABCDEF", "PAYROLL-0123456789ABCDEG",
            "ACTIVITY-" + "a" * 32, "ACTIVITY-" + "A" * 32 + "/teams/1",
        ):
            with self.subTest(reference=reference):
                self.assertIsNone(PEER_TASK_REFERENCE.fullmatch(reference))

    def test_explicit_original_request_prohibitions_map_to_effect_tools(self):
        self.assertEqual(
            prohibited_effects(
                "Do not create approvals, transfer money, refresh analytics, "
                "notify humans, or allow peer contact."
            ),
            {"send_peer", "request_payroll_approval", "refresh_analytics"},
        )
        self.assertEqual(
            prohibited_effects("Proceed after approval and contact the peer."),
            set(),
        )
        for name in ("Hotel Payroll", "Hotel Treasurer", "Hotel Finance", "Banking Operations"):
            with self.subTest(name=name):
                self.assertEqual(prohibited_effects(f"Do not contact {name}."), {"send_peer"})

    def test_explicit_peer_checks_cannot_be_satisfied_by_own_balances_alone(self):
        for name in ("Hotel Payroll", "Hotel Treasurer"):
            self.assertTrue(requests_peer_check(
                f"Please check payroll funding with {name} and report back. "
                "Do not request approval or move money.", name,
            ))
            self.assertFalse(requests_peer_check("Please check the payroll balance.", name))
            self.assertFalse(requests_peer_check(
                f"Please check payroll funding with {name} by email.", name,
            ))

    def test_approval_handoffs_accept_current_and_legacy_payroll_names(self):
        reference = "PAYROLL-0123456789ABCDEF"
        for name in ("Hotel Payroll", "Hotel Finance"):
            with self.subTest(name=name):
                match = APPROVAL_EXECUTE_MESSAGE.fullmatch(
                    f"{name} approved payroll request {reference}. Please complete the approved transfer."
                )
                self.assertIsNotNone(match)
                self.assertEqual(match.group(1), reference)
        self.assertIsNone(APPROVAL_EXECUTE_MESSAGE.fullmatch(
            f"Hotel Treasurer approved payroll request {reference}. Please complete the approved transfer."
        ))

    def test_human_messages_hide_internal_verification_plumbing(self):
        self.assertIsNone(human_message_style_issue(
            "Payroll is funded. The payroll account has $25,000 and the reserve has $35,000."
        ))
        for message in (
            "Work IQ verified the email.",
            "Foundry policy requires approval.",
            'Source ETag: "0x123"',
            "The projection freshness_verified field is true.",
        ):
            with self.subTest(message=message):
                self.assertEqual(
                    human_message_style_issue(message),
                    "contains internal implementation details",
                )
        prohibited = "SIMU" + "LATED FINANCIAL ACTION ONLY."
        self.assertEqual(
            human_message_style_issue(prohibited),
            "contains prohibited presentation wording",
        )
        self.assertEqual(
            human_message_style_issue("/" + "approve PAYROLL-0123456789ABCDEF"),
            "contains command-style approval wording",
        )
        for message in (
            "Banking Operations, please confirm payroll funding.",
            "Hotel Finance: Banking confirms the payroll funding request.",
            "hotel finance autopilot has completed the request.",
        ):
            with self.subTest(message=message):
                self.assertEqual(human_message_style_issue(message), "uses an obsolete agent name")
                with self.assertRaises(ValueError):
                    peer_wire_message(message, "payroll-funding", 1)
        self.assertIsNone(human_message_style_issue("Hotel Treasurer, please confirm funding for Hotel Payroll."))

    def test_peer_wire_format_is_natural_and_legacy_messages_remain_readable(self):
        reference = "ACTIVITY-0123456789ABCDEF0123456789ABCDEF"
        wire = peer_wire_message("Please confirm the current payroll balance.", reference, 2)
        self.assertEqual(
            wire,
            "Please confirm the current payroll balance.",
        )
        self.assertIsNone(parse_peer_wire_message(wire, AgentRole.BANKING_OPERATIONS.value))
        self.assertEqual(
            parse_peer_wire_message(
                f"{wire}\n\nReference: {reference}/2", AgentRole.BANKING_OPERATIONS.value,
            ),
            ("Please confirm the current payroll balance.", reference, 2),
        )
        legacy = "FINANCE-PEER/1\n" + json.dumps({
            "sender_role": AgentRole.BANKING_OPERATIONS.value,
            "hop": 2,
            "task_reference": reference,
            "message": "Please confirm the current payroll balance.",
        })
        self.assertEqual(
            parse_peer_wire_message(legacy, AgentRole.BANKING_OPERATIONS.value),
            ("Please confirm the current payroll balance.", reference, 2),
        )

    def test_internal_references_are_rejected_in_generated_human_messages(self):
        for text in (
            "Reference: ACTIVITY-663AEC249F86945F0E201AD2B686A173/2",
            "Please approve PAYROLL-0123456789ABCDEF.",
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    human_message_style_issue(text), "contains an internal routing reference",
                )
                with self.assertRaises(ValueError):
                    peer_wire_message(text, "payroll-funding", 1)

    def test_task_deduplication_binds_exact_content(self):
        first = FinanceRuntime._claim_turn(self.store, "activity", "hash-one")
        self.assertEqual(first["status"], "running")
        duplicate = FinanceRuntime._claim_turn(
            self.store, "activity", "hash-one", allow_running_duplicate=True,
        )
        self.assertTrue(duplicate["duplicate_running"])
        self.assertEqual(self.store.read().state["turns"]["activity"], first)
        with self.assertRaises(RuntimeError):
            FinanceRuntime._claim_turn(self.store, "activity", "hash-one")
        with self.assertRaises(PermissionError):
            FinanceRuntime._claim_turn(self.store, "activity", "hash-two")
        FinanceRuntime._complete_turn(self.store, "activity", "hash-one", "actual answer")
        self.assertEqual(
            FinanceRuntime._claim_turn(self.store, "activity", "hash-one")["answer"],
            "actual answer",
        )

    def test_uncited_policy_is_an_explicit_query_failure_not_an_activity_crash(self):
        for result in (
            {}, {"response": ["unsupported answer"], "references": []},
            {"response": [], "references": [{"id": "policy"}]},
            {"response": ["answer"], "references": "not-citations"},
        ):
            with self.subTest(result=result), self.assertRaises(DomainError) as failure:
                cited_policy_result(result)
            self.assertEqual(failure.exception.code, "IQ_POLICY_UNAVAILABLE")
        result = {"response": [{"content": [{"text": "Actual policy"}]}],
                  "references": [{"id": "policy"}]}
        self.assertIs(cited_policy_result(result), result)

    def test_authenticated_approval_executes_exactly_once(self):
        ledger = Ledger(self.store, LedgerConfig("tenant", ("marco", "ayca"), "hotel"))
        reference = "PAYROLL-0123456789ABCDEF"
        ledger.request_approval(
            ledger.payroll_funding_action(), request_id=reference, idempotency_key=reference
        )
        with self.assertRaises(DomainError):
            authenticated_decision(ledger, "tenant", "peer", f"approve {reference}")
        with self.assertRaises(DomainError):
            authenticated_decision(ledger, "other-tenant", "marco", f"approve {reference}")
        result = authenticated_decision(ledger, "tenant", "marco", f"approved {reference}")
        version = self.store.read().version
        retry = authenticated_decision(ledger, "tenant", "marco", f"yes {reference}")
        self.assertEqual(result["transaction"], retry["transaction"])
        self.assertEqual(self.store.read().version, version)
        state = ledger.snapshot()
        self.assertEqual(len(state["transactions"]), 1)
        self.assertEqual(state["accounts"]["hotel-payroll"]["balance_cents"], 2_500_000)
        self.assertEqual(state["accounts"]["hotel-reserve"]["balance_cents"], 3_500_000)
        with self.assertRaises(DomainError):
            authenticated_decision(ledger, "tenant", "ayca", f"no {reference}")

    def test_integer_cents_are_rendered_exactly_and_not_accepted_as_usd(self):
        ledger = Ledger(self.store, LedgerConfig("tenant", ("marco", "ayca"), "hotel"))
        reference = "PAYROLL-0123456789ABCDEF"
        ledger.request_approval(
            ledger.payroll_funding_action(), request_id=reference, idempotency_key=reference,
        )
        authenticated_decision(ledger, "tenant", "marco", "go ahead " + reference)
        state = ledger.snapshot()
        self.assertEqual(ledger_money(state), {
            "account_balances": {
                "hotel-reserve": "USD 35,000.00", "hotel-payroll": "USD 25,000.00",
            },
            "payroll_obligation": "USD 25,000.00",
            "approval_threshold": "USD 10,000.00",
            "transfer_amounts": {"tx-1": "USD 15,000.00"},
        })
        self.assertTrue(currency_units_valid("Transferred USD 15,000.00; payroll $25,000.", state))
        for answer in ("USD 1,500,000", "$2,500,000.00", "Reserve: USD 3500000"):
            with self.subTest(answer=answer):
                self.assertFalse(currency_units_valid(answer, state))
        self.assertEqual(display_money(10**20 + 1), "USD 1,000,000,000,000,000,000.01")
        for cents in (True, -1, 12.5):
            with self.subTest(cents=cents), self.assertRaises(ValueError):
                display_money(cents)

    def test_native_session_and_safe_failure_are_preserved_without_replay(self):
        FinanceRuntime._claim_turn(
            self.store, "activity", "hash", session_id="native-session",
            channel_id=ChannelId(channel="msteams", sub_channel="email"),
        )
        FinanceRuntime._record_turn_failure(self.store, "activity", "hash", "ReadTimeout")
        entry = self.store.read().state["turns"]["activity"]
        self.assertEqual(entry["session_id"], "native-session")
        self.assertEqual(entry["channel_id"], "msteams:email")
        self.assertEqual(entry["failure_type"], "ReadTimeout")
        self.assertEqual(entry["status"], "running")
        with self.assertRaises(RuntimeError):
            FinanceRuntime._claim_turn(self.store, "activity", "hash")
        with self.assertRaises(RuntimeError):
            FinanceRuntime._record_turn_failure(self.store, "activity", "other-hash", "ReadTimeout")
        with self.assertRaises(ValueError):
            FinanceRuntime._claim_turn(self.store, "invalid-channel", "hash", channel_id="bad/channel")
        for value in ("Bearer SECRET", "\nReadTimeout", ""):
            with self.assertRaises(ValueError):
                FinanceRuntime._record_turn_failure(self.store, "activity", "hash", value)
            with self.assertRaises(ValueError):
                FinanceRuntime._claim_turn(self.store, "new", "hash", session_id=value)

    def test_natural_decisions_are_context_bound_and_ambiguous_text_is_ignored(self):
        ledger = Ledger(self.store, LedgerConfig("tenant", ("marco", "ayca"), "hotel"))
        reference = "PAYROLL-0123456789ABCDEF"
        ledger.request_approval(
            ledger.payroll_funding_action(), request_id=reference, idempotency_key=reference
        )
        self.assertIsNone(parse_decision(ledger, "maybe approve"))
        self.assertIsNone(parse_decision(ledger, "yes"))
        result = authenticated_decision(
            ledger, "tenant", "marco", "no", context_reference=reference,
        )
        self.assertNotIn("transaction", result)
        self.assertEqual(ledger.snapshot()["transactions"], [])

    def test_hotel_decision_records_approval_before_banking_executes(self):
        ledger = Ledger(self.store, LedgerConfig("tenant", ("marco", "ayca"), "hotel"))
        reference = "PAYROLL-0123456789ABCDEF"
        ledger.request_approval(
            ledger.payroll_funding_action(), request_id=reference, idempotency_key=reference
        )
        result = authenticated_decision(
            ledger, "tenant", "marco", "go", execute=False, context_reference=reference,
        )
        self.assertEqual(result["request"]["status"], "approved")
        self.assertEqual(ledger.snapshot()["transactions"], [])
        transaction = execute_approved_request(ledger, reference)
        self.assertEqual(transaction["transaction_id"], "tx-1")
        self.assertEqual(execute_approved_request(ledger, reference), transaction)

    def test_natural_approval_vocabulary_remains_request_bound(self):
        phrases = (
            "approve", "approved", "yes", "yes, go ahead", "go", "go ahead",
            "go for it", "please do", "absolutely", "confirmed", "I approve it",
            "looks good", "sounds good", "that's fine", "that works",
        )
        for index, phrase in enumerate(phrases):
            with self.subTest(phrase=phrase):
                store = SQLiteStateStore(Path(self.directory.name) / f"phrase-{index}.db")
                ledger = Ledger(store, LedgerConfig("tenant", ("marco", "ayca"), "hotel"))
                reference = f"PAYROLL-{index:016X}"
                ledger.request_approval(
                    ledger.payroll_funding_action(),
                    request_id=reference,
                    idempotency_key=reference,
                )
                parsed = parse_decision(ledger, phrase, context_reference=reference)
                self.assertEqual(parsed, (reference, Decision.APPROVE))

    def test_banking_worker_executes_durable_approved_request_once(self):
        ledger = Ledger(self.store, LedgerConfig("tenant", ("marco", "ayca"), "hotel"))
        reference = "PAYROLL-0123456789ABCDEF"
        ledger.request_approval(
            ledger.payroll_funding_action(), request_id=reference, idempotency_key=reference
        )
        authenticated_decision(
            ledger, "tenant", "marco", "yes", execute=False, context_reference=reference,
        )
        completed = execute_pending_approvals(ledger)
        self.assertEqual([item["transaction_id"] for item in completed], ["tx-1"])
        self.assertEqual(execute_pending_approvals(ledger), [])

    def test_quoted_email_history_is_not_a_decision(self):
        reference = "PAYROLL-0123456789ABCDEF"
        self.assertEqual(
            email_reply_text(f"<div>approved</div><blockquote>denied {reference}</blockquote>"),
            "approved",
        )
        self.assertEqual(
            email_reply_text(f"<div>Need clarification.</div><div id='divRplyFwdMsg'>approved {reference}</div>"),
            "Need clarification.",
        )
        self.assertEqual(email_reply_text(f"<blockquote>approved {reference}</blockquote>"), "")
        self.assertEqual(email_reply_text(f"<script>approved {reference}</script>"), "")

    def test_approval_context_resolves_from_email_subject_or_recorded_group_chat(self):
        ledger = Ledger(self.store, LedgerConfig("tenant", ("marco", "ayca"), "hotel"))
        reference = "PAYROLL-0123456789ABCDEF"
        ledger.request_approval(
            ledger.payroll_funding_action(), request_id=reference, idempotency_key=reference
        )
        teams_id = next(
            key for key, value in ledger.snapshot()["outbox"].items()
            if value["channel"] == "teams"
        )
        ledger.claim_delivery(teams_id, attempt_id="attempt")
        ledger.record_delivery_result(
            teams_id,
            attempt_id="attempt",
            succeeded=True,
            provider_result={"chatId": "19:approval-group"},
        )
        self.assertEqual(
            approval_context_reference(ledger, email_subject=f"Re: Payroll approval {reference}"),
            reference,
        )
        self.assertEqual(
            approval_context_reference(ledger, conversation_id="19:approval-group"),
            reference,
        )

    def test_service_endpoints_are_host_bound(self):
        self.assertEqual(
            https_endpoint("https://api.fabric.microsoft.com/v1/mcp", "api.fabric.microsoft.com"),
            "https://api.fabric.microsoft.com/v1/mcp",
        )
        for endpoint in (
            "https://evilapi.fabric.microsoft.com/v1/mcp",
            "https://api.fabric.microsoft.com.evil.example/v1/mcp",
            "https://user@api.fabric.microsoft.com/v1/mcp",
            "http://api.fabric.microsoft.com/v1/mcp",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                https_endpoint(endpoint, "api.fabric.microsoft.com")

    def test_sdk_returned_token_must_match_own_recipient(self):
        token = jwt.encode({"tid": "tenant", "oid": "agent-user"}, "", algorithm="none")
        own_token_claims(token, "tenant", "agent-user")
        with self.assertRaises(PermissionError):
            own_token_claims(token, "tenant", "human-user")
        with self.assertRaises(PermissionError):
            own_token_claims(token, "other-tenant", "agent-user")

    def test_email_identity_requires_original_mailbox_provenance(self):
        message = {
            "isDraft": False,
            "from": {"emailAddress": {"address": "marco@example.test"}},
            "sender": {"emailAddress": {"address": "marco@example.test"}},
            "internetMessageHeaders": [
                {"name": "X-MS-Exchange-Organization-AuthAs", "value": "Internal"},
                {"name": "X-MS-Exchange-CrossTenant-id", "value": "tenant"},
            ],
        }
        people = [{"object_id": "marco-oid", "mail": "marco@example.test"}]
        self.assertEqual(authenticated_email_sender(message, "tenant", people), "marco-oid")
        for replacement in (
            {"isDraft": True},
            {"internetMessageHeaders": []},
            {"internetMessageHeaders": message["internetMessageHeaders"] + [
                {"name": "X-MS-Exchange-CrossTenant-id", "value": "other-tenant"},
            ]},
            {"internetMessageHeaders": [
                {"name": "X-MS-Exchange-Organization-AuthAs", "value": "Anonymous"},
                {"name": "X-MS-Exchange-CrossTenant-id", "value": "tenant"},
            ]},
            {"internetMessageHeaders": [
                {"name": "X-MS-Exchange-Organization-AuthAs", "value": "Internal"},
                {"name": "X-MS-Exchange-CrossTenant-id", "value": "other-tenant"},
            ]},
            {"sender": {"emailAddress": {"address": "attacker@example.test"}}},
            {"from": {"emailAddress": {"address": "attacker@example.test"}}},
        ):
            with self.subTest(replacement=replacement), self.assertRaises(PermissionError):
                authenticated_email_sender({**message, **replacement}, "tenant", people)
        with self.assertRaises(PermissionError):
            authenticated_email_sender(message, "tenant", people + people)

    def test_large_tool_results_are_explicit_failures_not_truncated_json(self):
        self.assertEqual(json.loads(FinanceRuntime._tool_output({"actual": True})), {"actual": True})
        with self.assertRaises(RuntimeError):
            FinanceRuntime._tool_output({"content": "x" * 60_001})

    def test_service_errors_log_only_safe_code_and_required_action(self):
        response = httpx.Response(403, request=httpx.Request("POST", "https://example.test"), json={
            "error": {
                "code": "AuthorizationFailed",
                "message": "SECRET_BODY Caller lacks Microsoft.CognitiveServices/accounts/AIServices/responses/write",
            },
        })
        with self.assertLogs("hotel_payroll_treasurer_agents.runtime", level="ERROR") as logs:
            with self.assertRaises(httpx.HTTPStatusError):
                require_service_success(response, "Foundry model")
        output = "\n".join(logs.output)
        self.assertIn("AuthorizationFailed", output)
        self.assertIn("Microsoft.CognitiveServices/accounts/AIServices/responses/write", output)
        self.assertNotIn("SECRET_BODY", output)

    def test_non_json_service_failure_is_not_hidden(self):
        response = httpx.Response(
            500, request=httpx.Request("POST", "https://example.test"), text="SECRET_BODY",
        )
        with self.assertLogs("hotel_payroll_treasurer_agents.runtime", level="ERROR") as logs:
            with self.assertRaises(httpx.HTTPStatusError):
                require_service_success(response, "Foundry model")
        self.assertNotIn("SECRET_BODY", "\n".join(logs.output))


class RuntimeIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_only_peer_turn_uses_bound_demo_even_after_active_run_changes(self):
        for role in AgentRole:
            with self.subTest(role=role), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                registry_store = SQLiteStateStore(root / "runs.db")
                registry = DemoRuns(registry_store)
                config = LedgerConfig("tenant", ("marco", "ayca"), "hotel")
                first = "ACTIVITY-" + "A" * 32
                second = "ACTIVITY-" + "B" * 32
                Ledger(registry.start(first), config)
                routes = PeerRoutes(SQLiteStateStore(root / "routes.db"), "tenant")
                base = SQLiteStateStore(root / "base.db")
                journal = SQLiteStateStore(root / "tasks.db")
                own = {
                    "instance_app_id": "own-app",
                    "agent_user_object_id": "00000000-0000-0000-0000-000000000001",
                    "task_blob_url": "tasks", "mail": "own@example.test",
                }
                peer = {
                    "agent_user_object_id": "peer-user", "mail": "peer@example.test",
                }
                peer_role = (
                    AgentRole.BANKING_OPERATIONS if role is AgentRole.HOTEL_FINANCE
                    else AgentRole.HOTEL_FINANCE
                )
                bindings = {
                    "instances": {peer_role.value: peer},
                    "approvers": [{"object_id": "marco"}, {"object_id": "ayca"}],
                    "hotel_owner_id": "hotel", "ledger_blob_url": "base",
                }
                activity = SimpleNamespace(
                    from_property=SimpleNamespace(aad_object_id="marco"),
                    text="Please check payroll funding with "
                         + ("Hotel Treasurer" if role is AgentRole.HOTEL_FINANCE else "Hotel Payroll")
                         + ". Do not request approval or move money.",
                    id="human-message", conversation=SimpleNamespace(id="human-chat"),
                    channel_id="msteams",
                )
                runtime = FinanceRuntime.__new__(FinanceRuntime)
                runtime.role, runtime.tenant, runtime.credential, runtime.http = role, "tenant", None, None
                runtime._bindings = AsyncMock(return_value=(bindings, own))
                runtime._agent_token = AsyncMock(return_value="own-token")
                runtime._demo_runs = Mock(return_value=registry)
                runtime._peer_routes = Mock(return_value=routes)
                runtime._orchestrate = AsyncMock(return_value="Checked payroll.")
                environment = {
                    "FINANCE_DEMO_REPLAY": "true", "FINANCE_WORKFLOW_MODE": "collaboration",
                    "FINANCE_PAYROLL_TEAMS_CHAT_ID": "19:payroll@thread.v2",
                }
                def store_for(url, credential):
                    return journal if url == "tasks" else base

                with patch.dict("os.environ", environment), patch(
                    "hotel_payroll_treasurer_agents.runtime.BlobStateStore.from_url", side_effect=store_for,
                ):
                    before = registry_store.read()
                    await runtime.turn(SimpleNamespace(activity=activity), None)
                    human_args = runtime._orchestrate.await_args
                    reference = human_args.args[11]
                    self.assertEqual(routes.demo_run(reference), first)
                    self.assertEqual(registry_store.read(), before)
                    Ledger(registry.start(second), config)
                    before = registry_store.read()
                    activity.from_property.aad_object_id = "peer-user"
                    activity.id = "peer-message"
                    activity.text = "Please confirm payroll funding."
                    runtime._teams_peer_route = AsyncMock(return_value=(reference, 1))
                    self.assertIsNone(await runtime.turn(SimpleNamespace(activity=activity), None))
                    peer_args = runtime._orchestrate.await_args
                    self.assertEqual(peer_args.args[5].run_id, first)
                    self.assertEqual(peer_args.kwargs["demo_run_id"], first)
                    self.assertEqual(registry.current().run_id, second)
                    self.assertEqual(registry_store.read(), before)
                    self.assertIsNone(base.read().state)

    async def test_both_roles_must_send_the_requested_check_and_first_peer_reply(self):
        for role in AgentRole:
            for hop in (0, 1):
                with self.subTest(role=role, hop=hop), tempfile.TemporaryDirectory() as directory:
                    store = SQLiteStateStore(Path(directory) / "ledger.db")
                    ledger = Ledger(store, LedgerConfig("tenant", ("marco", "ayca"), "hotel"))
                    runtime = FinanceRuntime.__new__(FinanceRuntime)
                    runtime.role = role
                    runtime.display_name = (
                        "Hotel Payroll" if role is AgentRole.HOTEL_FINANCE else "Hotel Treasurer"
                    )
                    peer_name = (
                        "Hotel Treasurer" if role is AgentRole.HOTEL_FINANCE else "Hotel Payroll"
                    )
                    runtime.model_endpoint = "https://finance.openai.azure.com"
                    runtime._managed_token = AsyncMock(return_value="root-token")
                    runtime._send_peer = AsyncMock(return_value={"accepted": True})
                    report = {"type": "message", "content": [
                        {"type": "output_text", "text": "Payroll checked."},
                    ]}
                    responses = [
                        [{"type": "function_call", "name": "foundry_iq_policies",
                          "arguments": '{"question":"Payroll policy"}', "call_id": "policy"},
                         {"type": "function_call", "name": "ledger_status",
                          "arguments": "{}", "call_id": "ledger"}],
                        [report],
                        [{"type": "function_call", "name": "send_peer", "arguments": json.dumps({
                            "channel": "teams", "message": f"{peer_name}, payroll checked.",
                        }), "call_id": "peer"}],
                        [report],
                    ]
                    requests = []

                    def respond(request):
                        if request.url.host.endswith(".search.windows.net"):
                            return httpx.Response(200, json={
                                "response": ["Policy"], "references": [{"id": "policy"}],
                            })
                        requests.append(json.loads(request.content))
                        return httpx.Response(200, json={"output": responses.pop(0)})

                    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
                        runtime.http = http
                        with patch.dict("os.environ", {"FINANCE_WORKFLOW_MODE": "collaboration"}):
                            answer = await runtime._orchestrate(
                                f"Please check payroll funding with {peer_name}. "
                                "Do not request approval or move money.",
                                None, None, {
                                    "foundry_iq": {"endpoint": "https://finance.search.windows.net",
                                                   "knowledge_base": "policies", "api_version": "v1"},
                                    "foundry": {"model_deployment": "model"},
                                }, {}, store, ledger, None, None, {}, hop, "payroll-funding",
                            )
                    self.assertEqual(answer, "Payroll checked.")
                    self.assertEqual(len(requests), 4)
                    runtime._send_peer.assert_awaited_once()
                    self.assertEqual(runtime._send_peer.await_args.args[-1], hop + 1)
                    self.assertIn("Use send_peer", requests[2]["input"][-1]["content"])
                    self.assertEqual(ledger.snapshot()["transactions"], [])
                    self.assertEqual(ledger.snapshot()["approvals"], {})

    async def test_native_peer_lookup_waits_for_the_accepted_send_checkpoint(self):
        runtime = FinanceRuntime.__new__(FinanceRuntime)
        route = ("ACTIVITY-" + "A" * 32, 2)
        routes = Mock()
        routes.resolve.side_effect = [None, route]
        runtime._peer_routes = Mock(return_value=routes)
        activity = SimpleNamespace(
            id="native-message", conversation=SimpleNamespace(id="native-chat"),
        )
        with patch("hotel_payroll_treasurer_agents.runtime.asyncio.sleep", new_callable=AsyncMock) as sleep:
            result = await runtime._teams_peer_route(
                {}, {"agent_user_object_id": "recipient"}, "sender", activity, "Natural text",
            )
        self.assertEqual(result, route)
        self.assertEqual(routes.resolve.call_count, 2)
        routes.resolve.assert_called_with(
            "sender", "recipient", "native-chat", "native-message", "Natural text",
        )
        sleep.assert_awaited_once_with(0.25)

    async def test_route_checkpoint_failure_never_marks_or_resends_a_peer_delivery(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        journal = SQLiteStateStore(Path(directory.name) / "journal.db")
        runtime = FinanceRuntime.__new__(FinanceRuntime)
        runtime.role = AgentRole.HOTEL_FINANCE
        messenger = Mock()
        messenger.send = AsyncMock(side_effect=RuntimeError("Could not checkpoint accepted send"))
        peer = {"agent_user_object_id": "recipient", "mail": "peer@example.test"}
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                await runtime._send_peer(
                    messenger, journal, peer, "teams",
                    "Hotel Treasurer, please confirm funding.", "payroll-funding", 1,
                )
        messenger.send.assert_awaited_once_with(
            "teams", {"object_id": "recipient", "mail": "peer@example.test"},
            "Hotel Treasurer, please confirm funding.", "payroll-funding", peer_hop=1,
        )
        entry = next(iter(journal.read().state["turns"].values()))
        self.assertEqual(entry["status"], "running")

    def analytical_proof(self, state=None):
        if state is None:
            with tempfile.TemporaryDirectory() as directory:
                state = Ledger(
                    SQLiteStateStore(Path(directory) / "ledger.db"),
                    LedgerConfig("tenant", ("marco", "ayca"), "hotel"),
                ).snapshot()
        return {"freshness_verified": True, "ontology_freshness_verified": False,
                "source_etag": "actual-etag", "delta_versions": {"projection_status": 5},
                "_source_state": state}

    async def test_treasurer_sends_one_shared_teams_approval_and_one_shared_email(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        ledger = Ledger(
            SQLiteStateStore(Path(directory.name) / "ledger.db"),
            LedgerConfig("tenant", ("marco", "ayca"), "hotel"),
        )
        reference = "PAYROLL-0123456789ABCDEF"
        request = ledger.request_approval(
            ledger.payroll_funding_action(),
            request_id=reference,
            idempotency_key=reference,
        )
        runtime = FinanceRuntime.__new__(FinanceRuntime)
        runtime.role = AgentRole.BANKING_OPERATIONS
        messenger = Mock()
        messenger.send_teams_group = AsyncMock(return_value={
            "accepted": True,
            "channel": "teams",
            "chatType": "group",
            "chatId": "19:approval-group",
            "messageId": "teams-message",
            "reference": reference,
        })
        messenger.send_approval_email = AsyncMock(return_value={
            "accepted": True,
            "channel": "email",
            "reference": reference,
            "inboxDeliveryVerified": False,
        })
        bindings = {"approvers": [
            {"object_id": "marco", "mail": "marco@example.test"},
            {"object_id": "ayca", "mail": "ayca@example.test"},
        ]}

        results = await runtime._notify_approvers(ledger, messenger, bindings, request)

        messenger.send_teams_group.assert_awaited_once()
        messenger.send_approval_email.assert_awaited_once_with(
            bindings["approvers"], messenger.send_teams_group.await_args.args[1], reference,
        )
        notification = messenger.send_teams_group.await_args.args[1]
        self.assertIn("Can one of you approve", notification)
        self.assertNotIn("/", notification)
        self.assertNotRegex(notification, r"(?i)financial action only")
        state = ledger.snapshot()
        self.assertTrue(all(item["status"] == "succeeded" for item in state["outbox"].values()))
        teams = [item for item in state["outbox"].values() if item["channel"] == "teams"]
        self.assertEqual(len(teams), 2)
        self.assertTrue(all(
            item["last_result"]["provider_result"]["chatId"] == "19:approval-group"
            for item in teams
        ))
        self.assertEqual(len(results), 4)
        self.assertEqual(state["transactions"], [])
        again = await runtime._notify_approvers(ledger, messenger, bindings, request)
        self.assertTrue(all(result["alreadyAccepted"] for result in again))
        messenger.send_teams_group.assert_awaited_once()
        messenger.send_approval_email.assert_awaited_once()

    async def test_shared_approval_email_preserves_rejection_uncertainty_and_partial_delivery(self):
        for outcome in ("rejected", "uncertain", "partial"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                ledger = Ledger(
                    SQLiteStateStore(Path(directory) / "ledger.db"),
                    LedgerConfig("tenant", ("marco", "ayca"), "hotel"),
                )
                reference = "PAYROLL-0123456789ABCDEF"
                request = ledger.request_approval(
                    ledger.payroll_funding_action(), request_id=reference, idempotency_key=reference,
                )
                for index in (0, 1):
                    delivery_id = f"{reference}/{index}/teams"
                    ledger.claim_delivery(delivery_id, attempt_id=f"teams-{index}")
                    ledger.record_delivery_result(
                        delivery_id, attempt_id=f"teams-{index}", succeeded=True,
                    )
                runtime = FinanceRuntime.__new__(FinanceRuntime)
                runtime.role = AgentRole.BANKING_OPERATIONS
                messenger = Mock()
                messenger.send_teams_group = AsyncMock()
                messenger.send_approval_email = AsyncMock()
                bindings = {"approvers": [
                    {"object_id": "marco", "mail": "marco@example.test"},
                    {"object_id": "ayca", "mail": "ayca@example.test"},
                ]}
                if outcome == "partial":
                    delivery_id = f"{reference}/0/email"
                    ledger.claim_delivery(delivery_id, attempt_id="legacy-email")
                    ledger.record_delivery_result(
                        delivery_id, attempt_id="legacy-email", succeeded=True,
                    )
                    with self.assertRaisesRegex(RuntimeError, "reconciliation"):
                        await runtime._notify_approvers(ledger, messenger, bindings, request)
                    messenger.send_approval_email.assert_not_awaited()
                elif outcome == "uncertain":
                    messenger.send_approval_email.side_effect = DeliveryUncertain("Unknown acceptance")
                    with self.assertRaises(DeliveryUncertain):
                        await runtime._notify_approvers(ledger, messenger, bindings, request)
                    with self.assertRaisesRegex(RuntimeError, "reconciliation"):
                        await runtime._notify_approvers(ledger, messenger, bindings, request)
                    messenger.send_approval_email.assert_awaited_once()
                else:
                    messenger.send_approval_email.side_effect = DeliveryRejected("Graph rejected email")
                    with self.assertLogs("hotel_payroll_treasurer_agents.runtime", level="WARNING"):
                        results = await runtime._notify_approvers(ledger, messenger, bindings, request)
                    self.assertEqual(sum(result.get("accepted") is False for result in results), 2)
                emails = [delivery for delivery in ledger.snapshot()["outbox"].values()
                          if delivery["channel"] == "email"]
                if outcome != "partial":
                    expected = "failed" if outcome == "rejected" else "in_flight"
                    self.assertTrue(all(delivery["status"] == expected for delivery in emails))
                self.assertEqual(ledger.snapshot()["transactions"], [])
                messenger.send_teams_group.assert_not_awaited()

    async def test_iq_discovery_cannot_authorize_reports_or_side_effects(self):
        runtime = FinanceRuntime.__new__(FinanceRuntime)
        runtime.role = AgentRole.BANKING_OPERATIONS
        runtime.display_name = "Hotel Treasurer"
        runtime.model_endpoint = "https://finance.openai.azure.com"
        runtime._managed_token = AsyncMock(return_value="root-token")
        runtime._agent_token = AsyncMock(return_value="own-user-token")
        runtime._send_peer = AsyncMock(return_value={"accepted": True})
        ledger = Mock()
        requests = []

        def call(name, identifier, arguments="{}"):
            return {"type": "function_call", "name": name, "arguments": arguments, "call_id": identifier}

        def report():
            return {"type": "message", "content": [{"type": "output_text", "text": "Read-only answer"}]}

        responses = [
            [report()],
            [call("send_peer", "blocked-peer", '{"channel":"email","message":"Status"}'),
             call("refresh_analytics", "blocked-refresh")],
            [call("foundry_iq_policies", "policy", '{"question":"Actual policy"}'),
             call("work_iq_0", "mail")],
            [call("work_iq_0", "mail-retry")],
            [call("work_iq_0", "mail-retry-after-service-error")],
            [report()],
            [call("fabric_iq_0", "fabric")],
            [call("send_peer", "permitted-peer", '{"channel":"email","message":"Status"}')],
            [report()],
        ]

        def respond(request):
            if request.url.host.endswith(".search.windows.net"):
                return httpx.Response(200, json={"response": ["SECRET_POLICY"], "references": [{"id": "policy"}]})
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"output": responses.pop(0)})

        runtime.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        self.addAsyncCleanup(runtime.http.aclose)
        client = AsyncMock()
        client.tools.return_value = [{"name": "GetMessage", "inputSchema": {"type": "object"}}]
        evidence = {"content": [{"type": "text", "text": "SECRET_IQ_EVIDENCE"}]}
        service_error = httpx.HTTPStatusError(
            "service unavailable",
            request=httpx.Request("POST", "https://api.fabric.microsoft.com/mcp"),
            response=httpx.Response(500),
        )
        client.call.side_effect = [McpToolError("SECRET_ERROR"), service_error, evidence, evidence]
        with patch("hotel_payroll_treasurer_agents.runtime.McpClient", return_value=client), patch(
            "hotel_payroll_treasurer_agents.runtime.refresh_runtime_projection",
        ) as refresh, patch(
            "hotel_payroll_treasurer_agents.runtime.read_current_projection", return_value=self.analytical_proof(),
        ), patch("hotel_payroll_treasurer_agents.runtime.revalidate_projection"), self.assertLogs(
            "hotel_payroll_treasurer_agents.runtime", level="INFO",
        ) as logs:
            answer = await runtime._orchestrate(
                "Report only. Do not contact the peer or refresh analytics.", None, None,
                {"fabric": {"mcp_url": "https://api.fabric.microsoft.com/v1/mcp/agent"},
                 "foundry_iq": {"endpoint": "https://finance.search.windows.net",
                                "knowledge_base": "policies", "api_version": "version"},
                 "foundry": {"model_deployment": "model"}},
                {}, None, ledger, None, None, None, 2, "task", "own-message-reference",
            )
        self.assertEqual(answer, "Read-only answer")
        self.assertEqual(len(requests), 9)
        self.assertEqual(requests[0]["input"], [{
            "role": "user",
            "content": "Report only. Do not contact the peer or refresh analytics.",
        }])
        self.assertIn("bound-peer request at hop 2", requests[0]["instructions"])
        self.assertIn("will not receive your final answer automatically", requests[0]["instructions"])
        self.assertIn("own-message-reference", requests[0]["instructions"])
        action_names = {"send_peer", "refresh_analytics", "request_payroll_approval"}
        for request in requests:
            self.assertFalse(action_names & {tool["name"] for tool in request["tools"]})
            self.assertIn("Currently available effect tools: []", request["instructions"])
            self.assertIn(
                "Effect tools explicitly prohibited by the original request: "
                + json.dumps(sorted(action_names)),
                request["instructions"],
            )
        failed_read = next(
            json.loads(item["output"]) for item in requests[3]["input"]
            if item.get("call_id") == "mail" and item.get("type") == "function_call_output"
        )
        self.assertEqual(failed_read["error"], "IQ_TOOL_FAILED")
        self.assertFalse(failed_read["completed"])
        self.assertNotIn("SECRET_ERROR", json.dumps(failed_read))
        unavailable_read = next(
            json.loads(item["output"]) for item in requests[4]["input"]
            if item.get("call_id") == "mail-retry" and item.get("type") == "function_call_output"
        )
        self.assertEqual(unavailable_read["error"], "IQ_SERVICE_UNAVAILABLE")
        self.assertFalse(unavailable_read["completed"])
        self.assertNotIn("service unavailable", json.dumps(unavailable_read))
        rejected = [json.loads(item["output"]) for item in requests[2]["input"]
                    if item.get("type") == "function_call_output"]
        self.assertEqual(len(rejected), 2)
        self.assertTrue(all(
            value["error"] == "ACTION_PROHIBITED_BY_REQUEST" for value in rejected
        ))
        self.assertTrue(all(value["completed"] is False for value in rejected))
        ledger.payroll_funding_action.assert_not_called()
        refresh.assert_not_called()
        runtime._send_peer.assert_not_awaited()
        self.assertEqual(client.call.await_count, 4)
        log_text = "\n".join(logs.output)
        self.assertEqual(log_text.count("IQ consultation completed:"), 3)
        self.assertNotIn("SECRET_", log_text)
        report_retry_text = [
            item["content"] for item in requests[6]["input"]
            if isinstance(item, dict) and isinstance(item.get("content"), str)
        ][-1]
        self.assertIn("fabric_iq", report_retry_text)
        self.assertNotIn("work_iq", report_retry_text)
        self.assertNotIn("foundry_iq", report_retry_text)
        blocked_peer = next(
            json.loads(item["output"]) for item in requests[8]["input"]
            if item.get("call_id") == "permitted-peer"
            and item.get("type") == "function_call_output"
        )
        self.assertEqual(blocked_peer["error"], "ACTION_PROHIBITED_BY_REQUEST")
        fabric_output = next(
            json.loads(item["output"]) for item in requests[7]["input"]
            if item.get("call_id") == "fabric" and item.get("type") == "function_call_output"
        )
        self.assertTrue(fabric_output["freshness_verified"])
        self.assertFalse(fabric_output["data_agent_response_is_verified_financial_evidence"])
        self.assertNotIn("SECRET_IQ_EVIDENCE", json.dumps(fabric_output))

    async def test_missing_or_changed_physical_proof_blocks_actions_and_reports_but_not_refresh(self):
        for changed in (False, True):
            with self.subTest(changed=changed):
                runtime = FinanceRuntime.__new__(FinanceRuntime)
                runtime.role = AgentRole.BANKING_OPERATIONS
                runtime.display_name = "Hotel Treasurer"
                runtime.model_endpoint = "https://finance.openai.azure.com"
                runtime._managed_token = AsyncMock(return_value="root-token")
                runtime._agent_token = AsyncMock(return_value="own-user-token")
                runtime._send_peer = AsyncMock()
                ledger = Mock()
                requests = []
                first = [
                    {"type": "function_call", "name": name, "arguments": arguments, "call_id": name}
                    for name, arguments in (
                        ("foundry_iq_policies", '{"question":"Actual policy"}'),
                        ("fabric_iq_0", "{}"), ("work_iq_0", "{}"),
                    )
                ]
                second = [
                    {"type": "function_call", "name": name, "arguments": arguments, "call_id": name}
                    for name, arguments in (
                        ("send_peer", '{"channel":"email","message":"Status"}'),
                        ("refresh_analytics", "{}"),
                    )
                ]

                def respond(request):
                    if request.url.host.endswith(".search.windows.net"):
                        return httpx.Response(200, json={"response": ["Policy"], "references": [{"id": "policy"}]})
                    requests.append(json.loads(request.content))
                    self.assertIn("authenticated human request", requests[-1]["instructions"])
                    number = len(requests)
                    output = first if number == 1 else second if number == 2 else [
                        {"type": "message", "content": [{"type": "output_text", "text": "Invented current report"}]}
                    ]
                    return httpx.Response(200, json={"output": output})

                runtime.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
                self.addAsyncCleanup(runtime.http.aclose)
                client = AsyncMock()
                client.tools.return_value = [{"name": "GetMessage", "inputSchema": {"type": "object"}}]
                client.call.return_value = {"content": [{"type": "text", "text": "Unverified USD 999"}]}
                with patch("hotel_payroll_treasurer_agents.runtime.McpClient", return_value=client), patch(
                    "hotel_payroll_treasurer_agents.runtime.read_current_projection",
                    return_value=self.analytical_proof(),
                    side_effect=None if changed else ValueError("Refreshing"),
                ), patch(
                    "hotel_payroll_treasurer_agents.runtime.revalidate_projection", side_effect=ValueError("Changed"),
                ), patch(
                    "hotel_payroll_treasurer_agents.runtime.refresh_runtime_projection",
                    return_value={"completed": True},
                ) as refresh, self.assertLogs(
                    "hotel_payroll_treasurer_agents.runtime", level="WARNING",
                ), self.assertRaisesRegex(RuntimeError, "bounded orchestration"):
                    await runtime._orchestrate(
                        "Report", None, None,
                        {"fabric": {"mcp_url": "https://api.fabric.microsoft.com/v1/mcp/agent"},
                         "foundry_iq": {"endpoint": "https://finance.search.windows.net",
                                        "knowledge_base": "policies", "api_version": "version"},
                         "foundry": {"model_deployment": "model"}},
                        {}, None, ledger, None, None, None, 0, "task",
                    )
                runtime._send_peer.assert_not_awaited()
                ledger.payroll_funding_action.assert_not_called()
                refresh.assert_called_once()
                results = {item["call_id"]: json.loads(item["output"]) for item in requests[2]["input"]
                           if item.get("type") == "function_call_output"}
                self.assertEqual(results["send_peer"]["error"], "IQ_ANALYTICS_UNAVAILABLE")
                self.assertTrue(results["refresh_analytics"]["completed"])
                offered = {tool["name"] for tool in requests[1]["tools"]}
                self.assertIn("refresh_analytics", offered)
                if not changed:
                    self.assertNotIn("send_peer", offered)

    async def test_uncited_iq_cannot_be_marked_consulted_or_exhaust_into_success(self):
        runtime = FinanceRuntime.__new__(FinanceRuntime)
        runtime.role = AgentRole.HOTEL_FINANCE
        runtime.display_name = "Hotel Payroll"
        runtime.model_endpoint = "https://finance.openai.azure.com"
        runtime._managed_token = AsyncMock(return_value="root-token")
        requests = []

        def respond(request):
            if request.url.host.endswith(".search.windows.net"):
                return httpx.Response(200, json={"response": ["Unsupported answer"], "references": []})
            requests.append(json.loads(request.content))
            output = ([{"type": "function_call", "name": "foundry_iq_policies",
                        "arguments": '{"question":"Policy"}', "call_id": "uncited"}]
                      if len(requests) == 1 else
                      [{"type": "message", "content": [{"type": "output_text", "text": "Invented success"}]}])
            return httpx.Response(200, json={"output": output})

        runtime.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        self.addAsyncCleanup(runtime.http.aclose)
        client = AsyncMock()
        client.tools.return_value = [{"name": "GetMessage", "inputSchema": {"type": "object"}}]
        with patch("hotel_payroll_treasurer_agents.runtime.McpClient", return_value=client), self.assertLogs(
            "hotel_payroll_treasurer_agents.runtime", level="INFO",
        ) as logs, self.assertRaisesRegex(RuntimeError, "bounded orchestration"):
            await runtime._orchestrate(
                "Report", None, None,
                {"fabric": {"mcp_url": "https://api.fabric.microsoft.com/v1/mcp/agent"},
                 "foundry_iq": {"endpoint": "https://finance.search.windows.net",
                                "knowledge_base": "policies", "api_version": "version"},
                 "foundry": {"model_deployment": "model"}},
                {}, None, None, None, None, None, 3, "task",
            )
        self.assertEqual(len(requests), 10)
        self.assertIn("foundry_iq", requests[-1]["input"][-1]["content"])
        self.assertNotIn("IQ consultation completed:", "\n".join(logs.output))
        client.call.assert_not_awaited()

    async def test_duplicate_financial_confirmation_reuses_both_accepted_routes(self):
        runtime = FinanceRuntime.__new__(FinanceRuntime)
        runtime.role = AgentRole.BANKING_OPERATIONS
        runtime._send_peer = AsyncMock()
        reference = "PAYROLL-0123456789ABCDEF"
        keys = [
            f"peer-delivery/banking_operations/{reference}/{channel}/1"
            for channel in ("email", "teams")
        ]
        journal = Mock()
        journal.read.return_value = Snapshot("journal-etag", {
            "schema_version": 1, "turns": {key: {"status": "complete"} for key in keys},
        })
        await runtime._confirm_funding(None, journal, None, "Updated request snapshot", reference)
        runtime._send_peer.assert_not_awaited()
        del journal.read.return_value.state["turns"][keys[1]]
        with self.assertRaisesRegex(RuntimeError, "reconciliation"):
            await runtime._confirm_funding(None, journal, None, "Updated request snapshot", reference)
        runtime._send_peer.assert_not_awaited()
        journal.read.return_value.state["turns"] = {}
        await runtime._confirm_funding("messenger", journal, "peer", "Actual result", reference)
        self.assertEqual(runtime._send_peer.await_count, 2)
        self.assertEqual(runtime._send_peer.await_args_list[0].args[3], "email")
        self.assertEqual(runtime._send_peer.await_args_list[1].args[3], "teams")

    async def test_rejected_currency_report_is_revised_without_repeating_tools(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state = Ledger(
            SQLiteStateStore(Path(directory.name) / "ledger.db"),
            LedgerConfig("tenant", ("marco", "ayca"), "hotel"),
        ).snapshot()
        store = Mock()
        store.read.return_value = Snapshot("real-etag", state)
        runtime = FinanceRuntime.__new__(FinanceRuntime)
        runtime.role = AgentRole.HOTEL_FINANCE
        runtime.display_name = "Hotel Payroll"
        runtime.model_endpoint = "https://finance.openai.azure.com"
        runtime._managed_token = AsyncMock(return_value="root-token")
        runtime._agent_token = AsyncMock(return_value="own-user-token")
        responses = [
            [{"type": "function_call", "name": "foundry_iq_policies",
              "arguments": '{"question":"Actual finance policy"}', "call_id": "policy"},
             {"type": "function_call", "name": "work_iq_0", "arguments": "{}", "call_id": "mail"},
             {"type": "function_call", "name": "fabric_iq_0", "arguments": "{}", "call_id": "fabric"},
             {"type": "function_call", "name": "ledger_status", "arguments": "{}",
              "call_id": "ledger-read", "id": "call-one"}],
            [{"type": "message", "content": [
                {"type": "output_text", "text": "Payroll balance USD 1,000,000"}]}],
            [{"type": "message", "content": [
                {"type": "output_text", "text": "Payroll balance USD 10,000.00"}]}],
        ]
        requests = []

        def model(request):
            if request.url.host.endswith(".search.windows.net"):
                return httpx.Response(200, json={"response": ["Policy"], "references": [{"id": "policy"}]})
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"output": responses.pop(0)})

        runtime.http = httpx.AsyncClient(transport=httpx.MockTransport(model))
        self.addAsyncCleanup(runtime.http.aclose)
        client = AsyncMock()
        client.call.return_value = {"content": [{"type": "text", "text": "Actual IQ evidence"}]}
        client.tools.return_value = [{
            "name": "GetMessage", "inputSchema": {"type": "object", "properties": {}},
        }]
        with patch("hotel_payroll_treasurer_agents.runtime.McpClient", return_value=client), patch(
            "hotel_payroll_treasurer_agents.runtime.read_current_projection", return_value=self.analytical_proof(state),
        ), patch("hotel_payroll_treasurer_agents.runtime.revalidate_projection"), self.assertLogs(
            "hotel_payroll_treasurer_agents.runtime", level="WARNING",
        ):
            answer = await runtime._orchestrate(
                "Report the payroll balance", "context", "authorization",
                {"fabric": {"mcp_url": "https://api.fabric.microsoft.com/v1/mcp/agent"},
                 "foundry_iq": {"endpoint": "https://finance.search.windows.net",
                                "knowledge_base": "policies", "api_version": "version"},
                 "foundry": {"model_deployment": "model"}},
                {}, store, None, None, None, None, 3, "payroll-funding",
            )
        self.assertEqual(answer, "Payroll balance USD 10,000.00")
        self.assertEqual(len(requests), 3)
        self.assertIn("Do not repeat", requests[-1]["input"][-1]["content"])
        store.read.assert_called_once()
        self.assertEqual(client.call.await_count, 2)

    async def test_fabric_uses_hired_application_and_work_iq_uses_bound_agent_user(self):
        runtime = FinanceRuntime.__new__(FinanceRuntime)
        runtime.role = AgentRole.HOTEL_FINANCE
        runtime.display_name = "Hotel Payroll"
        runtime.model_endpoint = "https://finance.openai.azure.com"
        runtime._managed_token = AsyncMock(return_value="root-token")
        runtime._agent_token = AsyncMock(return_value="own-user-token")
        runtime._hired_application_token = AsyncMock(return_value="hired-application-token")
        responses = [
            [{"type": "function_call", "name": "foundry_iq_policies",
              "arguments": '{"question":"Actual policy"}', "call_id": "policy"},
             {"type": "function_call", "name": "work_iq_0", "arguments": "{}", "call_id": "mail"},
             {"type": "function_call", "name": "fabric_iq_0", "arguments": "{}", "call_id": "fabric"}],
            [{"type": "message", "content": [{"type": "output_text", "text": "Read-only answer"}]}],
        ]

        def respond(request):
            if request.url.host.endswith(".search.windows.net"):
                return httpx.Response(200, json={"response": ["Policy"], "references": [{"id": "policy"}]})
            return httpx.Response(200, json={"output": responses.pop(0)})

        runtime.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        self.addAsyncCleanup(runtime.http.aclose)
        clients = []

        def client(endpoint, get_token, **kwargs):
            instance = AsyncMock()
            instance.call.return_value = {"content": [{"type": "text", "text": "Actual IQ evidence"}]}
            instance.tools.return_value = [{
                "name": "GetMessage" if not clients else "query",
                "inputSchema": {"type": "object", "properties": {}},
            }]
            clients.append((endpoint, get_token))
            return instance

        own = {"agent_user_object_id": "bound-user"}
        with patch("hotel_payroll_treasurer_agents.runtime.McpClient", side_effect=client), patch(
            "hotel_payroll_treasurer_agents.runtime.read_current_projection", return_value=self.analytical_proof(),
        ), patch("hotel_payroll_treasurer_agents.runtime.revalidate_projection"):
            answer = await runtime._orchestrate(
                "Read only", "context", "authorization",
                {"fabric": {"mcp_url": "https://api.fabric.microsoft.com/v1/mcp/agent"},
                 "foundry_iq": {"endpoint": "https://finance.search.windows.net",
                                "knowledge_base": "policies", "api_version": "version"},
                 "foundry": {"model_deployment": "model"}},
                own, None, None, None, None, None, 3, "payroll-funding",
            )
        self.assertEqual(answer, "Read-only answer")
        self.assertEqual(await clients[0][1](), "own-user-token")
        runtime._agent_token.assert_awaited_once_with(
            "context", "authorization", M365_SCOPE, own,
        )
        self.assertEqual(await clients[1][1](), "hired-application-token")
        runtime._hired_application_token.assert_awaited_once_with("context", own)
        self.assertEqual(runtime._agent_token.await_count, 1)
