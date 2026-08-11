"""Tests for host runtime policy and platform data paths."""

from pathlib import Path

import pytest

from backend.runtime import default_data_dir, detect_host_os, resolve_runtime


def test_detect_supported_hosts():
    assert detect_host_os("win32") == "windows"
    assert detect_host_os("darwin") == "macos"
    assert detect_host_os("linux") == "linux"


def test_detect_unsupported_host():
    with pytest.raises(RuntimeError, match="Unsupported operating system"):
        detect_host_os("freebsd")


def test_windows_defaults_to_native(tmp_path: Path):
    config = resolve_runtime(
        "win32",
        {"LOCALAPPDATA": str(tmp_path)},
        home=tmp_path,
    )
    assert config.runtime_mode == "native"
    assert config.viewer_mode == "native-window"
    assert config.data_dir == tmp_path / "CloakBrowser Manager"


def test_macos_defaults_to_native(tmp_path: Path):
    config = resolve_runtime("darwin", {}, home=tmp_path)
    assert config.runtime_mode == "native"
    assert config.viewer_mode == "native-window"
    assert config.data_dir == (
        tmp_path / "Library" / "Application Support" / "CloakBrowser Manager"
    )


def test_linux_defaults_to_docker():
    config = resolve_runtime("linux", {})
    assert config.runtime_mode == "docker"
    assert config.viewer_mode == "vnc"
    assert config.data_dir == Path("/data")


def test_native_linux_is_rejected():
    with pytest.raises(RuntimeError, match="Native Linux desktop mode is not supported"):
        resolve_runtime("linux", {"CLOAKBROWSER_MANAGER_RUNTIME": "native"})


def test_docker_on_desktop_host_is_rejected():
    with pytest.raises(RuntimeError, match="Docker runtime is supported only on Linux"):
        resolve_runtime("darwin", {"CLOAKBROWSER_MANAGER_RUNTIME": "docker"})


def test_canonical_data_dir_override_wins(tmp_path: Path):
    configured = tmp_path / "manager-data"
    assert default_data_dir(
        "macos",
        {
            "CLOAKBROWSER_MANAGER_DATA_DIR": str(configured),
            "DATA_DIR": "/legacy",
        },
        home=tmp_path,
    ) == configured


def test_legacy_data_dir_override_is_honored(tmp_path: Path):
    configured = tmp_path / "legacy-data"
    assert default_data_dir(
        "windows",
        {"CLOAKBROWSER_DATA_DIR": str(configured)},
        home=tmp_path,
    ) == configured
