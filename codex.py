#!/usr/bin/env python3
"""Run a host Codex harness with one remote-only Firecracker environment."""

import fcntl
import contextlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile


WORKSPACE_ID = re.compile(r"[a-z][a-z0-9_-]{0,47}\Z")
PROVIDER_KEY_ENV = "ENVIRONMENT_INFERENCE_API_KEY"
PROVIDER_URL = "https://ai.h.mattre.id/v1"
DEFAULT_MODEL = "gpt-6.1-sol"


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=90)
        self.socket_path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


def private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or path.stat().st_uid != os.getuid():
        raise RuntimeError("Harness state must be an owned directory")
    path.chmod(0o700)


def atomic_private_write(path, contents):
    fd, temporary = tempfile.mkstemp(prefix=".config-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def workspace_binding(workspace, control_socket, paperclip=None, profile=None):
    connection = UnixConnection(control_socket)
    try:
        path = "/workspaces" if paperclip is None else f"/paperclip/companies/{paperclip[0]}/agents/{paperclip[1]}/workspace"
        body = {"id": workspace}
        if profile is not None:
            body["profile"] = profile
        connection.request(
            "POST", path, body=json.dumps(body),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        payload = json.loads(response.read(1024 * 1024))
        if response.status not in (200, 201):
            # Do not echo arbitrary API bodies: they can contain capabilities.
            raise RuntimeError(f"Workspace binding failed (HTTP {response.status})")
    finally:
        connection.close()
    if paperclip is not None:
        workspace = payload.get("id")
        if not isinstance(workspace, str) or not WORKSPACE_ID.fullmatch(workspace):
            raise RuntimeError("Orchestrator returned an invalid Paperclip workspace")
    if payload.get("id") != workspace:
        raise RuntimeError("Orchestrator returned the wrong workspace")
    expected = f"ws://127.0.0.1:6090/workspaces/{workspace}/exec"
    if payload.get("exec_url") != expected:
        raise RuntimeError("Orchestrator returned an unexpected execution endpoint")
    if payload.get("workspace_path") != "/var/lib/agent/workspace":
        raise RuntimeError("Orchestrator returned an unexpected guest workspace path")
    token = payload.get("auth_bearer_token")
    if not isinstance(token, str) or not token or "\n" in token:
        raise RuntimeError("Orchestrator returned an invalid workspace capability")
    return payload


def environments_config(binding):
    workspace = binding["id"]
    return (
        f"default = {json.dumps(workspace)}\ninclude_local = false\n\n"
        "[[environments]]\n"
        f"id = {json.dumps(workspace)}\n"
        f"url = {json.dumps(binding['exec_url'])}\n"
        f"auth_bearer_token = {json.dumps(binding['auth_bearer_token'])}\n"
        "connect_timeout_sec = 90\ninitialize_timeout_sec = 90\n"
    )


def harness_config(paperclip_context=None, mail_context=None):
    config = f'''model = "{DEFAULT_MODEL}"
model_provider = "environment_inference"
approval_policy = "never"
sandbox_mode = "danger-full-access"
web_search = "disabled"

[model_providers.environment_inference]
name = "Local OpenAI inference"
base_url = "{PROVIDER_URL}"
env_key = "{PROVIDER_KEY_ENV}"
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false

[shell_environment_policy]
inherit = "core"
ignore_default_excludes = false
exclude = ["{PROVIDER_KEY_ENV}", "OPENAI_API_KEY", "CODEX_API_KEY", "PAPERCLIP_API_KEY", "*SECRET*", "*TOKEN*"]

[features]
apps = false
plugins = false
hooks = false
browser_use = false
browser_use_external = false
computer_use = false
in_app_browser = false
in_app_local_automation = false
code_mode_host = true
multi_agent = false
'''
    if paperclip_context:
        config += '\n[mcp_servers.paperclip]\nrequired = true\n'
        config += f'url = {json.dumps(paperclip_context["url"])}\n'
        config += '\n[mcp_servers.paperclip.http_headers]\n'
        config += f'Authorization = {json.dumps("Bearer " + paperclip_context["capability"])}\n'
    if mail_context:
        config += '\n[mcp_servers.mail]\nrequired = true\n'
        config += f'url = {json.dumps(mail_context["url"])}\n'
        config += '\n[mcp_servers.mail.http_headers]\n'
        config += f'Authorization = {json.dumps("Bearer " + mail_context["capability"])}\n'
    return config


def validate_codex_arguments(arguments):
    forbidden = {
        "-c", "--config", "-C", "--cd", "--add-dir", "--worktree",
        "-p", "--profile", "--oss", "--local-provider", "--ignore-user-config",
        "--remote", "--enable", "--disable", "--dangerously-bypass-hook-trust",
    }
    for index, argument in enumerate(arguments):
        option = argument.split("=", 1)[0]
        if option in forbidden or (argument.startswith("-c") and not argument.startswith("--")):
            raise RuntimeError(f"Option {option} can override the managed environment")
        if option in ("--model", "-m") or (argument.startswith("-m") and not argument.startswith("--")):
            model = argument[2:] if argument.startswith("-m") and len(argument) > 2 and argument[2] != "=" else (
                argument.split("=", 1)[1] if "=" in argument else (
                arguments[index + 1] if index + 1 < len(arguments) else ""
                )
            )
            if not re.fullmatch(r"(?:gpt-[A-Za-z0-9._-]+|o[1-9][A-Za-z0-9._-]*)", model):
                raise RuntimeError("This launcher supports OpenAI models only")
    if arguments and arguments[0] in {
        "exec-server", "app-server", "sandbox", "debug", "mcp", "mcp-server",
        "login", "logout", "completion", "apply", "worktree",
    }:
        raise RuntimeError("This launcher runs agent sessions only")


def isolated_command(bwrap, codex, state_home, host_home, guest_path, arguments):
    # No host root, /home, /var, runtime sockets, or credential file is mounted.
    command = [
        bwrap, "--die-with-parent", "--new-session", "--unshare-user",
        "--unshare-pid", "--unshare-ipc", "--unshare-uts",
        "--hostname", "environment-harness", "--ro-bind", "/nix/store", "/nix/store",
        "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
        "--dir", str(host_home), "--bind", str(state_home), str(state_home),
        "--dir", guest_path, "--chdir", guest_path,
    ]
    for source, destination in (
        ("/run/current-system/sw", "/run/current-system/sw"),
        ("/etc/resolv.conf", "/etc/resolv.conf"),
        ("/etc/hosts", "/etc/hosts"),
        ("/etc/ssl/certs/ca-certificates.crt", "/etc/ssl/certs/ca-certificates.crt"),
        ("/etc/ssl/certs/ca-certificates.crt", "/etc/ssl/certs/ca-bundle.crt"),
        ("/etc/nsswitch.conf", "/etc/nsswitch.conf"),
        ("/etc/passwd", "/etc/passwd"),
        ("/etc/group", "/etc/group"),
    ):
        if Path(source).exists():
            command.extend(["--ro-bind", str(Path(source).resolve()), destination])
    skills = host_home / ".codex/skills"
    if skills.is_dir():
        command.extend(["--ro-bind", str(skills), str(state_home / "skills")])
    command.extend(["--", codex, *arguments])
    return command


def main(argv=None, paperclip_context=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] in ("-h", "--help"):
        print("Usage: environment-codex WORKSPACE [codex arguments]")
        print("       environment-codex --paperclip COMPANY_UUID AGENT_UUID [codex arguments]")
        print("Examples: environment-codex demo; environment-codex demo exec --json 'Inspect this project'")
        return 0
    paperclip = None
    if arguments[0] == "--paperclip":
        import uuid
        if len(arguments) < 3:
            raise RuntimeError("Paperclip mode requires company and agent UUIDs")
        try:
            paperclip = tuple(str(uuid.UUID(value)) for value in arguments[1:3])
        except ValueError as error:
            raise RuntimeError("Paperclip mode requires company and agent UUIDs") from error
        workspace, codex_arguments = None, arguments[3:]
    else:
        workspace, codex_arguments = arguments[0], arguments[1:]
    if paperclip is None and not WORKSPACE_ID.fullmatch(workspace):
        raise RuntimeError("Workspace IDs need a lowercase letter, then up to 47 lowercase letters, digits, underscores, or hyphens")
    validate_codex_arguments(codex_arguments)
    bwrap = shutil.which("bwrap")
    codex = shutil.which("codex")
    if not bwrap or not codex:
        raise RuntimeError("The Nix package must provide bubblewrap and Codex")
    codex = os.path.realpath(codex)
    host_home = Path.home()
    service_home = host_home / ".local/share/environment-orchestrator"
    if paperclip is not None:
        binding = workspace_binding(None, service_home / "control.sock", paperclip, os.environ.get("ENVIRONMENT_PROFILE"))
        workspace = binding["id"]
    state_home = service_home / "harnesses" / workspace
    private_directory(state_home)
    lock_path = state_home / "launcher.lock"
    with lock_path.open("a") as lock, contextlib.ExitStack() as cleanup:
        lock_path.chmod(0o600)
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("Another harness already owns this workspace") from error
        binding = workspace_binding(workspace, service_home / "control.sock", profile=os.environ.get("ENVIRONMENT_PROFILE"))
        atomic_private_write(state_home / "environments.toml", environments_config(binding))
        bridge = None
        if paperclip_context:
            import paperclip_mcp
            bridge = cleanup.enter_context(paperclip_mcp.http_bridge(paperclip_context))
        import mail_mcp
        mail_bridge = cleanup.enter_context(mail_mcp.http_bridge())
        atomic_private_write(state_home / "config.toml", harness_config(bridge, mail_bridge))
        key_path = host_home / ".config/desktop-broker/inference-key"
        if key_path.is_symlink() or key_path.stat().st_uid != os.getuid() or key_path.stat().st_mode & 0o077:
            raise RuntimeError("The inference key file must be owned by this user with mode 0600")
        key = key_path.read_text().strip()
        if not key:
            raise RuntimeError("The inference key file is empty")
        # A small allowlist keeps unrelated host secrets out of the harness.
        environment = {
            "HOME": str(host_home), "USER": "agent", "LOGNAME": "agent",
            "CODEX_HOME": str(state_home), PROVIDER_KEY_ENV: key,
            "PATH": "/run/current-system/sw/bin", "SHELL": "/run/current-system/sw/bin/bash",
            "LANG": "C.UTF-8", "SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt",
            "NIX_SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt",
            "TERM": os.environ.get("TERM", "xterm-256color"),
        }
        # -C validates an empty namespace placeholder, then all model workspace
        # operations use the selected remote filesystem, including AGENTS.md.
        command = isolated_command(
            bwrap, codex, state_home, host_home, binding["workspace_path"],
            ["-C", binding["workspace_path"], *codex_arguments],
        )
        child = subprocess.Popen(command, env=environment)
        try:
            return child.wait()
        except KeyboardInterrupt:
            child.terminate()
            return child.wait()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, OSError, ValueError) as error:
        print(f"environment-codex: {error}", file=sys.stderr)
        sys.exit(1)
