import importlib.util
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "check_public_sample", Path(__file__).resolve().parents[1] / "scripts/check_public_sample.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class PublicationTests(unittest.TestCase):
    def test_operator_state_is_rejected(self):
        for path in (
            "demos/example/.state/bindings.json",
            "demos/example/.foundry/agent-metadata.yaml",
            "demos/example/config/environment.local.json",
            "demos/example/.env.local",
        ):
            with self.subTest(path=path):
                self.assertTrue(MODULE.problems_for_file(Path(path), "{}"))

    def test_environment_templates_are_allowed(self):
        for path in (
            "demos/example/.env.template",
            "demos/example/web/.env.example",
            "demos/example/config/azd.env.example",
        ):
            with self.subTest(path=path):
                self.assertEqual(MODULE.problems_for_file(Path(path), "REPLACE_WITH_VALUE"), [])

    def test_embedded_credentials_and_live_demo_domains_are_rejected(self):
        for content in (
            "-----BEGIN " + "PRIVATE KEY-----",
            "ghp_" + "a" * 36,
            "https://storage.example/file?" + "sig=" + "a" * 40,
            "person@" + "cam365" + "0000.onmicrosoft.com",
        ):
            with self.subTest(content=content[:20]):
                self.assertTrue(MODULE.problems_for_file(Path("config/example.json"), content))

    def test_synthetic_and_platform_values_are_allowed(self):
        self.assertEqual(
            MODULE.problems_for_file(
                Path("config/example.json"),
                "maria.garcia@example.com https://graph.microsoft.com "
                "00000000-0000-4000-8000-000000000001",
            ), []
        )
