import json
import unittest
from unittest.mock import Mock

import httpx

from finance_autopilots.communications import DeliveryRejected, DeliveryUncertain, OwnUserMessenger
from finance_autopilots.peer_routes import email_peer_route

OWN = "00000000-0000-0000-0000-000000000001"
RECIPIENT = {"object_id": "00000000-0000-0000-0000-000000000002",
             "mail": "approver@example.test"}
SECOND_RECIPIENT = {"object_id": "00000000-0000-0000-0000-000000000003",
                    "mail": "second-approver@example.test"}
PEER = {"object_id": "00000000-0000-0000-0000-000000000004", "mail": "peer@example.test"}


class CommunicationsTests(unittest.IsolatedAsyncioTestCase):
    async def token(self):
        return "own-user-token"

    async def test_email_is_sent_as_me_to_only_the_bound_recipient(self):
        requests = []

        def respond(request):
            requests.append(request)
            payload = json.loads(request.content)
            self.assertEqual(request.url.path, "/v1.0/me/sendMail")
            self.assertNotIn("from", payload["message"])
            self.assertEqual(payload["message"]["subject"], "Finance update")
            self.assertEqual(payload["message"]["toRecipients"],
                             [{"emailAddress": {"address": RECIPIENT["mail"]}}])
            return httpx.Response(202)

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            result = await OwnUserMessenger(http, self.token, OWN).send(
                "email", RECIPIENT, "Controlled message", "test-reference"
            )
        self.assertTrue(result["accepted"])
        self.assertFalse(result["inboxDeliveryVerified"])
        self.assertEqual(len(requests), 1)

    async def test_peer_email_keeps_correlation_in_headers_not_visible_text(self):
        reference = "ACTIVITY-" + "A" * 32
        text = "Hotel Treasurer, please confirm payroll funding."

        def respond(request):
            message = json.loads(request.content)["message"]
            self.assertEqual(message["body"], {"contentType": "Text", "content": text})
            self.assertNotIn(reference, message["subject"])
            self.assertEqual(email_peer_route(message["internetMessageHeaders"]), (reference, 1))
            return httpx.Response(202)

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            await OwnUserMessenger(http, self.token, OWN).send(
                "email", PEER, text, reference, peer_hop=1,
            )

    async def test_peer_teams_route_uses_provider_message_identity_not_visible_footer(self):
        reference = "ACTIVITY-" + "A" * 32
        text = "Hotel Treasurer, please confirm payroll funding."
        routes = Mock()

        def respond(request):
            if request.url.path == "/v1.0/chats":
                return httpx.Response(201, json={"id": "native-chat"})
            self.assertEqual(request.url.path, "/v1.0/chats/native-chat/messages")
            self.assertEqual(json.loads(request.content)["body"]["content"], text)
            return httpx.Response(201, json={"id": "native-message"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            result = await OwnUserMessenger(http, self.token, OWN, peer_routes=routes).send(
                "teams", PEER, text, reference, peer_hop=1,
            )
        self.assertTrue(result["accepted"])
        routes.record.assert_called_once_with(
            OWN, PEER["object_id"], "native-chat", "native-message", text, reference, 1,
        )

    async def test_missing_peer_route_storage_refuses_send_before_provider_acceptance(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: self.fail("An unrouteable peer message must not be sent")
        )) as http:
            with self.assertRaises(RuntimeError):
                await OwnUserMessenger(http, self.token, OWN).send(
                    "teams", PEER, "Please confirm funding.", "payroll-funding", peer_hop=1,
                )

    async def test_payroll_email_subjects_are_short_and_natural(self):
        subjects = []

        def respond(request):
            subjects.append(json.loads(request.content)["message"]["subject"])
            return httpx.Response(202)

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            messenger = OwnUserMessenger(http, self.token, OWN)
            await messenger.send(
                "email", RECIPIENT, "Payroll is funded.", "PAYROLL-0123456789ABCDEF"
            )
            await messenger.send(
                "email", RECIPIENT, "Please approve payroll funding.",
                "PAYROLL-0123456789ABCDEF", subject="Payroll approval needed",
            )
        self.assertEqual(subjects, ["Payroll funding update", "Payroll approval needed"])

    async def test_approval_email_has_one_bound_approver_in_to_and_the_other_in_cc(self):
        requests = []

        def respond(request):
            requests.append(request)
            self.assertEqual(request.url.path, "/v1.0/me/sendMail")
            payload = json.loads(request.content)
            message = payload["message"]
            self.assertEqual(message["subject"], "Payroll approval needed")
            self.assertEqual(message["toRecipients"],
                             [{"emailAddress": {"address": RECIPIENT["mail"]}}])
            self.assertEqual(message["ccRecipients"],
                             [{"emailAddress": {"address": SECOND_RECIPIENT["mail"]}}])
            self.assertNotIn("bccRecipients", message)
            self.assertNotIn("from", message)
            self.assertTrue(payload["saveToSentItems"])
            return httpx.Response(202)

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            result = await OwnUserMessenger(http, self.token, OWN).send_approval_email(
                [RECIPIENT, SECOND_RECIPIENT], "Please approve payroll funding.", "PAYROLL-0123456789ABCDEF"
            )
        self.assertEqual(len(requests), 1)
        self.assertTrue(result["accepted"])
        self.assertEqual(result["recipientObjectIds"],
                         [RECIPIENT["object_id"], SECOND_RECIPIENT["object_id"]])
        self.assertFalse(result["inboxDeliveryVerified"])

    async def test_approval_email_rejects_missing_duplicate_or_self_recipients_before_sending(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: self.fail("No HTTP call is allowed")
        )) as http:
            messenger = OwnUserMessenger(http, self.token, OWN)
            for targets in (
                [RECIPIENT],
                [RECIPIENT, RECIPIENT],
                [RECIPIENT, {**SECOND_RECIPIENT, "object_id": OWN}],
                [RECIPIENT, {**SECOND_RECIPIENT, "mail": RECIPIENT["mail"].upper()}],
                [RECIPIENT, {**SECOND_RECIPIENT, "mail": "invalid\r\n@example.test"}],
            ):
                with self.subTest(targets=targets), self.assertRaises((ValueError, PermissionError)):
                    await messenger.send_approval_email(targets, "Please approve.", "reference")

    async def test_approval_email_does_not_retry_rejected_or_uncertain_delivery(self):
        for status, exception in ((403, DeliveryRejected), (500, DeliveryUncertain), (200, DeliveryUncertain)):
            calls = []

            def respond(request):
                calls.append(request)
                return httpx.Response(status)

            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
                with self.subTest(status=status), self.assertRaises(exception):
                    await OwnUserMessenger(http, self.token, OWN).send_approval_email(
                        [RECIPIENT, SECOND_RECIPIENT], "Please approve.", "reference"
                    )
            self.assertEqual(len(calls), 1)

    async def test_teams_chat_contains_exactly_own_user_and_bound_recipient(self):
        def respond(request):
            payload = json.loads(request.content)
            if request.url.path == "/v1.0/chats":
                members = payload["members"]
                self.assertEqual([member["user@odata.bind"] for member in members], [
                    f"https://graph.microsoft.com/v1.0/users('{OWN}')",
                    f"https://graph.microsoft.com/v1.0/users('{RECIPIENT['object_id']}')",
                ])
                return httpx.Response(201, json={"id": "19:finance-chat"})
            self.assertEqual(request.url.path, "/v1.0/chats/19:finance-chat/messages")
            return httpx.Response(201, json={"id": "finance-message"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            result = await OwnUserMessenger(http, self.token, OWN).send(
                "teams", RECIPIENT, "Finance message", "test-reference"
            )
        self.assertEqual(result["messageId"], "finance-message")

    async def test_teams_group_contains_agent_and_both_approvers_once(self):
        def respond(request):
            payload = json.loads(request.content)
            if request.url.path == "/v1.0/chats":
                self.assertEqual(payload["chatType"], "group")
                self.assertEqual(payload["topic"], "Hotel Treasurer approvals")
                self.assertEqual(
                    [member["user@odata.bind"] for member in payload["members"]],
                    [
                        f"https://graph.microsoft.com/v1.0/users('{OWN}')",
                        f"https://graph.microsoft.com/v1.0/users('{RECIPIENT['object_id']}')",
                        f"https://graph.microsoft.com/v1.0/users('{SECOND_RECIPIENT['object_id']}')",
                    ],
                )
                return httpx.Response(201, json={"id": "19:approval-group"})
            self.assertEqual(request.url.path, "/v1.0/chats/19:approval-group/messages")
            return httpx.Response(201, json={"id": "approval-message"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            result = await OwnUserMessenger(http, self.token, OWN).send_teams_group(
                [RECIPIENT, SECOND_RECIPIENT],
                "Payroll funding needs approval.",
                "PAYROLL-0123456789ABCDEF",
            )
        self.assertEqual(result["chatType"], "group")
        self.assertEqual(result["messageId"], "approval-message")

    async def test_shared_payroll_group_is_reused_only_with_exact_bound_membership(self):
        for wrong_member in (False, True):
            with self.subTest(wrong_member=wrong_member):
                posts = []
                chat_id = "19:payroll_funding@thread.v2"
                context = {
                    "chat_id": chat_id, "tenant_id": "tenant", "peer_id": PEER["object_id"],
                    "approver_ids": [RECIPIENT["object_id"], SECOND_RECIPIENT["object_id"]],
                }

                def respond(request):
                    if request.method == "GET":
                        if request.url.path.endswith("/members"):
                            identifiers = [OWN, PEER["object_id"], *context["approver_ids"]]
                            if wrong_member:
                                identifiers[-1] = "00000000-0000-0000-0000-000000000005"
                            return httpx.Response(200, json={"value": [
                                {"userId": identifier, "tenantId": "tenant"}
                                for identifier in identifiers
                            ]})
                        return httpx.Response(200, json={
                            "id": chat_id, "chatType": "group", "topic": "Payroll Funding",
                        })
                    posts.append(request.url.path)
                    self.assertEqual(request.url.path, f"/v1.0/chats/{chat_id}/messages")
                    return httpx.Response(201, json={"id": "message"})

                async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
                    messenger = OwnUserMessenger(http, self.token, OWN, teams_context=context)
                    if wrong_member:
                        with self.assertRaises(PermissionError):
                            await messenger.send_teams_group(
                                [RECIPIENT, SECOND_RECIPIENT], "Please approve funding.", "reference",
                            )
                    else:
                        result = await messenger.send_teams_group(
                            [RECIPIENT, SECOND_RECIPIENT], "Please approve funding.", "reference",
                        )
                        self.assertTrue(result["sharedWithApprovers"])
                self.assertEqual(len(posts), 0 if wrong_member else 1)

    async def test_rejected_and_uncertain_sends_are_never_automatically_retried(self):
        for status, exception in ((403, DeliveryRejected), (429, DeliveryRejected),
                                  (500, DeliveryUncertain)):
            calls = []

            def respond(request):
                calls.append(request)
                return httpx.Response(status)

            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
                with self.assertRaises(exception):
                    await OwnUserMessenger(http, self.token, OWN).send(
                        "email", RECIPIENT, "Controlled message", "test-reference"
                    )
            self.assertEqual(len(calls), 1)

    async def test_self_targeting_and_unknown_channels_are_rejected(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: self.fail("No HTTP call is allowed")
        )) as http:
            messenger = OwnUserMessenger(http, self.token, OWN)
            with self.assertRaises(PermissionError):
                await messenger.send("teams", {"object_id": OWN, "mail": "me@project.example"}, "test", "ref")
            with self.assertRaises(ValueError):
                await messenger.send("sms", RECIPIENT, "test", "ref")
