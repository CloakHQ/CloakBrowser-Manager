"""Tests for per-profile binary selection and cache cleanup."""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend import binary_cache


def test_keyless_profile_resolves_to_the_platform_default() -> None:
    request = binary_cache.profile_binary_request({"license_key": "  "})

    assert request.license_key is None
    assert request.tier == "keyless"
    assert request.browser_version == "145.0.7632.109.2"  # conftest platform: linux-x64


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


@pytest.fixture()
def fake_wrapper_browser(monkeypatch: pytest.MonkeyPatch):
    """A `cloakbrowser.browser` whose build_launch_options resolves via ensure_binary()."""
    module = types.ModuleType("cloakbrowser.browser")
    exec(
        "def ensure_binary(license_key=None, browser_version=None, release_channel=None):\n"
        "    return f'resolved:{license_key}'\n"
        "def build_launch_options(license_key=None, **_):\n"
        "    return {'executable_path': ensure_binary(license_key=license_key)}\n",
        module.__dict__,
    )
    monkeypatch.setitem(sys.modules, "cloakbrowser.browser", module)
    monkeypatch.setattr(binary_cache, "_wrapper_hooked", False)
    return module


@pytest.mark.asyncio
async def test_keyless_pin_reaches_the_wrapper_thread_only_for_its_launch(fake_wrapper_browser):
    keyless = binary_cache.PreparedBinary("145.0.7632.109.2", "keyless", "/cache/keyless/chrome", None)
    pinned_in = asyncio.Event()
    release = asyncio.Event()

    async def keyless_launch() -> str:
        with binary_cache.wrapper_binary(keyless):
            pinned_in.set()
            await release.wait()  # a licensed launch runs while this pin is held
            return (await asyncio.to_thread(fake_wrapper_browser.build_launch_options))[
                "executable_path"
            ]

    async def licensed_launch() -> str:
        await pinned_in.wait()
        try:
            options = await asyncio.to_thread(
                fake_wrapper_browser.build_launch_options, license_key="cb_x"
            )
        finally:
            release.set()
        return options["executable_path"]

    keyless_path, licensed_path = await asyncio.gather(keyless_launch(), licensed_launch())

    assert keyless_path == "/cache/keyless/chrome"
    assert licensed_path == "resolved:cb_x"


def test_cleanup_preserves_referenced_and_running_binaries(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("CLOAKBROWSER_CACHE_DIR", str(tmp_path))
    unused_keyless = tmp_path / "chromium-146.0.7680.177.5"
    referenced_keyless = tmp_path / "chromium-145.0.7632.109.2"
    unused = tmp_path / "chromium-146.0.7680.177.5-pro"
    referenced = tmp_path / "chromium-147.0.7727.100.2-pro"
    running = tmp_path / "chromium-148.0.7778.215.2-pro"
    for directory in (unused_keyless, referenced_keyless, unused, referenced, running):
        directory.mkdir()
        (directory / "chrome").write_bytes(b"binary")

    profiles = [
        {"license_key": "cb_a", "browser_version": "147.0.7727.100.2"},
        {},  # keyless, platform default 145.0.7632.109.2
    ]
    sessions = [
        SimpleNamespace(
            browser_version="148.0.7778.215.2",
            binary_tier="licensed",
        )
    ]

    removed, reclaimed = binary_cache.cleanup_unused_binaries(profiles, sessions)

    assert sorted((item["version"], item["tier"]) for item in removed) == [
        ("146.0.7680.177.5", "keyless"),
        ("146.0.7680.177.5", "licensed"),
    ]
    assert reclaimed == 2 * len(b"binary")
    assert not unused_keyless.exists()
    assert not unused.exists()
    assert referenced_keyless.exists()
    assert referenced.exists()
    assert running.exists()
