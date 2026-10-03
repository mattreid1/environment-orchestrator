"""Check Claude routing against an authenticated executor fixture, without inference."""
import asyncio
import base64
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
import urllib.request

from aiohttp import web
import claude
import codex
import guest_mcp
import paperclip_claude


class LauncherTests(unittest.TestCase):
    def test_cli_override_flags_and_other_models_fail_closed(self):
        claude.validate_claude_arguments(["--print", "--output-format", "stream-json", "--model", "claude-sonnet-5-5", "Inspect this project"])
        for args in [["--tools", "Bash"], ["--mcp-config", "/tmp/host.json"], ["--settings", "{}"], ["--plugin-dir", "/tmp/plugin"], ["--model", "claude-opus-anything"], ["--fallback-model=claude-other"], ["--add-dir", "/etc/nixos"], ["install"], ["--resume", "../../host"]]:
            with self.subTest(args=args), self.assertRaises(RuntimeError):
                claude.validate_claude_arguments(args)

    def test_managed_configuration_removes_all_builtin_tools(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            args = claude.harness_arguments(state, {"url": "http://127.0.0.1:9999/mcp", "capability": "fixture-only"}, ["--print"])
            self.assertEqual(args[args.index("--tools") + 1], "")
            self.assertEqual(args[args.index("--setting-sources") + 1], "")
            self.assertIn("--bare", args)
            self.assertIn("--strict-mcp-config", args)
            config = json.loads((state / "mcp.json").read_text())
            self.assertEqual(set(config["mcpServers"]), {"environment"})
            self.assertEqual(config["mcpServers"]["environment"]["type"], "http")
            for name in ("mcp.json", "settings.json"):
                self.assertEqual((state / name).stat().st_mode & 0o777, 0o600)
            env = claude.harness_environment(state, "provider-fixture")
            self.assertNotIn("PAPERCLIP_API_KEY", env)
            self.assertEqual(env["ANTHROPIC_DEFAULT_HAIKU_MODEL"], claude.DEFAULT_MODEL)
            self.assertEqual(env["DISABLE_UPDATES"], "1")

    def test_isolation_mounts_private_state_and_not_host_files(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            state = home / "state"
            state.mkdir()
            command = codex.isolated_command("bwrap", "/nix/store/fixture/bin/claude", state, home, guest_mcp.WORKSPACE, ["--print"])
            mounts = [command[i + 1] for i, value in enumerate(command) if value in ("--bind", "--ro-bind")]
            self.assertNotIn("/", mounts)
            self.assertNotIn(str(home), mounts)
            self.assertNotIn("/etc/nixos", mounts)
            self.assertNotIn("/home/agent/.env", mounts)
            self.assertIn(str(state), mounts)
            self.assertIn("--unshare-pid", command)

    def test_paperclip_consumes_only_current_run_prompt_assets(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "run"
            root.mkdir()
            instructions = root / "instructions.md"
            instructions.write_text("Frontend role instructions")
            args, text = paperclip_claude.arguments_for_managed_claude(["--print", "--verbose", "--dangerously-skip-permissions", "--append-system-prompt-file", str(instructions), "--add-dir", str(root), "--setting-sources", "user"], root)
            self.assertEqual(args, ["--print", "--verbose"])
            self.assertEqual(text, "Frontend role instructions")
            outside = Path(folder) / "outside"
            outside.write_text("host secret")
            link = root / "link"
            link.symlink_to(outside)
            for path in (outside, link):
                with self.subTest(path=path), self.assertRaises(RuntimeError):
                    paperclip_claude.arguments_for_managed_claude(["--print", "--append-system-prompt-file", str(path)], root)
            with self.assertRaises(RuntimeError):
                paperclip_claude.arguments_for_managed_claude(["--print", "--mcp-config", str(instructions)], root)

    def test_paths_are_guest_uris_and_never_host_file_reads(self):
        self.assertEqual(guest_mcp.guest_uri("a b.txt"), "file:///var/lib/agent/workspace/a%20b.txt")
        self.assertEqual(guest_mcp.guest_uri("/etc/nixos/flake.nix"), "file:///etc/nixos/flake.nix")
        with self.assertRaises(ValueError):
            guest_mcp.guest_uri("file\x00name")

    def test_paperclip_native_mcp_defaults_are_consumed_without_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "mcp.json"
            token = "Bearer native-scoped-token-never-imported"
            config = {"mcpServers": {
                "Paperclip projects": {"type": "http", "url": "https://org.h.mattre.id/api/mcp/project-tools", "headers": {"Authorization": token}},
                "Paperclip connections": {"type": "http", "url": "https://org.h.mattre.id/mcp/runtime-tools", "headers": {"Authorization": token}},
            }}
            path.write_text(json.dumps(config))
            args, text = paperclip_claude.arguments_for_managed_claude(["--print", "--mcp-config", str(path), "--strict-mcp-config", "--model", "claude-sonnet-5-5"], root, "https://org.h.mattre.id")
            self.assertEqual(args, ["--print", "--model", "claude-sonnet-5-5"])
            self.assertEqual(text, "")
            self.assertNotIn(token, json.dumps([args, text]))
            self.assertNotIn(str(path), args)
            self.assertEqual(json.loads(path.read_text()), config)

    def test_paperclip_native_mcp_rejects_custom_servers_and_host_assets(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "run"
            root.mkdir()
            path = root / "mcp.json"
            valid = {"type": "http", "url": "https://org.h.mattre.id/api/mcp/project-tools", "headers": {"Authorization": "Bearer native-fixture"}}
            invalid = [
                {"mcpServers": {"Paperclip projects": {"type": "stdio", "command": "/bin/sh", "args": ["-c", "cat /home/agent/.env"]}}},
                {"mcpServers": {"Paperclip projects": {**valid, "url": "https://other.test/api/mcp/project-tools"}}},
                {"mcpServers": {"Paperclip projects": {**valid, "url": "https://org.h.mattre.id/api/mcp/project-tools?extra=1"}}},
                {"mcpServers": {"Paperclip projects": {**valid, "headers": {"Authorization": "Bearer fixture", "X-Extra": "unsupported"}}}},
                {"mcpServers": {"Paperclip projects": {**valid, "env": {"HOST_SECRET": "not-allowed"}}}},
                {"mcpServers": {"Custom server": valid}},
                {"mcpServers": {"Paperclip projects": valid}, "extra": "unsupported"},
            ]
            for config in invalid:
                path.write_text(json.dumps(config))
                with self.subTest(config=config), self.assertRaises(RuntimeError):
                    paperclip_claude.arguments_for_managed_claude(["--print", "--mcp-config", str(path), "--strict-mcp-config"], root, "https://org.h.mattre.id")
            outside = Path(folder) / "outside.json"
            outside.write_text(json.dumps({"mcpServers": {"Paperclip projects": valid}}))
            with self.assertRaises(RuntimeError):
                paperclip_claude.arguments_for_managed_claude(["--print", "--mcp-config", str(outside)], root, "https://org.h.mattre.id")
            with self.assertRaises(RuntimeError):
                paperclip_claude.arguments_for_managed_claude(["--print", "--strict-mcp-config"], root, "https://org.h.mattre.id")


class ExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        self.files = {}
        self.connections = 0
        self.close_on_write = False
        async def websocket(request):
            if request.headers.get("Authorization") != "Bearer guest-fixture":
                raise web.HTTPUnauthorized()
            self.connections += 1
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            async for frame in socket:
                data = json.loads(frame.data)
                self.calls.append(data)
                method, params = data.get("method"), data.get("params", {})
                if "id" not in data:
                    continue
                result = {}
                if method == "initialize":
                    result = {"sessionId": "fixture", "environmentInfo": {"executorVersion": "test"}}
                elif method == "fs/writeFile":
                    self.files[params["path"]] = params["dataBase64"]
                    if self.close_on_write:
                        await socket.close()
                        break
                elif method == "fs/readFile":
                    result = {"dataBase64": self.files.get(params["path"], base64.b64encode(b"guest fixture").decode())}
                elif method == "process/start":
                    result = {"processId": params["processId"]}
                elif method == "process/read":
                    result = {"chunks": [{"seq": 1, "stream": "stdout", "chunk": base64.b64encode(b"guest command output").decode()}], "nextSeq": 1, "exited": True, "exitCode": 0, "closed": True}
                elif method == "process/terminate":
                    result = {"running": False}
                await socket.send_json({"id": data["id"], "result": result})
            return socket
        app = web.Application()
        app.router.add_get("/exec", websocket)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.binding = {"exec_url": f"ws://127.0.0.1:{port}/exec", "auth_bearer_token": "guest-fixture"}
        self.executor = guest_mcp.GuestExecutor(self.binding)

    async def asyncTearDown(self):
        await asyncio.to_thread(self.executor.close)
        await self.runner.cleanup()

    async def call(self, name, arguments):
        return await asyncio.to_thread(self.executor.call, name, arguments)

    async def test_exec_uses_guest_environment_and_process_read(self):
        result = await self.call("guest_exec", {"command": "printf hello", "wait_ms": 1})
        self.assertEqual(result["output"], "guest command output")
        start = next(call for call in self.calls if call.get("method") == "process/start")["params"]
        self.assertEqual(start["cwd"], "file:///var/lib/agent/workspace")
        self.assertEqual(start["argv"], ["/run/current-system/sw/bin/bash", "-lc", "printf hello"])
        self.assertEqual(start["envPolicy"]["inherit"], "none")
        self.assertEqual(set(start["env"]), {"PATH", "HOME", "LANG"})
        self.assertTrue(result["exited"])
        self.assertEqual(self.connections, 1)

    async def test_text_and_image_operations_are_remote_only(self):
        await self.call("guest_write", {"path": "proof.txt", "text": "persistent guest"})
        result = await self.call("guest_read", {"path": "proof.txt"})
        self.assertEqual(result, {"text": "persistent guest"})
        data = b"\x89PNG\r\n\x1a\nfixture"
        self.files["file:///tmp/screenshot.png"] = base64.b64encode(data).decode()
        image = await self.call("guest_image", {"path": "/tmp/screenshot.png"})
        self.assertEqual(image["content"][0]["mimeType"], "image/png")
        self.assertEqual(base64.b64decode(image["content"][0]["data"]), data)
        self.assertEqual(self.connections, 1)

    async def test_invalid_tool_or_unknown_process_does_not_wake_guest(self):
        for name, arguments in [("host_exec", {"command": "true"}), ("guest_exec", {"command": "true", "env": {"ANTHROPIC_API_KEY": "leak"}}), ("guest_exec", {"command": "true", "wait_ms": True}), ("guest_wait", {"process_id": "host-process"})]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                await self.call(name, arguments)
        self.assertEqual(self.connections, 0)

    async def test_uncertain_mutation_is_not_replayed_or_reconnected(self):
        self.close_on_write = True
        with self.assertRaises(RuntimeError):
            await self.call("guest_write", {"path": "proof.txt", "text": "exactly once"})
        with self.assertRaises(RuntimeError):
            await self.call("guest_write", {"path": "proof.txt", "text": "exactly once"})
        writes = [call for call in self.calls if call.get("method") == "fs/writeFile"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(self.connections, 1)

    async def test_http_bridge_requires_capability_and_never_opens_host_files(self):
        # Give the bridge its own executor because it owns cleanup.
        context = guest_mcp.http_bridge(self.binding)
        bridge = context.__enter__()
        try:
            def request(method, params=None, authorized=True):
                body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).encode()
                headers = {"Authorization": "Bearer " + bridge["capability"]} if authorized else {}
                with urllib.request.urlopen(urllib.request.Request(bridge["url"], data=body, headers=headers), timeout=5) as response:
                    return json.load(response)
            with self.assertRaises(urllib.error.HTTPError) as denied:
                await asyncio.to_thread(request, "tools/list", authorized=False)
            self.assertEqual(denied.exception.code, 403)
            denied.exception.close()
            listed = await asyncio.to_thread(request, "tools/list")
            self.assertEqual({tool["name"] for tool in listed["result"]["tools"]}, {tool["name"] for tool in guest_mcp.TOOLS})
            self.assertEqual(self.connections, 0)
            result = await asyncio.to_thread(request, "tools/call", {"name": "guest_read", "arguments": {"path": "/etc/nixos/flake.nix"}})
            self.assertEqual(json.loads(result["result"]["content"][0]["text"]), {"text": "guest fixture"})
            self.assertEqual(self.calls[-1]["params"]["path"], "file:///etc/nixos/flake.nix")
        finally:
            await asyncio.to_thread(context.__exit__, None, None, None)


if __name__ == "__main__":
    unittest.main()
