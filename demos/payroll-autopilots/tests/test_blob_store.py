from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from azure.core import MatchConditions
from azure.core.exceptions import HttpResponseError

from finance_autopilots.blob_store import BlobStateStore, InvalidStoredState


def service_error(status: int, code: str) -> HttpResponseError:
    error = HttpResponseError(message=code)
    error.status_code = status
    error.error_code = code
    return error


class BlobStateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = Mock()
        self.store = BlobStateStore(self.client)

    def download(self, body: bytes, etag: str = '"version-1"') -> None:
        self.client.download_blob.return_value = SimpleNamespace(
            size=len(body), readall=lambda: body, properties=SimpleNamespace(etag=etag)
        )

    def test_reads_snapshot_with_opaque_etag(self) -> None:
        self.download(b'{"accounts":{"payroll":1000000}}')
        result = self.store.read()
        self.assertEqual(result.version, '"version-1"')
        self.assertEqual(result.state, {"accounts": {"payroll": 1000000}})

    def test_only_blob_not_found_is_empty(self) -> None:
        self.client.download_blob.side_effect = service_error(404, "BlobNotFound")
        self.assertIsNone(self.store.read().state)
        for status, code in (
            (404, "ContainerNotFound"),
            (403, "AuthorizationPermissionMismatch"),
            (429, "ServerBusy"),
            (500, "InternalError"),
        ):
            with self.subTest(code=code):
                error = service_error(status, code)
                self.client.download_blob.side_effect = error
                with self.assertRaises(HttpResponseError) as raised:
                    self.store.read()
                self.assertIs(raised.exception, error)

    def test_corruption_and_missing_etag_fail_closed(self) -> None:
        for payload in (b"\xff", b"{invalid", b"[]", b"null"):
            with self.subTest(payload=payload):
                self.download(payload)
                with self.assertRaises(InvalidStoredState):
                    self.store.read()
        self.download(b"{}", etag="")
        with self.assertRaises(InvalidStoredState):
            self.store.read()

    def test_create_is_not_overwrite(self) -> None:
        self.assertTrue(self.store.compare_and_swap(None, {"simulation": True}))
        body = self.client.upload_blob.call_args.args[0]
        options = self.client.upload_blob.call_args.kwargs
        self.assertEqual(json.loads(body), {"simulation": True})
        self.assertFalse(options["overwrite"])
        self.assertNotIn("etag", options)
        self.assertEqual(options["content_settings"].content_type, "application/json")

    def test_update_requires_exact_etag(self) -> None:
        self.assertTrue(self.store.compare_and_swap('"original"', {"sequence": 1}))
        options = self.client.upload_blob.call_args.kwargs
        self.assertTrue(options["overwrite"])
        self.assertEqual(options["etag"], '"original"')
        self.assertEqual(options["match_condition"], MatchConditions.IfNotModified)

    def test_conflicts_are_not_operational_failures(self) -> None:
        for version, status, code in (
            (None, 409, "BlobAlreadyExists"),
            ('"original"', 412, "ConditionNotMet"),
        ):
            with self.subTest(code=code):
                self.client.upload_blob.side_effect = service_error(status, code)
                self.assertFalse(self.store.compare_and_swap(version, {}))
        for version, status, code in (
            (None, 409, "ContainerBeingDeleted"),
            ('"original"', 412, "LeaseIdMissing"),
            ('"original"', 404, "BlobNotFound"),
            (None, 403, "AuthorizationPermissionMismatch"),
        ):
            with self.subTest(code=code):
                error = service_error(status, code)
                self.client.upload_blob.side_effect = error
                with self.assertRaises(HttpResponseError) as raised:
                    self.store.compare_and_swap(version, {})
                self.assertIs(raised.exception, error)

    def test_rejects_wildcard_invalid_state_and_nonfinite_json(self) -> None:
        for version in ("", "*", 123):
            with self.subTest(version=version):
                with self.assertRaises(ValueError):
                    self.store.compare_and_swap(version, {})
        with self.assertRaises(TypeError):
            self.store.compare_and_swap(None, [])
        with self.assertRaises(ValueError):
            self.store.compare_and_swap(None, {"amount": float("nan")})
        self.client.upload_blob.assert_not_called()

    def test_size_limits_apply_to_read_and_write(self) -> None:
        store = BlobStateStore(self.client, max_bytes=8)
        self.download(b'{"too":"large"}')
        with self.assertRaises(InvalidStoredState):
            store.read()
        with self.assertRaises(ValueError):
            store.compare_and_swap(None, {"too": "large"})
        self.client.upload_blob.assert_not_called()
        with self.assertRaises(ValueError):
            BlobStateStore(self.client, max_bytes=0)

    def test_factory_rejects_keys_sas_and_non_azure_endpoints(self) -> None:
        credential = Mock(get_token=Mock())
        url = "https://finance.blob.core.windows.net/ledger/state.json"
        with patch("finance_autopilots.blob_store.BlobClient") as sdk:
            store = BlobStateStore.from_url(url, credential)
            self.assertIsInstance(store, BlobStateStore)
            sdk.from_blob_url.assert_called_once_with(url, credential=credential)
        for invalid in (
            url + "?sig=forbidden",
            "http://finance.blob.core.windows.net/ledger/state.json",
            "https://finance.blob.core.windows.net.evil.example/ledger/state.json",
            "https://finance.blob.core.windows.net/ledger",
            "https://user:password@finance.blob.core.windows.net/ledger/state.json",
            "https://@finance.blob.core.windows.net/ledger/state.json",
            url + "#fragment",
        ):
            with self.subTest(url=invalid):
                with self.assertRaises(ValueError):
                    BlobStateStore.from_url(invalid, credential)
        with self.assertRaises(TypeError):
            BlobStateStore.from_url(url, "a-shared-key")


if __name__ == "__main__":
    unittest.main()
