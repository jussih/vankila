"""Renders what goes inside a sandbox: registry policy and harness config."""

from __future__ import annotations

import importlib.resources
import io
import json
import os
import stat
import tarfile
from pathlib import Path

from .config import Project, config_home, deep_merge

SETUP_MARKER = "/var/lib/vankila/setup-done"


def registry_files(registries: dict) -> dict[str, str]:
    """Hardened package-manager config, keyed by absolute guest path."""
    days = int(registries["cooldown_days"])
    scripts_off = bool(registries["ignore_scripts"])
    npmrc = [
        f"min-release-age={days}",
        f"ignore-scripts={'true' if scripts_off else 'false'}",
        "fund=false",
        "update-notifier=false",
    ]
    yarnrc = [f'npmMinimalAgeGate: "{days}d"', f"enableScripts: {'false' if scripts_off else 'true'}"]
    pnpm_yaml = [f"minimumReleaseAge: {days * 1440}", f"ignoreScripts: {str(scripts_off).lower()}"]
    pnpm_rc = [f"minimum-release-age={days * 1440}", f"ignore-scripts={str(scripts_off).lower()}"]
    bunfig = ["[install]", f"minimumReleaseAge = {days * 86400}",
              f"ignoreScripts = {str(scripts_off).lower()}"]
    cargo = [
        "# Cargo has no package-age cooldown yet. Everything else here is what it can do:",
        "# sparse index over HTTPS only, and git fetches through the system git, so that",
        "# git's config and the sandbox's egress policy apply to them.",
        '[registries.crates-io]',
        'protocol = "sparse"',
        "[net]",
        "git-fetch-with-cli = true",
    ]
    if days == 0:
        npmrc = npmrc[1:]
        yarnrc = yarnrc[1:]
        pnpm_yaml = pnpm_yaml[1:]
        pnpm_rc = pnpm_rc[1:]
        bunfig.pop(1)
    files = {
        "/usr/local/etc/npmrc": npmrc,
        "/home/agent/.yarnrc.yml": yarnrc,
        "/home/agent/.config/pnpm/config.yaml": pnpm_yaml,
        "/home/agent/.config/pnpm/rc": pnpm_rc,
        "/home/agent/.bunfig.toml": bunfig,
        "/home/agent/.cargo/config.toml": cargo,
    }
    if days:
        files["/etc/uv/uv.toml"] = [f'exclude-newer = "{days} days"']
        files["/etc/pip.conf"] = ["[global]", f"uploaded-prior-to = P{days}D"]
    return {path: "\n".join(lines) + "\n" for path, lines in files.items()}


GITCONFIG = """\
[safe]
\tdirectory = *
[credential]
\thelper =
[url "https://github.com/"]
\tinsteadOf = git@github.com:
\tinsteadOf = ssh://git@github.com/
[init]
\tdefaultBranch = main
"""


def guest_tar(project: Project) -> bytes:
    """Files laid over the guest as root on every start, so they cannot drift."""
    files = registry_files(project.config["registries"])
    files["/etc/gitconfig"] = GITCONFIG
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for path, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(path.lstrip("/"))
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def bootstrap_script(host_uid: int, host_gid: int) -> str:
    """Runs as root after each boot. Matches the agent's uid to the mount's owner."""
    return f"""\
set -e
if [ "$(id -u agent)" != "{host_uid}" ] || [ "$(id -g agent)" != "{host_gid}" ]; then
  groupmod -o -g {host_gid} agent
  usermod -o -u {host_uid} agent
  chown -R agent:agent /home/agent/.local /usr/local/rustup /usr/local/cargo 2>/dev/null || true
fi
chown -R {host_uid}:{host_gid} /home/agent/.config /home/agent/.cargo /home/agent/.yarnrc.yml /home/agent/.bunfig.toml
chown {host_uid}:{host_gid} /home/agent /workspace
mkdir -p "$(dirname {SETUP_MARKER})"
"""


def setup_script(project: Project) -> str:
    commands = project.config["sandbox"]["setup"]
    return "set -e\n" + "\n".join(commands) + "\n"


def claude_settings(project: Project) -> dict:
    defaults = {
        "env": {
            "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        },
        "skipDangerousModePermissionPrompt": True,
    }
    return deep_merge(defaults, project.config["harness"]["claude"]["settings"])


def pi_settings(project: Project) -> dict:
    return dict(project.config["harness"]["pi"]["settings"])


# Staged in a fresh directory as guest root, then applied there. See apply_harness.py.
APPLY_HARNESS_COMMAND = """\
set -e
staging="$(mktemp -d)"
trap 'rm -rf "$staging"' EXIT
tar -xf - -C "$staging" --no-same-owner
python3 -I "$staging/apply_harness.py" "$staging"
"""


def _add_file(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = 0o644
    tar.addfile(info, io.BytesIO(data))


def _add_global_overlay(tar: tarfile.TarFile, harness: str) -> None:
    """The global overlay is the user's own, outside anything the guest can write."""
    source = config_home() / "harness" / harness
    if source.is_symlink() or not source.is_dir():
        return
    for directory, subdirs, names in os.walk(source, followlinks=False):
        subdirs[:] = [name for name in subdirs if not (Path(directory) / name).is_symlink()]
        for name in names:
            path = Path(directory) / name
            if stat.S_ISREG(os.lstat(path).st_mode):
                _add_file(tar, f"global/{harness}/{path.relative_to(source).as_posix()}", path.read_bytes())


def harness_tar(project: Project, api_key_placeholder: str | None, uid: int, gid: int) -> bytes:
    """Everything the guest needs to lay down its own harness config."""
    payload = {
        "uid": uid,
        "gid": gid,
        "claude_dir": "/home/agent/.claude",
        "pi_dir": "/home/agent/.pi",
        "project_root": project.guest_root,
        "claude_settings": claude_settings(project),
        "pi_settings": pi_settings(project),
        "trusted_dirs": sorted({project.guest_root, project.workdir}),
        "api_key": api_key_placeholder,
    }
    script = (importlib.resources.files("vankila") / "assets" / "apply_harness.py").read_bytes()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        _add_file(tar, "apply_harness.py", script)
        _add_file(tar, "payload.json", json.dumps(payload).encode())
        _add_global_overlay(tar, "claude")
        _add_global_overlay(tar, "pi")
    return buffer.getvalue()
