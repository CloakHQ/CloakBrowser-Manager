"""Tests for the lightweight native Manager launcher."""

import pytest

import run as launcher


def test_linux_directs_users_to_docker(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(launcher.sys, "platform", "linux")
    with pytest.raises(RuntimeError, match="use Docker on Linux"):
        launcher._ensure_environment()


def test_platform_launchers_target_shared_runner():
    assert "run.py" in (launcher.ROOT / "run-windows.bat").read_text()
    assert "python3 run.py" in (launcher.ROOT / "run-macos.sh").read_text()


def test_frontend_source_timestamp_is_available():
    source_files = [
        path for path in (launcher.FRONTEND_DIR / "src").rglob("*") if path.is_file()
    ]
    assert source_files
    assert max(path.stat().st_mtime for path in source_files) > 0
