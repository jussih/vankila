"""End-to-end proof on real VMs, in a throwaway project, with dummy secrets only."""

from __future__ import annotations

import json
import os
import secrets as pysecrets
import shutil
import tempfile
from pathlib import Path

from . import sandbox, trust
from .config import load_project
from .smol import Smol
from .ui import bold, dim, green, red, say

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


def run() -> int:
    # Under home, not /tmp: it must be a directory smolvm can share with the guest.
    cache = Path.home() / ".cache"
    cache.mkdir(exist_ok=True)
    project_dir = Path(tempfile.mkdtemp(prefix="vk-selftest-", dir=cache))
    token = "vk-dummy-" + pysecrets.token_hex(16)
    env_names = {"VK_TEST_TOKEN": token, "VK_TEST_API_KEY": token, "VK_TEST_HEADER": f"Bearer {token}"}
    saved = {name: os.environ.get(name) for name in env_names}
    os.environ.update(env_names)
    (project_dir / "vankila.toml").write_text(f"""\
[network]
presets = ["npm"]
allow = ["{ECHO_HOST}", "{UNBOUND_ECHO_HOST}"]

[sandbox]
setup = ["touch /tmp/setup-ran"]

[credentials.bearer]
env = "VK_TEST_TOKEN"
hosts = ["{ECHO_HOST}"]

[credentials.apikey]
env = "VK_TEST_API_KEY"
hosts = ["{ECHO_HOST}"]
""")
    project = load_project(project_dir)
    trust.record(project)
    smol = Smol()
    results: list[bool] = []

    def check(label: str, passed: bool, detail: str = "") -> None:
        results.append(passed)
        mark = green("✓") if passed else red("✗")
        say(f"  {mark} {label}" + (f"  {dim(detail[:200])}" if detail and not passed else ""))

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
        sandbox.ensure_running(smol, project, assume_yes=True)
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

        say(bold("Lifecycle"))
        guest("touch /home/agent/persisted")
        smol.stop(project.machine)
        sandbox.ensure_running(smol, project, assume_yes=True)
        check("state survives a restart", guest("test -e /home/agent/persisted")[0] == 0)
        _, after = guest("printenv VK_TEST_TOKEN")
        check("placeholder is stable across restarts", after == placeholder, after)
        settings = project.state_dir / "claude" / "settings.json"
        check("harness config is in place", "DISABLE_AUTOUPDATER" in json.dumps(json.loads(settings.read_text())))
    finally:
        smol.stop(project.machine)
        smol.delete(project.machine)
        shutil.rmtree(project.state_dir, ignore_errors=True)
        shutil.rmtree(project_dir, ignore_errors=True)
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
