"""Host runtime policy and platform-specific Manager paths."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

HostOS = Literal["windows", "macos", "linux"]
RuntimeMode = Literal["native", "docker"]
ViewerMode = Literal["native-window", "vnc"]

_RUNTIME_ENV = "CLOAKBROWSER_MANAGER_RUNTIME"
_DATA_DIR_ENV = "CLOAKBROWSER_MANAGER_DATA_DIR"
_LEGACY_DATA_DIR_ENVS = ("CLOAKBROWSER_DATA_DIR",)


@dataclass(frozen=True)
class RuntimeConfig:
    host_os: HostOS
    runtime_mode: RuntimeMode
    viewer_mode: ViewerMode
    data_dir: Path

    @property
    def is_native(self) -> bool:
        return self.runtime_mode == "native"


def detect_host_os(platform_name: str | None = None) -> HostOS:
    """Map Python's platform identifier to Manager's supported hosts."""
    value = platform_name or sys.platform
    if value == "win32":
        return "windows"
    if value == "darwin":
        return "macos"
    if value.startswith("linux"):
        return "linux"
    raise RuntimeError(f"Unsupported operating system: {value}")


def default_data_dir(
    host_os: HostOS,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Return the Manager state directory without creating it."""
    env = os.environ if environ is None else environ
    configured = env.get(_DATA_DIR_ENV)
    if configured:
        return Path(configured).expanduser()

    for legacy_name in _LEGACY_DATA_DIR_ENVS:
        legacy_value = env.get(legacy_name)
        if legacy_value:
            return Path(legacy_value).expanduser()

    user_home = home or Path.home()
    if host_os == "windows":
        local_app_data = env.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else user_home / "AppData" / "Local"
        return base / "CloakBrowser Manager"
    if host_os == "macos":
        return user_home / "Library" / "Application Support" / "CloakBrowser Manager"
    return Path("/data")


def resolve_runtime(
    platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> RuntimeConfig:
    """Resolve the only supported runtime for the current host."""
    env = os.environ if environ is None else environ
    host_os = detect_host_os(platform_name)
    default_mode: RuntimeMode = "docker" if host_os == "linux" else "native"
    requested_mode = env.get(_RUNTIME_ENV, default_mode).strip().lower()
    if requested_mode not in {"native", "docker"}:
        raise RuntimeError(
            f"{_RUNTIME_ENV} must be 'native' or 'docker', got {requested_mode!r}"
        )

    runtime_mode = cast(RuntimeMode, requested_mode)
    if host_os == "linux" and runtime_mode != "docker":
        raise RuntimeError(
            "Native Linux desktop mode is not supported; run Manager with Docker/KasmVNC"
        )
    if host_os != "linux" and runtime_mode != "native":
        raise RuntimeError(
            f"Docker runtime is supported only on Linux, not {host_os}"
        )

    viewer_mode: ViewerMode = "vnc" if runtime_mode == "docker" else "native-window"
    return RuntimeConfig(
        host_os=host_os,
        runtime_mode=runtime_mode,
        viewer_mode=viewer_mode,
        data_dir=default_data_dir(host_os, env, home),
    )
