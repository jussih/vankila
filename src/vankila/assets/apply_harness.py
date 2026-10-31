"""Lays down harness config. Runs as root inside the guest, never on the host.

The harness directories are mounted from the host and the guest can plant
symlinks in them. Doing this work in the guest means those links resolve in the
guest's filesystem, so they can't redirect anything onto the host.

Usage: apply_harness.py STAGING_DIR, where STAGING_DIR holds payload.json and
the host's global overlays under global/{claude,pi}.
"""

import json
import os
import stat
import sys
import tempfile


def deep_merge(base, over):
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def read_json(path):
    """Anything that isn't a regular file holding a JSON object counts as empty."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
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


class Applier:
    def __init__(self, uid, gid):
        self.owner = (uid, gid) if os.geteuid() == 0 else None

    def _chown(self, path):
        if self.owner:
            os.chown(path, *self.owner, follow_symlinks=False)

    def makedirs(self, path):
        # Resolves here, in the guest, so a planted link can't reach the host.
        path = os.path.realpath(path)
        missing = []
        while not os.path.lexists(path):
            missing.append(path)
            path = os.path.dirname(path)
        for directory in reversed(missing):
            os.mkdir(directory)
            self._chown(directory)

    def write(self, path, data):
        """Atomic, so a harness reading the file never sees half of it."""
        parent = os.path.dirname(path)
        self.makedirs(parent)
        fd, tmp = tempfile.mkstemp(dir=parent, prefix=".vk-tmp-")
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(tmp, 0o644)
        self._chown(tmp)
        os.replace(tmp, path)

    def overlay(self, source, target):
        """Copy regular files only. Symlinks in an overlay are skipped."""
        if os.path.islink(source) or not os.path.isdir(source):
            return
        for directory, subdirs, names in os.walk(source, followlinks=False):
            subdirs[:] = [name for name in subdirs if not os.path.islink(os.path.join(directory, name))]
            for name in names:
                path = os.path.join(directory, name)
                if not stat.S_ISREG(os.lstat(path).st_mode):
                    continue
                with open(path, "rb") as handle:
                    data = handle.read()
                self.write(os.path.join(target, os.path.relpath(path, source)), data)

    def write_json(self, path, data):
        self.write(path, json.dumps(data, indent=2).encode() + b"\n")


def project_overlay(project_root, harness):
    current = project_root
    for part in (".vankila", "harness"):
        current = os.path.join(current, part)
        if os.path.islink(current):
            return None
    return os.path.join(current, harness)


def claude_state(state, trusted_dirs, api_key):
    """Skip Claude Code's first-run screens: the login, trust and API key prompts."""
    state["hasCompletedOnboarding"] = True
    state["bypassPermissionsModeAccepted"] = True
    projects = state.get("projects")
    projects = projects if isinstance(projects, dict) else {}
    for directory in trusted_dirs:
        entry = projects.get(directory)
        entry = entry if isinstance(entry, dict) else {}
        entry["hasTrustDialogAccepted"] = True
        projects[directory] = entry
    state["projects"] = projects
    if api_key:
        responses = state.get("customApiKeyResponses")
        responses = responses if isinstance(responses, dict) else {}
        approved = responses.get("approved")
        approved = approved if isinstance(approved, list) else []
        suffix = api_key[-20:]
        if suffix not in approved:
            approved.append(suffix)
        state["customApiKeyResponses"] = {"approved": approved, "rejected": []}
    return state


def main(staging):
    with open(os.path.join(staging, "payload.json")) as handle:
        payload = json.load(handle)
    apply = Applier(payload["uid"], payload["gid"])
    claude, pi = payload["claude_dir"], os.path.join(payload["pi_dir"], "agent")
    apply.makedirs(claude)
    apply.makedirs(pi)

    # Global overlays first, so project overlays win.
    for harness, target in (("claude", claude), ("pi", pi)):
        apply.overlay(os.path.join(staging, "global", harness), target)
        source = project_overlay(payload["project_root"], harness)
        if source:
            apply.overlay(source, target)

    settings = os.path.join(claude, "settings.json")
    apply.write_json(settings, deep_merge(read_json(settings), payload["claude_settings"]))
    if payload["pi_settings"]:
        settings = os.path.join(pi, "settings.json")
        apply.write_json(settings, deep_merge(read_json(settings), payload["pi_settings"]))

    state = os.path.join(claude, ".claude.json")
    apply.write_json(state, claude_state(read_json(state), payload["trusted_dirs"], payload["api_key"]))


if __name__ == "__main__":
    main(sys.argv[1])
