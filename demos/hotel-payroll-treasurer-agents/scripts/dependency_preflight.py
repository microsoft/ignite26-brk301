"""Verify the supported keyless SDK and storage provider inside the Linux artifact."""

from __future__ import annotations

import importlib.metadata
import json
from pathlib import Path

from azure.identity import ManagedIdentityCredential
from azure.storage.blob import BlobClient
from microsoft_agents.activity import Activity, load_configuration_from_env
from microsoft_agents.authentication.msal import MsalConnectionManager
from microsoft_agents.hosting.core import (
    AgentApplication,
    AgentAuthConfiguration,
    AuthTypes,
    Authorization,
    HttpAdapterBase,
    JwtTokenValidator,
    MemoryStorage,
)


def main() -> None:
    manifest = json.loads(Path("/opt/finance-dependency-manifest.json").read_text())
    for name, version in manifest["packages"].items():
        if importlib.metadata.version(name) != version:
            raise RuntimeError(f"Incorrect packaged SDK version for {name}")
    settings = {
        "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__AUTHTYPE": "IdentityProxyManager",
        "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID": "00000000-0000-0000-0000-000000000001",
        "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID": "00000000-0000-0000-0000-000000000002",
        "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__SCOPES__0": "https://api.botframework.com/.default",
        "CONNECTIONSMAP__0__SERVICEURL": "*",
        "CONNECTIONSMAP__0__CONNECTION": "SERVICE_CONNECTION",
        "AGENTAPPLICATION__USERAUTHORIZATION__AUTOSIGNIN": "false",
        "AGENTAPPLICATION__USERAUTHORIZATION__HANDLERS__AGENTIC__TYPE": "AgenticUserAuthorization",
        "AGENTAPPLICATION__USERAUTHORIZATION__HANDLERS__AGENTIC__SETTINGS__SCOPES": "ea9ffc3e-8a23-4a7d-836d-234d7c7565c1/.default",
    }
    configuration = load_configuration_from_env(settings)
    connections = MsalConnectionManager(**configuration)
    storage = MemoryStorage()
    adapter = HttpAdapterBase(connection_manager=connections)
    authorization = Authorization(storage, connections, **configuration)
    application = AgentApplication(
        storage=storage,
        adapter=adapter,
        authorization=authorization,
        **configuration,
    )
    jwt_config = AgentAuthConfiguration(
        auth_type=AuthTypes.identity_proxy_manager,
        client_id=settings["CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID"],
        tenant_id=settings["CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID"],
        anonymous_allowed=False,
    )
    JwtTokenValidator(jwt_config)
    activity = Activity(type="message", text="Controlled preflight; no message is sent.")
    if not activity.text or not callable(application.on_turn):
        raise RuntimeError("Activity host SDK initialization failed")
    credential = ManagedIdentityCredential(client_id=jwt_config.CLIENT_ID)
    BlobClient.from_blob_url(
        "https://controlled.blob.core.windows.net/validation/preflight.json",
        credential=credential,
    )
    credential.close()
    print(json.dumps({
        "platform": "linux/amd64",
        "keylessSdk": "IdentityProxyManager",
        "activityAdapter": "HttpAdapterBase",
        "agenticAuthorizationInitialized": True,
        "anonymousAuthenticationAllowed": False,
        "liveManagedIdentityTokenAcquired": False,
        "liveAgentMessagingVerified": False,
        "packages": manifest["packages"],
    }, indent=2))


if __name__ == "__main__":
    main()
