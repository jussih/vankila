"""vankila.toml lives in the mounted project, so the agent can write to it.

It can also arrive in a cloned repo. It decides mounts, egress and which host
commands supply secrets, so it only applies once approved here, on the host,
outside anything the guest can reach. Approval covers that exact content.
"""

from __future__ import annotations

import difflib
import os
import stat
import sys

from .config import PROJECT_FILE, Project, state_home
from .ui import Fail, bold, confirm, dim, green, red, say


def _approved_path(project: Project):
    return state_home() / "trust" / f"{project.machine}.toml"


def _read_project_file(project: Project) -> str | None:
    path = project.root / PROJECT_FILE
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(mode):
        raise Fail(f"{path} is not a regular file: refusing to read it")
    return path.read_text()


def record(project: Project) -> None:
    text = _read_project_file(project)
    if text is None:
        return
    path = _approved_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def ensure_trusted(project: Project, force_prompt: bool = False) -> None:
    text = _read_project_file(project)
    if text is None:
        return
    approved_path = _approved_path(project)
    approved = approved_path.read_text() if approved_path.exists() else None
    if text == approved and not force_prompt:
        return
    if not sys.stdin.isatty():
        raise Fail(f"{PROJECT_FILE} is new or changed and needs approval", "run: vk trust")
    if approved is None:
        say(bold(f"{project.root / PROJECT_FILE} hasn't been approved yet:"))
        for line in text.splitlines():
            say(f"  {line}")
    else:
        say(bold(f"{PROJECT_FILE} changed. The agent can edit this file, so check it:"))
        diff = difflib.unified_diff(approved.splitlines(), text.splitlines(), lineterm="", n=2)
        for line in list(diff)[2:]:
            paint = green if line.startswith("+") else red if line.startswith("-") else dim
            say(f"  {paint(line)}")
    if not confirm("Apply this config?", default=False):
        raise Fail(f"{PROJECT_FILE} not approved", "fix it, or run `vk trust` when ready")
    record(project)
