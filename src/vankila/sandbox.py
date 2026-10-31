"""Sandbox lifecycle: make the VM match the config, then run things in it."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import image, seed
from .config import Project, check_mountable
from .secrets import Binding, missing_credentials, resolve_bindings
from .config import HOST_RE
from .smol import Smol, check_kvm
from .ui import Fail, Spinner, bold, clean, confirm, dim, info, warn

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
        _refuse_symlinks_in_project(project.root, Path(source).expanduser())
        path = Path(source).expanduser()
        path = path if path.is_absolute() else project.root / path
        path = path.resolve()
        check_mountable(path)
        mounts.append(f"{path}:{rest}")
    return mounts


def _refuse_symlinks_in_project(root: Path, source: Path) -> None:
    """The guest can swap anything in the project, including an approved mount's source.

    The approved text wouldn't change, so that would pass the trust check. Mounts
    are checked with the VM stopped, so the guest can't change them afterwards.
    """
    current = Path("/") if source.is_absolute() else root
    for part in source.parts[1:] if source.is_absolute() else source.parts:
        current = current.parent if part == ".." else current / part
        if root in current.parents and current.is_symlink():
            raise Fail(
                f"mount source {current} is a symlink inside the project",
                "the sandbox can replace files there; restore the directory, or mount its target by path",
            )


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


def _vmm_paths(project: Project) -> tuple[list[str], list[str]]:
    """The host paths the VM process needs for this project's mounts."""
    read_write, read_only = [], [str(image.image_dir(project.config["tools"]))]
    for mount in _mounts(project):
        source, _, rest = mount.partition(":")
        (read_only if rest.endswith(":ro") else read_write).append(source)
    if project.config["sandbox"]["ssh_agent"]:
        read_write.append(os.environ.get("SSH_AUTH_SOCK", ""))
    return read_write, read_only


def _start(smol: Smol, project: Project, bindings: list[Binding]) -> None:
    if project.config["sandbox"]["ssh_agent"] and not os.environ.get("SSH_AUTH_SOCK"):
        raise Fail("sandbox.ssh_agent is on but SSH_AUTH_SOCK is not set", "start an ssh-agent")
    read_write, read_only = _vmm_paths(project)
    smol.start(project.machine, _start_env(bindings), read_write, read_only)
    root_exec = ("machine", "exec", "--name", project.machine, "--user", "root")
    smol.run(*root_exec, "-i", "--", "tar", "-xf", "-", "-C", "/", "--no-same-owner",
             stdin=seed.guest_tar(project))
    smol.run(*root_exec, "--", "bash", "-c", seed.bootstrap_script(os.getuid(), os.getgid()))


def _deliver_harness(smol: Smol, project: Project, bindings: list[Binding]) -> None:
    """The guest writes its own harness config, so `vk` never touches that state."""
    api_key = None
    if any(binding.env == "ANTHROPIC_API_KEY" for binding in bindings):
        proc = smol.run("machine", "exec", "--name", project.machine, "--user", "agent", "--",
                        "printenv", "ANTHROPIC_API_KEY", check=False)
        api_key = proc.stdout.decode().strip() or None
    smol.run("machine", "exec", "--name", project.machine, "--user", "root", "-i", "--",
             "bash", "-c", seed.APPLY_HARNESS_COMMAND,
             stdin=seed.harness_tar(project, api_key, os.getuid(), os.getgid()))


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


def _desired_path(project: Project) -> Path:
    # In the state directory but not in a mount, so the guest can't reach it.
    return project.state_dir / "desired.json"


def _save_desired(project: Project, desired: dict) -> None:
    fd = os.open(_desired_path(project), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(desired, handle)


def _show_state_changes(project: Project, desired: dict) -> None:
    """Say what changed. A new mount or credential shouldn't hide behind a generic prompt."""
    try:
        before = json.loads(_desired_path(project).read_text())
    except (OSError, ValueError):
        return
    for key in sorted(before.keys() | desired.keys()):
        if before.get(key) != desired.get(key):
            old, new = json.dumps(before.get(key)), json.dumps(desired.get(key))
            info(f"{key}: {clean(old)} -> {clean(new)}")


def _report_github_binding(project: Project, bindings: list[Binding], fresh: bool) -> None:
    for binding in bindings:
        if binding.name == "github":
            info(dim(f"The sandbox can act on GitHub with the token from {binding.source}."))
            return
    if fresh and "github" in project.config["network"]["presets"]:
        info(dim("No GitHub token is bound. To give the sandbox one: vk auth github"))


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
    # Only the mount roots. Everything inside them belongs to the guest.
    for directory in (project.state_dir / "claude", project.state_dir / "pi"):
        directory.mkdir(parents=True, exist_ok=True)

    desired = desired_state(project, bindings)
    wanted_hash = state_hash(desired)
    status = smol.status(project.machine)
    if status and status.get("labels", {}).get("vankila.state") != wanted_hash:
        running = status.get("state") == "running"
        _show_state_changes(project, desired)
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
        _save_desired(project, desired)

    if status.get("state") != "running":
        if not fresh:
            _sync_allowlist(smol, project, status)
        with Spinner("Starting the sandbox"):
            _start(smol, project, bindings)
        started = True
        _report_github_binding(project, bindings, fresh)
    else:
        _sync_allowlist(smol, project, status)
        started = False
    _deliver_harness(smol, project, bindings)
    if started:
        _run_setup(smol, project)
    return bindings


LOW_DISK_BYTES = 2 * 1024 ** 3
SNAPSHOTS_KEPT = 3


class Session:
    """Keeps the VM up while any session holds the lock, and stops it after the last."""

    def __init__(self, smol: Smol, project: Project):
        self.smol = smol
        self.project = project
        self.started = datetime.now(timezone.utc)
        project.state_dir.mkdir(parents=True, exist_ok=True)
        self.lock = open(project.state_dir / "session.lock", "w")
        self.startup_lock = open(project.state_dir / "startup.lock", "w")
        self.git_before: dict[str, str] = {}

    def __enter__(self) -> "Session":
        # Shared for the whole session. It waits only while a last session stops the VM.
        fcntl.flock(self.lock, fcntl.LOCK_SH)
        fcntl.flock(self.startup_lock, fcntl.LOCK_EX)
        running = self.smol.is_running(self.project.machine)
        self.git_before = git_fingerprint(self.project.root)
        if running:
            # The guest may have been running unwatched, as with keep_running.
            saved = _load_git_baseline(self.project)
            if saved is not None:
                report_git_changes(self.project.root, saved)
        elif self.project.config["sandbox"]["snapshot"]:
            snapshot_project(self.project)
        return self

    def attach(self) -> None:
        """Startup is done: let the next session start."""
        fcntl.flock(self.startup_lock, fcntl.LOCK_UN)

    def __exit__(self, *exc) -> None:
        self.startup_lock.close()
        report_denials(self.smol, self.project, self.started)
        report_git_changes(self.project.root, self.git_before)
        _save_git_baseline(self.project, git_fingerprint(self.project.root))
        report_low_disk(self.project.root)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            return
        if not self.project.config["sandbox"]["keep_running"]:
            self.smol.stop(self.project.machine)
        self.lock.close()


def snapshot_project(project: Project) -> None:
    """Copy the project with the VM stopped, into state the guest can't reach."""
    snapshots = project.state_dir / "snapshots"
    snapshots.mkdir(exist_ok=True)
    name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    staging = snapshots / f".{name}.tmp"
    # -a copies symlinks as symlinks, so a planted one can't pull host files in.
    proc = subprocess.run(["cp", "-a", "--reflink=auto", "--", str(project.root), str(staging)],
                          capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        shutil.rmtree(staging, ignore_errors=True)
        warn(f"Could not snapshot the project: {clean(proc.stderr.strip())}")
        return
    staging.rename(snapshots / name)
    info(dim(f"Snapshot of the project: {snapshots / name}"))
    for old in sorted(path for path in snapshots.iterdir() if not path.name.startswith("."))[:-SNAPSHOTS_KEPT]:
        shutil.rmtree(old, ignore_errors=True)


def report_low_disk(root: Path) -> None:
    """Mounts have no quota, so the guest can fill the host's disk."""
    try:
        free = shutil.disk_usage(root).free
    except OSError:
        return
    if free < LOW_DISK_BYTES:
        warn(f"Only {free / 1024 ** 3:.1f} GiB left on the disk holding {root}.")


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
    # Destinations come from guest traffic, so they aren't safe to print raw.
    warn(f"Blocked {len(events)} connection(s) to: {clean(', '.join(destinations[:5]))}")
    names = [_host_of(dest) for dest in destinations]
    names = [name for name in names if HOST_RE.match(name) and not name[0].isdigit()]
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


MAX_FINGERPRINT_BYTES = 64 * 1024 * 1024


def _fingerprint_file(path: Path) -> str | None:
    """A digest of a guest-writable file: never follows a link or blocks on a FIFO."""
    try:
        mode = os.lstat(path).st_mode
        if stat.S_ISLNK(mode):
            return f"symlink:{os.readlink(path)}"
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            return "not-a-file"
        try:
            return hashlib.sha256(handle.read(MAX_FINGERPRINT_BYTES)).hexdigest()
        except OSError:
            return None


def _git_dirs(root: Path, prints: dict[str, str]) -> list[Path]:
    """The git directories whose hooks and config run on the host for this project."""
    git = root / ".git"
    try:
        mode = os.lstat(git).st_mode
    except OSError:
        return []
    if stat.S_ISLNK(mode) or stat.S_ISREG(mode):
        # A worktree or submodule: this file says where its git directory is.
        print_ = _fingerprint_file(git)
        if print_:
            prints[".git"] = print_
    if stat.S_ISLNK(mode):
        return []
    if stat.S_ISDIR(mode):
        return [git]
    return _follow_gitfile(root, git)


def _read_small(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            return ""
        return handle.read(4096).decode(errors="replace").strip()


def _follow_gitfile(root: Path, git: Path) -> list[Path]:
    try:
        text = _read_small(git)
    except OSError:
        return []
    if not text.startswith("gitdir:"):
        return []
    gitdir = (root / text.removeprefix("gitdir:").strip())
    dirs = [gitdir]
    try:
        common = _read_small(gitdir / "commondir")
    except OSError:
        common = ""
    if common:
        dirs.append(gitdir / common)
    return dirs


def _submodule_dirs(gitdir: Path) -> list[Path]:
    found = []
    for directory, subdirs, names in os.walk(gitdir / "modules", followlinks=False):
        if "HEAD" in names:
            found.append(Path(directory))
        subdirs[:] = [name for name in subdirs if name not in ("objects", "refs", "logs", "hooks", "info")]
    return found


def git_fingerprint(root: Path) -> dict[str, str]:
    """Hooks and git config run on the host later, but the guest can write them."""
    prints: dict[str, str] = {}
    dirs = _git_dirs(root, prints)
    for gitdir in list(dirs):
        dirs += _submodule_dirs(gitdir)
    for gitdir in dirs:
        paths = [gitdir / "config", gitdir / "config.worktree"]
        hooks = gitdir / "hooks"
        if hooks.is_symlink():
            paths.append(hooks)
        elif hooks.is_dir():
            try:
                paths += [path for path in hooks.iterdir() if not path.name.endswith(".sample")]
            except OSError:
                pass
        for path in paths:
            print_ = _fingerprint_file(path)
            if print_:
                prints[str(path.relative_to(root)) if root in path.parents else str(path)] = print_
    return prints


def _git_baseline_path(project: Project) -> Path:
    return project.state_dir / "git-baseline.json"


def _load_git_baseline(project: Project) -> dict[str, str] | None:
    try:
        data = json.loads(_git_baseline_path(project).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _save_git_baseline(project: Project, prints: dict[str, str]) -> None:
    _git_baseline_path(project).write_text(json.dumps(prints))


def report_git_changes(root: Path, before: dict[str, str]) -> None:
    after = git_fingerprint(root)
    changed = sorted(key for key in before.keys() | after.keys() if before.get(key) != after.get(key))
    if changed:
        warn(f"The sandbox changed {clean(', '.join(changed))}.")
        info(dim("These run on your host the next time you use git here. Review them first."))
