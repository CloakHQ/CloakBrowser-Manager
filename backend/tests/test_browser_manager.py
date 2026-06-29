"""Tests for browser_manager pure functions — proxy parsing, fingerprint args, profile defaults."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import socket
from unittest.mock import AsyncMock, MagicMock

from backend.browser_manager import (
    BASE_CDP_PORT,
    CDP_PORT_RANGE,
    _init_profile_defaults,
    _normalize_proxy,
    _validate_proxy,
    BrowserManager,
)


# ── _normalize_proxy ─────────────────────────────────────────────────────────


def test_normalize_already_http():
    assert _normalize_proxy("http://user:pass@host:8080") == "http://user:pass@host:8080"


def test_normalize_already_https():
    assert _normalize_proxy("https://host:443") == "https://host:443"


def test_normalize_already_socks5():
    assert _normalize_proxy("socks5://host:1080") == "socks5://host:1080"


def test_normalize_host_port_user_pass():
    assert _normalize_proxy("proxy.com:8080:myuser:mypass") == "http://myuser:mypass@proxy.com:8080"


def test_normalize_host_port_only():
    assert _normalize_proxy("proxy.com:8080") == "http://proxy.com:8080"


def test_normalize_three_parts():
    # 3 parts doesn't match any pattern — returned as-is
    assert _normalize_proxy("a:b:c") == "a:b:c"


def test_normalize_five_parts():
    # 5 parts doesn't match — returned as-is
    assert _normalize_proxy("a:b:c:d:e") == "a:b:c:d:e"


def test_normalize_empty_parts():
    # host:port:user:pass with empty parts
    result = _normalize_proxy(":8080:user:pass")
    assert result == "http://user:pass@:8080"


# ── _validate_proxy ──────────────────────────────────────────────────────────


def test_validate_valid_http():
    _validate_proxy("http://proxy.com:8080")  # should not raise


def test_validate_valid_socks5():
    _validate_proxy("socks5://proxy.com:1080")  # should not raise


def test_validate_valid_with_auth():
    _validate_proxy("http://user:pass@proxy.com:8080")  # should not raise


def test_validate_bad_scheme():
    with pytest.raises(ValueError, match="Invalid proxy scheme 'ftp'"):
        _validate_proxy("ftp://host:80")


def test_validate_no_hostname():
    with pytest.raises(ValueError, match="missing hostname"):
        _validate_proxy("http://:8080")


def test_validate_no_port():
    with pytest.raises(ValueError, match="missing port"):
        _validate_proxy("http://host")


# ── _build_fingerprint_args ──────────────────────────────────────────────────

# Use the BrowserManager instance to call the method
_mgr = BrowserManager()


def test_build_args_always_includes_base():
    args = _mgr._build_fingerprint_args({})
    assert "--disable-infobars" in args
    assert "--test-type" in args
    assert "--use-angle=swiftshader" in args


def test_build_args_seed():
    args = _mgr._build_fingerprint_args({"fingerprint_seed": 42})
    assert "--fingerprint=42" in args


def test_build_args_no_seed():
    args = _mgr._build_fingerprint_args({"fingerprint_seed": None})
    assert not any(a.startswith("--fingerprint=") for a in args)


def test_build_args_platform():
    args = _mgr._build_fingerprint_args({"platform": "macos"})
    assert "--fingerprint-platform=macos" in args


def test_build_args_gpu():
    args = _mgr._build_fingerprint_args({
        "gpu_vendor": "NVIDIA Corporation",
        "gpu_renderer": "NVIDIA GeForce RTX 3070",
    })
    assert "--fingerprint-gpu-vendor=NVIDIA Corporation" in args
    assert "--fingerprint-gpu-renderer=NVIDIA GeForce RTX 3070" in args


def test_build_args_hardware_concurrency():
    args = _mgr._build_fingerprint_args({"hardware_concurrency": 8})
    assert "--fingerprint-hardware-concurrency=8" in args


def test_build_args_screen():
    args = _mgr._build_fingerprint_args({"screen_width": 2560, "screen_height": 1440})
    assert "--fingerprint-screen-width=2560" in args
    assert "--fingerprint-screen-height=1440" in args


def test_build_args_empty_profile():
    args = _mgr._build_fingerprint_args({})
    # Only the 3 base args
    assert len(args) == 3


# ── launch_args appended to extra_args ────────────────────────────────────────


def test_launch_args_appended_to_fingerprint_args():
    """launch_args from profile should appear in the args list after fingerprint args."""
    profile = {
        "fingerprint_seed": 42,
        "platform": "windows",
        "launch_args": ["--load-extension=/tmp/ext", "--disable-features=Foo"],
    }
    args = _mgr._build_fingerprint_args(profile)
    args += profile.get("launch_args") or []
    assert "--load-extension=/tmp/ext" in args
    assert "--disable-features=Foo" in args
    # Fingerprint args still present
    assert "--fingerprint=42" in args


def test_launch_args_empty_no_effect():
    profile = {"launch_args": []}
    args = _mgr._build_fingerprint_args(profile)
    base_count = len(args)
    args += profile.get("launch_args") or []
    assert len(args) == base_count


def test_launch_args_none_no_effect():
    profile = {"launch_args": None}
    args = _mgr._build_fingerprint_args(profile)
    base_count = len(args)
    args += profile.get("launch_args") or []
    assert len(args) == base_count


# ── VNC browser window bounds ─────────────────────────────────────────────────


@pytest.mark.anyio
async def test_fit_window_to_vnc_moves_oversized_window_back_inside_framebuffer():
    mgr = BrowserManager()
    page = MagicMock()
    session = MagicMock()
    session.send = AsyncMock(
        side_effect=[
            {
                "windowId": 123,
                "bounds": {
                    "left": 10,
                    "top": 10,
                    "width": 1928,
                    "height": 1078,
                    "windowState": "normal",
                },
            },
            {
                "windowId": 123,
                "bounds": {
                    "left": 0,
                    "top": 0,
                    "width": 1919,
                    "height": 1079,
                    "windowState": "normal",
                },
            },
        ]
    )
    context = MagicMock()
    context.pages = [page]
    context.new_cdp_session = AsyncMock(return_value=session)

    await mgr._fit_window_to_vnc(context, width=1920, height=1080)

    context.new_cdp_session.assert_awaited_once_with(page)
    session.send.assert_any_await("Browser.getWindowForTarget")
    session.send.assert_any_await(
        "Browser.setWindowBounds",
        {
            "windowId": 123,
            "bounds": {"left": 0, "top": 0, "width": 1920, "height": 1080},
        },
    )


# ── _allocate_cdp_port ───────────────────────────────────────────────────────


def test_allocate_cdp_port_returns_free_port():
    mgr = BrowserManager()
    port = mgr._allocate_cdp_port()
    assert BASE_CDP_PORT <= port < BASE_CDP_PORT + CDP_PORT_RANGE


def test_allocate_cdp_port_skips_occupied():
    mgr = BrowserManager()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("127.0.0.1", BASE_CDP_PORT))
        blocker.listen(1)
        port = mgr._allocate_cdp_port()
        assert port == BASE_CDP_PORT + 1


def test_allocate_cdp_port_advances_counter():
    mgr = BrowserManager()
    p1 = mgr._allocate_cdp_port()
    p2 = mgr._allocate_cdp_port()
    assert p2 == p1 + 1


def test_allocate_cdp_port_wraps_around():
    mgr = BrowserManager()
    mgr._next_cdp_port = BASE_CDP_PORT + CDP_PORT_RANGE - 1
    p1 = mgr._allocate_cdp_port()
    assert p1 == BASE_CDP_PORT + CDP_PORT_RANGE - 1
    p2 = mgr._allocate_cdp_port()
    assert p2 == BASE_CDP_PORT


def test_allocate_cdp_port_all_occupied_raises():
    mgr = BrowserManager()
    blockers = []
    try:
        for i in range(CDP_PORT_RANGE):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", BASE_CDP_PORT + i))
            s.listen(1)
            blockers.append(s)
        with pytest.raises(ValueError, match="No free CDP ports"):
            mgr._allocate_cdp_port()
    finally:
        for s in blockers:
            s.close()


# ── _init_profile_defaults ───────────────────────────────────────────────────


def test_init_creates_bookmarks(tmp_path: Path):
    _init_profile_defaults(tmp_path)
    bookmarks_path = tmp_path / "Default" / "Bookmarks"
    assert bookmarks_path.exists()
    data = json.loads(bookmarks_path.read_text())
    children = data["roots"]["bookmark_bar"]["children"]
    assert len(children) == 4  # 4 folders
    folder_names = {f["name"] for f in children}
    assert folder_names == {"Detection Tests", "Fingerprint", "Headers & TLS", "reCAPTCHA"}


def test_init_creates_preferences(tmp_path: Path):
    _init_profile_defaults(tmp_path)
    prefs_path = tmp_path / "Default" / "Preferences"
    assert prefs_path.exists()
    data = json.loads(prefs_path.read_text())
    assert "default_search_provider_data" in data
    assert "DuckDuckGo" in data["default_search_provider_data"]["template_url_data"]["short_name"]


def test_init_idempotent(tmp_path: Path):
    _init_profile_defaults(tmp_path)
    bookmarks_path = tmp_path / "Default" / "Bookmarks"
    original = bookmarks_path.read_text()

    # Write a sentinel to the file
    bookmarks_path.write_text("SENTINEL")

    # Second call should NOT overwrite (file already exists)
    _init_profile_defaults(tmp_path)
    assert bookmarks_path.read_text() == "SENTINEL"
