"""Exercise real workspace ownership, remote commands, and idle checkpoints."""
import argparse
import asyncio
import base64
import json
import os
from pathlib import Path
import time
import uuid

from aiohttp import ClientSession, ClientTimeout, UnixConnector, WSServerHandshakeError


class RPC:
    def __init__(self, socket):
        self.socket = socket
        self.sequence = 0
        self.waiters = {}
        self.events = asyncio.Queue()
        self.reader = asyncio.create_task(self.read())

    async def read(self):
        async for message in self.socket:
            data = json.loads(message.data)
            waiter = self.waiters.pop(data.get("id"), None)
            if waiter:
                waiter.set_result(data)
            else:
                await self.events.put(data)

    async def call(self, method, params=None):
        self.sequence += 1
        future = asyncio.get_running_loop().create_future()
        self.waiters[self.sequence] = future
        await self.socket.send_json({"id": self.sequence, "method": method, "params": params or {}})
        result = await asyncio.wait_for(future, 180)
        if "error" in result:
            raise RuntimeError(result["error"])
        return result["result"]

    async def start(self, command):
        process = "test-"+uuid.uuid4().hex
        await self.call("process/start", {"processId": process, "argv": ["/bin/sh", "-c", command],
            "cwd": "file:///var/lib/agent/workspace", "env": {"PATH": "/run/current-system/sw/bin"}, "tty": False})
        return process

    async def finish(self, process):
        output = bytearray()
        async with asyncio.timeout(180):
            while True:
                event = await self.events.get()
                params = event.get("params", {})
                if params.get("processId") != process:
                    continue
                if event.get("method") == "process/output":
                    output.extend(base64.b64decode(params["chunk"]))
                if event.get("method") == "process/exited":
                    return params["exitCode"], output.decode()

    async def close(self):
        await self.socket.close()
        self.reader.cancel()
        await asyncio.gather(self.reader, return_exceptions=True)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default="swe-demo-b")
    parser.add_argument("--idle", action="store_true", help="Wait for the configured automatic idle suspension")
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    state = Path(os.environ.get("ENVIRONMENT_STATE", str(Path.home()/".local/share/environment-orchestrator")))
    evidence = {"workspace": args.workspace, "checks": []}
    async with ClientSession(connector=UnixConnector(path=str(state/"control.sock")), timeout=ClientTimeout(total=180)) as admin:
        async def call(path, data=None, method="GET", expected=200):
            async with admin.request(method, "http://localhost"+path, json=data) as response:
                body = await response.json()
                assert response.status == expected, body
                return body
        binding = await call("/workspaces", {"id": args.workspace}, method="POST")
        path = "/workspaces/"+args.workspace
        async with ClientSession(timeout=ClientTimeout(total=None)) as session:
            try:
                await session.ws_connect(binding["exec_url"], headers={"Authorization": "Bearer incorrect"})
                raise AssertionError("Incorrect capability was accepted")
            except WSServerHandshakeError as error:
                assert error.status == 401
            evidence["checks"].append("incorrect_capability_rejected")
            socket = await session.ws_connect(binding["exec_url"], headers={"Authorization": "Bearer "+binding["auth_bearer_token"]})
            rpc = RPC(socket)
            try:
                initialized = await rpc.call("initialize", {"clientName": "orchestrator-integration"})
                await socket.send_json({"method": "initialized"})
                evidence["session"] = initialized["sessionId"]
                try:
                    await session.ws_connect(binding["exec_url"], headers={"Authorization": "Bearer "+binding["auth_bearer_token"]})
                    raise AssertionError("A second writer was accepted")
                except WSServerHandshakeError as error:
                    assert error.status == 409
                evidence["checks"].append("second_writer_rejected")
                marker = "workspace-"+uuid.uuid4().hex
                file_path = "file:///var/lib/agent/workspace/orchestrator-marker.txt"
                await rpc.call("fs/writeFile", {"path": file_path, "dataBase64": base64.b64encode(marker.encode()).decode()})
                process = await rpc.start("cat /proc/sys/kernel/random/boot_id; test ! -e /home/agent/.env; printf 'guest-tool-ok\\n'")
                code, output = await rpc.finish(process)
                assert code == 0 and "guest-tool-ok" in output, (code, output)
                evidence["boot_id"] = output.splitlines()[0]
                evidence["checks"].append("guest_execution_and_host_path_denial")
                process = await rpc.start("sleep 10; printf finished")
                status = await call(path)
                assert status["leases"] > 0 and status["active_operations"] > 0, status
                await call(path+"/suspend", {}, method="POST", expected=409)
                await rpc.call("process/terminate", {"processId": process})
                await rpc.finish(process)
                evidence["checks"].append("active_process_blocks_suspend_and_can_be_cancelled")
                for _ in range(100):
                    if not (await call(path))["active_operations"]:
                        break
                    await asyncio.sleep(.01)
                await call(path+"/suspend", {}, method="POST")
                asleep = await call(path)
                assert asleep["state"] == "suspended" and asleep["pid"] is None and asleep["writer_connected"], asleep
                evidence["first_suspend_ms"] = asleep["last_suspend_ms"]
                for _ in range(3):
                    assert (await rpc.call("environment/status"))["status"] == "ready"
                assert (await call(path))["pid"] is None
                evidence["checks"].append("open_connection_and_health_probes_allow_suspension")
                started = time.monotonic()
                contents = await rpc.call("fs/readFile", {"path": file_path})
                assert base64.b64decode(contents["dataBase64"]).decode() == marker
                status = await call(path)
                evidence["restore_ms"] = status["last_wake_ms"]
                evidence["restore_tool_ms"] = round((time.monotonic()-started)*1000, 2)
                process = await rpc.start("cat /proc/sys/kernel/random/boot_id")
                code, output = await rpc.finish(process)
                assert code == 0 and output.strip() == evidence["boot_id"]
                evidence["checks"].append("same_executor_connection_files_and_boot_identity_survive_restore")
                if args.idle:
                    deadline = time.monotonic()+180
                    while time.monotonic() < deadline:
                        await rpc.call("environment/status")
                        status = await call(path)
                        if status["state"] == "suspended" and status["pid"] is None:
                            break
                        await asyncio.sleep(5)
                    else:
                        raise AssertionError("Automatic idle suspension did not happen")
                    contents = await rpc.call("fs/readFile", {"path": file_path})
                    assert base64.b64decode(contents["dataBase64"]).decode() == marker
                    evidence["checks"].append("automatic_idle_suspend_with_open_connection_and_restore")
            finally:
                await rpc.close()
        # Give the gateway a chance to drop its writer claim before final status.
        for _ in range(100):
            if not (await call(path))["writer_connected"]:
                break
            await asyncio.sleep(.01)
        await call(path+"/suspend", {}, method="POST")
        evidence["final"] = await call(path)
    if args.evidence:
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(json.dumps(evidence, indent=2)+"\n")
    print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
