import json
import unittest

import httpx

from payroll_agents.mcp_client import McpClient, McpProtocolError, McpToolError


class McpTests(unittest.IsolatedAsyncioTestCase):
    async def token(self):
        return "controlled-unit-test-token"

    async def test_discovery_session_pagination_and_sse_call(self):
        requests = []

        def respond(request):
            body = json.loads(request.content)
            requests.append(body)
            self.assertEqual(request.headers["Authorization"], "Bearer controlled-unit-test-token")
            if body["method"] == "initialize":
                result = {"protocolVersion": "2025-03-26", "capabilities": {}}
                return httpx.Response(200, json={"id": body["id"], "result": result},
                                      headers={"Mcp-Session-Id": "controlled-session"})
            self.assertEqual(request.headers["Mcp-Session-Id"], "controlled-session")
            if body["method"] == "notifications/initialized":
                return httpx.Response(202)
            if body["method"] == "tools/list":
                cursor = body["params"].get("cursor")
                result = {"tools": [{"name": "query" if not cursor else "read", "inputSchema": {}}]}
                if not cursor:
                    result["nextCursor"] = "page-2"
                return httpx.Response(200, json={"id": body["id"], "result": result})
            result = {"content": [{"type": "text", "text": "actual result"}]}
            message = json.dumps({"id": body["id"], "result": result})
            return httpx.Response(200, text=f"event: message\ndata: {message}\n\n",
                                  headers={"Content-Type": "text/event-stream"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = McpClient("https://controlled.example/mcp", self.token, http=http)
            self.assertEqual([tool["name"] for tool in await client.tools()], ["query", "read"])
            self.assertEqual((await client.call("query", {"question": "project"}))["content"][0]["text"],
                             "actual result")
            self.assertEqual(len(requests), 5)

    async def test_action_failures_are_not_retried(self):
        calls = []

        def respond(request):
            body = json.loads(request.content)
            calls.append(body["method"])
            if body["method"] == "initialize":
                return httpx.Response(200, json={"id": body["id"],
                                                "result": {"protocolVersion": "2025-03-26"}})
            if body["method"] == "notifications/initialized":
                return httpx.Response(202)
            return httpx.Response(200, json={"id": body["id"], "result": {
                "isError": True, "content": [{"type": "text", "text": "denied"}]}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = McpClient("https://controlled.example/mcp", self.token, http=http)
            with self.assertRaises(McpToolError):
                await client.call("send", {})
        self.assertEqual(calls.count("tools/call"), 1)

    async def test_notification_acceptance_does_not_parse_a_response_body(self):
        for status, text in ((202, "Accepted"), (202, "\n"), (204, ""), (200, "\n")):
            with self.subTest(status=status, text=text):
                async with httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda request: httpx.Response(status, text=text)
                )) as http:
                    client = McpClient("https://controlled.example/mcp", self.token, http=http)
                    self.assertEqual(
                        await client._rpc("notifications/initialized", {}, notification=True), {}
                    )
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(202, text="Accepted")
        )) as http:
            client = McpClient("https://controlled.example/mcp", self.token, http=http)
            with self.assertRaises(McpProtocolError):
                await client._rpc("tools/call", {"name": "query", "arguments": {}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(403, text="Rejected")
        )) as http:
            client = McpClient("https://controlled.example/mcp", self.token, http=http)
            with self.assertRaises(httpx.HTTPStatusError):
                await client._rpc("notifications/initialized", {}, notification=True)

    async def test_invalid_protocol_and_empty_tokens_fail_closed(self):
        async def missing():
            return ""

        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"id": 1, "result": {
                "protocolVersion": "unknown"}})
        )) as http:
            client = McpClient("https://controlled.example/mcp", self.token, http=http)
            with self.assertRaises(McpProtocolError):
                await client.initialize()
            client = McpClient("https://controlled.example/mcp", missing, http=http)
            with self.assertRaises(McpProtocolError):
                await client.initialize()

    async def test_http_failures_log_safe_diagnostics_and_never_retry(self):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(500, json={
                "errorCode": "BackendFailure",
                "requestId": "12345678-1234-1234-1234-123456789abc",
                "message": "SECRET_RESPONSE_BODY",
            })

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = McpClient("https://controlled.example/mcp", self.token, http=http)
            with self.assertLogs("payroll_agents.mcp_client", level="ERROR") as logs:
                with self.assertRaises(httpx.HTTPStatusError):
                    await client._rpc("tools/call", {"name": "query", "arguments": {}})
        self.assertEqual(len(requests), 1)
        output = "\n".join(logs.output)
        self.assertIn("BackendFailure", output)
        self.assertIn("12345678-1234-1234-1234-123456789abc", output)
        self.assertNotIn("SECRET_RESPONSE_BODY", output)
        self.assertNotIn("controlled-unit-test-token", output)

    async def test_untrusted_error_fields_and_non_json_bodies_are_not_logged(self):
        responses = [
            httpx.Response(500, text="SECRET_RESPONSE_BODY"),
            httpx.Response(500, json=["SECRET_RESPONSE_BODY"]),
            httpx.Response(500, json={
                "error": {"code": "SECRET_RESPONSE_BODY\ninjected"},
                "requestId": "SECRET_RESPONSE_BODY",
            }),
            httpx.Response(403, json={
                "error": {"code": "AuthorizationFailed", "message": "SECRET_RESPONSE_BODY"},
            }),
        ]
        for response in responses:
            with self.subTest(response=response):
                async with httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda request: response
                )) as http:
                    client = McpClient("https://controlled.example/mcp", self.token, http=http)
                    with self.assertLogs("payroll_agents.mcp_client", level="ERROR") as logs:
                        with self.assertRaises(httpx.HTTPStatusError):
                            await client._rpc("tools/call", {"name": "query", "arguments": {}})
                self.assertNotIn("SECRET_RESPONSE_BODY", "\n".join(logs.output))

    async def test_tool_error_diagnostics_never_log_private_result_text(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={
                "id": json.loads(request.content)["id"], "result": {
                    "isError": True, "content": [{"type": "text", "text": json.dumps({
                        "error": {"code": "AccessDenied", "message": "SECRET_RESPONSE_BODY"},
                    })}],
                },
            })
        )) as http:
            client = McpClient("https://controlled.example/mcp", self.token, http=http)
            client._initialized = True
            with self.assertLogs("payroll_agents.mcp_client", level="ERROR") as logs:
                with self.assertRaises(McpToolError):
                    await client.call("GetMessage", {})
        output = "\n".join(logs.output)
        self.assertIn("AccessDenied", output)
        self.assertNotIn("SECRET_RESPONSE_BODY", output)

    def test_endpoint_does_not_accept_credentials_or_insecure_http(self):
        for url in ("http://example.com/mcp", "https://user@example.com/mcp",
                    "https://example.com/mcp?sig=forbidden"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                McpClient(url, self.token)
