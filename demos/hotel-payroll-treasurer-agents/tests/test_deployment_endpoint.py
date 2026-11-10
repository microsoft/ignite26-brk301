import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from deploy_agents import account_model_endpoint, workflow_environment
import deploy_agents
import publish_agents


class AgentNamingTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "tenantId": "tenant", "subscriptionId": "subscription",
            "managerUpn": "manager@example.test", "resourceGroup": "finance",
            "foundry": {"account": "finance", "project": "finance"},
        }
        self.endpoint = "https://finance.services.ai.azure.com/api/projects/finance"
        self.agents = {
            "hotel_finance": {"name": "hotel-finance-autopilot", "version": "1"},
            "banking_operations": {"name": "banking-operations-autopilot", "version": "1"},
        }
        self.names = {
            "hotel-finance-autopilot": "Hotel Payroll",
            "banking-operations-autopilot": "Hotel Treasurer",
        }

    def operator_result(self, arguments):
        if arguments[:2] == ("account", "show"):
            return {"id": "subscription", "tenantId": "tenant"}
        if arguments[:3] == ("ad", "signed-in-user", "show"):
            return {"userPrincipalName": self.config["managerUpn"]}
        return None

    def test_publication_uses_exact_new_hire_names_with_existing_resources(self):
        deployed = {
            "tenant_id": "tenant", "project_endpoint": self.endpoint,
            "api_version": "2025-11-15-preview", "agents": self.agents,
        }
        published = {}

        def az(*arguments):
            operator = self.operator_result(arguments)
            if operator is not None:
                return operator
            url = arguments[arguments.index("--url") + 1]
            method = arguments[arguments.index("--method") + 1]
            if method == "get":
                return {"status": "active", "blueprint": {"client_id": url.split("/agents/")[1]}}
            if method == "post":
                name = url.split("/agents/")[1].split("/")[0]
                published[name] = json.loads(arguments[arguments.index("--body") + 1])
            return {}

        with patch.object(publish_agents, "az", side_effect=az), patch.object(
            Path, "read_text", side_effect=[json.dumps(self.config), json.dumps(deployed)]
        ), patch.object(Path, "write_text"), patch(
            "sys.argv", ["publish_agents.py", "--config", "config.json"]
        ), patch("builtins.print"):
            publish_agents.main()

        self.assertEqual(set(published), set(self.names))
        for resource, name in self.names.items():
            self.assertEqual(published[resource]["agentDisplayName"], name)
            self.assertTrue(published[resource]["fullDescription"].startswith(name + " "))

    def test_deployment_descriptions_use_new_names_without_changing_roles(self):
        storage = {key: key for key in (
            "runtimeBindingsBlobUrl", "runtimeBindingsContainerId", "ledgerContainerId",
            "bankTasksContainerId", "hotelTasksContainerId",
        )}
        storage.update(tenantId="tenant", subscriptionId="subscription")
        versions = {}

        def az(*arguments):
            operator = self.operator_result(arguments)
            if operator is not None:
                return operator
            if arguments[:3] == ("cognitiveservices", "account", "show"):
                return {"id": "/account", "properties": {"endpoints": {
                    "OpenAI Language Model Instance API": "https://finance.openai.azure.com/",
                }}}
            if arguments[:2] == ("role", "assignment"):
                return {}
            method = arguments[arguments.index("--method") + 1]
            if method == "get":
                return {"properties": {"endpoints": {"AI Foundry API": self.endpoint}}}
            url = arguments[arguments.index("--url") + 1]
            name = url.split("/agents/")[1].split("/")[0]
            versions[name] = json.loads(arguments[arguments.index("--body") + 1])
            return {"name": name, "blueprint": {"client_id": name}, "version": "1",
                    "instance_identity": {"principal_id": name}}

        with patch.object(deploy_agents, "az", side_effect=az), patch.object(
            Path, "read_text", side_effect=[
                json.dumps(self.config), json.dumps(storage),
                json.dumps({"searchResourceId": "/search"}), json.dumps({"searchResourceId": "/search"}),
            ]
        ), patch.object(Path, "write_text"), patch.object(Path, "mkdir"), patch(
            "sys.argv", ["deploy_agents.py", "--config", "config.json",
                         "--image", "finance.azurecr.io/runtime@sha256:" + "a" * 64,
                         "--project-endpoint", self.endpoint]
        ), patch("builtins.print"):
            deploy_agents.main()

        self.assertEqual(set(versions), set(self.names))
        for role, agent in self.agents.items():
            body = versions[agent["name"]]
            self.assertTrue(body["description"].startswith(self.names[agent["name"]] + ":"))
            self.assertEqual(body["definition"]["environment_variables"]["FINANCE_ROLE"], role)


class ModelEndpointTests(unittest.TestCase):
    def account(self, endpoint):
        return {"properties": {"endpoints": {"OpenAI Language Model Instance API": endpoint}}}

    def test_model_endpoint_comes_from_the_selected_account(self):
        endpoint = "https://finance.openai.azure.com/"
        self.assertEqual(account_model_endpoint(self.account(endpoint)), endpoint.rstrip("/"))

    def test_credentials_and_non_account_endpoints_fail_closed(self):
        for endpoint in (
            "http://finance.openai.azure.com",
            "https://finance.openai.azure.com.evil.test",
            "https://user:secret@finance.openai.azure.com",
            "https://finance.openai.azure.com:444",
            "https://finance.openai.azure.com/?key=secret",
            "https://finance.services.ai.azure.com/api/projects/finance",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                account_model_endpoint(self.account(endpoint))

    def test_missing_advertised_endpoint_is_not_guessed(self):
        with self.assertRaises(KeyError):
            account_model_endpoint({"properties": {"endpoints": {}}})


class WorkflowConfigurationTests(unittest.TestCase):
    def test_default_new_deployment_keeps_three_iq_and_single_ledger(self):
        self.assertEqual(workflow_environment({}), {})

    def test_updates_preserve_existing_demo_settings_without_copying_identity_fields(self):
        settings = {
            "FINANCE_WORKFLOW_MODE": "collaboration", "FINANCE_DEMO_REPLAY": "true",
            "FINANCE_PAYROLL_TEAMS_CHAT_ID": "19:payroll_funding@thread.v2",
        }
        self.assertEqual(workflow_environment({**settings, "TENANT_ID": "tenant"}), settings)
        self.assertEqual(
            workflow_environment({}, mode="collaboration", replay=True,
                                 chat_id=settings["FINANCE_PAYROLL_TEAMS_CHAT_ID"]),
            settings,
        )

    def test_invalid_or_incompatible_settings_cannot_be_deployed(self):
        for settings in (
            {"mode": "unknown"}, {"replay": True}, {"mode": "three_iq", "replay": True},
            {"chat_id": "arbitrary"}, {"chat_id": "19:group@thread.v2/messages"},
        ):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                workflow_environment({}, **settings)
        with self.assertRaises(ValueError):
            workflow_environment({"FINANCE_DEMO_REPLAY": "yes"})

    def test_explicit_transition_out_of_replay_requires_disabling_it(self):
        previous = {"FINANCE_WORKFLOW_MODE": "collaboration", "FINANCE_DEMO_REPLAY": "true"}
        with self.assertRaises(ValueError):
            workflow_environment(previous, mode="three_iq")
        self.assertEqual(
            workflow_environment(previous, mode="three_iq", replay=False),
            {"FINANCE_WORKFLOW_MODE": "three_iq", "FINANCE_DEMO_REPLAY": "false"},
        )
