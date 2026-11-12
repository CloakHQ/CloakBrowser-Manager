"""Per-profile CloakBrowser binary resolution and cache management."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

_VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+){3,4}$")
# Licensed builds live in `chromium-<version>-pro` (written by the wrapper);
# keyless builds in `chromium-<version>` (written by this module, the same layout
# wrappers before 0.7 used, so their downloads are reused).
_CACHE_DIR_RE = re.compile(
    r"^chromium-(?P<version>[0-9]+(?:\.[0-9]+){3,4})(?P<pro>-pro)?$"
)

# Wrapper 0.7 dropped keyless builds, but CloakHQ's signed public releases stay on
# GitHub. A keyless profile without a pin gets the newest build of the line this
# Manager used before 0.7; linux-arm64 never had a 145 build.
KEYLESS_RELEASES_URL = "https://github.com/CloakHQ/cloakbrowser/releases/download"
_KEYLESS_DEFAULT_VERSIONS = {
    "linux-x64": "145.0.7632.109.2",
    "darwin-arm64": "145.0.7632.109.2",
    "darwin-x64": "145.0.7632.109.2",
    "windows-x64": "145.0.7632.109.2",
    "linux-arm64": "146.0.7680.177.4",
}


@dataclass(frozen=True)
class BinaryRequest:
    license_key: str | None
    browser_version: str | None
    release_channel: str

    @property
    def tier(self) -> str:
        return "licensed" if self.license_key else "keyless"


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
    """Resolve one profile's license, channel, and optional exact version pin.

    A keyless profile always resolves to an exact version: its pin, or the
    platform default.
    """
    release_channel = str(profile.get("release_channel") or "stable").strip().lower()
    if release_channel not in {"stable", "preview"}:
        raise ValueError("release_channel must be 'stable' or 'preview'")

    license_key = _clean_optional(profile.get("license_key"))
    browser_version = normalize_version(profile.get("browser_version"))
    if license_key is None and browser_version is None:
        from cloakbrowser.config import get_platform_tag

        platform_tag = get_platform_tag()
        browser_version = _KEYLESS_DEFAULT_VERSIONS.get(platform_tag)
        if browser_version is None:
            raise ValueError(f"No keyless CloakBrowser build exists for {platform_tag}")

    return BinaryRequest(
        license_key=license_key,
        browser_version=browser_version,
        release_channel=release_channel,
    )


def _cache_identity(binary_path: Path) -> tuple[str | None, str, Path | None]:
    for parent in (binary_path, *binary_path.parents):
        match = _CACHE_DIR_RE.fullmatch(parent.name)
        if match:
            tier = "licensed" if match.group("pro") else "keyless"
            return match.group("version"), tier, parent
    return None, "external", None


def _keyless_binary_path(version: str) -> tuple[Path, Path]:
    """(install dir, executable) for a keyless build: the wrapper's layout minus `-pro`."""
    from cloakbrowser.config import get_binary_dir, get_binary_path

    relative = get_binary_path(version).relative_to(get_binary_dir(version))
    install_dir = cache_dir() / f"chromium-{version}"
    return install_dir, install_dir / relative


def _ensure_keyless_binary(version: str) -> Path:
    """Download a public keyless build once, verified like the wrapper verifies Pro builds.

    The release's SHA256SUMS must carry a valid Ed25519 signature from a key the
    wrapper pins, must declare the requested version (no silent downgrade), and
    must list this platform's archive with a matching SHA-256. Releases without a
    signature are refused.
    """
    import httpx
    from cloakbrowser.config import get_archive_ext, get_archive_name, get_platform_tag
    from cloakbrowser.download import (
        _download_file,
        _extract_archive,
        _is_executable,
        _parse_checksums,
        _parse_manifest_version,
        _verify_checksum,
        _verify_signature,
    )

    install_dir, binary = _keyless_binary_path(version)
    if binary.exists() and _is_executable(binary):
        return binary

    base = f"{KEYLESS_RELEASES_URL}/chromium-v{version}"
    archive_name = get_archive_name()
    try:
        manifest = httpx.get(f"{base}/SHA256SUMS", follow_redirects=True, timeout=10.0)
        manifest.raise_for_status()
        signature = httpx.get(f"{base}/SHA256SUMS.sig", follow_redirects=True, timeout=10.0)
        signature.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise ValueError(
                f"Keyless build {version} is not a signed public CloakBrowser release"
            ) from exc
        raise
    _verify_signature(manifest.content, signature.content)
    manifest_text = manifest.content.decode("utf-8")
    declared = _parse_manifest_version(manifest_text)
    if declared != version:
        raise RuntimeError(
            f"Signed SHA256SUMS declares {declared or 'no version'}, requested {version}"
        )
    expected = _parse_checksums(manifest_text).get(archive_name)
    if expected is None:
        raise ValueError(f"Keyless build {version} has no {get_platform_tag()} archive")

    install_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=get_archive_ext(), delete=False) as tmp:
        archive = Path(tmp.name)
    try:
        _download_file(f"{base}/{archive_name}", archive)
        _verify_checksum(archive, expected)
        _extract_archive(archive, install_dir, binary)
    finally:
        archive.unlink(missing_ok=True)
    if not binary.exists():
        raise RuntimeError(f"Keyless build {version} extracted without {binary.name}")
    return binary


def ensure_profile_binary(profile: dict[str, Any]) -> PreparedBinary:
    """Ensure the profile's selected binary exists, downloading it when absent."""
    request = profile_binary_request(profile)
    if request.license_key is None:
        assert request.browser_version is not None
        path = _ensure_keyless_binary(request.browser_version)
    else:
        from cloakbrowser.download import ensure_binary

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
        tier=request.tier if tier == "external" else tier,
        binary_path=str(path),
        cache_dir=str(cache_dir) if cache_dir else None,
    )


# The wrapper resolves its executable through `cloakbrowser.browser.ensure_binary`,
# which on 0.7+ demands a key. A keyless launch pins its prepared executable here;
# asyncio.to_thread() copies the context, so the pin reaches the wrapper's worker
# thread and never leaks into a concurrent launch of another profile.
_pinned_binary: ContextVar[str | None] = ContextVar("cloak_pinned_binary", default=None)
_wrapper_hooked = False


def _hook_wrapper() -> None:
    global _wrapper_hooked
    if _wrapper_hooked:
        return
    import cloakbrowser.browser as wrapper_browser

    if "ensure_binary" not in wrapper_browser.build_launch_options.__code__.co_names:
        raise RuntimeError(
            "This cloakbrowser wrapper no longer resolves its binary through "
            "ensure_binary(); keyless profiles cannot launch on it"
        )
    resolve = wrapper_browser.ensure_binary

    def ensure_binary(
        license_key: str | None = None,
        browser_version: str | None = None,
        release_channel: str | None = None,
    ) -> str:
        pinned = _pinned_binary.get()
        if pinned is not None:
            return pinned
        return resolve(
            license_key=license_key,
            browser_version=browser_version,
            release_channel=release_channel,
        )

    wrapper_browser.ensure_binary = ensure_binary
    _wrapper_hooked = True


@contextmanager
def wrapper_binary(prepared: PreparedBinary) -> Iterator[None]:
    """Make wrapper launches in this context use a prepared keyless executable."""
    if prepared.tier != "keyless":
        yield
        return
    _hook_wrapper()
    token = _pinned_binary.set(prepared.binary_path)
    try:
        yield
    finally:
        _pinned_binary.reset(token)


def cache_dir() -> Path:
    from cloakbrowser.config import get_cache_dir

    return get_cache_dir()


def assert_no_global_license_file() -> None:
    """Reject a wrapper-global cached key: it would hand one seat to every keyless profile."""
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
        return {(request.browser_version, "keyless")} if request.browser_version else set()

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
