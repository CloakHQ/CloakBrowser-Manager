"""Tests for per-profile binary selection and cache cleanup."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend import binary_cache


def test_keyless_profile_is_refused_before_the_wrapper(monkeypatch) -> None:
    from cloakbrowser import download

    ensure = MagicMock()
    monkeypatch.setattr(download, "ensure_binary", ensure)

    with pytest.raises(binary_cache.MissingLicenseKeyError):
        binary_cache.ensure_profile_binary({"license_key": "  "})
    ensure.assert_not_called()


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
    legacy_keyless = tmp_path / "chromium-145.0.7632.109.2"
    unused = tmp_path / "chromium-146.0.7680.177.5-pro"
    referenced = tmp_path / "chromium-147.0.7727.100.2-pro"
    running = tmp_path / "chromium-148.0.7778.215.2-pro"
    for directory in (legacy_keyless, unused, referenced, running):
        directory.mkdir()
        (directory / "chrome").write_bytes(b"binary")

    profiles = [
        {"license_key": "cb_a", "browser_version": "147.0.7727.100.2"},
        # Pre-0.7 keyless profile: it pins nothing it could ever launch with.
        {"browser_version": "145.0.7632.109.2"},
    ]
    sessions = [
        SimpleNamespace(
            browser_version="148.0.7778.215.2",
            binary_tier="licensed",
        )
    ]

    removed, reclaimed = binary_cache.cleanup_unused_binaries(profiles, sessions)

    assert sorted(item["version"] for item in removed) == [
        "145.0.7632.109.2",
        "146.0.7680.177.5",
    ]
    assert reclaimed == 2 * len(b"binary")
    assert not legacy_keyless.exists()
    assert not unused.exists()
    assert referenced.exists()
    assert running.exists()
