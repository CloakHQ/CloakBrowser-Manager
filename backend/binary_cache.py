"""Per-profile CloakBrowser binary resolution and cache management."""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

_VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+){3,4}$")
# Wrapper 0.7+ only writes `chromium-<version>-pro`; bare `chromium-<version>`
# directories are keyless builds left by wrappers before 0.7 and are never used.
_CACHE_DIR_RE = re.compile(
    r"^chromium-(?P<version>[0-9]+(?:\.[0-9]+){3,4})(?P<pro>-pro)?$"
)
FREE_KEY_URL = "https://cloakbrowser.dev/free"


class MissingLicenseKeyError(ValueError):
    """A profile without its own key cannot launch on wrapper 0.7+."""

    def __init__(self) -> None:
        super().__init__(
            "This profile has no license key. Add one under Browser in the "
            f"profile settings (get a free key at {FREE_KEY_URL})."
        )


@dataclass(frozen=True)
class BinaryRequest:
    license_key: str | None
    browser_version: str | None
    release_channel: str


@dataclass(frozen=True)
class PreparedBinary:
    version: str | None
    tier: str
    binary_path: str
    cache_dir: str | None


def _clean_optional(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def normalize_version(value: Any) -> str | None:
    version = _clean_optional(value)
    if version is None:
        return None
    if not _VERSION_RE.fullmatch(version):
        raise ValueError(
            "Invalid browser version. Use a full numeric Chromium version, "
            "for example '145.0.7632.109.2'."
        )
    return version


def profile_binary_request(profile: dict[str, Any]) -> BinaryRequest:
    """Resolve one profile's license, channel, and optional exact version pin."""
    release_channel = str(profile.get("release_channel") or "stable").strip().lower()
    if release_channel not in {"stable", "preview"}:
        raise ValueError("release_channel must be 'stable' or 'preview'")

    return BinaryRequest(
        license_key=_clean_optional(profile.get("license_key")),
        browser_version=normalize_version(profile.get("browser_version")),
        release_channel=release_channel,
    )


def launch_binary_request(profile: dict[str, Any]) -> BinaryRequest:
    """The profile's request, refusing a keyless profile before the wrapper sees it.

    Without a key the wrapper falls back to a process-wide key (env or
    `<cache>/license.key`) or starts a free-key claim that saves one there, which
    would hand one profile's seat to every keyless profile.
    """
    request = profile_binary_request(profile)
    if request.license_key is None:
        raise MissingLicenseKeyError()
    return request


def _cache_identity(binary_path: Path) -> tuple[str | None, str, Path | None]:
    for parent in (binary_path, *binary_path.parents):
        match = _CACHE_DIR_RE.fullmatch(parent.name)
        if match:
            tier = "licensed" if match.group("pro") else "keyless"
            return match.group("version"), tier, parent
    return None, "external", None


def ensure_profile_binary(profile: dict[str, Any]) -> PreparedBinary:
    """Ensure the profile's selected binary exists, downloading it when absent."""
    from cloakbrowser.download import ensure_binary

    request = launch_binary_request(profile)
    path = Path(
        ensure_binary(
            license_key=request.license_key,
            browser_version=request.browser_version,
            release_channel=request.release_channel,
        )
    )
    version, tier, cache_dir = _cache_identity(path)
    return PreparedBinary(
        version=version or request.browser_version,
        tier="licensed" if tier == "external" else tier,
        binary_path=str(path),
        cache_dir=str(cache_dir) if cache_dir else None,
    )


def cache_dir() -> Path:
    from cloakbrowser.config import get_cache_dir

    return get_cache_dir()


def assert_no_global_license_file() -> None:
    """Reject a wrapper-global cached key; every profile must carry its own."""
    key_file = cache_dir() / "license.key"
    if key_file.exists():
        raise RuntimeError(
            f"Global CloakBrowser key file is not supported: {key_file}. "
            "Move the key into each profile, then remove this file."
        )


def _directory_size(path: Path) -> int:
    total = 0
    for root, _directories, files in os.walk(path):
        for filename in files:
            try:
                total += (Path(root) / filename).stat().st_size
            except OSError:
                continue
    return total


def _configured_references(profile: dict[str, Any]) -> set[tuple[str, str]]:
    request = profile_binary_request(profile)
    if not request.license_key:
        return set()

    from cloakbrowser.config import get_effective_version

    references: set[tuple[str, str]] = set()
    if request.browser_version:
        references.add((request.browser_version, "licensed"))
    pro_version = get_effective_version(release_channel=request.release_channel)
    if pro_version:
        references.add((pro_version, "licensed"))
    return references


def _references(
    profiles: Iterable[dict[str, Any]],
    running_profiles: Iterable[Any],
) -> dict[tuple[str, str], dict[str, int]]:
    references: dict[tuple[str, str], dict[str, int]] = {}
    for profile in profiles:
        for reference in _configured_references(profile):
            counts = references.setdefault(reference, {"profiles": 0, "running": 0})
            counts["profiles"] += 1

    for running in running_profiles:
        version = getattr(running, "browser_version", None)
        tier = getattr(running, "binary_tier", None)
        if not version or tier not in {"licensed", "keyless"}:
            continue
        counts = references.setdefault((version, tier), {"profiles": 0, "running": 0})
        counts["running"] += 1
    return references


def list_installed_binaries(
    profiles: Iterable[dict[str, Any]],
    running_profiles: Iterable[Any],
) -> list[dict[str, Any]]:
    root = cache_dir()
    references = _references(profiles, running_profiles)
    binaries: list[dict[str, Any]] = []
    if not root.exists():
        return binaries

    for child in root.iterdir():
        if child.is_symlink() or not child.is_dir():
            continue
        match = _CACHE_DIR_RE.fullmatch(child.name)
        if not match:
            continue
        version = match.group("version")
        tier = "licensed" if match.group("pro") else "keyless"
        counts = references.get((version, tier), {"profiles": 0, "running": 0})
        binaries.append(
            {
                "version": version,
                "tier": tier,
                "path": str(child),
                "size_bytes": _directory_size(child),
                "profile_count": counts["profiles"],
                "running_count": counts["running"],
                "in_use": bool(counts["profiles"] or counts["running"]),
            }
        )

    binaries.sort(
        key=lambda item: tuple(int(part) for part in item["version"].split(".")),
        reverse=True,
    )
    return binaries




def cleanup_unused_binaries(
    profiles: Iterable[dict[str, Any]],
    running_profiles: Iterable[Any],
) -> tuple[list[dict[str, Any]], int]:
    installed = list_installed_binaries(profiles, running_profiles)
    removed: list[dict[str, Any]] = []
    reclaimed = 0
    for binary in installed:
        if binary["in_use"]:
            continue
        path = Path(binary["path"])
        shutil.rmtree(path)
        removed.append(binary)
        reclaimed += int(binary["size_bytes"])

    return removed, reclaimed
