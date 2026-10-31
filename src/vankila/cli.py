"""The `vk` command. Anything that isn't a vk command runs inside the sandbox."""

from __future__ import annotations

import argparse
import getpass
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__, image, sandbox, trust
from .config import (
    PROJECT_FILE,
    Project,
    add_allowed_host,
    load_project,
    project_from_text,
    render_template,
    write_project_file,
)
from .secrets import BUILTIN, delete_secret, resolve_bindings, save_secret
from .smol import SMOLVM_VERSION, Smol, check_kvm, find_binary
from .ui import Fail, bold, clean, dim, green, info, ok, red, say, warn, yellow

HELP = f"""\
{bold("vk")} runs coding agents in micro-VMs, with your project mounted, egress filtered by an
allow list, and secrets that never enter the VM.

{bold("Run things")}
  vk claude [args]       Claude Code in this project's sandbox
  vk pi [args]           pi in this project's sandbox
  vk shell               a shell in the sandbox
  vk <anything>          any command in the sandbox, e.g. `vk cargo test`

{bold("Set up")}
  vk auth                show credentials; `vk auth claude` stores a token
  vk init                write a vankila.toml for this project
  vk trust               review and approve a changed vankila.toml
  vk doctor              check that everything works
  vk selftest            prove the isolation on real VMs, with dummy secrets

{bold("Network")}
  vk status              this sandbox's state, allow list and credentials
  vk allow <host>        allow a host for this project
  vk blocked             what the sandbox tried to reach and couldn't

{bold("Manage")}
  vk ls                  all sandboxes
  vk stop                stop this sandbox (it restarts in about a second)
  vk rm [--state]        delete this sandbox; --state also drops harness sessions
  vk build [--refresh]   build the image; --refresh updates all tools

No config is needed. Options: -y/--yes skips prompts, -C DIR picks the project.
"""


def _project_for(command: str, directory: str | None) -> Project:
    cwd = Path(directory).resolve() if directory else Path.cwd().resolve()
    if command in ("trust", "init", "selftest"):
        # These must work while vankila.toml is unapproved, or even unparseable.
        return load_project(cwd, read_project_file=False)
    return trust.load_approved(cwd)


def _in_sandbox(project: Project, command: list[str], assume_yes: bool) -> int:
    return sandbox.run_in_sandbox(Smol(), project, command, assume_yes)


def _warn_no_auth(project: Project, *envs: str) -> None:
    bound = {binding.env for binding in resolve_bindings(project)}
    if not bound & set(envs):
        names = {provider.env: provider.name for provider in BUILTIN}
        warn(f"No credential for this harness yet. Set one on the host: vk auth {names[envs[0]]}")


def cmd_claude(project: Project, argv: list[str], assume_yes: bool) -> int:
    _warn_no_auth(project, "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
    harness = project.config["harness"]["claude"]
    command = []
    bound = {binding.env for binding in resolve_bindings(project)}
    both = {"CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"} <= bound
    if both and project.config["auth"]["claude"] == "oauth":
        # An API key would outrank the subscription token.
        command += ["env", "-u", "ANTHROPIC_API_KEY"]
    command.append("claude")
    if harness.get("skip_permissions", True):
        command.append("--dangerously-skip-permissions")
    return _in_sandbox(project, command + argv, assume_yes)


PI_CREDENTIAL_ENVS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY",
                      "MISTRAL_API_KEY", "GEMINI_API_KEY")


def cmd_pi(project: Project, argv: list[str], assume_yes: bool) -> int:
    _warn_no_auth(project, *PI_CREDENTIAL_ENVS)
    return _in_sandbox(project, ["pi", *argv], assume_yes)


def cmd_shell(project: Project, argv: list[str], assume_yes: bool) -> int:
    # Not a login shell: Debian's /etc/profile would reset PATH.
    return _in_sandbox(project, ["bash", *argv], assume_yes)


def cmd_init(project: Project, argv: list[str], assume_yes: bool) -> int:
    path = project.cwd / PROJECT_FILE
    if os.path.lexists(path):
        info(f"{path} already exists")
        return 0
    text = render_template(project.cwd)
    write_project_file(project.cwd, text, create=True)
    trust.record(Project(root=project.cwd, cwd=project.cwd, config={}), text)
    ok(f"Wrote {PROJECT_FILE}")
    return 0


def cmd_trust(project: Project, argv: list[str], assume_yes: bool) -> int:
    had_approval = trust.approved_text(project) is not None
    if trust.ensure_trusted(project, force_prompt=True) is None:
        if had_approval:
            ok(f"Accepted the deletion of {PROJECT_FILE}")
        else:
            info(f"No {PROJECT_FILE} here")
        return 0
    ok(f"Approved {project.root / PROJECT_FILE}")
    return 0


def cmd_auth(project: Project, argv: list[str], assume_yes: bool) -> int:
    parser = argparse.ArgumentParser(prog="vk auth")
    parser.add_argument("name", nargs="?")
    parser.add_argument("--rm", action="store_true", help="forget a stored secret")
    args = parser.parse_args(argv)
    if not args.name:
        return _auth_table(project)
    if args.rm:
        ok(f"Forgot {args.name}") if delete_secret(args.name) else info(f"{args.name} wasn't stored")
        return 0
    known = {provider.name for provider in BUILTIN} | set(project.config["credentials"])
    if args.name not in known:
        raise Fail(f"unknown credential {args.name!r}", f"known: {', '.join(sorted(known))}")
    if args.name == "claude" and sys.stdin.isatty():
        info("Get a token by running `claude setup-token` on your host, then paste it here.")
    value = getpass.getpass(f"{args.name} secret (hidden): ").strip() if sys.stdin.isatty() else sys.stdin.read().strip()
    if not value:
        raise Fail("no value given")
    save_secret(args.name, value)
    ok(f"Stored {args.name} on the host. Sandboxes only ever get a placeholder.")
    smol_path = find_binary()
    if smol_path:
        smol = Smol(smol_path)
        status = smol.status(project.machine)
        if status and status.get("state") == "running":
            info("Restart to pick it up: vk stop")
    return 0


def _auth_table(project: Project) -> int:
    bindings = {binding.name: binding for binding in resolve_bindings(project)}
    say(bold("Credentials") + dim("  (real values stay on the host, injected at the VM boundary)"))
    for provider in BUILTIN:
        binding = bindings.get(provider.name)
        if binding:
            hosts = ", ".join(binding.hosts)
            say(f"  {green('✓')} {provider.name:<11} {dim(provider.env):<30} from {binding.source} → {hosts}")
        else:
            say(f"  {dim('·')} {provider.name:<11} {dim(provider.label + f': vk auth {provider.name}')}")
    for name in project.config["credentials"]:
        binding = bindings.get(name)
        mark = green("✓") if binding else yellow("!")
        detail = f"from {binding.source}" if binding else f"no value: vk auth {name}"
        say(f"  {mark} {name:<11} {detail}")
    return 0


def _human(size: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def cmd_status(project: Project, argv: list[str], assume_yes: bool) -> int:
    say(f"{bold(project.slug)}  {dim(str(project.root))}")
    state = "not created"
    binary = find_binary()
    smol = status = None
    if binary:
        smol = Smol(binary)
        status = smol.status(project.machine)
        if status:
            state = status.get("state", "unknown")
    color = green if state == "running" else dim
    say(f"  state     {color(state)}  {dim(project.machine)}")
    say(f"  guest     {project.guest_root}")
    sandbox_cfg = project.config["sandbox"]
    size = f"{sandbox_cfg['cpus']} vCPU, {sandbox_cfg['memory']} MiB"
    if smol and status and state == "running":
        used = smol.memory_used(project.machine, status.get("pid"))
        if used is not None:
            size += f" ({_human(used)} in use)"
    say(f"  size      {size}")
    if smol and status:
        usage = smol.disk_usage(project.machine)
        if usage:
            say(f"  disk      {_human(usage[0])} on host {dim(f'({_human(usage[1])} provisioned)')}")
    registries = project.config["registries"]
    say(f"  cooldown  {registries['cooldown_days']} days (not cargo: unsupported upstream)")
    say(bold("Egress allow list") + dim("  (limits accidents, not a determined guest: see the README)"))
    for host in project.allowed_hosts():
        say(f"  {host}")
    return _auth_table(project)


def cmd_allow(project: Project, argv: list[str], assume_yes: bool) -> int:
    parser = argparse.ArgumentParser(prog="vk allow")
    parser.add_argument("hosts", nargs="+")
    args = parser.parse_args(argv)
    # Start from the approved text and confirm the file still matches it, so
    # this can't approve an edit the guest made after the trust check.
    text = trust.approved_text(project) or ""
    if (trust.read_project_file(project.root) or "") != text:
        raise Fail(f"{PROJECT_FILE} changed just now", "review it with `vk trust`, then retry")
    for host in args.hosts:
        if host in project.allowed_hosts():
            info(f"{host} is already allowed")
            continue
        text = add_allowed_host(text, host)
        project = project_from_text(project.root, project.cwd, text)
        project.allowed_hosts()
        write_project_file(project.root, text)
        trust.record(project, text)
        ok(f"Allowed {host} in {PROJECT_FILE}")
    binary = find_binary()
    if binary:
        smol = Smol(binary)
        status = smol.status(project.machine)
        if status and status.get("state") == "running":
            sandbox._sync_allowlist(smol, project, status)
    return 0


def cmd_blocked(project: Project, argv: list[str], assume_yes: bool) -> int:
    smol = Smol()
    events = smol.denials(project.machine)
    if not events:
        ok("Nothing blocked")
        return 0
    for event in events:
        when = str(event.get("timestamp", ""))[:19].replace("T", " ")
        # Destinations come from guest traffic, so they aren't safe to print raw.
        say(f"  {dim(clean(when))}  {clean(str(event.get('operation', '?'))):<8} {clean(str(event.get('dest', '?')))}")
    return 0


def cmd_ls(project: Project, argv: list[str], assume_yes: bool) -> int:
    machines = [m for m in Smol().machines() if (m.get("labels") or {}).get("vankila")]
    if not machines:
        info("No sandboxes yet. Try: vk claude")
        return 0
    for machine in machines:
        state = machine.get("state", "?")
        color = green if state == "running" else dim
        say(f"  {color(f'{state:<9}')} {machine.get('labels', {}).get('vankila.root', machine.get('name'))}")
    return 0


def cmd_stop(project: Project, argv: list[str], assume_yes: bool) -> int:
    Smol().stop(project.machine)
    ok("Stopped")
    return 0


def cmd_rm(project: Project, argv: list[str], assume_yes: bool) -> int:
    parser = argparse.ArgumentParser(prog="vk rm")
    parser.add_argument("--state", action="store_true", help="also delete harness sessions and config")
    args = parser.parse_args(argv)
    smol = Smol()
    smol.stop(project.machine)
    smol.delete(project.machine)
    if args.state:
        shutil.rmtree(project.state_dir, ignore_errors=True)
    ok("Removed the sandbox" + (" and its harness state" if args.state else ". Your project is untouched."))
    return 0


def cmd_build(project: Project, argv: list[str], assume_yes: bool) -> int:
    parser = argparse.ArgumentParser(prog="vk build")
    parser.add_argument("--refresh", action="store_true", help="rebuild, updating all tools")
    args = parser.parse_args(argv)
    check_kvm()
    tools = project.config["tools"]
    if image.image_path(tools).exists() and not args.refresh:
        ok(f"Image is up to date ({image.image_hash(tools)})")
        return 0
    image.build(Smol(), project, refresh=args.refresh)
    return 0


def cmd_doctor(project: Project, argv: list[str], assume_yes: bool) -> int:
    failures = 0

    def check(label: str, passed: bool, hint: str = "") -> None:
        nonlocal failures
        failures += not passed
        mark = green("✓") if passed else red("✗")
        say(f"  {mark} {label}" + (f"  {dim(hint)}" if hint and not passed else ""))

    binary = find_binary()
    check(f"smolvm ({binary or 'missing'})", bool(binary), "vk offers to install it, or put it on your PATH")
    try:
        check_kvm()
        check("KVM", True)
    except Fail as err:
        check("KVM", False, str(err))
    tools = project.config["tools"]
    built = image.image_path(tools).exists()
    check(f"image {image.image_hash(tools)}", built, "vk build")
    bindings = {binding.env for binding in resolve_bindings(project)}
    check("Claude Code credential", bool(bindings & {"CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"}), "vk auth claude")
    check("pi credential", bool(bindings & set(PI_CREDENTIAL_ENVS)), "vk auth anthropic (or openai, openrouter, mistral, gemini)")
    # Optional: a GitHub token is only bound when you ask for one.
    mark = green("✓") if "GH_TOKEN" in bindings else dim("·")
    say(f"  {mark} GitHub credential" + ("" if "GH_TOKEN" in bindings else f"  {dim('optional: vk auth github')}"))
    return 1 if failures else 0


def cmd_selftest(project: Project, argv: list[str], assume_yes: bool) -> int:
    from .selftest import run

    return run()


COMMANDS = {
    "claude": cmd_claude,
    "pi": cmd_pi,
    "shell": cmd_shell,
    "sh": cmd_shell,
    "init": cmd_init,
    "trust": cmd_trust,
    "auth": cmd_auth,
    "status": cmd_status,
    "allow": cmd_allow,
    "blocked": cmd_blocked,
    "ls": cmd_ls,
    "stop": cmd_stop,
    "rm": cmd_rm,
    "build": cmd_build,
    "doctor": cmd_doctor,
    "selftest": cmd_selftest,
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    assume_yes = False
    directory = None
    while argv and argv[0] in ("-y", "--yes", "-C"):
        flag = argv.pop(0)
        if flag == "-C":
            if not argv:
                print("vk: -C needs a directory", file=sys.stderr)
                return 2
            directory = argv.pop(0)
        else:
            assume_yes = True
    if not argv or argv[0] in ("-h", "--help", "help"):
        say(HELP)
        return 0
    if argv[0] in ("-V", "--version"):
        print(f"vankila {__version__} (smolvm {SMOLVM_VERSION})")
        return 0
    try:
        project = _project_for(argv[0], directory)
        handler = COMMANDS.get(argv[0])
        if handler:
            return handler(project, argv[1:], assume_yes)
        if argv[0] == "--":
            argv = argv[1:]
        return _in_sandbox(project, argv, assume_yes)
    except Fail as err:
        say(f"{red('error:')} {err}")
        if err.hint:
            say(f"  {dim(err.hint)}")
        return 1
    except KeyboardInterrupt:
        return 130
    except subprocess.CalledProcessError as err:
        say(f"{red('error:')} {err}")
        return err.returncode or 1
