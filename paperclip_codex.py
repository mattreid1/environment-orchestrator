"""Paperclip's SSH Codex entry point. All workspace tools remain guest-only."""
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
from urllib.parse import urlsplit
import codex


def arguments_for_managed_codex(arguments):
    # Paperclip sets these permissions on its staging directory. The guest
    # launcher owns permissions and the inference provider for our environment.
    ignored_config = {"sandbox_mode", "sandbox_workspace_write.network_access",
                      "model_reasoning_effort", "service_tier", "features.fast_mode"}
    result = []
    index = 0
    while index < len(arguments):
        item = arguments[index]
        if item in ("-c", "--config"):
            if index + 1 >= len(arguments) or arguments[index + 1].split("=", 1)[0].strip() not in ignored_config:
                raise RuntimeError("Paperclip attempted an unsupported configuration override")
            index += 2
            continue
        if item == "--search":
            raise RuntimeError("Shared browser routing is not enabled for this agent")
        result.append(item)
        index += 1
    if not result or result[0] != "exec":
        raise RuntimeError("The Paperclip launcher accepts Codex exec sessions only")
    if "--skip-git-repo-check" not in result:
        result.insert(1, "--skip-git-repo-check")
    codex.validate_codex_arguments(result)
    return result


def paperclip_api_origin(configured_origin, supplied_origin, bridge_mode):
    # Remote Paperclip adapters replace the run token with a per-run callback
    # token. Only their loopback callback server can use that token.
    if not bridge_mode:
        return configured_origin.rstrip("/")
    parsed = urlsplit(supplied_origin)
    if bridge_mode not in {"queue_v1", "http2_v1"} or parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or not parsed.port or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise RuntimeError("Paperclip callback bridge must use a loopback HTTP origin")
    return supplied_origin.rstrip("/")


def main():
    if sys.argv[1:] in (["--version"], ["-V"]):
        return subprocess.call(["codex", "--version"])
    company = str(uuid.UUID(os.environ["PAPERCLIP_COMPANY_ID"]))
    agent = str(uuid.UUID(os.environ["PAPERCLIP_AGENT_ID"]))
    run = str(uuid.UUID(os.environ["PAPERCLIP_RUN_ID"]))
    key = os.environ.get("PAPERCLIP_API_KEY", "")
    if not key or "\n" in key:
        raise RuntimeError("Paperclip must supply its scoped run credential")
    config = json.loads((Path.home() / ".local/share/environment-orchestrator/paperclip.json").read_text())
    if company not in {item["id"] for item in config["companies"]}:
        raise RuntimeError("Paperclip company is not enabled on this host")
    context = {"PAPERCLIP_COMPANY_ID":company, "PAPERCLIP_AGENT_ID":agent,
               "PAPERCLIP_RUN_ID":run, "PAPERCLIP_API_KEY":key,
               "PAPERCLIP_API_URL":paperclip_api_origin(config["base_url"], os.environ.get("PAPERCLIP_API_URL", ""), os.environ.get("PAPERCLIP_API_BRIDGE_MODE", ""))}
    return codex.main(["--paperclip", company, agent, *arguments_for_managed_codex(sys.argv[1:])], context)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, KeyError, ValueError, OSError):
        # Configuration errors can contain credentials. Do not echo them.
        print("Paperclip launcher failed. Check its identity, run credential, arguments, and local configuration.", file=sys.stderr)
        raise SystemExit(1)
