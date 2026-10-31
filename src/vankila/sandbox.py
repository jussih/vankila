"""Sandbox lifecycle: make the VM match the config, then run things in it."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import image, seed
from .config import Project, check_mountable
from .secrets import Binding, missing_credentials, resolve_bindings
from .smol import Smol, check_kvm
from .ui import Fail, Spinner, bold, confirm, dim, info, warn

GUEST_PATH = (
    "/home/agent/.local/bin:/usr/local/cargo/bin:/usr/local/sbin:/usr/local/bin:"
    "/usr/sbin:/usr/bin:/sbin:/bin"
)
GUEST_ENV = {
    "PATH": GUEST_PATH,
    "HOME": "/home/agent",
    "LANG": "C.UTF-8",
    "RUSTUP_HOME": "/usr/local/rustup",
    "CARGO_HOME": "/usr/local/cargo",
    "CLAUDE_CONFIG_DIR": "/home/agent/.claude",
    "IS_SANDBOX": "1",
    "DISABLE_AUTOUPDATER": "1",
    "PI_SKIP_VERSION_CHECK": "1",
    "PI_TELEMETRY": "0",
    "VANKILA": "1",
}


def _mounts(project: Project) -> list[str]:
    state = project.state_dir
    mounts = [
        f"{project.root}:{project.guest_root}",
        f"{state / 'claude'}:/home/agent/.claude",
        f"{state / 'pi'}:/home/agent/.pi",
    ]
    for spec in project.config["sandbox"]["mounts"]:
        source, _, rest = spec.partition(":")
        if not rest:
            raise Fail(f"mount {spec!r} needs a guest path: HOST:GUEST[:ro]")
        path = Path(source).expanduser()
        path = path if path.is_absolute() else project.root / path
        path = path.resolve()
        check_mountable(path)
        mounts.append(f"{path}:{rest}")
    return mounts


def desired_state(project: Project, bindings: list[Binding]) -> dict:
    """Everything a machine fixes at creation. A change means a new machine."""
    sandbox = project.config["sandbox"]
    return {
        "image": image.image_id(project.config["tools"]),
        "mounts": _mounts(project),
        "ports": sandbox["ports"],
        "env": sandbox["env"],
        "cpus": sandbox["cpus"],
        "memory": sandbox["memory"],
        "overlay": sandbox["overlay"],
        "storage": sandbox["storage"],
        "ssh_agent": sandbox["ssh_agent"],
        "allow_private": project.config["network"]["allow_private"],
        "credentials": sorted(binding.flag for binding in bindings),
        "setup": sandbox["setup"],
    }


def state_hash(desired: dict) -> str:
    return hashlib.sha256(json.dumps(desired, sort_keys=True).encode()).hexdigest()[:16]


def create_args(project: Project, bindings: list[Binding], desired_hash: str) -> list[str]:
    sandbox = project.config["sandbox"]
    args = [
        "machine", "create",
        "--name", project.machine,
        "--from", str(image.image_path(project.config["tools"])),
        "--cpus", str(sandbox["cpus"]),
        "--mem", str(sandbox["memory"]),
        "--overlay", str(sandbox["overlay"]),
        "--storage", str(sandbox["storage"]),
        "--user", "agent",
        "--workdir", project.guest_root,
        "--net",
        "--block-private",
        "--label", "vankila=1",
        "--label", f"vankila.root={project.root}",
        "--label", f"vankila.state={desired_hash}",
    ]
    for host in project.allowed_hosts():
        args += ["--allow-host-pattern", host]
    for cidr in project.config["network"]["allow_private"]:
        args += ["--allow-private", cidr]
    for mount in _mounts(project):
        args += ["--volume", mount]
    for port in sandbox["ports"]:
        args += ["--port", str(port)]
    for key, value in {**GUEST_ENV, **sandbox["env"]}.items():
        args += ["--env", f"{key}={value}"]
    for binding in bindings:
        args += ["--credential", binding.flag]
    if sandbox["ssh_agent"]:
        args.append("--ssh-agent")
    return args


def _normalize_host(host: str) -> str:
    return host.removeprefix("=")


def _start_env(bindings: list[Binding]) -> dict[str, str]:
    """The only place real secret values leave this process."""
    return {binding.env: binding.value for binding in bindings}


def _start(smol: Smol, project: Project, bindings: list[Binding]) -> None:
    if project.config["sandbox"]["ssh_agent"] and not os.environ.get("SSH_AUTH_SOCK"):
        raise Fail("sandbox.ssh_agent is on but SSH_AUTH_SOCK is not set", "start an ssh-agent")
    smol.run("machine", "start", "--name", project.machine, env=_start_env(bindings))
    root_exec = ("machine", "exec", "--name", project.machine, "--user", "root")
    smol.run(*root_exec, "-i", "--", "tar", "-xf", "-", "-C", "/", "--no-same-owner",
             stdin=seed.guest_tar(project))
    smol.run(*root_exec, "--", "bash", "-c", seed.bootstrap_script(os.getuid(), os.getgid()))
    api_key = None
    if any(binding.env == "ANTHROPIC_API_KEY" for binding in bindings):
        proc = smol.run("machine", "exec", "--name", project.machine, "--user", "agent", "--",
                        "printenv", "ANTHROPIC_API_KEY", check=False)
        api_key = proc.stdout.decode().strip() or None
    seed.write_claude_state(project, api_key)


def _run_setup(smol: Smol, project: Project) -> None:
    if not project.config["sandbox"]["setup"]:
        return
    probe = smol.run("machine", "exec", "--name", project.machine, "--user", "root", "--",
                     "test", "-e", seed.SETUP_MARKER, check=False)
    if probe.returncode == 0:
        return
    info("Running project setup")
    code = subprocess.call([
        smol.binary, "machine", "exec", "--name", project.machine, "--stream", "--user", "agent",
        "--workdir", project.guest_root, "--", "bash", "-c", seed.setup_script(project),
    ])
    if code != 0:
        raise Fail(f"sandbox.setup failed (exit {code})", "fix it, then run `vk rm` and retry")
    smol.run("machine", "exec", "--name", project.machine, "--user", "root", "--",
             "touch", seed.SETUP_MARKER)


def _sync_allowlist(smol: Smol, project: Project, status: dict) -> None:
    """Allow-list edits don't need a new machine. Apply them in place."""
    wanted = project.allowed_hosts()
    current = sorted(_normalize_host(host) for host in status.get("allowed_hosts") or [])
    if current == wanted:
        return
    args = ["machine", "update", "--name", project.machine, "--reset-allow-list"]
    for host in wanted:
        args += ["--allow-host-pattern", host]
    proc = smol.run(*args, check=False)
    if proc.returncode != 0:
        warn(f"Could not update the allow list live. It applies after: {bold('vk stop')}")


def ensure_running(smol: Smol, project: Project, assume_yes: bool = False) -> list[Binding]:
    check_mountable(project.root)
    check_kvm()
    image.build(smol, project)
    tools = project.config["tools"]
    age = image.image_age_days(tools)
    if age >= image.MAX_AGE_DAYS:
        info(dim(f"The image is {age} days old. `vk build --refresh` updates the tools."))

    bindings = resolve_bindings(project)
    for name in missing_credentials(project, bindings):
        warn(f"Credential {name!r} has no value, so it is not bound. Set it: vk auth {name}")
    seed.write_harness_state(project)

    desired = desired_state(project, bindings)
    wanted_hash = state_hash(desired)
    status = smol.status(project.machine)
    if status and status.get("labels", {}).get("vankila.state") != wanted_hash:
        running = status.get("state") == "running"
        question = "Config changed, so the sandbox must be recreated"
        question += " (open sessions will close)." if running else "."
        question += " Your project and harness sessions are kept. Continue?"
        if assume_yes or (sys.stdin.isatty() and confirm(question)):
            smol.stop(project.machine)
            smol.delete(project.machine)
            status = None
        else:
            warn("Keeping the existing sandbox, which doesn't match your config. `vk rm` resets it.")

    fresh = status is None
    if fresh:
        with Spinner("Creating the sandbox"):
            smol.run(*create_args(project, bindings, wanted_hash))
        status = smol.status(project.machine) or {}

    if status.get("state") != "running":
        if not fresh:
            _sync_allowlist(smol, project, status)
        with Spinner("Starting the sandbox"):
            _start(smol, project, bindings)
        _run_setup(smol, project)
    else:
        _sync_allowlist(smol, project, status)
    return bindings


class Session:
    """Keeps the VM up while any session holds the lock, and stops it after the last."""

    def __init__(self, smol: Smol, project: Project):
        self.smol = smol
        self.project = project
        self.started = datetime.now(timezone.utc)
        project.state_dir.mkdir(parents=True, exist_ok=True)
        self.lock = open(project.state_dir / "session.lock", "w")
        self.startup_lock = open(project.state_dir / "startup.lock", "w")
        self.git_before = git_fingerprint(project.root)

    def __enter__(self) -> "Session":
        # Shared for the whole session. It waits only while a last session stops the VM.
        fcntl.flock(self.lock, fcntl.LOCK_SH)
        fcntl.flock(self.startup_lock, fcntl.LOCK_EX)
        return self

    def attach(self) -> None:
        """Startup is done: let the next session start."""
        fcntl.flock(self.startup_lock, fcntl.LOCK_UN)

    def __exit__(self, *exc) -> None:
        self.startup_lock.close()
        report_denials(self.smol, self.project, self.started)
        report_git_changes(self.project.root, self.git_before)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            return
        if not self.project.config["sandbox"]["keep_running"]:
            self.smol.stop(self.project.machine)
        self.lock.close()


def exec_args(project: Project, command: list[str], tty: bool) -> list[str]:
    args = ["machine", "exec", "--name", project.machine, "--user", "agent",
            "--workdir", project.workdir]
    if tty:
        args += ["-i", "-t"]
    for key in ("TERM", "COLORTERM"):
        if os.environ.get(key):
            args += ["--env", f"{key}={os.environ[key]}"]
    args += _git_identity(project.root)
    return [*args, "--", *command]


def _git_identity(root: Path) -> list[str]:
    out = []
    for key, names in (
        ("user.name", ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME")),
        ("user.email", ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL")),
    ):
        proc = subprocess.run(["git", "-C", str(root), "config", "--get", key],
                              capture_output=True, text=True, check=False)
        value = proc.stdout.strip()
        if value:
            for name in names:
                out += ["--env", f"{name}={value}"]
    return out


def run_in_sandbox(smol: Smol, project: Project, command: list[str], assume_yes: bool = False) -> int:
    with Session(smol, project) as session:
        ensure_running(smol, project, assume_yes)
        session.attach()
        tty = sys.stdin.isatty() and sys.stdout.isatty()
        return smol.interactive(*exec_args(project, command, tty))


def report_denials(smol: Smol, project: Project, since: datetime) -> None:
    events = [event for event in smol.denials(project.machine) if _after(event, since)]
    destinations = sorted({str(event.get("dest", "?")) for event in events})
    if not destinations:
        return
    warn(f"Blocked {len(events)} connection(s) to: {', '.join(destinations[:5])}")
    names = [_host_of(dest) for dest in destinations]
    names = [name for name in names if not name[0].isdigit()]
    if names:
        info(dim(f"To allow one: vk allow {names[0]}"))


def _host_of(dest: str) -> str:
    return dest.rsplit(":", 1)[0] if ":" in dest else dest


def _after(event: dict, since: datetime) -> bool:
    try:
        when = datetime.fromisoformat(str(event.get("timestamp", "")).replace("Z", "+00:00"))
    except ValueError:
        return True
    return when >= since


def git_fingerprint(root: Path) -> dict[str, str]:
    """Hooks and git config run on the host later, but the guest can write them."""
    git = root / ".git"
    if not git.is_dir():
        return {}
    prints: dict[str, str] = {}
    paths = [git / "config"]
    hooks = git / "hooks"
    if hooks.is_dir():
        paths += [path for path in hooks.iterdir() if not path.name.endswith(".sample")]
    for path in paths:
        try:
            if path.is_symlink():
                prints[str(path.relative_to(root))] = f"symlink:{os.readlink(path)}"
            elif path.is_file():
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                prints[str(path.relative_to(root))] = digest
        except OSError:
            continue
    return prints


def report_git_changes(root: Path, before: dict[str, str]) -> None:
    after = git_fingerprint(root)
    changed = sorted(key for key in before.keys() | after.keys() if before.get(key) != after.get(key))
    if changed:
        warn(f"The sandbox changed {', '.join(changed)}.")
        info(dim("These run on your host the next time you use git here. Review them first."))
