import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from deploy_agents import validate_tenant_endpoint
from hotel_payroll_treasurer_agents.host import gateway_identity


class GatewayAuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.environment = {
            "FOUNDRY_AGENT_SESSION_ID": "native-session",
            "FOUNDRY_PROJECT_ENDPOINT": "https://project.services.ai.azure.com/api/projects/project",
        }

    def test_platform_context_does_not_impersonate_a_human(self):
        with patch.dict(os.environ, self.environment, clear=True):
            identity = gateway_identity({"x-agent-user-id": "opaque-subject"}, "blueprint", "tenant")
        self.assertTrue(identity.is_authenticated)
        self.assertEqual(identity.authentication_type, "FoundryGateway")
        self.assertEqual(identity.get_app_id(), "blueprint")
        self.assertEqual(identity.claims["tid"], "tenant")
        self.assertNotIn("oid", identity.claims)
        self.assertNotIn("scp", identity.claims)
        self.assertIsNone(identity.security_token)

    def test_standalone_host_cannot_enable_gateway_authentication(self):
        for missing in self.environment:
            environment = dict(self.environment)
            del environment[missing]
            with self.subTest(missing=missing), patch.dict(os.environ, environment, clear=True):
                with self.assertRaises(RuntimeError):
                    gateway_identity({"x-agent-user-id": "subject"}, "blueprint", "tenant")

    def test_unprotected_client_header_and_invalid_subject_fail_closed(self):
        for headers in (
            {}, {"x-client-user-id": "subject"}, {"x-agent-user-id": ""},
            {"x-agent-user-id": "x" * 1025}, {"x-agent-user-id": "subject\n"},
        ):
            with self.subTest(headers=headers), patch.dict(os.environ, self.environment, clear=True):
                with self.assertRaises(PermissionError):
                    gateway_identity(headers, "blueprint", "tenant")

    def test_gateway_deployment_refuses_additional_or_unsigned_authorization(self):
        endpoint = {
            "authorization_schemes": [{"type": "BotServiceTenant"}],
            "protocols": ["activity"],
            "protocol_configuration": {"activity": {"enable_m365_public_endpoint": True}},
        }
        validate_tenant_endpoint(endpoint)
        for schemes in ([], [{"type": "BotServiceRbac"}], [
            {"type": "BotServiceTenant"}, {"type": "EntraID"},
        ]):
            with self.subTest(schemes=schemes), self.assertRaises(RuntimeError):
                validate_tenant_endpoint({**endpoint, "authorization_schemes": schemes})
