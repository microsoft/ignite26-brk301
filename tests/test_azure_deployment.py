import copy
import importlib.util
import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "check_azure_deployment", ROOT / "scripts" / "check_azure_deployment.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class AzureDeploymentTests(unittest.TestCase):
    def test_build_comparison_rejects_resource_changes_but_not_generator_metadata(self):
        template = json.loads((ROOT / "infra" / "azuredeploy.json").read_text())
        regenerated = copy.deepcopy(template)
        regenerated["metadata"]["_generator"]["version"] = "different-compiler"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "compiled.json"
            path.write_text(json.dumps(regenerated))
            with redirect_stdout(io.StringIO()):
                self.assertEqual(MODULE.check(path), 0)
            regenerated["resources"].pop()
            path.write_text(json.dumps(regenerated))
            with redirect_stdout(io.StringIO()):
                self.assertEqual(MODULE.check(path), 1)

    def test_readme_button_opens_azure_with_the_committed_template(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        target = re.search(r"\[!\[Deploy this demo\].*?\]\(([^)]+)\)", readme).group(1)
        portal = urlsplit(target)
        self.assertEqual((portal.scheme, portal.netloc), ("https", "portal.azure.com"))
        raw_url = unquote(portal.fragment.removeprefix("create/Microsoft.Template/uri/"))
        raw = urlsplit(raw_url)
        self.assertEqual(
            (raw.scheme, raw.netloc),
            ("https", "raw.githubusercontent.com"),
        )
        prefix = "/microsoft/ignite26-brk301/main/"
        self.assertTrue(raw.path.startswith(prefix))
        template = json.loads((ROOT / raw.path.removeprefix(prefix)).read_text())
        self.assertTrue(template["resources"])

    def test_deployment_outputs_link_to_the_post_deployment_checklist(self):
        template = json.loads((ROOT / "infra" / "azuredeploy.json").read_text())
        target = template["outputs"]["NEXT_STEPS_URL"]["value"]
        prefix = "https://github.com/microsoft/ignite26-brk301/blob/main/"
        self.assertTrue(target.startswith(prefix))
        relative, anchor = target.removeprefix(prefix).split("#")
        guide = (ROOT / relative).read_text()
        self.assertEqual(anchor, "2-continue-after-azure-deployment")
        self.assertIn("## 2. Continue after Azure deployment", guide)
