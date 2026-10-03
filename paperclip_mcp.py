"""Small stdio MCP bridge for an agent's company-scoped Paperclip API."""
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request

MAX_BYTES = 1024 * 1024
REDACTED_KEYS = {"token", "password", "privateKey", "authToken", "apiKey", "auth_bearer_token", "Authorization", "OPENAI_API_KEY", "PAPERCLIP_API_KEY", "CODEX_API_KEY"}


def redact(value):
    if isinstance(value, dict):
        return {key:redact(item) for key,item in value.items() if key not in REDACTED_KEYS}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str) and os.environ.get("PAPERCLIP_API_KEY"):
        return value.replace(os.environ["PAPERCLIP_API_KEY"], "<redacted>")
    return value


def allowed_path(path, company):
    # Paperclip verifies entity ownership and the caller's permissions.
    return isinstance(path, str) and len(path) < 2048 and ".." not in path and not any(c in path for c in "\\\r\n#%") and (
        path == "/agents/me" or path.startswith(f"/companies/{company}/")
        or re.fullmatch(r"/(?:agents|issues|approvals)/[a-f0-9-]{36}(?:/[a-zA-Z0-9_/-]+)?(?:\?[^#]*)?", path) is not None
    ) and not any(part in path.split("/") for part in ("keys", "secrets", "permissions", "config-revisions"))


def request_api(arguments):
    path = arguments.get("path")
    method = arguments.get("method", "GET")
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"} or not allowed_path(path, os.environ["PAPERCLIP_COMPANY_ID"]):
        raise ValueError("Unsupported Paperclip API request")
    body = None if method == "GET" else json.dumps(arguments.get("body", {})).encode()
    if body and len(body) > MAX_BYTES:
        raise ValueError("Paperclip request is too large")
    request = urllib.request.Request(os.environ["PAPERCLIP_API_URL"].rstrip("/") + "/api" + path,
        data=body, method=method, headers={"Authorization":"Bearer " + os.environ["PAPERCLIP_API_KEY"],
            "Content-Type":"application/json", "X-Paperclip-Run-Id":os.environ["PAPERCLIP_RUN_ID"]})
    context = ssl.create_default_context(cafile="/etc/ssl/certs/ca-certificates.crt")
    # A scoped credential must not follow redirects to another origin.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=context))
    with opener.open(request, timeout=30) as response:
        raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("Paperclip response is too large")
        return redact(json.loads(raw))


def handle(message):
    method = message.get("method")
    if method == "initialize":
        return {"protocolVersion":message.get("params", {}).get("protocolVersion", "2024-11-05"),
                "capabilities":{"tools":{}}, "serverInfo":{"name":"environment-paperclip","version":"0.1.0"}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools":[{"name":"paperclip_api", "description":"Read or update Paperclip records for your company. Use paths relative to /api. Paperclip enforces your agent permissions. Use this tool for hiring and task coordination.",
            "inputSchema":{"type":"object", "properties":{"method":{"type":"string", "enum":["GET","POST","PUT","PATCH","DELETE"],"default":"GET"}, "path":{"type":"string"}, "body":{"type":"object"}}, "required":["path"], "additionalProperties":False}}]}
    if method == "tools/call" and message.get("params", {}).get("name") == "paperclip_api":
        try:
            result = request_api(message["params"].get("arguments", {}))
            return {"content":[{"type":"text", "text":json.dumps(result)}]}
        except urllib.error.HTTPError as error:
            try:
                detail = redact(json.loads(error.read(MAX_BYTES)))
                text = json.dumps({"status":error.code,"error":detail.get("error","Request rejected"),"details":detail.get("details")})
            except Exception:
                text = f"Paperclip rejected the request (HTTP {error.code})."
            return {"isError":True, "content":[{"type":"text", "text":text}]}
        except Exception:
            return {"isError":True, "content":[{"type":"text", "text":"Paperclip request failed. Check the path, payload, connection, and scoped run credential."}]}
    raise ValueError("Unsupported MCP method")


def main():
    for line in sys.stdin:
        if len(line) > MAX_BYTES:
            return
        message = json.loads(line)
        if "id" not in message:
            continue
        response = {"jsonrpc":"2.0", "id":message["id"]}
        try:
            response["result"] = handle(message)
        except Exception:
            response["error"] = {"code":-32601, "message":"Unsupported or invalid MCP request"}
        print(json.dumps(response), flush=True)


if __name__ == "__main__":
    main()
