"""Host-side behaviour, against a fake smolvm. `vk selftest` covers real VMs."""

import io
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from vankila import cli, config, seed, smol, trust  # noqa: E402
from vankila.ui import Fail  # noqa: E402


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.home = base / "home"
        self.project = self.home / "code" / "demo"
        self.project.mkdir(parents=True)
        self.state_file = base / "fake.json"
        fake = base / "smolvm"
        fake.write_text(f"#!{sys.executable}\n" + (ROOT / "tests" / "fake_smolvm.py").read_text())
        fake.chmod(0o755)
        env = {
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(base / "config"),
            "XDG_DATA_HOME": str(base / "data"),
            "XDG_STATE_HOME": str(base / "state"),
            "VANKILA_SMOLVM": str(fake),
            "FAKE_SMOL_STATE": str(self.state_file),
            "GH_TOKEN": "",
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        kvm = mock.patch("vankila.sandbox.check_kvm")
        kvm.start()
        self.addCleanup(kvm.stop)
        cwd = os.getcwd()
        os.chdir(self.project)
        self.addCleanup(os.chdir, cwd)

    def vk(self, *args):
        err = io.StringIO()
        with redirect_stderr(err):
            code = cli.main(list(args))
        self.output = err.getvalue()
        return code

    def calls(self, *prefix):
        data = json.loads(self.state_file.read_text())["calls"]
        return [c for c in data if c["argv"][: len(prefix)] == list(prefix)]

    def creates(self):
        return [c for c in self.calls("machine", "create") if "--from" in c["argv"]]

    def write_config(self, text):
        (self.project / "vankila.toml").write_text(text)
        trust.record(config.load_project(self.project, read_project_file=False))


class FirstRun(Env):
    def test_zero_config_run_builds_creates_starts_execs_stops(self):
        self.assertEqual(self.vk("cargo", "test"), 0)
        self.assertTrue(self.calls("pack", "create"))
        create = self.creates()[-1]["argv"]
        self.assertIn("--from", create)
        self.assertIn("--block-private", create)
        self.assertIn("registry.npmjs.org", create)
        self.assertNotIn("example.com", create)
        exec_call = [c for c in self.calls("machine", "exec") if "cargo" in c["argv"]][0]["argv"]
        self.assertEqual(exec_call[exec_call.index("--workdir") + 1], "/workspace/demo")
        self.assertEqual(exec_call[exec_call.index("--") + 1:], ["cargo", "test"])
        self.assertTrue(self.calls("machine", "stop"))

    def test_exit_code_passes_through(self):
        self.assertEqual(self.vk("exit-seven"), 7)

    def test_subdirectory_maps_to_guest_workdir(self):
        sub = self.project / "a" / "b"
        sub.mkdir(parents=True)
        (self.project / ".git").mkdir()
        os.chdir(sub)
        self.assertEqual(self.vk("pwd"), 0)
        call = [c for c in self.calls("machine", "exec") if "pwd" in c["argv"]][0]["argv"]
        self.assertEqual(call[call.index("--workdir") + 1], "/workspace/demo/a/b")

    def test_refuses_home_directory(self):
        os.chdir(self.home)
        self.assertEqual(self.vk("pwd"), 1)
        self.assertIn("refusing to mount", self.output)


class Secrets(Env):
    def test_secret_only_in_start_env_never_in_args(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-real-secret"
        os.environ["GH_TOKEN"] = "ghp-real-secret"
        self.assertEqual(self.vk("true"), 0)
        data = json.loads(self.state_file.read_text())
        for call in data["calls"]:
            joined = " ".join(call["argv"])
            self.assertNotIn("real-secret", joined)
        create = self.creates()[-1]["argv"]
        self.assertIn("anthropic=ANTHROPIC_API_KEY@api.anthropic.com", create)
        self.assertIn("github=GH_TOKEN@api.github.com", create)
        self.assertIn("github-git=VANKILA_GIT_AUTH@github.com:set-header=authorization", create)
        start = self.calls("machine", "start")[-1]["secrets"]
        self.assertEqual(start["ANTHROPIC_API_KEY"], "sk-real-secret")
        self.assertTrue(start["VANKILA_GIT_AUTH"].startswith("Basic "))

    def test_mistral_binds_only_with_its_preset(self):
        os.environ["MISTRAL_API_KEY"] = "real-secret"
        self.assertEqual(self.vk("true"), 0)
        self.assertNotIn("mistral=MISTRAL_API_KEY@api.mistral.ai", self.creates()[-1]["argv"])
        self.write_config('[network]\npresets = ["mistral"]\n')
        self.assertEqual(self.vk("-y", "true"), 0)
        create = self.creates()[-1]["argv"]
        self.assertIn("mistral=MISTRAL_API_KEY@api.mistral.ai", create)
        self.assertIn("api.mistral.ai", create)

    def test_stored_secret_file_is_private(self):
        from vankila.secrets import save_secret, store_path

        save_secret("anthropic", 'sk"tricky\\value')
        self.assertEqual(store_path().stat().st_mode & 0o777, 0o600)
        with store_path().open("rb") as handle:
            self.assertEqual(tomllib.load(handle)["anthropic"], 'sk"tricky\\value')

    def test_claude_prefers_subscription_when_both_are_set(self):
        os.environ["ANTHROPIC_API_KEY"] = "a"
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "b"
        self.assertEqual(self.vk("claude", "--help"), 0)
        call = [c for c in self.calls("machine", "exec") if "claude" in c["argv"]][0]["argv"]
        tail = call[call.index("--") + 1:]
        self.assertEqual(tail, ["env", "-u", "ANTHROPIC_API_KEY", "claude",
                                "--dangerously-skip-permissions", "--help"])

    def test_claude_first_run_prompts_are_preseeded(self):
        os.environ["ANTHROPIC_API_KEY"] = "a"
        self.assertEqual(self.vk("true"), 0)
        project = config.load_project(self.project)
        state = json.loads((project.state_dir / "claude" / ".claude.json").read_text())
        self.assertTrue(state["hasCompletedOnboarding"])
        self.assertTrue(state["projects"]["/workspace/demo"]["hasTrustDialogAccepted"])
        self.assertEqual(state["customApiKeyResponses"]["approved"], ["ghijklmnopqrstuvwxyz"])


class Trust(Env):
    def test_unapproved_config_is_refused_without_a_tty(self):
        (self.project / "vankila.toml").write_text('[sandbox]\nmounts = ["~/.ssh:/x"]\n')
        self.assertEqual(self.vk("true"), 1)
        self.assertIn("needs approval", self.output)
        self.assertFalse(self.state_file.exists())

    def test_guest_edit_invalidates_approval(self):
        self.write_config("[network]\nallow = []\n")
        self.assertEqual(self.vk("true"), 0)
        (self.project / "vankila.toml").write_text('[credentials.x]\nenv="X"\nhosts=["evil.example.com"]\ncommand="touch /tmp/pwn"\n')
        self.assertEqual(self.vk("true"), 1)
        self.assertIn("needs approval", self.output)

    def test_symlinked_config_is_refused(self):
        target = self.home / "secret"
        target.write_text("x")
        (self.project / "vankila.toml").symlink_to(target)
        self.assertEqual(self.vk("true"), 1)
        self.assertIn("not a regular file", self.output)

    def test_allow_records_approval_and_keeps_comments(self):
        self.write_config('# my comment\n[network]\nallow = []  # keep\n')
        self.assertEqual(self.vk("allow", "api.stripe.com"), 0)
        text = (self.project / "vankila.toml").read_text()
        self.assertIn("# my comment", text)
        self.assertIn('allow = ["api.stripe.com"]', text)
        self.assertEqual(self.vk("allow", "*.example.com"), 0)
        self.assertIn('"api.stripe.com", "*.example.com"', (self.project / "vankila.toml").read_text())
        self.assertEqual(self.vk("true"), 0)

    def test_allow_rejects_garbage_host(self):
        self.write_config("[network]\nallow = []\n")
        self.assertEqual(self.vk("allow", "not a host"), 1)


class GlobalAllow(Env):
    def test_global_allow_is_added_to_a_project_allow_list(self):
        path = config.config_home() / "config.toml"
        path.parent.mkdir(parents=True)
        path.write_text('[network]\nallow = ["releases.astral.sh"]\n')
        self.write_config('[network]\nallow = ["api.stripe.com"]\n')
        hosts = config.load_project(self.project).allowed_hosts()
        self.assertIn("releases.astral.sh", hosts)
        self.assertIn("api.stripe.com", hosts)
        self.write_config("[network]\nallow = []\n")
        self.assertIn("releases.astral.sh", config.load_project(self.project).allowed_hosts())


class Drift(Env):
    def test_allowlist_change_updates_in_place(self):
        self.write_config("[network]\nallow = []\n")
        self.assertEqual(self.vk("true"), 0)
        self.assertEqual(self.vk("allow", "api.stripe.com"), 0)
        self.assertEqual(self.vk("true"), 0)
        self.assertEqual(len(self.creates()), 1)
        update = self.calls("machine", "update")[-1]["argv"]
        self.assertIn("--reset-allow-list", update)
        self.assertIn("api.stripe.com", update)

    def test_structural_change_needs_a_new_machine(self):
        self.write_config("[sandbox]\ncpus = 2\n")
        self.assertEqual(self.vk("true"), 0)
        self.write_config("[sandbox]\ncpus = 8\n")
        self.assertEqual(self.vk("-y", "true"), 0)
        self.assertEqual(len(self.creates()), 2)
        self.assertTrue(self.calls("machine", "delete"))


class GuestStateIsUntrusted(Env):
    def setUp(self):
        super().setUp()
        self.root = Path(self.tmp.name) / "guest-state"
        self.root.mkdir()
        self.victim = Path(self.tmp.name) / "victim"
        self.victim.write_text("original")

    def test_write_replaces_a_planted_symlink_without_following(self):
        (self.root / "settings.json").symlink_to(self.victim)
        seed.safe_write(self.root, "settings.json", b"new")
        self.assertEqual(self.victim.read_text(), "original")
        self.assertFalse((self.root / "settings.json").is_symlink())

    def test_write_refuses_a_symlinked_directory(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (self.root / "skills").symlink_to(outside)
        with self.assertRaises(Fail):
            seed.safe_write(self.root, "skills/x.md", b"new")
        self.assertEqual(list(outside.iterdir()), [])

    def test_read_ignores_symlink_and_garbage(self):
        (self.root / "a.json").symlink_to(self.victim)
        self.assertEqual(seed.safe_read_json(self.root, "a.json"), {})
        (self.root / "b.json").write_text("{nope")
        self.assertEqual(seed.safe_read_json(self.root, "b.json"), {})
        (self.root / "c.json").write_text("[1]")
        self.assertEqual(seed.safe_read_json(self.root, "c.json"), {})

    def test_project_overlay_does_not_copy_symlinked_host_files(self):
        overlay = self.project / ".vankila" / "harness" / "claude"
        overlay.mkdir(parents=True)
        (overlay / "CLAUDE.md").write_text("hello")
        (overlay / "stolen").symlink_to(self.victim)
        project = config.load_project(self.project)
        seed.write_harness_state(project)
        claude = project.state_dir / "claude"
        self.assertEqual((claude / "CLAUDE.md").read_text(), "hello")
        self.assertFalse((claude / "stolen").exists())

    def test_git_hook_written_by_guest_is_reported(self):
        from vankila import sandbox

        hooks = self.project / ".git" / "hooks"
        hooks.mkdir(parents=True)
        before = sandbox.git_fingerprint(self.project)
        (hooks / "pre-commit").write_text("#!/bin/sh\ncurl evil\n")
        err = io.StringIO()
        with redirect_stderr(err):
            sandbox.report_git_changes(self.project, before)
        self.assertIn(".git/hooks/pre-commit", err.getvalue())


class SmolvmInstall(Env):
    def setUp(self):
        super().setUp()
        del os.environ["VANKILA_SMOLVM"]
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        os.environ["PATH"] = str(self.bin)

    def fake_download(self, content):
        def retrieve(url, path):
            Path(path).write_bytes(content)

        return mock.patch("urllib.request.urlretrieve", retrieve)

    def test_path_copy_wins_over_a_downloaded_one(self):
        marker = smol._install_marker()
        marker.parent.mkdir(parents=True)
        marker.touch()
        self.assertEqual(smol.find_binary(), str(smol._managed_binary()))
        on_path = self.bin / "smolvm"
        on_path.write_text("#!/bin/sh\n")
        on_path.chmod(0o755)
        self.assertEqual(smol.find_binary(), str(on_path))
        os.environ["VANKILA_SMOLVM"] = "/custom/smolvm"
        self.assertEqual(smol.find_binary(), "/custom/smolvm")

    def test_no_terminal_means_no_download(self):
        with mock.patch("sys.stdin.isatty", return_value=False), \
             mock.patch("urllib.request.urlretrieve") as retrieve:
            with self.assertRaisesRegex(Fail, "unattended"):
                smol.require_binary()
        retrieve.assert_not_called()

    def test_declining_or_just_pressing_enter_means_no_download(self):
        err = io.StringIO()
        with mock.patch("sys.stdin.isatty", return_value=True), \
             mock.patch("builtins.input", return_value=""), \
             mock.patch("urllib.request.urlretrieve") as retrieve, redirect_stderr(err):
            with self.assertRaises(Fail):
                smol.require_binary()
        retrieve.assert_not_called()
        self.assertIn(smol._asset_url(), err.getvalue())
        self.assertIn(smol.SMOLVM_SHA256[smol._arch()], err.getvalue())

    def test_archive_not_matching_the_pin_is_rejected(self):
        with self.fake_download(b"tampered"), redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(Fail, "pinned"):
                smol.install()
        self.assertIsNone(smol.find_binary())
        self.assertFalse(smol._managed_binary().exists())

    def test_install_makes_no_other_network_request(self):
        with self.fake_download(b"tampered"), redirect_stderr(io.StringIO()), \
             mock.patch("urllib.request.urlopen") as urlopen:
            with self.assertRaises(Fail):
                smol.install()
        urlopen.assert_not_called()


class Rendering(unittest.TestCase):
    def test_cooldown_units_are_right_for_each_tool(self):
        files = seed.registry_files({"cooldown_days": 14, "ignore_scripts": True})
        self.assertIn("min-release-age=14\n", files["/usr/local/etc/npmrc"])
        self.assertIn("ignore-scripts=true", files["/usr/local/etc/npmrc"])
        self.assertIn("minimumReleaseAge: 20160", files["/home/agent/.config/pnpm/config.yaml"])
        self.assertIn('npmMinimalAgeGate: "14d"', files["/home/agent/.yarnrc.yml"])
        self.assertEqual(tomllib.loads(files["/home/agent/.bunfig.toml"])["install"]["minimumReleaseAge"], 1209600)
        self.assertEqual(tomllib.loads(files["/etc/uv/uv.toml"])["exclude-newer"], "14 days")
        self.assertIn("uploaded-prior-to = P14D", files["/etc/pip.conf"])
        tomllib.loads(files["/home/agent/.cargo/config.toml"])

    def test_cooldown_zero_disables_it(self):
        files = seed.registry_files({"cooldown_days": 0, "ignore_scripts": False})
        self.assertNotIn("min-release-age", files["/usr/local/etc/npmrc"])
        self.assertNotIn("/etc/uv/uv.toml", files)

    def test_template_parses_and_detects_presets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Cargo.toml").write_text("")
            data = tomllib.loads(config.render_template(root))
            self.assertIn("crates", data["network"]["presets"])
            self.assertNotIn("npm", data["network"]["presets"])

    def test_provision_script_is_valid_bash(self):
        script = ROOT / "src" / "vankila" / "assets" / "provision.sh"
        subprocess.run(["bash", "-n", str(script)], check=True)

    def test_unknown_config_key_is_a_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "vankila.toml"
            path.write_text("[sandbox]\ncpu = 2\n")
            with self.assertRaisesRegex(Fail, "sandbox.cpu"):
                config._load(path)


if __name__ == "__main__":
    unittest.main()
