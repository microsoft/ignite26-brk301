import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from bind_instances import (
    preserve_projection_seed, projection_only_upgrade, publish_projection_upgrade,
    validated_ledger, verify_hire,
)
from payroll_agents.ledger import DomainError, LedgerConfig


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.user = {
            "id": "agent-user", "identityParentId": "hired-identity",
            "mail": "agent@example.test", "accountEnabled": True,
        }
        self.identity = {
            "id": "hired-identity", "appId": "hired-app",
            "agentIdentityBlueprintId": "blueprint", "accountEnabled": True,
        }
        self.manager = {"id": "marco"}

    def verify(self):
        return verify_hire(
            self.user, self.identity, self.manager, "blueprint", "marco", "default-root",
        )

    def test_hired_identity_not_default_root_is_bound(self):
        self.assertEqual(self.verify()["instance_app_id"], "hired-app")
        self.assertEqual(self.verify()["agent_user_object_id"], "agent-user")

    def test_wrong_blueprint_manager_parent_disabled_or_unready_user_fails(self):
        for target, key, value in (
            ("identity", "agentIdentityBlueprintId", "unrelated"),
            ("identity", "id", "default-root"),
            ("identity", "accountEnabled", False),
            ("user", "identityParentId", "unrelated"),
            ("user", "accountEnabled", False),
            ("user", "mail", None),
            ("manager", "id", "another-person"),
        ):
            with self.subTest(target=target, key=key):
                previous = copy.deepcopy(getattr(self, target))
                getattr(self, target)[key] = value
                with self.assertRaises(PermissionError):
                    self.verify()
                setattr(self, target, previous)

    def test_existing_ledger_is_validated_without_reset(self):
        config = LedgerConfig("tenant", ("marco", "ayca"), "hotel")
        original = validated_ledger(config)
        original["accounts"]["hotel-payroll"]["balance_cents"] = 2_500_000
        self.assertEqual(validated_ledger(config, original), original)
        with self.assertRaises(DomainError):
            validated_ledger(LedgerConfig("other-tenant", ("marco", "ayca"), "hotel"), original)

    def test_projection_upgrade_cannot_change_existing_authorizations(self):
        existing = {"instances": {"bank": "bound-hire"}, "fabric": {"mcp_url": "bound-source"}}
        desired = {**existing, "fabric": {**existing["fabric"], "projection": {"writer": "root"}}}
        self.assertTrue(projection_only_upgrade(existing, desired))
        self.assertFalse(projection_only_upgrade(desired, desired))
        for replacement in (
            {**desired, "instances": {"bank": "another-hire"}},
            {**desired, "fabric": {"mcp_url": "another-source", "projection": {"writer": "root"}}},
        ):
            self.assertFalse(projection_only_upgrade(existing, replacement))

    def test_projection_upgrade_is_conditional_and_rejects_concurrent_binding_drift(self):
        existing = {"fabric": {"mcp_url": "bound-source"}}
        desired = {"fabric": {**existing["fabric"], "projection": {"writer": "root"}}}
        storage = {"storageAccountName": "storage"}
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "bindings.json"
            with patch("bind_instances.az", return_value={"properties": {"etag": "actual-etag"}}) as az:
                with patch("bind_instances.blob_json", return_value=existing):
                    publish_projection_upgrade(storage, existing, desired, destination)
                upload = az.call_args.args
                self.assertEqual(upload[upload.index("--if-match") + 1], "actual-etag")
                with patch("bind_instances.blob_json", return_value={"instances": "changed"}):
                    with self.assertRaises(RuntimeError):
                        publish_projection_upgrade(storage, existing, desired, destination)
                self.assertEqual(az.call_count, 3)

    def test_live_freshness_updates_do_not_change_the_immutable_binding_seed(self):
        original = {
            "writer": "root", "projection_freshness": {"source_etag": "baseline"},
        }
        existing = {"fabric": {"projection": original}}
        desired = {"fabric": {"projection": {
            **original, "projection_freshness": {"source_etag": "actual-live-version"},
        }}}
        preserve_projection_seed(existing, desired)
        self.assertEqual(desired, existing)
        desired["fabric"]["projection"]["writer"] = "foreign-root"
        with self.assertRaises(PermissionError):
            preserve_projection_seed(existing, desired)
