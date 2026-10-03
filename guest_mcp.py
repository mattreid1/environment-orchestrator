"""Authenticated HTTP MCP tools for one guest executor. No host tool fallback."""
import asyncio
import base64
import concurrent.futures
import contextlib
import hmac
import json
import posixpath
import secrets
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote

from aiohttp import ClientSession, ClientTimeout, ClientWSTimeout, WSMsgType

MAX_BYTES = 8 * 1024 * 1024
WORKSPACE = "/var/lib/agent/workspace"
TOOLS = [
    {"name": "guest_exec", "description": "Run Bash inside your microVM. The default directory is /var/lib/agent/workspace. Commands have guest tools and network access. If still running, use guest_wait or guest_terminate with the returned process_id. Host commands and host files are unavailable.",
     "inputSchema": {"type": "object", "properties": {"command": {"type": "string"}, "cwd": {"type": "string"}, "wait_ms": {"type": "integer", "minimum": 0, "maximum": 30000}}, "required": ["command"], "additionalProperties": False}},
    {"name": "guest_wait", "description": "Read new output from a command started with guest_exec. A running command keeps its microVM active. Use repeated calls to wait for completion.",
     "inputSchema": {"type": "object", "properties": {"process_id": {"type": "string"}, "wait_ms": {"type": "integer", "minimum": 0, "maximum": 30000}}, "required": ["process_id"], "additionalProperties": False}},
    {"name": "guest_terminate", "description": "Stop a command started in this session. This cannot stop host processes.",
     "inputSchema": {"type": "object", "properties": {"process_id": {"type": "string"}}, "required": ["process_id"], "additionalProperties": False}},
    {"name": "guest_read", "description": "Read a UTF-8 text file inside your microVM. Relative paths use /var/lib/agent/workspace. Use guest_exec for directory listing, search, or large files.",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False}},
    {"name": "guest_write", "description": "Create or replace a UTF-8 text file inside your microVM. Relative paths use /var/lib/agent/workspace. Parent directories must exist. Use guest_exec for patches or directory creation.",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "text": {"type": "string"}}, "required": ["path", "text"], "additionalProperties": False}},
    {"name": "guest_image", "description": "Inspect a PNG, JPEG, GIF, or WebP file inside your microVM. Use guest_exec with agent-browser screenshot to create a browser screenshot first.",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False}},
]


class GuestOperationError(RuntimeError):
    """The guest returned a definite operation error; the connection is usable."""


def guest_uri(path):
    if not isinstance(path, str) or not path or "\x00" in path or len(path) > 4096:
        raise ValueError("Invalid guest path")
    # Construct a URI; never open, resolve, or stat a caller-supplied host path.
    if not path.startswith("/"):
        path = WORKSPACE + "/" + path
    return "file://" + quote(posixpath.normpath(path), safe="/")


def bounded_wait(arguments):
    value = arguments.get("wait_ms", 1000)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 30000:
        raise ValueError("wait_ms must be an integer from 0 through 30000")
    return value


class GuestExecutor:
    """One lazy, persistent writer connection, with no replay after disconnect."""
    def __init__(self, binding):
        self.binding = binding
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.session = self.socket = self.reader = None
        self.pending = {}
        self.sequence = 0
        self.processes = {}
        self.failed = False
        self.guard = None

    async def read(self):
        try:
            async for message in self.socket:
                if message.type != WSMsgType.TEXT:
                    if message.type in (WSMsgType.CLOSED, WSMsgType.CLOSE, WSMsgType.ERROR):
                        break
                    continue
                data = json.loads(message.data)
                # Process notifications are read through process/read instead.
                if data.get("method"):
                    if "id" in data:
                        await self.socket.send_json({"id": data["id"], "error": {"code": -32601, "message": "Guest callbacks are unsupported"}})
                    continue
                waiter = self.pending.pop(data.get("id"), None)
                if waiter is not None and not waiter.done():
                    if "error" in data:
                        waiter.set_exception(GuestOperationError("Guest rejected the operation"))
                    else:
                        waiter.set_result(data.get("result", {}))
        finally:
            self.failed = True
            for waiter in self.pending.values():
                if not waiter.done():
                    waiter.set_exception(RuntimeError("Guest connection closed; the operation was not replayed"))
            self.pending.clear()

    async def rpc(self, method, params=None):
        if self.failed:
            raise RuntimeError("Guest connection closed; start a new harness session")
        self.sequence += 1
        identifier = self.sequence
        future = self.loop.create_future()
        self.pending[identifier] = future
        try:
            await self.socket.send_json({"id": identifier, "method": method, "params": params or {}})
            return await asyncio.wait_for(future, 120)
        except GuestOperationError:
            # A definite guest error, such as a missing file, has a known
            # result. It must not disable unrelated subsequent operations.
            raise
        except Exception:
            # A timed-out mutation has an unknown result. Do not retry it.
            self.failed = True
            raise
        finally:
            self.pending.pop(identifier, None)

    async def connect(self):
        if self.failed:
            raise RuntimeError("Guest connection closed; start a new harness session")
        if self.socket is None:
            self.session = ClientSession(timeout=ClientTimeout(total=None, sock_connect=120))
            self.socket = await self.session.ws_connect(self.binding["exec_url"],
                headers={"Authorization": "Bearer " + self.binding["auth_bearer_token"]},
                timeout=ClientWSTimeout(ws_close=3), max_msg_size=64 * 1024 * 1024)
            self.reader = asyncio.create_task(self.read())
            await self.rpc("initialize", {"clientName": "environment-claude"})
            await self.socket.send_json({"method": "initialized"})

    async def wait(self, process, wait_ms):
        if process not in self.processes:
            raise ValueError("Unknown process_id for this session")
        response = await self.rpc("process/read", {"processId": process,
            "afterSeq": self.processes[process], "maxBytes": 65536, "waitMs": wait_ms})
        self.processes[process] = response["nextSeq"]
        result = {"process_id": process, "exited": response["exited"],
                  "exit_code": response.get("exitCode"),
                  "output": "".join(base64.b64decode(chunk["chunk"]).decode("utf-8", "replace") for chunk in response.get("chunks", []))}
        if response.get("failure"):
            result["failure"] = "Guest command failed"
        return result

    async def invoke(self, name, arguments):
        if self.guard is None:
            self.guard = asyncio.Lock()
        async with self.guard:
            # Validate input before opening a guest connection or waking a VM.
            schemas = {tool["name"]: tool["inputSchema"] for tool in TOOLS}
            schema = schemas.get(name)
            if not schema or not isinstance(arguments, dict) or set(arguments) - set(schema["properties"]) or not set(schema["required"]).issubset(arguments):
                raise ValueError("Unsupported guest tool or arguments")
            if name in {"guest_exec", "guest_wait"}:
                wait_ms = bounded_wait(arguments)
            if name in {"guest_wait", "guest_terminate"} and arguments["process_id"] not in self.processes:
                raise ValueError("Unknown process_id for this session")
            if name in {"guest_read", "guest_write", "guest_image"}:
                path = guest_uri(arguments["path"])
            if name == "guest_exec":
                command = arguments["command"]
                if not isinstance(command, str) or not command or len(command.encode()) > 1024 * 1024:
                    raise ValueError("Invalid guest command")
                cwd = guest_uri(arguments.get("cwd", WORKSPACE))
            if name == "guest_write":
                text = arguments["text"]
                if not isinstance(text, str) or len(text.encode()) > MAX_BYTES:
                    raise ValueError("Guest write exceeds the text limit")
            await self.connect()
            if name == "guest_exec":
                process = "claude-" + uuid.uuid4().hex
                self.processes[process] = None
                await self.rpc("process/start", {"processId": process,
                    "argv": ["/run/current-system/sw/bin/bash", "-lc", command], "cwd": cwd,
                    "env": {"PATH": "/run/current-system/sw/bin", "HOME": "/var/lib/agent", "LANG": "C.UTF-8"},
                    "envPolicy": {"inherit": "none", "ignoreDefaultExcludes": False, "exclude": [], "set": {}, "includeOnly": []}, "tty": False})
                return await self.wait(process, wait_ms)
            if name == "guest_wait":
                return await self.wait(arguments["process_id"], wait_ms)
            if name == "guest_terminate":
                return await self.rpc("process/terminate", {"processId": arguments["process_id"]})
            if name == "guest_write":
                await self.rpc("fs/writeFile", {"path": path, "dataBase64": base64.b64encode(text.encode()).decode()})
                return {"written": True}
            response = await self.rpc("fs/readFile", {"path": path})
            data = base64.b64decode(response["dataBase64"], validate=True)
            if len(data) > MAX_BYTES:
                raise ValueError("Guest file exceeds the read limit")
            if name == "guest_read":
                return {"text": data.decode("utf-8")}
            mime = None
            for prefix, kind in [(b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"), (b"GIF8", "image/gif")]:
                if data.startswith(prefix):
                    mime = kind
            if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
                mime = "image/webp"
            if mime is None:
                raise ValueError("Unsupported guest image type")
            return {"content": [{"type": "image", "mimeType": mime, "data": base64.b64encode(data).decode()}]}

    def call(self, name, arguments):
        future = asyncio.run_coroutine_threadsafe(self.invoke(name, arguments), self.loop)
        try:
            return future.result(timeout=150)
        except concurrent.futures.TimeoutError:
            future.cancel()
            self.failed = True
            raise RuntimeError("Guest operation timed out; its result is unknown") from None

    async def disconnect(self):
        if self.reader is not None:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
        if self.socket is not None:
            try:
                await asyncio.wait_for(self.socket.close(), 3)
            except asyncio.TimeoutError:
                pass
        if self.session is not None:
            await self.session.close()

    def close(self):
        try:
            asyncio.run_coroutine_threadsafe(self.disconnect(), self.loop).result(timeout=5)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=5)
            if not self.thread.is_alive():
                self.loop.close()


def handle(message, executor, paperclip_context=None):
    method = message.get("method")
    if method == "initialize":
        return {"protocolVersion": message.get("params", {}).get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}}, "serverInfo": {"name": "environment-guest", "version": "0.1.0"}}
    if method == "ping":
        return {}
    if method == "tools/list":
        tools = list(TOOLS)
        if paperclip_context:
            import paperclip_mcp
            tools += paperclip_mcp.handle(message, paperclip_context)["tools"]
        return {"tools": tools}
    if method == "tools/call":
        params = message.get("params", {})
        if params.get("name") == "paperclip_api" and paperclip_context:
            import paperclip_mcp
            return paperclip_mcp.handle(message, paperclip_context)
        try:
            result = executor.call(params.get("name"), params.get("arguments", {}))
            if "content" in result:
                return result
            return {"content": [{"type": "text", "text": json.dumps(result)}]}
        except Exception:
            return {"isError": True, "content": [{"type": "text", "text": "Guest operation failed. Check the command, guest path, process_id, and workspace connection. Unknown operations are not replayed."}]}
    raise ValueError("Unsupported MCP method")


@contextlib.contextmanager
def http_bridge(binding, paperclip_context=None, executor_factory=GuestExecutor):
    capability = secrets.token_urlsafe(32)
    executor = executor_factory(binding)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            if self.path != "/mcp" or not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + capability):
                self.send_error(403)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_BYTES:
                    raise ValueError("Invalid request size")
                message = json.loads(self.rfile.read(length))
                if not isinstance(message, dict):
                    raise ValueError("Invalid MCP request")
                if "id" not in message:
                    self.send_response(202)
                    self.end_headers()
                    return
                body = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": handle(message, executor, paperclip_context)}).encode()
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
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.1), daemon=True)
    thread.start()
    try:
        yield {"url": f"http://127.0.0.1:{server.server_address[1]}/mcp", "capability": capability}
    finally:
        server.shutdown()
        server.server_close()
        executor.close()
        thread.join(timeout=2)
