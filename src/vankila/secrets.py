"""Secrets stay on the host. The guest only ever receives placeholders.

smolvm's credential substitution replaces a placeholder with the real value
at the VM boundary, on HTTPS requests to the bound hosts, in request headers.
Real values reach smolvm only through the environment of `machine start`.
"""

from __future__ import annotations

import base64
import os
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .config import Project, config_home
from .ui import Fail


@dataclass(frozen=True)
class Provider:
    name: str
    env: str
    hosts: tuple[str, ...]
    presets: tuple[str, ...]
    label: str
    command: str = ""


BUILTIN = (
    Provider("claude", "CLAUDE_CODE_OAUTH_TOKEN", ("api.anthropic.com",), ("claude", "anthropic"),
             "Claude subscription token (from `claude setup-token`)"),
    Provider("anthropic", "ANTHROPIC_API_KEY", ("api.anthropic.com",), ("claude", "anthropic"),
             "Anthropic API key"),
    Provider("github", "GH_TOKEN", ("api.github.com",), ("github",),
             "GitHub token"),
    Provider("openai", "OPENAI_API_KEY", ("api.openai.com",), ("openai",), "OpenAI API key"),
    Provider("openrouter", "OPENROUTER_API_KEY", ("openrouter.ai",), ("openrouter",),
             "OpenRouter API key"),
    Provider("mistral", "MISTRAL_API_KEY", ("api.mistral.ai",), ("mistral",), "Mistral API key"),
    Provider("gemini", "GEMINI_API_KEY", ("generativelanguage.googleapis.com",), ("google-ai",),
             "Google AI API key"),
)

GIT_AUTH_ENV = "VANKILA_GIT_AUTH"


def store_path() -> Path:
    return config_home() / "secrets.toml"


def _read_store() -> dict[str, str]:
    path = store_path()
    if not path.exists():
        return {}
    if path.stat().st_mode & 0o077:
        os.chmod(path, 0o600)
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _write_store(values: dict[str, str]) -> None:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        for key, value in sorted(values.items()):
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            handle.write(f'"{key}" = "{escaped}"\n')
    os.replace(tmp, path)


def save_secret(name: str, value: str) -> None:
    values = _read_store()
    values[name] = value
    _write_store(values)


def delete_secret(name: str) -> bool:
    values = _read_store()
    if name not in values:
        return False
    del values[name]
    _write_store(values)
    return True


def _run_command(command: str, name: str) -> str | None:
    try:
        proc = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=30, check=False
        )
    except subprocess.TimeoutExpired:
        raise Fail(f"secret {name}: `{command}` timed out") from None
    return (proc.stdout.strip() or None) if proc.returncode == 0 else None


@dataclass(frozen=True)
class Binding:
    """One secret bound to hosts, ready for `--credential`."""

    name: str
    env: str
    hosts: tuple[str, ...]
    source: str
    value: str
    set_header: str = ""

    @property
    def flag(self) -> str:
        spec = f"{self.name}={self.env}@{','.join(self.hosts)}"
        return f"{spec}:set-header={self.set_header}" if self.set_header else spec


def _resolve(name: str, env: str, command: str, store: dict[str, str]) -> tuple[str, str] | None:
    if os.environ.get(env):
        return os.environ[env], f"${env}"
    if store.get(name):
        return store[name], "vk auth"
    if command:
        value = _run_command(command, name)
        if value:
            return value, f"`{command}`"
    return None


def _resolve_provider(provider: Provider, project: Project, store: dict[str, str]) -> tuple[str, str] | None:
    if provider.name != "github":
        return _resolve(provider.name, provider.env, provider.command, store)
    # A GitHub token can reach far beyond this project, so it's opt-in. Whatever the
    # host happens to be logged in with, or has exported, doesn't count.
    mode = project.config["auth"]["github"]
    if mode == "gh":
        return _resolve(provider.name, provider.env, "gh auth token", store)
    if mode != "stored":
        raise Fail(f'unknown auth.github value {mode!r}', 'use "stored" or "gh"')
    return (store[provider.name], "vk auth") if store.get(provider.name) else None


def resolve_bindings(project: Project) -> list[Binding]:
    """Every credential that has a value and whose hosts this project may reach."""
    store = _read_store()
    presets = set(project.config["network"]["presets"])
    allowed = set(project.allowed_hosts())
    bindings: list[Binding] = []
    for provider in BUILTIN:
        if not (presets & set(provider.presets)) and not set(provider.hosts) <= allowed:
            continue
        found = _resolve_provider(provider, project, store)
        if not found:
            continue
        value, source = found
        bindings.append(Binding(provider.name, provider.env, provider.hosts, source, value))
        if provider.name == "github" and "github.com" in allowed:
            # git sends Basic auth, whose base64 hides a placeholder from the
            # boundary, so the whole header is set there instead.
            token = base64.b64encode(f"x-access-token:{value}".encode()).decode()
            bindings.append(
                Binding("github-git", GIT_AUTH_ENV, ("github.com",), source,
                        f"Basic {token}", set_header="authorization")
            )
    for name, cred in project.config["credentials"].items():
        if "env" not in cred or not cred.get("hosts"):
            raise Fail(f"[credentials.{name}] needs `env` and `hosts`")
        found = _resolve(name, cred["env"], cred.get("command", ""), store)
        if not found:
            continue
        value, source = found
        bindings.append(
            Binding(name, cred["env"], tuple(cred["hosts"]), source, value,
                    set_header=cred.get("set_header", ""))
        )
    return bindings


def missing_credentials(project: Project, bindings: list[Binding]) -> list[str]:
    bound = {binding.name for binding in bindings}
    return [name for name in project.config["credentials"] if name not in bound]
