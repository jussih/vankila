"""Configuration: built-in defaults < global config < project vankila.toml."""

from __future__ import annotations

import copy
import hashlib
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .ui import Fail

PROJECT_FILE = "vankila.toml"

# Hostname groups. Entries are exact hosts or `*.domain` (subdomains only).
PRESETS: dict[str, list[str]] = {
    "anthropic": ["api.anthropic.com"],
    "claude": ["api.anthropic.com", "claude.ai", "platform.claude.com"],
    "github": [
        "github.com",
        "api.github.com",
        "codeload.github.com",
        "*.githubusercontent.com",
    ],
    "npm": ["registry.npmjs.org", "registry.yarnpkg.com"],
    "pypi": ["pypi.org", "files.pythonhosted.org"],
    "crates": ["crates.io", "index.crates.io", "static.crates.io"],
    "rust": ["static.rust-lang.org"],
    "apt": ["deb.debian.org"],
    "openai": ["api.openai.com"],
    "openrouter": ["openrouter.ai"],
    "mistral": ["api.mistral.ai"],
    "google-ai": ["generativelanguage.googleapis.com"],
}

DEFAULTS: dict = {
    "sandbox": {
        "cpus": 4,
        "memory": 8192,
        "overlay": 20,
        "storage": 20,
        "ports": [],
        "mounts": [],
        "env": {},
        "setup": [],
        "ssh_agent": False,
        "keep_running": False,
    },
    "network": {
        "presets": ["claude", "github", "npm", "pypi", "crates", "rust", "apt"],
        "allow": [],
        "allow_private": [],
    },
    "tools": {
        "base": "debian:trixie-slim",
        "node": "24",
        "rust": "stable",
        "apt": [],
    },
    "registries": {
        "cooldown_days": 14,
        "ignore_scripts": True,
    },
    "auth": {
        # Which credential Claude Code uses when both are configured.
        "claude": "oauth",
    },
    "credentials": {},
    "harness": {
        "claude": {"skip_permissions": True, "settings": {}},
        "pi": {"settings": {}},
    },
}

SCHEMA_SECTIONS = set(DEFAULTS)
HOST_RE = re.compile(r"^(\*\.)?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


def config_home() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "vankila"


def data_home() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or "~/.local/share"
    return Path(base).expanduser() / "vankila"


def state_home() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or "~/.local/state"
    return Path(base).expanduser() / "vankila"


def deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _load(path: Path) -> dict:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as err:
        raise Fail(f"{path}: {err}") from err
    unknown = set(data) - SCHEMA_SECTIONS
    if unknown:
        raise Fail(
            f"{path}: unknown section [{sorted(unknown)[0]}]",
            f"valid sections: {', '.join(sorted(SCHEMA_SECTIONS))}",
        )
    for section, body in data.items():
        if section in ("credentials", "harness"):
            continue
        extra = set(body) - set(DEFAULTS[section])
        if extra:
            raise Fail(
                f"{path}: unknown key {section}.{sorted(extra)[0]}",
                f"valid keys: {', '.join(sorted(DEFAULTS[section]))}",
            )
    return data


def find_root(start: Path) -> Path:
    """The nearest directory with vankila.toml, else the git root, else `start`."""
    for directory in (start, *start.parents):
        if (directory / PROJECT_FILE).is_file():
            return directory
    for directory in (start, *start.parents):
        if (directory / ".git").exists():
            return directory
    return start


def slugify(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", name).strip("-")[:40] or "project"


@dataclass
class Project:
    root: Path
    cwd: Path
    config: dict

    @property
    def slug(self) -> str:
        return slugify(self.root.name)

    @property
    def machine(self) -> str:
        digest = hashlib.sha256(str(self.root).encode()).hexdigest()[:8]
        return f"vk-{self.slug}-{digest}"

    @property
    def guest_root(self) -> str:
        return f"/workspace/{self.slug}"

    @property
    def workdir(self) -> str:
        rel = self.cwd.relative_to(self.root)
        return self.guest_root if rel == Path(".") else f"{self.guest_root}/{rel.as_posix()}"

    @property
    def state_dir(self) -> Path:
        return state_home() / "projects" / self.machine

    def allowed_hosts(self) -> list[str]:
        net = self.config["network"]
        hosts: list[str] = []
        for preset in net["presets"]:
            if preset not in PRESETS:
                raise Fail(
                    f"unknown network preset {preset!r}",
                    f"available presets: {', '.join(sorted(PRESETS))}",
                )
            hosts += PRESETS[preset]
        hosts += net["allow"]
        for cred in self.config["credentials"].values():
            hosts += cred.get("hosts", [])
        for host in hosts:
            if not HOST_RE.match(host):
                raise Fail(
                    f"invalid host {host!r} in network config",
                    "use an exact name (api.example.com) or subdomains (*.example.com)",
                )
        return sorted(set(hosts))


def load_global() -> dict:
    return _load(config_home() / "config.toml")


def load_project(cwd: Path | None = None, read_project_file: bool = True) -> Project:
    cwd = (cwd or Path.cwd()).resolve()
    root = find_root(cwd)
    global_config = load_global()
    merged = deep_merge(copy.deepcopy(DEFAULTS), global_config)
    if read_project_file:
        merged = deep_merge(merged, _load(root / PROJECT_FILE))
    # Lists normally replace, but a global allow must survive a project's `allow = []`.
    global_allow = global_config.get("network", {}).get("allow", [])
    merged["network"]["allow"] = list(dict.fromkeys([*global_allow, *merged["network"]["allow"]]))
    return Project(root=root, cwd=cwd, config=merged)


def check_mountable(root: Path) -> None:
    home = Path.home().resolve()
    if root == Path("/") or root == home or root in home.parents:
        raise Fail(
            f"refusing to mount {root} into a sandbox: it holds your credentials and dotfiles",
            "cd into a project directory, or `vk init` one",
        )


def detect_presets(root: Path) -> list[str]:
    """The minimum sensible network presets for a project directory."""
    presets = ["claude", "github"]
    if any((root / name).exists() for name in ("package.json", "pnpm-lock.yaml", "yarn.lock")):
        presets.append("npm")
    if any((root / name).exists() for name in ("pyproject.toml", "requirements.txt", "uv.lock")):
        presets.append("pypi")
    if (root / "Cargo.toml").exists():
        presets += ["crates", "rust"]
    return presets


def render_template(root: Path) -> str:
    presets = ", ".join(f'"{preset}"' for preset in detect_presets(root))
    return f"""\
# vankila sandbox config. Delete anything you don't need: defaults apply.

[network]
# Everything else is blocked. Available presets: {", ".join(sorted(PRESETS))}.
presets = [{presets}]
allow = []                     # e.g. "api.stripe.com", "*.example.com"

[sandbox]
cpus = 4
memory = 8192                  # MiB; a cap, only touched memory is used
ports = []                     # e.g. "3000" or "8080:80"
setup = []                     # run once, as the agent user, on a new sandbox

[registries]
cooldown_days = 14             # npm, pnpm, yarn, bun, uv, pip. Cargo has no equivalent yet.
ignore_scripts = true          # npm-family install scripts

# A secret the sandbox can use but never see:
# [credentials.stripe]
# env = "STRIPE_API_KEY"       # the sandbox gets a placeholder in this variable
# hosts = ["api.stripe.com"]   # the real value is only ever sent here
"""


def add_allowed_host(root: Path, host: str) -> None:
    """Add a host to [network].allow, keeping the file's comments intact."""
    path = root / PROJECT_FILE
    text = path.read_text() if path.exists() else ""
    allow = re.search(r"^(allow\s*=\s*\[)(.*?)(\])", text, flags=re.M | re.S)
    if allow and "[network]" in text[: allow.start()]:
        body = allow.group(2).strip().rstrip(",")
        body = f'{body}, "{host}"' if body else f'"{host}"'
        text = f"{text[: allow.start()]}{allow.group(1)}{body}{allow.group(3)}{text[allow.end():]}"
    elif "[network]" in text:
        text = text.replace("[network]", f'[network]\nallow = ["{host}"]', 1)
    else:
        text = f'{text.rstrip()}\n\n[network]\nallow = ["{host}"]\n'.lstrip()
    path.write_text(text)
