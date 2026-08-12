"""Unit tests for system_check.py's get_system_check().

_chrome_gpu_mode()'s own detection logic (device presence, env aliasing) is
already exhaustively covered in test_browser_manager.py — these tests only
check that get_system_check wires it (and everything else) together
correctly, not that GPU detection itself is correct.
"""

from __future__ import annotations

import pytest

from backend import system_check


def test_reports_gpu_mode_from_chrome_gpu_mode(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("CHROME_GPU_ACCEL", "0")  # swiftshader, no device probe needed
    result = system_check.get_system_check(tmp_path)
    assert result["gpu_mode"] == "swiftshader"


def test_license_configured_true_when_env_var_set(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("CHROME_GPU_ACCEL", "0")
    monkeypatch.setenv("CLOAKBROWSER_LICENSE_KEY", "some-real-key")
    result = system_check.get_system_check(tmp_path)
    assert result["license_configured"] is True


def test_license_configured_false_when_env_var_unset(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("CHROME_GPU_ACCEL", "0")
    monkeypatch.delenv("CLOAKBROWSER_LICENSE_KEY", raising=False)
    result = system_check.get_system_check(tmp_path)
    assert result["license_configured"] is False


def test_license_configured_false_for_an_empty_string(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """An operator-set but blank env var must not read as "configured"."""
    monkeypatch.setenv("CHROME_GPU_ACCEL", "0")
    monkeypatch.setenv("CLOAKBROWSER_LICENSE_KEY", "")
    result = system_check.get_system_check(tmp_path)
    assert result["license_configured"] is False


def test_reports_the_hardcoded_kasmvnc_version(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("CHROME_GPU_ACCEL", "0")
    result = system_check.get_system_check(tmp_path)
    assert result["kasmvnc_version"] == "1.5.0"


def test_disk_usage_fields_are_internally_consistent(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("CHROME_GPU_ACCEL", "0")
    result = system_check.get_system_check(tmp_path)

    assert result["disk_total_bytes"] > 0
    assert result["disk_used_bytes"] >= 0
    assert result["disk_free_bytes"] >= 0
    # shutil.disk_usage's own free/used need not sum exactly to total
    # (reserved blocks), but neither can exceed it.
    assert result["disk_used_bytes"] <= result["disk_total_bytes"]
    assert result["disk_free_bytes"] <= result["disk_total_bytes"]
    assert 0.0 <= result["disk_percent_used"] <= 100.0


def test_disk_percent_used_matches_used_over_total(monkeypatch: pytest.MonkeyPatch, tmp_path):
    from collections import namedtuple

    monkeypatch.setenv("CHROME_GPU_ACCEL", "0")
    Usage = namedtuple("usage", ["total", "used", "free"])
    monkeypatch.setattr(system_check.shutil, "disk_usage", lambda _path: Usage(200, 50, 150))

    result = system_check.get_system_check(tmp_path)

    assert result["disk_total_bytes"] == 200
    assert result["disk_used_bytes"] == 50
    assert result["disk_free_bytes"] == 150
    assert result["disk_percent_used"] == 25.0
