"""Tests for per-profile binary selection and cache cleanup."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from backend import binary_cache


def test_keyless_profile_defaults_to_legacy_145() -> None:
    request = binary_cache.profile_binary_request({})

    assert request.license_key is None
    assert request.browser_version == binary_cache.LEGACY_KEYLESS_VERSION
    assert request.release_channel == "stable"


def test_profile_can_pin_licensed_preview_version() -> None:
    request = binary_cache.profile_binary_request(
        {
            "license_key": " cb_test ",
            "release_channel": "preview",
            "browser_version": "148.0.7778.215.2",
        }
    )

    assert request.license_key == "cb_test"
    assert request.browser_version == "148.0.7778.215.2"
    assert request.release_channel == "preview"


def test_cleanup_preserves_referenced_and_running_binaries(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("CLOAKBROWSER_CACHE_DIR", str(tmp_path))
    unused = tmp_path / "chromium-146.0.7680.177.5"
    referenced = tmp_path / "chromium-145.0.7632.109.2"
    running = tmp_path / "chromium-148.0.7778.215.2-pro"
    for directory in (unused, referenced, running):
        directory.mkdir()
        (directory / "chrome").write_bytes(b"binary")

    profiles = [{"browser_version": "145.0.7632.109.2"}]
    sessions = [
        SimpleNamespace(
            browser_version="148.0.7778.215.2",
            binary_tier="licensed",
        )
    ]

    removed, reclaimed = binary_cache.cleanup_unused_binaries(profiles, sessions)

    assert [item["version"] for item in removed] == ["146.0.7680.177.5"]
    assert reclaimed == len(b"binary")
    assert not unused.exists()
    assert referenced.exists()
    assert running.exists()
