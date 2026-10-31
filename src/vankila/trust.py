"""vankila.toml lives in the mounted project, so the agent can write to it.

It can also arrive in a cloned repo. It decides mounts, egress and which host
commands supply secrets, so it only applies once approved here, on the host,
outside anything the guest can reach. Approval covers that exact content.

The guest can change the file at any moment, so it is read once. The bytes that
are compared, shown and recorded are the same bytes that get parsed and used.
"""

from __future__ import annotations

import difflib
import errno
import os
import stat
import sys
from pathlib import Path

from .config import PROJECT_FILE, Project, find_root, project_from_text, state_home
from .ui import Fail, bold, clean, confirm, dim, green, red, say

MAX_PROJECT_FILE_BYTES = 1024 * 1024


def _approved_path(project: Project) -> Path:
    return state_home() / "trust" / f"{project.machine}.toml"


def read_project_file(root: Path) -> str | None:
    """Read vankila.toml once, refusing anything but a regular file."""
    path = root / PROJECT_FILE
    not_regular = Fail(f"{path} is not a regular file: refusing to read it")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as err:
        if err.errno == errno.ELOOP:
            raise not_regular from None
        raise
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise not_regular
        data = handle.read(MAX_PROJECT_FILE_BYTES + 1)
    if len(data) > MAX_PROJECT_FILE_BYTES:
        raise Fail(f"{path} is larger than {MAX_PROJECT_FILE_BYTES} bytes: refusing to read it")
    try:
        return data.decode()
    except UnicodeDecodeError as err:
        raise Fail(f"{path} is not valid UTF-8: {err}") from err


def approved_text(project: Project) -> str | None:
    path = _approved_path(project)
    return path.read_text() if path.exists() else None


def record(project: Project, text: str) -> None:
    """Approve exactly this text. Never re-read it: that could approve something unseen."""
    path = _approved_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def forget(project: Project) -> None:
    _approved_path(project).unlink(missing_ok=True)


def _show_diff(approved: str, text: str) -> None:
    diff = difflib.unified_diff(approved.splitlines(), text.splitlines(), lineterm="", n=2)
    for line in list(diff)[2:]:
        paint = green if line.startswith("+") else red if line.startswith("-") else dim
        say(f"  {paint(clean(line))}")


def _confirm_deletion(project: Project, approved: str) -> None:
    """A deleted file isn't "no config": it restores the broader defaults."""
    if not sys.stdin.isatty():
        raise Fail(f"{PROJECT_FILE} was deleted and needs approval", "run: vk trust")
    say(bold(f"{project.root / PROJECT_FILE} was deleted. The agent can do that, so check it."))
    say("Without it the built-in defaults apply, which may allow more. The approved file said:")
    _show_diff(approved, "")
    if not confirm("Accept the deletion and use the defaults?", default=False):
        raise Fail(f"deleting {PROJECT_FILE} not approved", "restore the file, or run `vk trust` when ready")
    forget(project)


def ensure_trusted(project: Project, force_prompt: bool = False) -> str | None:
    """Returns the approved content, or None if the project runs without a file."""
    text = read_project_file(project.root)
    approved = approved_text(project)
    if text is None:
        if approved is not None:
            _confirm_deletion(project, approved)
        return None
    if text == approved and not force_prompt:
        return text
    if not sys.stdin.isatty():
        raise Fail(f"{PROJECT_FILE} is new or changed and needs approval", "run: vk trust")
    if approved is None:
        say(bold(f"{project.root / PROJECT_FILE} hasn't been approved yet:"))
        for line in text.splitlines():
            say(f"  {clean(line)}")
    else:
        say(bold(f"{PROJECT_FILE} changed. The agent can edit this file, so check it:"))
        _show_diff(approved, text)
    if not confirm("Apply this config?", default=False):
        raise Fail(f"{PROJECT_FILE} not approved", "fix it, or run `vk trust` when ready")
    record(project, text)
    return text


def _check_deleted_configs(cwd: Path) -> None:
    """Deleting the file can also move the project root, so look wherever it applied."""
    for directory in (cwd, *cwd.parents):
        if os.path.lexists(directory / PROJECT_FILE):
            return
        candidate = Project(root=directory, cwd=cwd, config={})
        if _approved_path(candidate).exists():
            ensure_trusted(candidate)


def load_approved(cwd: Path) -> Project:
    """The only way to load a project that vk will act on."""
    _check_deleted_configs(cwd)
    root = find_root(cwd)
    text = ensure_trusted(Project(root=root, cwd=cwd, config={}))
    return project_from_text(root, cwd, text)
