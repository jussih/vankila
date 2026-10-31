"""End-to-end proof on real VMs, in a throwaway project, with dummy secrets only."""

from __future__ import annotations

import os
import secrets as pysecrets
import shutil
import socket
import tempfile
from pathlib import Path

from . import confine, image, sandbox, trust
from .config import load_project
from .smol import Smol
from .ui import Fail, bold, dim, green, red, say, yellow

ECHO_HOST = "httpbingo.org"
UNBOUND_ECHO_HOST = "postman-echo.com"
TOOLS = {
    "node": "node --version",
    "npm": "npm --version",
    "uv": "uv --version",
    "rustc": "rustc --version",
    "cargo": "cargo --version",
    "jq": "jq --version",
    "gh": "gh --version",
    "git": "git --version",
    "claude": "claude --version",
    "pi": "pi --version",
}


# Sends one DNS query for example.com to a public resolver, and prints the answer count.
DNS_QUERY = (
    "import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.settimeout(5);"
    r"s.sendto(b'\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x07example\x03com\x00\x00\x01\x00\x01',"
    "('1.1.1.1',53));r=s.recv(512);print(r[6]*256+r[7])"
)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _listening_addresses(port: int) -> set[str]:
    """Local addresses with a listener on this TCP port, from /proc/net (hex, as the kernel prints)."""
    found = set()
    for name in ("tcp", "tcp6"):
        for line in Path(f"/proc/net/{name}").read_text().splitlines()[1:]:
            fields = line.split()
            address, _, hex_port = fields[1].partition(":")
            if fields[3] == "0A" and int(hex_port, 16) == port:
                found.add(address)
    return found


LOOPBACK_ADDRESSES = {"0100007F", "00000000000000000000000001000000"}


def run() -> int:
    # Under home, not /tmp: it must be a directory smolvm can share with the guest.
    cache = Path.home() / ".cache"
    cache.mkdir(exist_ok=True)
    project_dir = Path(tempfile.mkdtemp(prefix="vk-selftest-", dir=cache))
    canary = Path(tempfile.mkdtemp(prefix="vk-selftest-canary-", dir=cache))
    published_port = _free_port()
    token = "vk-dummy-" + pysecrets.token_hex(16)
    env_names = {"VK_TEST_TOKEN": token, "VK_TEST_API_KEY": token, "VK_TEST_HEADER": f"Bearer {token}"}
    saved = {name: os.environ.get(name) for name in env_names}
    os.environ.update(env_names)
    config_text = f"""\
[network]
presets = ["npm"]
allow = ["{ECHO_HOST}", "{UNBOUND_ECHO_HOST}"]

[sandbox]
setup = ["touch /tmp/setup-ran"]
ports = ["{published_port}:8000"]

[harness.pi]
settings = {{ defaultProvider = "selftest" }}

[credentials.bearer]
env = "VK_TEST_TOKEN"
hosts = ["{ECHO_HOST}"]

[credentials.apikey]
env = "VK_TEST_API_KEY"
hosts = ["{ECHO_HOST}"]
"""
    (project_dir / "vankila.toml").write_text(config_text)
    project = load_project(project_dir)
    trust.record(project, config_text)
    smol = Smol()
    results: list[bool] = []

    def check(label: str, passed: bool, detail: str = "") -> None:
        results.append(passed)
        mark = green("✓") if passed else red("✗")
        say(f"  {mark} {label}" + (f"  {dim(detail[:200])}" if detail and not passed else ""))

    def known(label: str, leaks: bool) -> None:
        """A documented weakness. It isn't a failure, but a change in it should be noticed."""
        if leaks:
            say(f"  {yellow('!')} known weakness: {label}")
        else:
            say(f"  {green('✓')} no longer happens, so the README is out of date: {label}")

    def guest(script: str, stdin: bytes | None = None) -> tuple[int, str]:
        args = ["machine", "exec", "--name", project.machine, "--user", "agent",
                "--workdir", project.guest_root]
        if stdin is not None:
            args.append("-i")
        proc = smol.run(*args, "--", "bash", "-c", script, stdin=stdin, check=False)
        return proc.returncode, (proc.stdout + proc.stderr).decode(errors="replace").strip()

    def curl_headers(*headers: str) -> str:
        parts = " ".join(f'-H "{header}"' for header in headers)
        return guest(f"curl -sS -m 20 {parts} https://{ECHO_HOST}/headers")[1]

    try:
        say(bold("Boot"))
        bindings = sandbox.ensure_running(smol, project, assume_yes=True)
        status = smol.status(project.machine) or {}
        check("sandbox is running", status.get("state") == "running", str(status.get("state")))

        say(bold("Tools"))
        for tool, command in TOOLS.items():
            code, out = guest(command)
            check(tool, code == 0, out)

        say(bold("Secrets stay on the host"))
        _, placeholder = guest("printenv VK_TEST_TOKEN")
        check("guest env holds a placeholder", placeholder.startswith("SMOL_PLACEHOLDER_"), placeholder)
        # The token travels on stdin: `exec` arguments are logged on the host.
        code, out = guest("sudo grep -qaFf /dev/stdin /proc/[0-9]*/environ; echo $?", token.encode())
        check("token is in no guest process environment", out.endswith("1"), out)
        code, out = guest("sudo grep -rqaFf /dev/stdin /etc /home/agent /run 2>/dev/null; echo $?", token.encode())
        check("token is in no guest file", out.endswith("1"), out)
        echoed = curl_headers('Authorization: Bearer $VK_TEST_TOKEN')
        check("Bearer placeholder is substituted at the boundary", token in echoed, echoed)
        echoed = curl_headers('x-api-key: $VK_TEST_API_KEY')
        check("x-api-key placeholder is substituted at the boundary", token in echoed, echoed)
        _, out = guest(f'curl -sS -m 20 -H "x-api-key: $VK_TEST_TOKEN" https://{UNBOUND_ECHO_HOST}/headers')
        check("an allowed but unbound host never gets the real value",
              token not in out and placeholder in out, out)
        cert = guest(f"node -e \"fetch('https://{ECHO_HOST}/get').then(r=>console.log(r.status))\"")[1]
        check("node trusts the boundary CA", cert.endswith("200"), cert)

        say(bold("Egress"))
        code, out = guest("curl -sS -m 10 https://registry.npmjs.org/left-pad -o /dev/null -w '%{http_code}'")
        check("allow-listed host works", out == "200", out)
        code, out = guest("curl -sS -m 10 https://example.com -o /dev/null")
        check("other hosts are blocked", code != 0, out)
        code, out = guest("getent hosts example.com")
        check("other names don't resolve", code != 0, out)
        code, out = guest("curl -sS -m 5 http://169.254.169.254/ -o /dev/null")
        check("cloud metadata is blocked", code != 0, out)
        code, out = guest("curl -sS -m 5 http://192.168.1.1/ -o /dev/null")
        check("the LAN is blocked", code != 0, out)
        check("denials are recorded", bool(smol.denials(project.machine)))

        say(bold("Hardened registries"))
        check("npm cooldown", guest("npm config get min-release-age")[1] == "14")
        check("npm scripts off", guest("npm config get ignore-scripts")[1] == "true")
        check("uv cooldown", "exclude-newer" in guest("cat /etc/uv/uv.toml")[1])
        check("pip cooldown", "P14D" in guest("cat /etc/pip.conf")[1])

        say(bold("Project mount"))
        guest("echo hello-from-guest > written-by-guest")
        written = project_dir / "written-by-guest"
        check("guest writes reach the host", written.exists() and "hello" in written.read_text())
        check("files are owned by you", written.exists() and written.stat().st_uid == os.getuid())
        code, out = guest("git init -q . && git status --short | head -1")
        check("git works in the mount", code == 0, out)
        check("project setup ran", guest("test -e /tmp/setup-ran")[0] == 0)

        say(bold("Escapes"))
        guest("mkdir -p .vankila/harness/pi && echo planted > .vankila/harness/pi/.bashrc")
        guest(f"rm -rf ~/.pi/agent && ln -s {canary} ~/.pi/agent")
        planted = (project.state_dir / "pi" / "agent").is_symlink()
        check("a guest can plant a link in harness state", planted)
        sandbox.ensure_running(smol, project, assume_yes=True)
        check("vk doesn't follow a planted link onto the host", not list(canary.iterdir()),
              str(list(canary.iterdir())))
        check("the link resolves inside the guest", guest("test -e ~/.pi/agent/.bashrc")[0] == 0)
        guest(f"rm -f ~/.pi/agent && rm -rf {canary}")
        code, out = guest("sudo mknod selftest-device b 8 0")
        check("guest root can't reach host devices through the mount", code != 0, out)
        guest("sudo install -m 4755 /bin/true selftest-setuid")
        setuid = project_dir / "selftest-setuid"
        check("a guest setuid file grants nothing on the host",
              not setuid.exists() or setuid.stat().st_uid == os.getuid())
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.setblocking(False)
            port = listener.getsockname()[1]
            guest(f"curl -sS -m 5 http://100.96.0.1:{port}/ -o /dev/null")
            try:
                listener.accept()
                reached = True
            except BlockingIOError:
                reached = False
        check("the host's loopback is out of reach", not reached)
        code, out = guest("curl -sS -m 5 https://1.1.1.1/ -o /dev/null")
        check("an unlisted IP is blocked", code != 0, out)
        code, out = guest("curl -sS -m 5 -6 'https://[2606:4700:4700::1111]/' -o /dev/null")
        check("an unlisted IPv6 address is blocked", code != 0, out)
        # smolvm answers port 53 itself, so a reply isn't a leak. An answer for this name would be.
        code, out = guest(f"python3 -c \"{DNS_QUERY}\"")
        check("a public resolver gives no answer for an unlisted name", code != 0 or out == "0", out)
        addresses = _listening_addresses(published_port)
        check("published ports bind only to loopback", bool(addresses) and addresses <= LOOPBACK_ADDRESSES,
              str(addresses))

        say(bold("Known egress weaknesses"))
        code, out = guest(
            "ip=$(getent hosts registry.npmjs.org | awk '{print $1; exit}'); "
            "curl -sS -m 10 -o /dev/null -w '%{http_code}' --resolve example.com:443:$ip https://example.com/"
        )
        known("an unlisted site is reachable through an allowed host's CDN address", out == "200")
        code, _ = guest("timeout 5 bash -c 'exec 3<>/dev/tcp/registry.npmjs.org/8443'")
        known("any port on an allowed host's address connects", code == 0)

        say(bold("Lifecycle"))
        guest("touch /home/agent/persisted")
        smol.stop(project.machine)
        sandbox.ensure_running(smol, project, assume_yes=True)
        check("state survives a restart", guest("test -e /home/agent/persisted")[0] == 0)
        _, after = guest("printenv VK_TEST_TOKEN")
        check("placeholder is stable across restarts", after == placeholder, after)
        check("harness config is in place", "DISABLE_AUTOUPDATER" in guest("cat /home/agent/.claude/settings.json")[1])

        say(bold("VM process confinement"))
        if confine.abi_version() < 1:
            say(f"  {yellow('!')} this kernel doesn't have Landlock enabled, so the VM process isn't confined")
        else:
            # The real proof it applies to the VM process, not just to `machine start`:
            # withhold the project from the rules, and the guest must lose it.
            smol.stop(project.machine)
            state = [str(project.state_dir / "claude"), str(project.state_dir / "pi")]
            marker = project_dir / "confinement-marker"
            try:
                smol.start(project.machine, sandbox._start_env(bindings), state,
                           [str(image.image_dir(project.config["tools"]))])
                code, out = guest("echo reached > confinement-marker")
            except Fail as err:
                code, out = 1, str(err)
            check("the VM process can't reach a path it wasn't granted", code != 0 and not marker.exists(), out)
    finally:
        smol.stop(project.machine)
        smol.delete(project.machine)
        shutil.rmtree(project.state_dir, ignore_errors=True)
        trust.forget(project)
        shutil.rmtree(project_dir, ignore_errors=True)
        shutil.rmtree(canary, ignore_errors=True)
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    failed = results.count(False)
    say()
    if failed:
        say(red(f"{failed} of {len(results)} checks failed"))
        return 1
    say(green(f"All {len(results)} checks passed"))
    return 0
