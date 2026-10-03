"""Small stdio MCP bridge for an agent's company-scoped Paperclip API."""
import json
import contextlib
import hmac
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import re
import ssl
import sys
import urllib.error
import urllib.request

MAX_BYTES = 1024 * 1024
REDACTED_KEYS = {"token", "password", "privateKey", "authToken", "apiKey", "auth_bearer_token", "Authorization", "OPENAI_API_KEY", "PAPERCLIP_API_KEY", "CODEX_API_KEY"}


def redact(value, context=None):
    context = os.environ if context is None else context
    if isinstance(value, dict):
        return {key:redact(item, context) for key,item in value.items() if key not in REDACTED_KEYS}
    if isinstance(value, list):
        return [redact(item, context) for item in value]
    if isinstance(value, str) and context.get("PAPERCLIP_API_KEY"):
        return value.replace(context["PAPERCLIP_API_KEY"], "<redacted>")
    return value


def allowed_path(path, company):
    # Paperclip verifies entity ownership and the caller's permissions.
    return isinstance(path, str) and len(path) < 2048 and ".." not in path and not any(c in path for c in "\\\r\n#%") and (
        path == "/agents/me" or path.startswith(f"/companies/{company}/")
        or re.fullmatch(r"/(?:agents|issues|approvals)/[a-f0-9-]{36}(?:/[a-zA-Z0-9_/-]+)?(?:\?[^#]*)?", path) is not None
    ) and not any(part in path.split("/") for part in ("keys", "secrets", "permissions", "config-revisions"))


def request_api(arguments, context=None):
    context = os.environ if context is None else context
    path = arguments.get("path")
    method = arguments.get("method", "GET")
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"} or not allowed_path(path, context["PAPERCLIP_COMPANY_ID"]):
        raise ValueError("Unsupported Paperclip API request")
    body = None if method == "GET" else json.dumps(arguments.get("body", {})).encode()
    if body and len(body) > MAX_BYTES:
        raise ValueError("Paperclip request is too large")
    request = urllib.request.Request(context["PAPERCLIP_API_URL"].rstrip("/") + "/api" + path,
        data=body, method=method, headers={"Authorization":"Bearer " + context["PAPERCLIP_API_KEY"],
            "Content-Type":"application/json", "X-Paperclip-Run-Id":context["PAPERCLIP_RUN_ID"]})
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
        return redact(json.loads(raw), context)


def handle(message, context=None):
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
            result = request_api(message["params"].get("arguments", {}), context)
            return {"content":[{"type":"text", "text":json.dumps(result)}]}
        except urllib.error.HTTPError as error:
            try:
                detail = redact(json.loads(error.read(MAX_BYTES)), context)
                text = json.dumps({"status":error.code,"error":detail.get("error","Request rejected"),"details":detail.get("details")})
            except Exception:
                text = f"Paperclip rejected the request (HTTP {error.code})."
            return {"isError":True, "content":[{"type":"text", "text":text}]}
        except Exception:
            return {"isError":True, "content":[{"type":"text", "text":"Paperclip request failed. Check the path, payload, connection, and scoped run credential."}]}
    raise ValueError("Unsupported MCP method")



@contextlib.contextmanager
def http_bridge(context):
    """Expose only scoped Paperclip tools; local workspace execution stays disabled."""
    capability = secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            if self.path != "/mcp" or not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + capability):
                self.send_error(403)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > MAX_BYTES:
                    raise ValueError("Invalid request size")
                message = json.loads(self.rfile.read(length))
                if not isinstance(message, dict):
                    raise ValueError("Invalid MCP request")
                if "id" not in message:
                    self.send_response(202)
                    self.end_headers()
                    return
                response = {"jsonrpc":"2.0", "id":message["id"], "result":handle(message, context)}
                body = json.dumps(response).encode()
            except Exception:
                self.send_error(400)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.send_error(405)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=lambda:server.serve_forever(poll_interval=.1), daemon=True)
    thread.start()
    try:
        yield {"url":f"http://127.0.0.1:{server.server_address[1]}/mcp", "capability":capability}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


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
