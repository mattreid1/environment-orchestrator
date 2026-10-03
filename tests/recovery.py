"""Crash an idle test guest and verify explicit file recovery without stale RAM."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import signal

from aiohttp import ClientSession, ClientTimeout, UnixConnector


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default="swe-demo-b")
    parser.add_argument("--crash-guest", action="store_true", required=True)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    state = Path(os.environ.get("ENVIRONMENT_STATE", str(Path.home()/".local/share/environment-orchestrator")))
    path = "/workspaces/"+args.workspace
    async with ClientSession(connector=UnixConnector(path=str(state/"control.sock")), timeout=ClientTimeout(total=180)) as client:
        async def call(action=None, expected=200):
            async with client.request("POST" if action else "GET", "http://localhost"+path+("/"+action if action else ""), json={} if action else None) as response:
                result = await response.json()
                assert response.status == expected, result
                return result
        status = await call()
        assert not status["writer_connected"] and status["active_operations"] == 0, status
        await call("resume")
        status = await call()
        folder = state/"workspaces"/args.workspace
        async def guest(command):
            process = await asyncio.create_subprocess_exec("ssh", "-i", str(folder/"id_ed25519"),
                "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                "-o", "UserKnownHostsFile="+str(folder/"known_hosts"), "root@"+status["guest_host"], command,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            output, error = await process.communicate()
            assert process.returncode == 0, error.decode()
            return output.decode()
        boot_before = (await guest("sync; cat /proc/sys/kernel/random/boot_id")).strip()
        files_before = await guest("cat /var/lib/agent/workspace/parallel-owner.txt /var/lib/agent/workspace/orchestrator-marker.txt")
        os.kill(status["pid"], signal.SIGKILL)
        for _ in range(100):
            failed = await call()
            if failed["recovery_required"] and failed["pid"] is None:
                break
            await asyncio.sleep(.01)
        else:
            raise AssertionError("VM failure was not detected")
        await call("resume", expected=409)
        await call("recover")
        boot_after = (await guest("cat /proc/sys/kernel/random/boot_id")).strip()
        files_after = await guest("cat /var/lib/agent/workspace/parallel-owner.txt /var/lib/agent/workspace/orchestrator-marker.txt")
        assert boot_before != boot_after
        assert files_before == files_after
        await call("suspend")
        evidence = {"workspace": args.workspace, "boot_before": boot_before, "boot_after": boot_after,
            "checks": ["crash_detected", "stale_checkpoint_refused", "explicit_recovery_cold_boots", "saved_files_preserved"],
            "final": await call()}
        if args.evidence:
            args.evidence.write_text(json.dumps(evidence, indent=2)+"\n")
        print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
