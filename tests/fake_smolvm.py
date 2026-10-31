#!/usr/bin/env python3
"""Stands in for smolvm: keeps machine records in a JSON file, logs every call."""
import json
import os
import sys
from pathlib import Path

state_path = Path(os.environ["FAKE_SMOL_STATE"])
state = json.loads(state_path.read_text()) if state_path.exists() else {"machines": {}, "calls": []}
argv = sys.argv[1:]
call = {"argv": argv, "secrets": {k: v for k, v in os.environ.items() if k.startswith("FAKE_SECRET") or k in ("GH_TOKEN", "ANTHROPIC_API_KEY", "VANKILA_GIT_AUTH", "CLAUDE_CODE_OAUTH_TOKEN")}}
state["calls"].append(call)


def opt(name, default=None):
    return argv[argv.index(name) + 1] if name in argv else default


def opts(name):
    return [argv[i + 1] for i, arg in enumerate(argv) if arg == name]


def save():
    state_path.write_text(json.dumps(state))


if argv[:2] == ["pack", "create"]:
    output = Path(opt("--output"))
    output.with_name(output.name + ".smolmachine").write_text("pack")
elif argv[0] == "machine":
    verb, name = argv[1], opt("--name")
    machines = state["machines"]
    if verb == "create":
        labels = dict(item.split("=", 1) for item in opts("--label"))
        machines[name] = {
            "name": name, "state": "created", "labels": labels,
            "allowed_hosts": [f"={host}" for host in opts("--allow-host-pattern")],
        }
    elif verb == "start":
        machines[name]["state"] = "running"
    elif verb == "stop":
        if name in machines:
            machines[name]["state"] = "stopped"
    elif verb == "delete":
        machines.pop(name, None)
    elif verb == "update":
        machines[name]["allowed_hosts"] = [f"={host}" for host in opts("--allow-host-pattern")]
    elif verb == "status":
        save()
        if name not in machines:
            print("not found", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(machines[name]))
        sys.exit(0)
    elif verb == "ls":
        print(json.dumps(list(machines.values())))
    elif verb == "egress-events":
        print(json.dumps(state.get("denials", [])))
    elif verb == "exec":
        command = argv[argv.index("--") + 1:]
        if command[:1] == ["printenv"]:
            print("SMOL_PLACEHOLDER_ANTHROPIC_abcdefghijklmnopqrstuvwxyz")
        elif "--stream" in argv and "bash" in command and "/tmp/provision.sh" in command:
            print("==> Installing things")
        elif command == ["exit-seven"]:
            save()
            sys.exit(7)
save()
