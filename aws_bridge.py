"""Vend temporary AWS role credentials to authorized Paperclip guests."""
import argparse
from contextlib import closing
import datetime
import getpass
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import codex

AIME = "8744dbdb-cdd7-4fee-8b46-3bd6ae6705fe"
PROFILES = {"swe", "frontend"}
CONFIG = Path.home() / ".config/environment-orchestrator/aws.json"
STATE = Path.home() / ".local/share/environment-orchestrator"
PORT = 6092


def private_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 16384:
            raise ValueError("AWS configuration must be a private owned regular file.")
        return json.load(stream)


def load_config(path=CONFIG):
    value = private_json(path)
    if not isinstance(value, dict) or set(value) != {"account_id", "role_arn", "region", "access_key_id", "secret_access_key"}:
        raise ValueError("Invalid AWS configuration.")
    if any(not isinstance(v, str) or not v or any(c in v for c in "\r\n\x00") for v in value.values()):
        raise ValueError("Invalid AWS configuration.")
    if not re.fullmatch(r"[0-9]{12}", value["account_id"]) or not re.fullmatch(r"arn:aws:iam::" + value["account_id"] + r":role/[A-Za-z0-9+=,.@_/-]+", value["role_arn"]):
        raise ValueError("Invalid AWS account or role.")
    if value["region"] != "ca-central-1":
        raise ValueError("AIME's default AWS region must be ca-central-1.")
    return value


def aws(config, arguments):
    # Do not inherit host profiles, endpoint overrides, or other AWS credentials.
    env = {"PATH": os.environ.get("PATH", "/run/current-system/sw/bin"),
           "HOME": "/nonexistent", "AWS_CONFIG_FILE": "/dev/null",
           "AWS_SHARED_CREDENTIALS_FILE": "/dev/null", "AWS_EC2_METADATA_DISABLED": "true",
           "AWS_ACCESS_KEY_ID": config["access_key_id"], "AWS_SECRET_ACCESS_KEY": config["secret_access_key"],
           "AWS_DEFAULT_REGION": config["region"], "AWS_PAGER": "",
           "SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt"}
    result = subprocess.run(["aws", *arguments, "--output", "json", "--no-cli-pager"],
                            env=env, capture_output=True, timeout=45)
    if result.returncode:
        # AWS errors can contain credential or request details. Keep them private.
        raise RuntimeError("AWS authentication failed. Check the AIME IAM user and role configuration.")
    return json.loads(result.stdout)


def role_credentials(config, workspace, runner=aws):
    result = runner(config, ["sts", "assume-role", "--role-arn", config["role_arn"],
                            "--role-session-name", "aime-" + workspace[:48], "--duration-seconds", "3600"])
    value = result["Credentials"]
    expiry = datetime.datetime.fromisoformat(value["Expiration"].replace("Z", "+00:00"))
    remaining = (expiry - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
    if not 60 < remaining <= 3700 or not value["AccessKeyId"].startswith("ASIA"):
        raise ValueError("AWS did not return short-lived role credentials.")
    return {"Version": 1, **{k: value[k] for k in ("AccessKeyId", "SecretAccessKey", "SessionToken", "Expiration")}}


def workspace_scope(workspace, state=STATE):
    if not isinstance(workspace, str) or not codex.WORKSPACE_ID.fullmatch(workspace):
        return None
    # Read the actual binding and immutable allocated profile, not request claims.
    with closing(sqlite3.connect(f"file:{state / 'paperclip.sqlite'}?mode=ro", uri=True)) as db:
        agent = db.execute("SELECT company FROM agents WHERE workspace=? AND present=1 AND status IN ('idle','running','error')", (workspace,)).fetchone()
    with closing(sqlite3.connect(f"file:{state / 'state.sqlite'}?mode=ro", uri=True)) as db:
        binding = db.execute("SELECT slot,profile FROM workspaces WHERE id=?", (workspace,)).fetchone()
    if not agent or agent[0] != AIME or not binding or binding[1] not in PROFILES or not 0 <= binding[0] < 62:
        return None
    return {"slot": binding[0], "guest_ip": f"172.30.78.{2 + 4 * binding[0]}"}


def credential_request(body, client_ip, state=STATE, config_path=CONFIG, runner=aws):
    if not isinstance(body, dict) or set(body) != {"workspace", "token"}:
        raise ValueError("Access denied.")
    scope = workspace_scope(body["workspace"], state)
    if not scope or client_ip != scope["guest_ip"] or not isinstance(body["token"], str):
        raise ValueError("Access denied.")
    capability = private_json(state / "aws-capabilities" / (body["workspace"] + ".json"))
    if not hmac.compare_digest(body["token"], capability["token"]):
        raise ValueError("Access denied.")
    # Check scope and capability on every request, before loading source keys.
    return role_credentials(load_config(config_path), body["workspace"], runner)


def provision(binding, company_id):
    if company_id != AIME or binding.get("profile") not in PROFILES or not CONFIG.exists():
        return
    scope = workspace_scope(binding["id"])
    if not scope:
        raise RuntimeError("AWS workspace scope does not match the AIME binding.")
    load_config()
    folder = STATE / "aws-capabilities"
    codex.private_directory(folder)
    path = folder / (binding["id"] + ".json")
    # Rotate on each launch; old harness capabilities stop working immediately.
    token = secrets.token_urlsafe(48)
    codex.atomic_private_write(path, json.dumps({"token": token}))
    import guest_mcp
    executor = guest_mcp.GuestExecutor(binding)
    try:
        operation = executor.call("guest_exec", {"command": "mkdir -p /var/lib/agent/.aws && chmod 700 /var/lib/agent/.aws", "wait_ms": 30000})
        while not operation["exited"]:
            operation = executor.call("guest_wait", {"process_id": operation["process_id"], "wait_ms": 10000})
        if operation["exit_code"] != 0:
            raise RuntimeError("Could not prepare guest AWS configuration.")
        endpoint = f"http://172.30.78.{1 + 4 * scope['slot']}:{PORT}/credentials"
        executor.call("guest_write", {"path": "/var/lib/agent/.aws/aime-bridge.json",
                      "text": json.dumps({"url": endpoint, "workspace": binding["id"], "token": token})})
        executor.call("guest_write", {"path": "/var/lib/agent/.aws/config",
                      "text": "# Managed AIME AWS profile\n[default]\nregion = ca-central-1\noutput = json\ncredential_process = /run/current-system/sw/bin/environment-aws-credentials\n"})
        operation = executor.call("guest_exec", {"command": "chmod 600 /var/lib/agent/.aws/aime-bridge.json /var/lib/agent/.aws/config", "wait_ms": 30000})
        while not operation["exited"]:
            operation = executor.call("guest_wait", {"process_id": operation["process_id"], "wait_ms": 10000})
        if operation["exit_code"] != 0:
            raise RuntimeError("Could not secure guest AWS configuration.")
    finally:
        executor.close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if self.path != "/credentials" or not 0 < length <= 4096:
                raise ValueError("Access denied.")
            body = json.loads(self.rfile.read(length))
            result = credential_request(body, self.client_address[0])
            payload = json.dumps(result).encode()
            status = 200
        except Exception:
            payload, status = b'{"error":"AWS credentials unavailable for this workspace."}', 403
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        return connection, address


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="action", required=True)
    setup = subs.add_parser("setup")
    setup.add_argument("--role-arn", required=True)
    subs.add_parser("check")
    subs.add_parser("serve")
    args = parser.parse_args()
    if args.action == "serve":
        Server(("0.0.0.0", PORT), Handler).serve_forever()
        return
    if args.action == "setup":
        if not sys.stdin.isatty() or not sys.stderr.isatty():
            raise ValueError("Enter AWS keys in a private interactive terminal.")
        match = re.fullmatch(r"arn:aws:iam::([0-9]{12}):role/[A-Za-z0-9+=,.@_/-]+", args.role_arn)
        if not match:
            raise ValueError("Enter the AIME role ARN from the CloudFormation outputs.")
        config = {"account_id": match[1], "role_arn": args.role_arn, "region": "ca-central-1",
                  "access_key_id": getpass.getpass("AIME IAM user access key ID (hidden): "),
                  "secret_access_key": getpass.getpass("AIME IAM user secret access key (hidden): ")}
        identity = aws(config, ["sts", "get-caller-identity"])
        if identity.get("Account") != config["account_id"] or not identity.get("Arn", "").startswith(f"arn:aws:iam::{config['account_id']}:user/"):
            raise ValueError("Use the dedicated AIME IAM user's keys, not root keys.")
        role_credentials(config, "setup-check")
        codex.private_directory(CONFIG.parent)
        codex.atomic_private_write(CONFIG, json.dumps(config))
        print("AIME AWS authentication configured. New SWE and Frontend SWE runs receive temporary role credentials.")
    else:
        role_credentials(load_config(), "setup-check")
        print("AIME role authentication works; default region ca-central-1.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("environment-aws: Setup or authentication failed. Check the role ARN, IAM keys, trust policy, and private file permissions.", file=sys.stderr)
        raise SystemExit(1)
