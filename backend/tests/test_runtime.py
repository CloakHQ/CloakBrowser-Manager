"""Tests for host runtime policy and platform data paths."""

from pathlib import Path

import pytest
from packaging.requirements import Requirement

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


@pytest.mark.parametrize(
    ("python_version", "compatible_version"),
    [("3.10", "16.1.1"), ("3.12", "17.4")],
)
def test_websockets_requirement_preserves_python_compatibility(
    python_version: str, compatible_version: str,
):
    requirements = Path(__file__).resolve().parents[1] / "requirements.txt"
    candidates = [
        Requirement(line)
        for line in requirements.read_text().splitlines()
        if line.startswith("websockets")
    ]
    applicable = [
        item for item in candidates
        if item.marker is None or item.marker.evaluate({"python_version": python_version})
    ]
    assert len(applicable) == 1
    assert compatible_version in applicable[0].specifier
    if python_version == "3.12":
        assert "16.1.1" not in applicable[0].specifier


def test_legacy_data_dir_override_is_honored(tmp_path: Path):
    configured = tmp_path / "legacy-data"
    assert default_data_dir(
        "windows",
        {"CLOAKBROWSER_DATA_DIR": str(configured)},
        home=tmp_path,
    ) == configured
