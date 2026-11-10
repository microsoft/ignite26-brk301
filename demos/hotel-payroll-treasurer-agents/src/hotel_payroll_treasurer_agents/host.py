"""Keyless Microsoft 365 Activity Protocol host using the SDK's generic adapter."""

from __future__ import annotations

import json
import asyncio
import html
import logging
import os
from urllib.parse import urlsplit

from aiohttp import web
from azure.core.exceptions import AzureError
import httpx
from jwt import InvalidTokenError, PyJWKClientError
from microsoft_agents.activity import Activity, load_configuration_from_env
from microsoft_agents.authentication.msal import MsalConnectionManager
from microsoft_agents.hosting.core import (
    AgentApplication, AgentAuthConfiguration, AuthTypes, Authorization,
    ClaimsIdentity, HttpAdapterBase, JwtTokenValidator, MemoryStorage,
)
from .projection import ProjectionRefreshError

logger = logging.getLogger(__name__)


def required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Required hosted configuration is missing: {name}")
    return value


def gateway_identity(headers, blueprint: str, tenant: str) -> ClaimsIdentity:
    """Project authenticated Foundry transport context, not a human JWT identity."""
    required("FOUNDRY_AGENT_SESSION_ID")
    required("FOUNDRY_PROJECT_ENDPOINT")
    subject = headers.get("x-agent-user-id", "")
    if not subject or len(subject) > 1024 or any(ord(c) < 32 for c in subject):
        raise PermissionError("The protected Foundry user header is required")
    return ClaimsIdentity(
        {"aud": blueprint, "tid": tenant, "foundry_user_id": subject},
        True, authentication_type="FoundryGateway",
    )


class ActivityRequest:
    def __init__(self, request: web.Request, identity):
        self._request = request
        self._identity = identity

    @property
    def method(self):
        return self._request.method

    @property
    def headers(self):
        return dict(self._request.headers)

    async def json(self):
        return await self._request.json()

    def get_claims_identity(self):
        return self._identity

    def get_path_param(self, name):
        return self._request.match_info.get(name, "")


def validate_service_url(url: str) -> None:
    parsed = urlsplit(url)
    hostname = parsed.hostname or ""
    allowed = (
        hostname == "smba.trafficmanager.net"
        or hostname.endswith(".smba.trafficmanager.net")
        or hostname == "api.botframework.com"
        or hostname.endswith(".api.botframework.com")
        or hostname.endswith(".cloud.microsoft")
    )
    if (
        parsed.scheme != "https" or not allowed or parsed.username is not None
        or parsed.password is not None or parsed.port not in (None, 443)
        or parsed.query or parsed.fragment
    ):
        raise PermissionError("Activity service URL is not a trusted Microsoft endpoint")


def create_app():
    from .runtime import FinanceRuntime
    from microsoft_agents_a365.notifications import EmailResponse
    from microsoft_agents_a365.notifications.agent_notification import AgentNotification, ChannelId

    tenant = required("TENANT_ID")
    blueprint = required("FOUNDRY_AGENT_BLUEPRINT_CLIENT_ID")
    required("FOUNDRY_AGENT_DEFAULT_INSTANCE_CLIENT_ID")
    role = required("FINANCE_ROLE")
    if role not in ("hotel_finance", "banking_operations"):
        raise ValueError("FINANCE_ROLE is not a supported persona")
    ingress = os.environ.get("FINANCE_ACTIVITY_AUTHENTICATION", "signed_activity")
    if ingress not in ("signed_activity", "foundry_gateway"):
        raise ValueError("Unsupported Activity authentication mode")
    if ingress == "foundry_gateway":
        required("FOUNDRY_AGENT_SESSION_ID")
        required("FOUNDRY_PROJECT_ENDPOINT")
    settings = dict(os.environ)
    settings.update({
        "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__AUTHTYPE": "IdentityProxyManager",
        "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID": blueprint,
        "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID": tenant,
        "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__SCOPES__0":
            "5a807f24-c9de-44ee-a3a7-329e88a00ffc/.default",
        "CONNECTIONSMAP__0__SERVICEURL": "*",
        "CONNECTIONSMAP__0__CONNECTION": "SERVICE_CONNECTION",
        "AGENTAPPLICATION__USERAUTHORIZATION__AUTOSIGNIN": "false",
        "AGENTAPPLICATION__USERAUTHORIZATION__HANDLERS__AGENTIC__TYPE": "AgenticUserAuthorization",
        "AGENTAPPLICATION__USERAUTHORIZATION__HANDLERS__AGENTIC__SETTINGS__SCOPES":
            "ea9ffc3e-8a23-4a7d-836d-234d7c7565c1/.default",
    })
    configuration = load_configuration_from_env(settings)
    connections = MsalConnectionManager(**configuration)
    storage = MemoryStorage()
    adapter = HttpAdapterBase(connection_manager=connections)
    authorization = Authorization(storage, connections, **configuration)
    agent = AgentApplication(
        storage=storage, adapter=adapter, authorization=authorization,
        start_typing_timer=False, **configuration
    )
    validator = JwtTokenValidator(AgentAuthConfiguration(
        auth_type=AuthTypes.identity_proxy_manager,
        client_id=blueprint, tenant_id=tenant, anonymous_allowed=False,
    ))
    runtime = FinanceRuntime(role, tenant, connections)

    async def turn_error(context, error):
        logger.error("Activity processing failed: %s", type(error).__name__, exc_info=True)
        await context.send_activity(
            "The operation failed and is not reported as complete. "
            "No unapproved transfer is permitted. Please retain the request reference."
        )
        raise error

    adapter.on_turn_error = turn_error

    @agent.activity("installationUpdate")
    async def installation(context, state):
        if context.activity.action == "add":
            await context.send_activity(
                f"I'm your {runtime.display_name}. I act under my own Microsoft 365 identity. "
                "Financial access requires a manager-configured instance binding; "
                "additional hires do not inherit the project pair's resources."
            )

    async def process(context, notification_activity=None):
        result = await runtime.turn(context, agent.auth, notification_activity)
        if result is None:
            return
        channel = context.activity.channel_id
        if channel and (channel.channel == "email" or channel.sub_channel == "email"):
            await context.send_activity(EmailResponse.create_email_response_activity(
                "<pre>" + html.escape(result) + "</pre>"
            ))
        else:
            await context.send_activity(Activity(type="message", text=result))

    @agent.activity("message", auth_handlers=["AGENTIC"])
    async def message(context, state):
        await process(context)

    notifications = AgentNotification(agent)

    @notifications.on_agent_notification(
        channel_id=ChannelId(channel="agents", sub_channel="email"),
        auth_handlers=["AGENTIC"], is_agentic=True,
    )
    async def notification(context, state, notification_activity):
        await process(context, notification_activity)

    async def messages(request):
        header = request.headers.get("Authorization", "")
        if ingress == "foundry_gateway":
            try:
                identity = gateway_identity(request.headers, blueprint, tenant)
            except PermissionError:
                logger.warning("Activity rejected: protected Foundry transport identity missing")
                raise web.HTTPUnauthorized(text="Foundry transport identity is required") from None
        elif not header.startswith("Bearer ") or not header[7:]:
            logger.warning(
                "Activity rejected: bearer token missing; header names=%s",
                sorted(request.headers),
            )
            raise web.HTTPUnauthorized(text="A validated bearer token is required")
        else:
            try:
                identity = await validator.validate_token(header[7:])
            except (InvalidTokenError, PyJWKClientError, ValueError, KeyError):
                logger.warning("Inbound activity JWT validation failed")
                raise web.HTTPUnauthorized(text="Invalid activity token") from None
        claims = identity.claims
        if claims.get("aud", "").casefold() != blueprint.casefold():
            logger.warning("Activity rejected: unexpected token audience")
            raise web.HTTPUnauthorized(text="Activity token has the wrong audience")
        issuer = claims.get("iss")
        if ingress == "signed_activity" and issuer not in (
            "https://api.botframework.com",
            f"https://sts.windows.net/{tenant}/",
            f"https://login.microsoftonline.com/{tenant}/v2.0",
        ):
            logger.warning("Activity rejected: untrusted token issuer")
            raise web.HTTPUnauthorized(text="Untrusted activity issuer")
        if issuer != "https://api.botframework.com" and claims.get("tid") != tenant:
            logger.warning("Activity rejected: token tenant mismatch")
            raise web.HTTPUnauthorized(text="Activity token belongs to another tenant")
        try:
            body = await request.json()
            activity = Activity.model_validate(body)
            validate_service_url(activity.service_url or "")
            if claims.get("serviceurl") and claims["serviceurl"] != activity.service_url:
                raise PermissionError("Activity service URL does not match its signed claim")
            if activity.get_agentic_tenant_id() != tenant:
                raise PermissionError("Activity recipient belongs to another tenant")
            if claims.get("scp"):
                sender = activity.from_property
                if not sender or sender.aad_object_id != claims.get("oid"):
                    raise PermissionError("Delegated caller cannot assert another sender")
        except (json.JSONDecodeError, ValueError):
            raise web.HTTPBadRequest(text="Invalid Activity Protocol payload") from None
        except PermissionError:
            raise web.HTTPForbidden(text="Activity tenant, sender, or service URL is invalid") from None
        result = await adapter.process_request(ActivityRequest(request, identity), agent)
        if result.body is None:
            return web.Response(status=result.status_code)
        return web.json_response(result.body, status=result.status_code)

    async def health(request):
        return web.json_response({
            "activityHost": "ready", "role": role,
            "authentication": ingress, "outboundAuthentication": "IdentityProxyManager",
            "financialAccessRequiresBoundHiredInstance": True,
        })

    async def cleanup(app):
        await runtime.close()

    async def projection_worker(app):
        if role != "banking_operations":
            yield
            return
        stopping = asyncio.Event()

        async def work():
            while not stopping.is_set():
                try:
                    result = await runtime.refresh_pending_projections()
                    if result["status"] == "stale":
                        logger.warning("Post-transfer projection remains stale; queue is pending")
                except (AzureError, httpx.HTTPError, ProjectionRefreshError, RuntimeError, ValueError,
                        PermissionError, KeyError) as error:
                    logger.error(
                        "Post-transfer projection queue is pending: %s; "
                        "no financial action was replayed",
                        type(error).__name__,
                    )
                try:
                    await asyncio.wait_for(stopping.wait(), timeout=30)
                except TimeoutError:
                    pass

        task = asyncio.create_task(work())
        try:
            yield
        finally:
            stopping.set()
            await task

    app = web.Application(client_max_size=1_048_576)
    app.router.add_post("/api/messages", messages)
    for path in ("/", "/liveness", "/readiness"):
        app.router.add_get(path, health)
    app.on_cleanup.append(cleanup)
    app.cleanup_ctx.append(projection_worker)
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)
    logging.getLogger("microsoft_agents.authentication").setLevel(logging.WARNING)
    web.run_app(create_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8088")))
