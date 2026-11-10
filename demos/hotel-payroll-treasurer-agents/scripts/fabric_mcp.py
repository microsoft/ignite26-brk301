"""Small Azure CLI-only streamable HTTP MCP client; no credential fallbacks."""
from fabric_rest import FABRIC


def query_mcp(api, url, questions, skill="fabriciq-ontology-cli"):
    headers = {"Accept": "application/json, text/event-stream"}
    sequence = 0

    def rpc(method, params=None, notification=False):
        nonlocal sequence
        sequence += 1
        body = {"jsonrpc": "2.0", "method": method}
        if not notification:
            body["id"] = sequence
        if params is not None:
            body["params"] = params
        status, response_headers, response = api.raw("post", url, body, headers, skill=skill)
        if response_headers.get("mcp-session-id"):
            headers["Mcp-Session-Id"] = response_headers["mcp-session-id"]
        if response.get("error"):
            raise RuntimeError(f"MCP protocol error: {response['error']}")
        return response

    initialize = rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "finance-source-bound-validation", "version": "1.0"}})
    headers["MCP-Protocol-Version"] = initialize["result"]["protocolVersion"]
    rpc("notifications/initialized", notification=True)
    tools = rpc("tools/list")
    advertised = tools["result"]["tools"]
    if len(advertised) != 1:
        raise RuntimeError("Expected exactly one published DataAgent tool")
    tool = advertised[0]
    properties = tool["inputSchema"]["properties"]
    question_arg = next(iter(tool["inputSchema"].get("required", properties)))
    responses = []
    for question in questions:
        response = rpc("tools/call", {"name": tool["name"],
                                     "arguments": {question_arg: question}})
        responses.append({"question": question, "response": response})
    return {"initialize": initialize, "tools": tools, "queries": responses}


def response_text(response):
    return "\n".join(v.get("text", "") for v in response.get("result", {}).get("content", [])
                     if v.get("type") == "text")
