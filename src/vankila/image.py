"""The golden image: every tool baked once, then shared by all sandboxes."""

from __future__ import annotations

import hashlib
import importlib.resources
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .config import Project, data_home
from .smol import Smol
from .ui import Fail, Spinner, ok

MAX_AGE_DAYS = 30


def provision_script() -> str:
    return (importlib.resources.files("vankila") / "assets" / "provision.sh").read_text()


def image_hash(tools: dict) -> str:
    recipe = json.dumps(tools, sort_keys=True) + provision_script()
    return hashlib.sha256(recipe.encode()).hexdigest()[:12]


def image_dir(tools: dict) -> Path:
    return data_home() / "images" / image_hash(tools)


def image_path(tools: dict) -> Path:
    return image_dir(tools) / "base.smolmachine"


def image_id(tools: dict) -> str:
    """Changes when the image is rebuilt, even from an unchanged recipe."""
    path = image_path(tools)
    return f"{image_hash(tools)}-{int(path.stat().st_mtime)}"


def image_age_days(tools: dict) -> int:
    return int((time.time() - image_path(tools).stat().st_mtime) / 86400)


def build(smol: Smol, project: Project, refresh: bool = False) -> None:
    tools = project.config["tools"]
    if image_path(tools).exists() and not refresh:
        return
    name = f"vankila-bake-{image_hash(tools)}"
    target = image_dir(tools)
    target.parent.mkdir(parents=True, exist_ok=True)
    log_path = target.parent / f"{target.name}.log"
    smol.delete(name)
    started = time.monotonic()
    try:
        with Spinner("Building the sandbox image (once; a few minutes)") as spinner:
            smol.run("machine", "create", "--name", name, "--image", tools["base"], "--net",
                     "--cpus", "4", "--mem", "4096", "--storage", "20", "--overlay", "20")
            spinner.update("Booting the build VM")
            smol.run("machine", "start", "--name", name)
            with tempfile.NamedTemporaryFile("w", suffix=".sh") as script:
                script.write(provision_script())
                script.flush()
                smol.run("machine", "cp", script.name, f"{name}:/tmp/provision.sh")
            _provision(smol, name, tools, spinner, log_path)
            spinner.update("Snapshotting the image")
            smol.run("machine", "stop", "--name", name)
            staging = target.with_name(target.name + ".tmp")
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir()
            smol.run("pack", "create", "--from-vm", name, "--output", str(staging / "base"))
            shutil.rmtree(target, ignore_errors=True)
            staging.rename(target)
    finally:
        smol.delete(name)
    ok(f"Image ready in {int(time.monotonic() - started)}s")


def _provision(smol: Smol, name: str, tools: dict, spinner: Spinner, log_path: Path) -> None:
    command = [
        smol.binary, "machine", "exec", "--name", name, "--stream", "--user", "root",
        "--env", f"VK_NODE={tools['node']}",
        "--env", f"VK_RUST={tools['rust']}",
        "--env", f"VK_APT={' '.join(tools['apt'])}",
        "--", "bash", "/tmp/provision.sh",
    ]
    with log_path.open("w") as log:
        proc = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            log.write(line)
            if line.startswith("==> "):
                spinner.update(line[4:].strip())
        code = proc.wait()
    if code != 0:
        raise Fail(f"image build failed (exit {code})", f"full log: {log_path}")
