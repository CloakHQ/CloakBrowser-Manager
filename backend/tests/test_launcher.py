"""Tests for the lightweight native Manager launcher."""

import json

import pytest
from packaging.requirements import Requirement

import run as launcher


def test_linux_directs_users_to_docker(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(launcher.sys, "platform", "linux")
    with pytest.raises(RuntimeError, match="use Docker on Linux"):
        launcher._ensure_environment()


def test_platform_launchers_target_shared_runner():
    assert "run.py" in (launcher.ROOT / "run-windows.bat").read_text()
    assert "python3 run.py" in (launcher.ROOT / "run-macos.sh").read_text()


@pytest.fixture
def setup_environment(tmp_path, monkeypatch):
    root = tmp_path
    python = root / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.touch()
    files = {
        "backend/requirements.txt": "fastapi>=0.162.0\n",
        "packaging/requirements-build.txt": "pip>=26.3\nsetuptools>=84.1.0\npyinstaller>=6.22.3\n",
        "frontend/package-lock.json": "{}",
        "frontend/src/app.ts": "",
        "frontend/dist/index.html": "",
    }
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (root / "frontend" / "node_modules").mkdir()
    monkeypatch.setattr(launcher, "ROOT", root)
    monkeypatch.setattr(launcher, "VENV_DIR", python.parent.parent)
    monkeypatch.setattr(launcher, "SETUP_MARKER", python.parent.parent / ".manager-setup.json")
    monkeypatch.setattr(launcher, "FRONTEND_DIR", root / "frontend")
    monkeypatch.setattr(launcher, "_venv_python", lambda: python)
    monkeypatch.setattr(launcher.sys, "platform", "darwin")
    monkeypatch.setattr(launcher.shutil, "which", lambda name: "/fake/npm")
    return root


@pytest.mark.parametrize("bootstrap_state", [None, "outdated", "current"])
def test_bootstrap_updates_existing_environments_once(setup_environment, monkeypatch, bootstrap_state):
    root = setup_environment
    bootstrap = root / "packaging" / "requirements-build.txt"
    state = {
        "requirements": launcher._file_hash(root / "backend" / "requirements.txt"),
        "frontend": launcher._file_hash(root / "frontend" / "package-lock.json"),
    }
    if bootstrap_state is not None:
        state["bootstrap"] = launcher._file_hash(bootstrap) if bootstrap_state == "current" else "old"
    launcher.SETUP_MARKER.write_text(json.dumps(state))
    calls = []
    monkeypatch.setattr(launcher, "_run", lambda command, **kwargs: calls.append(command))
    launcher._ensure_environment()
    assert calls == ([] if bootstrap_state == "current" else [[
        str(launcher._venv_python()), "-m", "pip", "install", "--disable-pip-version-check",
        "-r", str(bootstrap),
    ]])
    assert launcher._load_setup_state()["bootstrap"] == launcher._file_hash(bootstrap)


def test_bootstrap_precedes_runtime_installation(setup_environment, monkeypatch):
    calls = []
    monkeypatch.setattr(launcher, "_run", lambda command, **kwargs: calls.append(command))
    launcher._ensure_environment()
    assert calls[0][-1] == str(setup_environment / "packaging" / "requirements-build.txt")
    assert calls[1][-1] == str(setup_environment / "backend" / "requirements.txt")


def test_bootstrap_failure_is_not_cached(setup_environment, monkeypatch):
    def fail(command, **kwargs):
        assert command[-1] == str(setup_environment / "packaging" / "requirements-build.txt")
        raise launcher.subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(launcher, "_run", fail)
    with pytest.raises(launcher.subprocess.CalledProcessError):
        launcher._ensure_environment()
    assert not launcher.SETUP_MARKER.exists()


@pytest.mark.parametrize("path", [
    "Dockerfile", ".github/workflows/ci.yml", "packaging/build_macos.sh",
    "packaging/build_windows.ps1",
])
def test_install_paths_share_tooling_requirements(path):
    source = (launcher.ROOT / path).read_text()
    assert "requirements-build.txt" in source
    if path == ".github/workflows/ci.yml":
        source = source[source.index("- run:"):]
    runtime = "backend\\requirements.txt" if path == "packaging/build_windows.ps1" else "backend/requirements.txt"
    assert source.index("requirements-build.txt") < source.index(runtime)


@pytest.mark.parametrize("path", ["packaging/build_macos.sh", "packaging/build_windows.ps1"])
def test_installer_scripts_install_shared_tooling_once(path):
    assert (launcher.ROOT / path).read_text().count("requirements-build.txt") == 1


def test_tooling_uses_one_requirements_file():
    assert not (launcher.ROOT / "packaging" / "requirements-bootstrap.txt").exists()
    source = (launcher.ROOT / "packaging" / "requirements-build.txt").read_text()
    requirements = {}
    for line in source.splitlines():
        if line.strip() and not line.startswith("#"):
            item = Requirement(line)
            requirements[item.name] = item
    assert set(requirements) == {"pip", "setuptools", "pyinstaller"}
    assert "25.0.1" not in requirements["pip"].specifier
    assert "79.0.1" not in requirements["setuptools"].specifier
    assert "6.10" not in requirements["pyinstaller"].specifier


def test_frontend_source_timestamp_is_available():
    source_files = [
        path for path in (launcher.FRONTEND_DIR / "src").rglob("*") if path.is_file()
    ]
    assert source_files
    assert max(path.stat().st_mtime for path in source_files) > 0
