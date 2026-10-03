"""Verify launcher isolation contracts without inference or VM side effects."""

import importlib.util
from pathlib import Path
import tempfile
import tomllib
import unittest

spec = importlib.util.spec_from_file_location("environment_codex", Path(__file__).with_name("codex.py"))
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class CodexLauncherTests(unittest.TestCase):
    def test_only_workspace_environment_is_configured(self):
        config = tomllib.loads(launcher.environments_config({
            "id": "example", "exec_url": "ws://127.0.0.1:6090/workspaces/example/exec",
            "auth_bearer_token": "private-capability",
        }))
        self.assertFalse(config["include_local"])
        self.assertEqual(config["default"], "example")
        self.assertEqual([item["id"] for item in config["environments"]], ["example"])
        self.assertEqual(config["environments"][0]["auth_bearer_token"], "private-capability")

    def test_host_inference_key_cannot_be_inherited_by_shell_tools(self):
        config = tomllib.loads(launcher.harness_config())
        self.assertEqual(config["model_provider"], "environment_inference")
        self.assertEqual(config["shell_environment_policy"]["inherit"], "core")
        self.assertFalse(config["shell_environment_policy"]["ignore_default_excludes"])
        self.assertIn(launcher.PROVIDER_KEY_ENV, config["shell_environment_policy"]["exclude"])
        self.assertFalse(config["features"]["hooks"])
        self.assertTrue(config["features"]["code_mode_host"])

    def test_namespace_cannot_access_host_workspace_or_key_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            state = home / "state"
            state.mkdir()
            command = launcher.isolated_command(
                "bwrap", "/nix/store/fake/bin/codex", state, home,
                "/var/lib/agent/workspace", ["exec", "Check files"],
            )
            bindings = []
            for index, argument in enumerate(command):
                if argument in ("--bind", "--ro-bind"):
                    bindings.append(command[index + 1])
            self.assertIn(str(state), bindings)
            self.assertNotIn(str(home), bindings)
            self.assertNotIn("/", bindings)
            self.assertNotIn("/var/lib/agent/workspace", bindings)
            self.assertFalse(any("desktop-broker" in item for item in bindings))
            # NixOS's certificate directory contains /etc/static symlinks.
            # The namespace needs the resolved bundle, not those symlinks.
            self.assertNotIn("/etc/ssl/certs", bindings)
            if Path("/etc/ssl/certs/ca-certificates.crt").exists():
                self.assertIn(str(Path("/etc/ssl/certs/ca-certificates.crt").resolve()), bindings)

    def test_reject_overrides_and_non_openai_models(self):
        for arguments in (["exec", "-c", "model_provider=other"], ["--cd=/home/agent"],
                          ["exec", "--model", "claude-anything"], ["exec-server"],
                          ["--enable", "hooks"]):
            with self.subTest(arguments=arguments), self.assertRaises(RuntimeError):
                launcher.validate_codex_arguments(arguments)
        launcher.validate_codex_arguments(["exec", "--model", "gpt-6.1-sol", "--json", "Inspect files"])

    def test_config_files_are_private_and_replace_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "environments.toml"
            launcher.atomic_private_write(path, "first")
            launcher.atomic_private_write(path, "second")
            self.assertEqual(path.read_text(), "second")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(Path(directory).iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
