"""Renders what goes inside a sandbox: registry policy and harness config."""

from __future__ import annotations

import io
import json
import os
import stat
import tarfile
from pathlib import Path

from .config import Project, config_home, deep_merge
from .ui import Fail

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


def _safe_parent(root: Path, rel: Path) -> Path:
    """Create `rel`'s parents under `root`, refusing to walk through a symlink.

    Harness directories are guest-writable. A guest could leave a symlink there
    pointing at a host file, and this code runs on the host.
    """
    current = root
    for part in rel.parts[:-1]:
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            current.mkdir()
            continue
        if not stat.S_ISDIR(mode):
            raise Fail(
                f"unsafe path in sandbox state: {current} is not a plain directory",
                "something in the sandbox replaced it; inspect it, or run `vk rm --state`",
            )
    return current


def safe_write(root: Path, rel: str | Path, data: bytes) -> None:
    rel = Path(rel)
    if rel.is_absolute() or ".." in rel.parts:
        raise Fail(f"unsafe harness path: {rel}")
    parent = _safe_parent(root, rel)
    tmp = parent / f".vk-tmp-{os.getpid()}-{rel.name}"
    # O_EXCL refuses a planted symlink at the temp name; rename replaces, never follows.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    os.replace(tmp, parent / rel.name)


def safe_read_json(root: Path, rel: str) -> dict:
    """Read guest-writable JSON. Anything suspicious counts as empty."""
    try:
        fd = os.open(root / rel, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return {}
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            return {}
        try:
            data = json.loads(handle.read(16 * 1024 * 1024))
        except ValueError:
            return {}
    return data if isinstance(data, dict) else {}


def _overlay_dir(source: Path, target: Path) -> None:
    """Copy a host-side harness overlay (CLAUDE.md, skills, agents) into state."""
    # Project overlays are guest-writable. Following a symlink here would copy
    # host files such as ~/.ssh/id_ed25519 into the guest.
    if source.is_symlink() or not source.is_dir():
        return
    for directory, subdirs, names in os.walk(source, followlinks=False):
        subdirs[:] = [name for name in subdirs if not (Path(directory) / name).is_symlink()]
        for name in names:
            path = Path(directory) / name
            if not stat.S_ISREG(os.lstat(path).st_mode):
                continue
            safe_write(target, path.relative_to(source), path.read_bytes())


def _overlay_project(project: Project, harness: str, target: Path) -> None:
    current = project.root
    for part in (".vankila", "harness"):
        current = current / part
        if current.is_symlink():
            return
    _overlay_dir(current / harness, target)


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


def write_harness_state(project: Project) -> None:
    """Lay down harness config. Global overlays first, project overlays win."""
    state = project.state_dir
    claude, pi = state / "claude", state / "pi"
    for directory in (claude, pi / "agent"):
        directory.mkdir(parents=True, exist_ok=True)
    _overlay_dir(config_home() / "harness" / "claude", claude)
    _overlay_project(project, "claude", claude)
    _overlay_dir(config_home() / "harness" / "pi", pi / "agent")
    _overlay_project(project, "pi", pi / "agent")

    settings = deep_merge(safe_read_json(claude, "settings.json"), claude_settings(project))
    safe_write(claude, "settings.json", json.dumps(settings, indent=2).encode() + b"\n")

    wanted = pi_settings(project)
    if wanted:
        merged = deep_merge(safe_read_json(pi / "agent", "settings.json"), wanted)
        safe_write(pi / "agent", "settings.json", json.dumps(merged, indent=2).encode() + b"\n")


def write_claude_state(project: Project, api_key_placeholder: str | None) -> None:
    """Skip Claude Code's first-run screens: the login, trust and API key prompts."""
    claude = project.state_dir / "claude"
    state = safe_read_json(claude, ".claude.json")
    state["hasCompletedOnboarding"] = True
    state["bypassPermissionsModeAccepted"] = True
    projects = state.get("projects")
    projects = projects if isinstance(projects, dict) else {}
    for guest_dir in {project.guest_root, project.workdir}:
        entry = projects.get(guest_dir)
        entry = entry if isinstance(entry, dict) else {}
        entry["hasTrustDialogAccepted"] = True
        projects[guest_dir] = entry
    state["projects"] = projects
    if api_key_placeholder:
        responses = state.get("customApiKeyResponses")
        responses = responses if isinstance(responses, dict) else {}
        approved = responses.get("approved")
        approved = approved if isinstance(approved, list) else []
        suffix = api_key_placeholder[-20:]
        if suffix not in approved:
            approved.append(suffix)
        state["customApiKeyResponses"] = {"approved": approved, "rejected": []}
    safe_write(claude, ".claude.json", json.dumps(state, indent=2).encode() + b"\n")
