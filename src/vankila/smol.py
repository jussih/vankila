"""A thin wrapper over the smolvm CLI, and installing it on first use."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from . import confine
from .config import data_home
from .ui import Fail, Spinner, confirm, dim, ok, say, warn

SMOLVM_VERSION = "1.32.4"
RELEASES = "https://github.com/smol-machines/smolvm/releases/download"
# Pinned here rather than read from the release: anyone able to replace a release
# asset can replace its checksum file too. Updating smolvm means reviewing this.
SMOLVM_SHA256 = {
    "x86_64": "7b0580c2ef98e183ac5c3a1567eca3a336d58aa0e2ca5904350f7d3a851205e8",
    "arm64": "693b34530d9eac9d6d1b61e22b33bb34c87c511b1a8c9098fe35d9c4a77e07ce",
}


# What smolvm needs read-only, beyond the mounts. Deliberately not home.
VMM_READ_ONLY = ("/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc", "/proc", "/sys")
VMM_READ_WRITE = ("/dev/kvm", "/dev/net/tun", "/dev/null", "/dev/zero", "/dev/urandom", "/dev/random",
                  "/dev/ptmx", "/dev/pts")
NO_CONFINE_ENV = "VANKILA_NO_CONFINE"

INSTALL_HINT = "put smolvm on your PATH, or set VANKILA_SMOLVM: https://smolmachines.com/docs/local"


def _managed_binary() -> Path:
    return data_home() / "smolvm" / f"smolvm-{SMOLVM_VERSION}-linux-{_arch()}" / "smolvm"


def _arch() -> str:
    machine = platform.machine()
    return {"x86_64": "x86_64", "aarch64": "arm64", "arm64": "arm64"}.get(machine, machine)


def _install_marker() -> Path:
    return _managed_binary().parent.parent / f".installed-{SMOLVM_VERSION}-{_arch()}"


def find_binary() -> str | None:
    override = os.environ.get("VANKILA_SMOLVM")
    if override:
        return override
    # A copy you installed yourself wins over one vankila downloaded.
    on_path = shutil.which("smolvm")
    if on_path:
        return on_path
    # The marker is written last, so an interrupted install doesn't count.
    return str(_managed_binary()) if _install_marker().exists() else None


def _asset_url() -> str:
    return f"{RELEASES}/v{SMOLVM_VERSION}/smolvm-{SMOLVM_VERSION}-linux-{_arch()}.tar.gz"


def install() -> str:
    target = data_home() / "smolvm"
    url = _asset_url()
    asset = url.rsplit("/", 1)[1]
    final = _managed_binary().parent
    target.mkdir(parents=True, exist_ok=True)
    with Spinner(f"Downloading smolvm {SMOLVM_VERSION}"), tempfile.TemporaryDirectory(dir=target) as tmp:
        archive = Path(tmp) / asset
        try:
            urllib.request.urlretrieve(url, archive)
        except OSError as err:
            raise Fail(f"could not download smolvm: {err}") from err
        actual = hashlib.sha256(archive.read_bytes()).hexdigest()
        if actual != SMOLVM_SHA256[_arch()]:
            raise Fail(
                f"{asset} doesn't match the hash pinned in vankila: refusing to install it",
                f"expected {SMOLVM_SHA256[_arch()]}, got {actual}",
            )
        # System tar, not tarfile: the guest rootfs in here has absolute symlinks,
        # which tarfile's safe filter rejects and which must stay as they are.
        staging = Path(tmp) / "unpacked"
        staging.mkdir()
        proc = subprocess.run(["tar", "-xzf", str(archive), "-C", str(staging)],
                              capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise Fail(f"could not unpack smolvm: {proc.stderr.strip()}")
        shutil.rmtree(final, ignore_errors=True)
        (staging / final.name).rename(final)
    _install_marker().touch()
    ok(f"Installed smolvm {SMOLVM_VERSION} to {target}")
    return str(_managed_binary())


def require_binary() -> str:
    binary = find_binary()
    if binary:
        return binary
    if platform.system() != "Linux":
        raise Fail(
            "smolvm is not installed",
            INSTALL_HINT,
        )
    if _arch() not in SMOLVM_SHA256:
        raise Fail(f"no pinned smolvm build for {_arch()}", INSTALL_HINT)
    if not sys.stdin.isatty():
        raise Fail("smolvm is not installed, and vankila won't download it unattended", INSTALL_HINT)
    say("smolvm is not installed. vankila can download it, and it will run unsandboxed on this host:")
    say(f"  {dim('from')}    {_asset_url()}")
    say(f"  {dim('sha256')}  {SMOLVM_SHA256[_arch()]}")
    say(f"  {dim('to')}      {data_home() / 'smolvm'}")
    if not confirm("Download and install it?", default=False):
        raise Fail("vankila needs smolvm", INSTALL_HINT)
    return install()


def check_kvm() -> None:
    if platform.system() != "Linux":
        return
    kvm = Path("/dev/kvm")
    if not kvm.exists():
        raise Fail(
            "/dev/kvm is missing: this host cannot run VMs",
            "enable virtualisation in firmware or load the kvm module",
        )
    if not os.access(kvm, os.R_OK | os.W_OK):
        raise Fail("no access to /dev/kvm", "add yourself to the group: sudo usermod -aG kvm $USER")


class Smol:
    def __init__(self, binary: str | None = None):
        self.binary = binary or require_binary()

    def run(
        self,
        *args: str,
        env: dict[str, str] | None = None,
        stdin: bytes | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        full_env = {**os.environ, **(env or {})}
        proc = subprocess.run(
            [self.binary, *args], env=full_env, input=stdin, capture_output=True, check=False
        )
        if check and proc.returncode != 0:
            message = proc.stderr.decode(errors="replace").strip() or "no error output"
            raise Fail(f"smolvm {' '.join(args[:2])} failed: {message}")
        return proc

    def start(self, name: str, env: dict[str, str], read_write: list[str], read_only: list[str]) -> None:
        """Start the VM with its process limited to the given host paths, where the kernel allows."""
        args = ("machine", "start", "--name", name)
        if os.environ.get(NO_CONFINE_ENV):
            warn(f"The VM process is not confined: {NO_CONFINE_ENV} is set.")
            self.run(*args, env=env)
            return
        if confine.abi_version() < 1:
            warn("The VM process is not confined: this kernel doesn't have Landlock enabled.")
            self.run(*args, env=env)
            return
        cache_home = Path(os.environ.get("XDG_CACHE_HOME") or "~/.cache").expanduser()
        spec = {
            "read_only": [*VMM_READ_ONLY, str(Path(self.binary).resolve().parent), *read_only],
            "read_write": [*VMM_READ_WRITE, str(data_home()), str(data_home().parent / "smolvm"),
                           str(cache_home / "smolvm"), *read_write],
        }
        proc = subprocess.run(
            confine.wrapper_command(spec, [self.binary, *args]),
            env={**os.environ, **env}, capture_output=True, check=False,
        )
        if proc.returncode != 0:
            message = proc.stderr.decode(errors="replace").strip() or "no error output"
            raise Fail(f"smolvm machine start failed: {message}",
                       f"if the confinement is the cause, {NO_CONFINE_ENV}=1 turns it off")

    def status(self, name: str) -> dict | None:
        proc = self.run("machine", "status", "--name", name, "--json", check=False)
        if proc.returncode != 0:
            return None
        return json.loads(proc.stdout)

    def disk_usage(self, name: str) -> tuple[int, int] | None:
        """(allocated, apparent) bytes of the machine's host-side data. Disks are sparse."""
        proc = self.run("machine", "data-dir", "--name", name, check=False)
        path = Path(proc.stdout.decode().strip())
        if proc.returncode != 0 or not path.is_dir():
            return None
        allocated = apparent = 0
        for root, _dirs, files in os.walk(path):
            for entry in files:
                try:
                    st = os.lstat(os.path.join(root, entry))
                except OSError:
                    continue
                allocated += st.st_blocks * 512
                apparent += st.st_size
        return allocated, apparent

    def memory_used(self, name: str, pid: int | None) -> int | None:
        """Bytes of host memory the running machine holds, or None if it can't be read."""
        proc = self.run("machine", "stats", "--name", name, "--json", check=False)
        if proc.returncode == 0:
            try:
                stats = json.loads(proc.stdout)
            except ValueError:
                stats = {}
            for key, value in stats.items() if isinstance(stats, dict) else []:
                if "mem" in key and "used" in key and isinstance(value, (int, float)):
                    return int(value) * (1 << 20 if key.endswith("mib") else 1)
        if pid:
            try:
                for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) * 1024
            except (OSError, ValueError):
                pass
        return None

    def is_running(self, name: str) -> bool:
        status = self.status(name)
        return bool(status) and status.get("state") == "running"

    def machines(self) -> list[dict]:
        proc = self.run("machine", "ls", "--json")
        data = json.loads(proc.stdout or b"[]")
        return data.get("machines", []) if isinstance(data, dict) else data

    def stop(self, name: str) -> None:
        self.run("machine", "stop", "--name", name, check=False)

    def delete(self, name: str) -> None:
        self.run("machine", "delete", "--name", name, "--force", check=False)

    def denials(self, name: str) -> list[dict]:
        proc = self.run("machine", "egress-events", "--name", name, "--json", check=False)
        if proc.returncode != 0 or not proc.stdout.strip():
            return []
        data = json.loads(proc.stdout)
        return data.get("events", []) if isinstance(data, dict) else data

    def interactive(self, *args: str) -> int:
        return subprocess.call([self.binary, *args])
