"""Verify AWS scope and temporary credentials without contacting AWS."""
import datetime
from contextlib import closing, contextmanager
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import aws_bridge


@contextmanager
def database(path):
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            yield connection


class AWSTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        with database(self.root / "state.sqlite") as db:
            db.execute("CREATE TABLE workspaces(id TEXT, slot INTEGER, profile TEXT)")
            db.execute("INSERT INTO workspaces VALUES('pc-aime',2,'swe')")
        with database(self.root / "paperclip.sqlite") as db:
            db.execute("CREATE TABLE agents(company TEXT, workspace TEXT, present INTEGER, status TEXT)")
            db.execute("INSERT INTO agents VALUES(?,'pc-aime',1,'idle')", (aws_bridge.AIME,))
        self.config = self.root / "aws.json"
        self.write(self.config, {"account_id": "123456789012", "role_arn": "arn:aws:iam::123456789012:role/AIMEAgentsAdmin", "region": "ca-central-1", "access_key_id": "fixture", "secret_access_key": "fixture-secret"})
        (self.root / "aws-capabilities").mkdir()
        self.cap = self.root / "aws-capabilities/pc-aime.json"
        self.write(self.cap, {"token": "fixture-token"})
        self.runner = Mock(return_value={"Credentials": {"AccessKeyId": "ASIAfixture", "SecretAccessKey": "temporary-secret", "SessionToken": "temporary-token", "Expiration": (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)).isoformat()}})

    def write(self, path, value):
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    def request(self, **changes):
        body = {"workspace": "pc-aime", "token": "fixture-token", **changes}
        return aws_bridge.credential_request(body, "172.30.78.10", self.root, self.config, self.runner)

    def test_allowed_profiles_get_only_temporary_role_credentials(self):
        for profile in ("swe", "frontend"):
            with database(self.root / "state.sqlite") as db:
                db.execute("UPDATE workspaces SET profile=?", (profile,))
            result = self.request()
            self.assertEqual(result["Version"], 1)
            self.assertEqual(result["AccessKeyId"], "ASIAfixture")
            self.assertNotIn("fixture-secret", json.dumps(result))
            self.assertIn("assume-role", self.runner.call_args.args[1])
            self.assertIn("3600", self.runner.call_args.args[1])

    def test_denied_company_profiles_removal_and_standalone(self):
        for profile in ("research", "sales", "marketing"):
            with database(self.root / "state.sqlite") as db:
                db.execute("UPDATE workspaces SET profile=?", (profile,))
            with self.assertRaises(ValueError):
                self.request()
        with database(self.root / "state.sqlite") as db:
            db.execute("UPDATE workspaces SET profile='swe'")
        with database(self.root / "paperclip.sqlite") as db:
            db.execute("UPDATE agents SET company='other'")
        with self.assertRaises(ValueError):
            self.request()
        with database(self.root / "paperclip.sqlite") as db:
            db.execute("UPDATE agents SET company=?,present=0", (aws_bridge.AIME,))
        with self.assertRaises(ValueError):
            self.request()
        with self.assertRaises(ValueError):
            self.request(workspace="standalone")
        self.runner.assert_not_called()

    def test_spoofed_claims_paths_tokens_and_other_guest_address_denied(self):
        for changes in ({"company": aws_bridge.AIME}, {"profile": "swe"}, {"workspace": "../pc-aime"}, {"token": "wrong"}):
            with self.assertRaises(ValueError):
                self.request(**changes)
        with self.assertRaises(ValueError):
            aws_bridge.credential_request({"workspace": "pc-aime", "token": "fixture-token"}, "172.30.78.14", self.root, self.config, self.runner)
        self.runner.assert_not_called()

    def test_insecure_config_and_symlinks_fail_closed(self):
        self.config.chmod(0o644)
        with self.assertRaises(ValueError):
            self.request()
        self.config.chmod(0o600)
        alternate = self.root / "target.json"
        self.config.rename(alternate)
        self.config.symlink_to(alternate)
        with self.assertRaises(OSError):
            self.request()
        self.runner.assert_not_called()

    def test_expired_or_long_lived_keys_rejected(self):
        self.runner.return_value["Credentials"]["AccessKeyId"] = "AKIAfixture"
        with self.assertRaises(ValueError):
            self.request()
        self.runner.return_value["Credentials"]["AccessKeyId"] = "ASIAfixture"
        self.runner.return_value["Credentials"]["Expiration"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self.assertRaises(ValueError):
            self.request()

    def test_aws_process_does_not_inherit_host_secrets_or_overrides(self):
        with patch.dict("os.environ", {"AWS_SESSION_TOKEN": "host-token", "AWS_ENDPOINT_URL": "https://untrusted.invalid", "OPENAI_API_KEY": "inference"}), patch("subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = b'{}'
            aws_bridge.aws(aws_bridge.load_config(self.config), ["sts", "get-caller-identity"])
            env = run.call_args.kwargs["env"]
            self.assertNotIn("AWS_SESSION_TOKEN", env)
            self.assertNotIn("AWS_ENDPOINT_URL", env)
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertEqual(env["AWS_CONFIG_FILE"], "/dev/null")

    def test_unconfigured_or_unauthorized_launcher_never_wakes_guest(self):
        with patch("guest_mcp.GuestExecutor") as executor:
            aws_bridge.provision({"profile": "swe"}, "other")
            aws_bridge.provision({"profile": "research"}, aws_bridge.AIME)
            with patch.object(aws_bridge, "CONFIG", self.root / "missing"):
                aws_bridge.provision({"profile": "swe"}, aws_bridge.AIME)
            executor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
