"""Bounded Streamable HTTP MCP calls, with no automatic action retries."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)


def safe_error_diagnostics(payload: Any) -> tuple[str | None, str | None]:
    if not isinstance(payload, dict):
        payload = {}
    error = payload.get("error")
    code = error.get("code") if isinstance(error, dict) else payload.get(
        "errorCode", payload.get("code"),
    )
    request_id = payload.get("requestId")
    if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", code):
        code = None
    if not isinstance(request_id, str) or not re.fullmatch(
        r"[A-Fa-f0-9]{8}(?:-[A-Fa-f0-9]{4}){3}-[A-Fa-f0-9]{12}", request_id
    ):
        request_id = None
    return code, request_id


def log_http_failure(response: httpx.Response, method: str) -> None:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    code, request_id = safe_error_diagnostics(payload)
    logger.error(
        "MCP %s rejected request: HTTP %s code=%r request_id=%r",
        method, response.status_code, code, request_id,
    )


class McpProtocolError(RuntimeError):
    pass


class McpToolError(RuntimeError):
    pass


class McpClient:
    def __init__(
        self,
        url: str,
        token: Callable[[], Awaitable[str]],
        *,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or parsed.query
        ):
            raise ValueError("MCP requires a configured HTTPS endpoint without credentials")
        self.url = url
        self._token = token
        self._http = http or httpx.AsyncClient(timeout=90, follow_redirects=False)
        self._owns_http = http is None
        self._session: str | None = None
        self._protocol = "2025-03-26"
        self._sequence = 0
        self._initialized = False

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def _rpc(
        self, method: str, params: dict[str, Any], *, notification: bool = False
    ) -> dict[str, Any]:
        self._sequence += 1
        identifier = self._sequence
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            payload["id"] = identifier
        bearer = await self._token()
        if not bearer:
            raise McpProtocolError("MCP token provider returned no access token")
        headers = {
            "Authorization": f"Bearer {bearer}",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self._protocol,
        }
        if self._session:
            headers["Mcp-Session-Id"] = self._session
        response = await self._http.post(self.url, json=payload, headers=headers)
        if response.is_error:
            log_http_failure(response, method)
        response.raise_for_status()
        session = response.headers.get("Mcp-Session-Id")
        if session:
            self._session = session
        if notification and (
            response.status_code in (202, 204)
            or response.status_code == 200 and not response.content.strip()
        ):
            return {}
        if response.headers.get("Content-Type", "").startswith("text/event-stream"):
            messages: list[dict[str, Any]] = []
            for event in response.text.replace("\r\n", "\n").split("\n\n"):
                text = "\n".join(
                    line[5:].lstrip() for line in event.splitlines() if line.startswith("data:")
                )
                if text:
                    item = json.loads(text)
                    if isinstance(item, dict):
                        messages.append(item)
        else:
            try:
                item = response.json()
            except json.JSONDecodeError:
                raise McpProtocolError("MCP returned an invalid JSON response") from None
            messages = [item] if isinstance(item, dict) else []
        matching = [item for item in messages if item.get("id") == identifier]
        if len(matching) != 1:
            raise McpProtocolError("MCP did not return exactly one response for this request")
        result = matching[0]
        if "error" in result:
            error = result["error"]
            raise McpProtocolError(f"MCP request rejected: {error.get('code')}: {error.get('message')}")
        if not isinstance(result.get("result"), dict):
            raise McpProtocolError("MCP returned a malformed result")
        return result["result"]

    async def initialize(self) -> None:
        if self._initialized:
            return
        result = await self._rpc("initialize", {
            "protocolVersion": self._protocol,
            "capabilities": {},
            "clientInfo": {"name": "hotel-payroll-treasurer-agents", "version": "0.1.0"},
        })
        protocol = result.get("protocolVersion")
        if protocol not in ("2024-11-05", "2025-03-26", "2025-06-18"):
            raise McpProtocolError("MCP server negotiated an unsupported protocol version")
        self._protocol = protocol
        await self._rpc("notifications/initialized", {}, notification=True)
        self._initialized = True

    async def tools(self) -> list[dict[str, Any]]:
        await self.initialize()
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(16):
            result = await self._rpc("tools/list", {"cursor": cursor} if cursor else {})
            page = result.get("tools")
            if not isinstance(page, list):
                raise McpProtocolError("MCP tool discovery returned no tool list")
            for tool in page:
                if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                    raise McpProtocolError("MCP returned an invalid tool descriptor")
                if not isinstance(tool.get("inputSchema"), dict) or tool["name"] in seen:
                    raise McpProtocolError("MCP returned duplicate tools or missing input schemas")
                seen.add(tool["name"])
                tools.append(tool)
            cursor = result.get("nextCursor")
            if not cursor:
                return tools
        raise McpProtocolError("MCP tool discovery exceeded the bounded page limit")

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        await self.initialize()
        result = await self._rpc("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            candidates = [result.get("structuredContent"), result]
            for content in result.get("content", []):
                text = content.get("text") if isinstance(content, dict) else None
                if isinstance(text, str) and len(text) <= 60_000:
                    try:
                        candidates.append(json.loads(text))
                    except ValueError:
                        pass
            diagnostics = [safe_error_diagnostics(value) for value in candidates]
            code = next((code for code, _ in diagnostics if code is not None), None)
            request_id = next((identifier for _, identifier in diagnostics if identifier), None)
            safe_name = name if re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", name) else None
            logger.error(
                "MCP tool %r reported failure: code=%r request_id=%r",
                safe_name, code, request_id,
            )
            raise McpToolError(f"MCP tool {name} reported failure")
        if not result.get("content") and not result.get("structuredContent"):
            raise McpProtocolError(f"MCP tool {name} returned no result content")
        return result
