"""Azure CLI-only HTTP transport. Never persists tokens or verbose CLI logs."""
from __future__ import annotations

import json
import ast
import re
import subprocess
import time
from urllib.parse import urlparse

FABRIC = "https://api.fabric.microsoft.com"


def redact(value: str) -> str:
    value = re.sub(r"(?im)^.*(?:authorization|accessToken|refreshToken|client_secret).*$",
                   "[redacted credential line]", value)
    value = re.sub(r"(?i)Bearer\s+\S+", "Bearer [redacted]", value)
    return re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b",
                  "[redacted token]", value)


def cli(*args: str):
    result = subprocess.run(["az", *args, "--output", "json"],
                            capture_output=True, text=True, timeout=180)
    if result.returncode:
        raise RuntimeError(redact(result.stderr)[-2500:])
    return json.loads(result.stdout) if result.stdout.strip() else {}


def parse_response(stdout: str, stderr: str):
    """Only extract allowlisted response headers; never return raw verbose logs."""
    statuses = re.findall(r"Response status:\s*(\d{3})", stderr, re.I)
    status = int(statuses[-1]) if statuses else None
    headers = {}
    # Azure CLI formats verbose response headers as a Python dictionary.
    response_log = (stderr.split("Response headers:")[-1].split("Response content:", 1)[0]
                    if "Response headers:" in stderr else "")
    for name in ("location", "x-ms-operation-id", "retry-after", "mcp-session-id", "etag"):
        hits = re.findall(
            r"['\"]" + name + r"""['"]\s*:\s*("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')""",
            response_log, re.I,
        )
        if hits:
            headers[name] = ast.literal_eval(hits[-1])
    text = stdout.strip()
    if not text or (status in (202, 204) and text == "Accepted"):
        body = {}
    elif text.startswith("data:") or "\ndata:" in text:
        messages = [json.loads(line[5:].strip()) for line in text.splitlines()
                    if line.startswith("data:") and line[5:].strip() not in ("", "[DONE]")]
        body = next((m for m in reversed(messages) if "result" in m or "error" in m),
                    messages[-1] if messages else {})
    else:
        body = json.loads(text)
    return status, headers, body


class FabricRest:
    def __init__(self, timeout_seconds=900):
        self.timeout_seconds = timeout_seconds
        self.events = []

    def raw(self, method, url, body=None, headers=None, resource=FABRIC,
            skill="spark-cli", timeout_seconds=300):
        if timeout_seconds <= 0:
            raise ValueError("Positive HTTP timeout required")
        if url.startswith("/"):
            url = FABRIC + "/v1" + url
        host = urlparse(url).hostname
        audiences = {"api.fabric.microsoft.com": FABRIC,
                     "onelake.table.fabric.microsoft.com": "https://storage.azure.com",
                     "onelake.dfs.fabric.microsoft.com": "https://storage.azure.com"}
        if host not in audiences or resource != audiences[host]:
            raise ValueError("Only allowlisted Fabric/OneLake hosts with their exact audiences are permitted")
        args = ["az", "rest", "--method", method, "--url", url,
                "--resource", resource, "--output", "json", "--verbose",
                "--headers", f"x-ms-fabric-skill={skill}", "Content-Type=application/json"]
        args.extend(f"{k}={v}" for k, v in (headers or {}).items())
        if body is not None:
            args.extend(["--body", json.dumps(body, separators=(",", ":"))])
        result = subprocess.run(args, capture_output=True, text=True, timeout=min(300, timeout_seconds))
        status, response_headers, response = parse_response(result.stdout, result.stderr)
        event = {"method": method, "url": url, "http_status": status,
                 "operation_id": response_headers.get("x-ms-operation-id")}
        self.events.append(event)
        if result.returncode:
            response_errors = re.findall(r"INFO: (\{[^\n]+\})", result.stderr)
            error = {}
            for candidate in reversed(response_errors):
                try:
                    parsed = json.loads(candidate)
                except ValueError:
                    continue
                if "errorCode" in parsed or "error" in parsed:
                    error = parsed
                    break
            event["error"] = error or {"message": "Azure CLI request failed"}
            raise RuntimeError(f"{method} {url}: {redact(json.dumps(event['error']))}")
        return status, response_headers, response

    def request(self, method, url, body=None, *, result_required=True, skill="spark-cli"):
        status, headers, response = self.raw(method, url, body, skill=skill)
        if status != 202:
            return response
        operation_id = headers.get("x-ms-operation-id")
        location = headers.get("location", "")
        if not operation_id:
            match = re.search(r"/operations/([0-9a-f-]{36})", location, re.I)
            operation_id = match.group(1) if match else None
        if not operation_id:
            raise RuntimeError("202 without operation ID; inspect owned item/job, do not retry POST")
        poll_url = f"/operations/{operation_id}"
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            state = self.raw("get", poll_url, skill=skill)[2]
            if state.get("status") == "Succeeded":
                # Mutations may have a 204 result; read operations need their result.
                return self.raw("get", poll_url + "/result", skill=skill)[2] if result_required else state
            if state.get("status") in ("Failed", "Cancelled", "Canceled"):
                raise RuntimeError(json.dumps(state))
            time.sleep(min(max(int(headers.get("retry-after", "5")), 2), 30))
        raise TimeoutError(f"Operation {operation_id} exceeded bounded deadline; do not resubmit")

    def listing(self, url, skill="spark-cli"):
        values = []
        while url:
            page = self.request("get", url, skill=skill)
            values.extend(page["value"])
            url = page.get("continuationUri")
            if not url and page.get("continuationToken"):
                raise RuntimeError("Pagination token without continuation URI; refusing incomplete lookup")
        return values
