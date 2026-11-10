"""Own-user Graph delivery to manager-bound recipients, without action retries."""

from __future__ import annotations

import asyncio
import re
from uuid import UUID

import httpx

from .peer_routes import HOP_HEADER, REFERENCE_HEADER, validate_route

class DeliveryRejected(RuntimeError):
    """The provider explicitly rejected a send before acceptance."""


class DeliveryUncertain(RuntimeError):
    """Reconcile provider state before retrying a possibly accepted send."""


class OwnUserMessenger:
    def __init__(self, http, token, own_user_id, *, teams_context=None, peer_routes=None):
        UUID(own_user_id)
        self.http = http
        self.token = token
        self.own_user_id = own_user_id
        self.teams_context = teams_context
        self.peer_routes = peer_routes

    def _coordination_members(self):
        context = self.teams_context
        identifiers = [self.own_user_id, context["peer_id"], *context["approver_ids"]]
        if len(context["approver_ids"]) != 2 or len(set(identifiers)) != 4:
            raise PermissionError("Shared coordination requires both agents and both bound approvers")
        for identifier in identifiers:
            UUID(identifier)
        return identifiers

    async def _shared_peer_chat(self, target):
        context = self.teams_context
        if context is None or target["object_id"] != context["peer_id"]:
            return None
        identifiers = self._coordination_members()
        chat_id = context["chat_id"]
        if not isinstance(chat_id, str) or not re.fullmatch(r"19:[A-Za-z0-9_-]+@thread\.v2", chat_id):
            raise ValueError("Payroll Funding requires a configured native Teams group identity")
        token = await self.token()
        if not token:
            raise PermissionError("Own-user Graph authorization returned no token")
        responses = []
        for suffix in ("", "/members"):
            response = await self.http.get(
                f"https://graph.microsoft.com/v1.0/chats/{chat_id}{suffix}",
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            responses.append(response.json())
        chat, membership = responses
        if chat.get("id") != chat_id:
            raise PermissionError("Graph coordination chat identity does not match the configured Payroll Funding group")
        members = membership.get("value")
        if not isinstance(members, list) or not all(isinstance(member, dict) for member in members):
            raise RuntimeError("Graph did not return coordination chat membership")
        if (
            chat.get("chatType") != "group" or chat.get("topic") != "Payroll Funding"
            or membership.get("@odata.nextLink")
            or len(members) != 4
            or {member.get("userId") for member in members} != set(identifiers)
            or any(member.get("tenantId") != context["tenant_id"] for member in members)
        ):
            raise PermissionError("Configured Payroll Funding group must contain exactly both agents and both same-tenant approvers")
        return chat_id

    async def _post(self, path, body):
        token = await self.token()
        if not token:
            raise PermissionError("Own-user Graph authorization returned no token")
        try:
            response = await self.http.post(
                "https://graph.microsoft.com/v1.0" + path,
                headers={"Authorization": f"Bearer {token}"},
                json=body,
            )
        except httpx.TransportError as error:
            raise DeliveryUncertain("Graph transport failed; send acceptance is unknown") from error
        if 400 <= response.status_code < 500:
            raise DeliveryRejected(f"Graph rejected delivery with HTTP {response.status_code}")
        if response.status_code >= 500:
            raise DeliveryUncertain(f"Graph returned HTTP {response.status_code}; reconcile before retry")
        response.raise_for_status()
        return response

    async def send(self, channel, target, text, reference, *, subject=None, peer_hop=None):
        UUID(target["object_id"])
        if not text.strip() or len(text) > 16_384 or not reference:
            raise ValueError("A bounded message and stable reference are required")
        if target["object_id"] == self.own_user_id:
            raise PermissionError("An agent cannot target itself for collaboration")
        if peer_hop is not None:
            validate_route(reference, peer_hop)
            if channel == "teams" and self.peer_routes is None:
                raise RuntimeError("Peer Teams delivery requires private durable routing")
        if channel == "email":
            address = target["mail"]
            if address.count("@") != 1 or any(character in address for character in "\r\n"):
                raise ValueError("Bound recipient has an invalid mailbox address")
            default_subject = (
                "Payroll funding update"
                if reference == "payroll-funding" or reference.startswith("PAYROLL-")
                else "Finance update"
            )
            message = {
                "subject": subject or default_subject,
                "body": {"contentType": "Text", "content": text},
                "toRecipients": [{"emailAddress": {"address": address}}],
            }
            if peer_hop is not None:
                message["internetMessageHeaders"] = [
                    {"name": REFERENCE_HEADER, "value": reference},
                    {"name": HOP_HEADER, "value": str(peer_hop)},
                ]
            response = await self._post("/me/sendMail", {
                "message": message,
                "saveToSentItems": True,
            })
            if response.status_code != 202:
                raise DeliveryUncertain("Graph sendMail did not return the documented acceptance status")
            return {"accepted": True, "channel": "email", "reference": reference,
                    "inboxDeliveryVerified": False}
        if channel != "teams":
            raise ValueError("Only Teams and email are supported")
        chat_id = await self._shared_peer_chat(target)
        shared = chat_id is not None
        if not shared:
            members = [{
                "@odata.type": "#microsoft.graph.aadUserConversationMember",
                "roles": ["owner"],
                "user@odata.bind": f"https://graph.microsoft.com/v1.0/users('{identifier}')",
            } for identifier in (self.own_user_id, target["object_id"])]
            chat_response = await self._post("/chats", {"chatType": "oneOnOne", "members": members})
            chat_id = chat_response.json().get("id")
        if not isinstance(chat_id, str) or not chat_id or "/" in chat_id or "?" in chat_id:
            raise DeliveryUncertain("Graph returned an invalid chat identity")
        if shared:
            if not re.fullmatch(r"19:[A-Za-z0-9_-]+@thread\.v2", chat_id):
                raise DeliveryUncertain("Graph returned an invalid native coordination group identity")
        response = await self._post(f"/chats/{chat_id}/messages", {
            "body": {"contentType": "text", "content": text},
        })
        message_id = response.json().get("id")
        if response.status_code != 201 or not isinstance(message_id, str) or not message_id:
            raise DeliveryUncertain("Graph did not return a confirmed Teams message identity")
        if peer_hop is not None:
            await asyncio.to_thread(
                self.peer_routes.record, self.own_user_id, target["object_id"],
                chat_id, message_id, text, reference, peer_hop,
            )
        return {"accepted": True, "channel": "teams", "chatId": chat_id,
                "messageId": message_id, "reference": reference,
                "chatType": "group" if shared else "oneOnOne", "sharedWithApprovers": shared}

    async def send_teams_group(self, targets, text, reference):
        if not isinstance(targets, list) or len(targets) != 2:
            raise ValueError("A shared approval chat requires exactly two bound approvers")
        identifiers = [self.own_user_id]
        for target in targets:
            UUID(target["object_id"])
            identifiers.append(target["object_id"])
        if len(set(identifiers)) != 3:
            raise PermissionError("Shared approval chat members must be distinct")
        if not text.strip() or len(text) > 16_384 or not reference:
            raise ValueError("A bounded message and stable reference are required")
        if self.teams_context is not None:
            if {target["object_id"] for target in targets} != set(self.teams_context["approver_ids"]):
                raise PermissionError("Approval recipients must match the bound coordination approvers")
            return await self.send(
                "teams", {"object_id": self.teams_context["peer_id"]}, text, reference,
            )
        members = [{
            "@odata.type": "#microsoft.graph.aadUserConversationMember",
            "roles": ["owner"],
            "user@odata.bind": f"https://graph.microsoft.com/v1.0/users('{identifier}')",
        } for identifier in identifiers]
        chat_response = await self._post("/chats", {
            "chatType": "group",
            "topic": "Hotel Treasurer approvals",
            "members": members,
        })
        chat_id = chat_response.json().get("id")
        if not isinstance(chat_id, str) or not chat_id or "/" in chat_id or "?" in chat_id:
            raise DeliveryUncertain("Graph returned an invalid group chat identity")
        response = await self._post(f"/chats/{chat_id}/messages", {
            "body": {"contentType": "text", "content": text},
        })
        message_id = response.json().get("id")
        if response.status_code != 201 or not isinstance(message_id, str) or not message_id:
            raise DeliveryUncertain("Graph did not return a confirmed Teams message identity")
        return {
            "accepted": True,
            "channel": "teams",
            "chatType": "group",
            "chatId": chat_id,
            "messageId": message_id,
            "reference": reference,
        }

    async def send_approval_email(self, targets, text, reference):
        if not isinstance(targets, list) or len(targets) != 2:
            raise ValueError("An approval email requires exactly two bound approvers")
        identifiers = [self.own_user_id]
        addresses = []
        for target in targets:
            UUID(target["object_id"])
            identifiers.append(target["object_id"])
            address = target["mail"]
            if address.count("@") != 1 or any(character in address for character in "\r\n"):
                raise ValueError("Bound approver has an invalid mailbox address")
            addresses.append(address)
        if len(set(identifiers)) != 3 or len({address.casefold() for address in addresses}) != 2:
            raise PermissionError("Approval email recipients must be distinct bound approvers")
        if not text.strip() or len(text) > 16_384 or not reference:
            raise ValueError("A bounded message and stable reference are required")
        response = await self._post("/me/sendMail", {
            "message": {
                "subject": "Payroll approval needed",
                "body": {"contentType": "Text", "content": text},
                "toRecipients": [{"emailAddress": {"address": addresses[0]}}],
                "ccRecipients": [{"emailAddress": {"address": addresses[1]}}],
            },
            "saveToSentItems": True,
        })
        if response.status_code != 202:
            raise DeliveryUncertain("Graph sendMail did not return the documented acceptance status")
        return {
            "accepted": True, "channel": "email", "reference": reference,
            "recipientObjectIds": [target["object_id"] for target in targets],
            "inboxDeliveryVerified": False,
        }
