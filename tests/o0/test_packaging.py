"""Verify the wheel from an isolated install rather than an editable checkout."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from zipfile import ZipFile

import pytest


ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIRS = {"backstop", "so", "server", "data", "evals", "analysis", "ui"}


def _run(command: list[str], cwd: Path, *, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=180)
    if result.returncode:
        pytest.fail(f"Command failed: {command[0]}\n{result.stdout}\n{result.stderr}")
    return result.stdout


def test_wheel_installs_with_runtime_resources_outside_checkout(tmp_path):
    uv = shutil.which("uv")
    assert uv is not None, "Wheel acceptance requires uv"
    source = tmp_path / "source"
    source.mkdir()
    tracked = _run(["git", "ls-files", "-z"], ROOT).split("\0")
    resources = set()
    for name in filter(None, tracked):
        relative = Path(name)
        if name != "pyproject.toml" and relative.parts[0] not in PACKAGE_DIRS:
            continue
        destination = source / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, destination)
        if (name.startswith(("so/prompts/", "ui/"))
                or name in {"data/questions.jsonl", "data/traps.jsonl"}):
            resources.add(name)

    dist = tmp_path / "dist"
    _run([uv, "build", "--wheel", str(source), "--out-dir", str(dist)], tmp_path)
    wheel, = dist.glob("*.whl")
    with ZipFile(wheel) as archive:
        names = set(archive.namelist())
        assert resources <= names, f"Missing wheel resources: {sorted(resources - names)}"
        assert not any(name.startswith(("tests/", "cache/")) or Path(name).name in {".env", "REVIEW.md"}
                       for name in names)

    environment = tmp_path / "installed"
    _run([uv, "venv", "--python", sys.executable, str(environment)], tmp_path)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    _run([uv, "pip", "install", "--python", str(python), str(wheel)], tmp_path)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    probe_env = {
        "SO_ENV_FILE": os.devnull,
        "SO_OUT_DIR": str(runtime / "out"),
        "SO_LOG_DIR": str(runtime / "logs"),
        "SO_CACHE_DIR": str(runtime / "cache"),
    }
    probe = '''
import json
import sys
from pathlib import Path
import so.agent
import so.checker
from so import config
from data.load import load_questions, load_traps
from fastapi.testclient import TestClient
from server.app import app

assert Path(so.checker.__file__).is_relative_to(Path(sys.prefix))
assert Path(so.agent.__file__).is_relative_to(Path(sys.prefix))
assert config.AGENT_PROMPT_PATH.read_text(encoding="utf-8")
assert so.checker.SYSTEM_PROMPT and so.checker.PAGE_PROMPT
questions, traps = load_questions(), load_traps()
assert len(questions) == 300 and len(traps) == 30
assets = ["/", "/race", "/ui/app.js", "/ui/race.js", "/ui/race-state.js", "/ui/race-access.js",
          "/ui/run-stream.js", "/ui/style.css", "/ui/favicon.svg"]
with TestClient(app) as client:
    for path in assets:
        response = client.get(path)
        assert response.status_code == 200, (path, response.status_code)
        assert response.content
print(json.dumps({"imports": 2, "prompts": 3, "questions": len(questions),
                  "traps": len(traps), "asset_routes": len(assets)}))
'''
    result = json.loads(_run([str(python), "-I", "-c", probe], runtime, env=probe_env))
    assert result == {"imports": 2, "prompts": 3, "questions": 300, "traps": 30, "asset_routes": 9}
