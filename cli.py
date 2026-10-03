"""Administrative CLI. This interface is not a model tool."""
import argparse
import asyncio
import json
import os
from pathlib import Path
from aiohttp import ClientSession, ClientTimeout, UnixConnector


async def main():
    parser = argparse.ArgumentParser(description="Manage private Firecracker agent environments")
    parser.add_argument("action", choices=["create", "status", "resume", "suspend", "shutdown", "recover"])
    parser.add_argument("workspace", nargs="?")
    parser.add_argument("--profile", help="Workspace profile for create (default: swe)")
    args = parser.parse_args()
    if args.profile and args.action != "create":
        parser.error("--profile is only available for create")
    if args.action != "status" and not args.workspace:
        parser.error("This action requires a workspace ID")
    state = Path(os.environ.get("ENVIRONMENT_STATE", str(Path.home()/".local/share/environment-orchestrator")))
    path = "/workspaces" + ("/"+args.workspace if args.workspace and args.action != "create" else "")
    method, data = "GET", None
    if args.action == "create":
        method, data = "POST", {"id": args.workspace}
        if args.profile:
            data["profile"] = args.profile
    elif args.action != "status":
        method, path = "POST", path+"/"+args.action
    async with ClientSession(connector=UnixConnector(path=str(state/"control.sock")), timeout=ClientTimeout(total=180)) as session:
        async with session.request(method, "http://localhost"+path, json=data) as response:
            if response.status != 200:
                raise SystemExit(await response.text())
            result = await response.json()
            result.pop("auth_bearer_token", None)
            print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
