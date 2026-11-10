"""Entra-authenticated, whole-snapshot optimistic concurrency for the ledger."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

from azure.core import MatchConditions
from azure.core.credentials import TokenCredential
from azure.core.exceptions import HttpResponseError
from azure.storage.blob import BlobClient, ContentSettings

from .ledger import Snapshot


class InvalidStoredState(RuntimeError):
    pass


class BlobStateStore:
    def __init__(self, client: BlobClient, *, max_bytes: int = 1_048_576) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self._client = client
        self._max_bytes = max_bytes

    @classmethod
    def from_url(
        cls, blob_url: str, credential: TokenCredential, *, max_bytes: int = 1_048_576
    ) -> BlobStateStore:
        parsed = urlsplit(blob_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not parsed.hostname.endswith(".blob.core.windows.net")
            or parsed.port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or len(parsed.path.strip("/").split("/")) < 2
        ):
            raise ValueError("An HTTPS Azure Blob URL without SAS or userinfo is required")
        if not callable(getattr(credential, "get_token", None)):
            raise TypeError("An Entra TokenCredential is required; keys are not supported")
        return cls(
            BlobClient.from_blob_url(blob_url, credential=credential),
            max_bytes=max_bytes,
        )

    def read(self) -> Snapshot:
        try:
            response = self._client.download_blob()
            if response.size > self._max_bytes:
                raise InvalidStoredState("Stored snapshot exceeds the configured size limit")
            payload = response.readall()
        except HttpResponseError as error:
            if error.status_code == 404 and getattr(error, "error_code", None) == "BlobNotFound":
                return Snapshot(version=None, state=None)
            raise
        if len(payload) > self._max_bytes:
            raise InvalidStoredState("Stored snapshot exceeds the configured size limit")
        try:
            state = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise InvalidStoredState("Stored snapshot is not valid UTF-8 JSON") from error
        if not isinstance(state, dict):
            raise InvalidStoredState("Stored snapshot must be a JSON object")
        etag = response.properties.etag
        if not isinstance(etag, str) or not etag:
            raise InvalidStoredState("Azure Blob did not return a concurrency ETag")
        return Snapshot(version=etag, state=state)

    def compare_and_swap(
        self, expected_version: str | None, state: dict[str, Any]
    ) -> bool:
        if not isinstance(state, dict):
            raise TypeError("State must be a dictionary")
        if expected_version is not None and (
            not isinstance(expected_version, str)
            or not expected_version
            or expected_version == "*"
        ):
            raise ValueError("Updates require the exact ETag returned by read()")
        payload = json.dumps(
            state, ensure_ascii=True, allow_nan=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        if len(payload) > self._max_bytes:
            raise ValueError("Snapshot exceeds the configured size limit")
        options: dict[str, Any] = {
            "overwrite": expected_version is not None,
            "content_settings": ContentSettings(content_type="application/json"),
        }
        if expected_version is not None:
            options.update(
                etag=expected_version, match_condition=MatchConditions.IfNotModified
            )
        try:
            self._client.upload_blob(payload, **options)
        except HttpResponseError as error:
            if (
                expected_version is None
                and error.status_code == 409
                and getattr(error, "error_code", None) == "BlobAlreadyExists"
            ):
                return False
            if (
                expected_version is not None
                and error.status_code == 412
                and getattr(error, "error_code", None) == "ConditionNotMet"
            ):
                return False
            raise
        return True
