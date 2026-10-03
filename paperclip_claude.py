"""Paperclip claude_local SSH entry point, with guest-only workspace tools."""
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
from urllib.parse import urlsplit

import claude
import paperclip_codex


def staged_path(value, root, directory=False):
    path = Path(value)
    resolved = path.resolve()
    if path.is_symlink() or not resolved.is_relative_to(root.resolve()) or resolved.stat().st_uid != os.getuid():
        raise RuntimeError("Paperclip prompt assets must be owned files in this run")
    if directory:
        if not resolved.is_dir():
            raise RuntimeError("Paperclip prompt asset directory is invalid")
    elif not resolved.is_file() or resolved.stat().st_size > 1024 * 1024:
        raise RuntimeError("Paperclip instruction file is invalid")
    return resolved


def validate_native_mcp_config(value, root, configured_origin):
    """Recognize Paperclip's defaults, then discard their transport credentials."""
    if not isinstance(configured_origin, str):
        raise RuntimeError("Paperclip MCP origin is missing")
    origin = urlsplit(configured_origin)
    if origin.scheme != "https" or not origin.hostname or origin.username or origin.password or origin.path not in {"", "/"} or origin.query or origin.fragment:
        raise RuntimeError("Paperclip MCP origin is invalid")
    try:
        config = json.loads(staged_path(value, root).read_text())
    except (ValueError, UnicodeError):
        raise RuntimeError("Paperclip MCP JSON is invalid") from None
    if not isinstance(config, dict) or set(config) != {"mcpServers"}:
        raise RuntimeError("Paperclip MCP configuration is unsupported")
    servers = config["mcpServers"]
    expected = {"Paperclip projects": configured_origin.rstrip("/") + "/api/mcp/project-tools",
                "Paperclip connections": configured_origin.rstrip("/") + "/mcp/runtime-tools"}
    if not isinstance(servers, dict) or not servers or set(servers) - set(expected):
        raise RuntimeError("Paperclip MCP servers are unsupported")
    for name, server in servers.items():
        if not isinstance(server, dict) or set(server) != {"type", "url", "headers"} or server["type"] != "http" or server["url"] != expected[name]:
            raise RuntimeError("Paperclip MCP server is unsupported")
        headers = server["headers"]
        if not isinstance(headers, dict) or set(headers) != {"Authorization"}:
            raise RuntimeError("Paperclip MCP headers are unsupported")
        credential = headers["Authorization"]
        if not isinstance(credential, str) or not credential.startswith("Bearer ") or not credential[7:] or len(credential) > 16384 or any(c in credential for c in "\r\n\x00"):
            raise RuntimeError("Paperclip MCP authorization is invalid")


def arguments_for_managed_claude(arguments, root, configured_origin=None):
    # Paperclip's CLI adapter points to host staging assets. Consume only its
    # owned prompt text, then remove those paths from the isolated harness.
    result, instructions = [], []
    native_mcp, strict_mcp = False, False
    index = 0
    while index < len(arguments):
        item = arguments[index]
        if item in {"--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"}:
            index += 1
            continue
        if item == "--append-system-prompt-file":
            if index + 1 >= len(arguments):
                raise RuntimeError("Paperclip instruction file is missing")
            instructions.append(staged_path(arguments[index + 1], root).read_text())
            index += 2
            continue
        if item == "--add-dir":
            if index + 1 >= len(arguments):
                raise RuntimeError("Paperclip prompt directory is missing")
            staged_path(arguments[index + 1], root, directory=True)
            index += 2
            continue
        if item == "--setting-sources":
            if index + 1 >= len(arguments) or arguments[index + 1] != "user":
                raise RuntimeError("Paperclip settings override is unsupported")
            index += 2
            continue
        if item == "--mcp-config":
            if native_mcp or index + 1 >= len(arguments):
                raise RuntimeError("Paperclip MCP configuration is missing or duplicated")
            validate_native_mcp_config(arguments[index + 1], root, configured_origin)
            native_mcp = True
            index += 2
            continue
        if item == "--strict-mcp-config":
            if strict_mcp:
                raise RuntimeError("Paperclip MCP configuration is duplicated")
            strict_mcp = True
            index += 1
            continue
        # Native defaults are recognized but never imported. The host supplies
        # its own guest tools and scoped Paperclip API tool instead.
        result.append(item)
        index += 1
    if strict_mcp and not native_mcp:
        raise RuntimeError("Paperclip strict MCP configuration is missing")
    if "--print" not in result and "-p" not in result:
        raise RuntimeError("The Paperclip launcher accepts Claude print sessions only")
    claude.validate_claude_arguments(result)
    return result, "\n\n".join(instructions)


def main():
    if sys.argv[1:] in (["--version"], ["-v"]):
        return subprocess.call(["claude", "--version"])
    company = str(uuid.UUID(os.environ["PAPERCLIP_COMPANY_ID"]))
    agent = str(uuid.UUID(os.environ["PAPERCLIP_AGENT_ID"]))
    run = str(uuid.UUID(os.environ["PAPERCLIP_RUN_ID"]))
    key = os.environ.get("PAPERCLIP_API_KEY", "")
    if not key or "\n" in key:
        raise RuntimeError("Paperclip must supply its scoped run credential")
    config = json.loads((Path.home() / ".local/share/environment-orchestrator/paperclip.json").read_text())
    if company not in {item["id"] for item in config["companies"]}:
        raise RuntimeError("Paperclip company is not enabled on this host")
    context = {"PAPERCLIP_COMPANY_ID": company, "PAPERCLIP_AGENT_ID": agent,
        "PAPERCLIP_RUN_ID": run, "PAPERCLIP_API_KEY": key,
        "PAPERCLIP_API_URL": paperclip_codex.paperclip_api_origin(config["base_url"], os.environ.get("PAPERCLIP_API_URL", ""), os.environ.get("PAPERCLIP_API_BRIDGE_MODE", ""))}
    root = Path.home() / ".local/share/environment-orchestrator/paperclip-staging/.paperclip-runtime/runs" / run
    arguments, instructions = arguments_for_managed_claude(sys.argv[1:], root, config["base_url"])
    return claude.main(["--paperclip", company, agent, *arguments], context, instructions)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, KeyError, ValueError, OSError):
        print("Paperclip Claude launcher failed. Check its identity, run credential, arguments, and local configuration.", file=sys.stderr)
        raise SystemExit(1)
