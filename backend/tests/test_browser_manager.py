"""Tests for browser_manager pure functions — proxy parsing, fingerprint args, profile defaults."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.browser_manager import (
    _init_profile_defaults,
    _normalize_proxy,
    _validate_proxy,
    BrowserManager,
    ProfileBusyError,
    RunningProfile,
    SEARCH_ENGINE_MARKER,
)
from backend.runtime import RuntimeConfig

DOCKER_RUNTIME = RuntimeConfig(
    host_os="linux",
    runtime_mode="docker",
    viewer_mode="vnc",
    data_dir=Path("/data"),
)
NATIVE_RUNTIME = RuntimeConfig(
    host_os="windows",
    runtime_mode="native",
    viewer_mode="native-window",
    data_dir=Path("C:/manager-data"),
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
_mgr = BrowserManager(DOCKER_RUNTIME)


def test_build_args_uses_only_current_managed_flags():
    args = _mgr._build_fingerprint_args({})
    assert "--disable-infobars" not in args
    assert "--test-type" not in args
    assert "--use-angle=swiftshader" in args


def test_build_args_seed():
    args = _mgr._build_fingerprint_args({"fingerprint_seed": 42})
    assert "--fingerprint=42" in args


def test_build_args_no_seed():
    args = _mgr._build_fingerprint_args({"fingerprint_seed": None})
    assert not any(a.startswith("--fingerprint=") for a in args)


def test_build_args_platform_comes_from_runtime():
    assert "--fingerprint-platform=windows" in _mgr._build_fingerprint_args({})
    mac_runtime = RuntimeConfig(
        host_os="macos",
        runtime_mode="native",
        viewer_mode="native-window",
        data_dir=Path("/tmp/manager-data"),
    )
    mac_manager = BrowserManager(mac_runtime)
    assert "--fingerprint-platform=macos" in mac_manager._build_fingerprint_args({})
    assert not any(
        "gpu-vendor" in arg
        for arg in mac_manager._build_fingerprint_args({"gpu_family": "nvidia"})
    )


def test_build_args_gpu_family_and_cookie_compatibility():
    nvidia = _mgr._build_fingerprint_args({"gpu_family": "nvidia", "allow_3p_cookies": True})
    assert "--fingerprint-gpu-vendor=NVIDIA" in nvidia
    assert "--fingerprint-allow-3p-cookies" in nvidia
    intel = _mgr._build_fingerprint_args({"gpu_family": "intel"})
    assert "--fingerprint-gpu-vendor=Intel" in intel
    assert not any("gpu-vendor" in arg for arg in _mgr._build_fingerprint_args({"gpu_family": "auto"}))


def test_build_args_screen():
    args = _mgr._build_fingerprint_args({"screen_width": 2560, "screen_height": 1440})
    assert "--fingerprint-screen-width=2560" in args
    assert "--fingerprint-screen-height=1440" in args


def test_build_args_empty_profile():
    args = _mgr._build_fingerprint_args({})
    # Docker software rendering + runtime platform.
    assert len(args) == 2


def test_native_build_args_do_not_force_software_gl():
    args = BrowserManager(NATIVE_RUNTIME)._build_fingerprint_args({})
    assert "--use-angle=swiftshader" not in args


# ── launch_args appended to extra_args ────────────────────────────────────────


def test_launch_args_appended_to_fingerprint_args():
    """launch_args from profile should appear in the args list after fingerprint args."""
    profile = {
        "fingerprint_seed": 42,
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


# ── runtime-specific launch behavior ─────────────────────────────────────────


def _launch_profile(tmp_path: Path) -> dict:
    user_data_dir = tmp_path / "profile-1"
    user_data_dir.mkdir(parents=True, exist_ok=True)
    # Skip the one-time default-search-engine setup (unrelated to launch mechanics;
    # it would otherwise spawn its own real browser launches during these tests).
    (user_data_dir / SEARCH_ENGINE_MARKER).write_text("google\n")
    return {
        "id": "profile-1",
        "name": "Native",
        "user_data_dir": str(user_data_dir),
        "screen_width": 1920,
        "screen_height": 1080,
        "launch_args": [],
    }


@pytest.mark.asyncio
async def test_native_launch_skips_vnc_and_display(monkeypatch, tmp_path: Path):
    from backend import browser_manager as module

    context = MagicMock()
    context.pages = []
    context.add_init_script = AsyncMock()
    manager = BrowserManager(NATIVE_RUNTIME)
    manager.vnc.allocate = AsyncMock()
    manager.vnc.start_vnc = AsyncMock()
    manager._wait_for_cdp = AsyncMock()
    launch = AsyncMock(return_value=context)
    monkeypatch.setattr(module, "launch_persistent_context_async", launch)

    running = await manager.launch(_launch_profile(tmp_path))

    assert running.display is None
    assert running.ws_port is None
    manager.vnc.allocate.assert_not_awaited()
    manager.vnc.start_vnc.assert_not_awaited()
    context.add_init_script.assert_not_awaited()
    options = launch.await_args.kwargs
    assert "env" not in options
    assert "viewport" not in options
    assert "--use-angle=swiftshader" not in options["args"]
    assert "--remote-debugging-address=127.0.0.1" in options["args"]
    assert options["headless"] is False
    assert options["extension_paths"] == []


@pytest.mark.asyncio
async def test_native_close_event_releases_session(monkeypatch, tmp_path: Path):
    from backend import browser_manager as module

    context = MagicMock(pages=[])
    context.add_init_script = AsyncMock()
    manager = BrowserManager(NATIVE_RUNTIME)
    manager._wait_for_cdp = AsyncMock()
    monkeypatch.setattr(
        module,
        "launch_persistent_context_async",
        AsyncMock(return_value=context),
    )
    running = await manager.launch(_launch_profile(tmp_path))
    close_callback = context.on.call_args.args[1]

    await close_callback(context)

    assert "profile-1" not in manager.running
    assert running.cdp_port not in manager._cdp_ports


@pytest.mark.asyncio
async def test_launch_rejects_user_debugging_flags(tmp_path: Path):
    manager = BrowserManager(NATIVE_RUNTIME)
    profile = _launch_profile(tmp_path)
    profile["launch_args"] = ["--remote-debugging-address=0.0.0.0"]

    with pytest.raises(ValueError, match="Manager owns remote debugging"):
        await manager.launch(profile)

    assert "profile-1" not in manager._launching
    assert manager._cdp_ports == set()


@pytest.mark.asyncio
async def test_docker_launch_keeps_vnc_display(monkeypatch, tmp_path: Path, tmp_db):
    from backend import browser_manager as module

    context = MagicMock()
    context.pages = []
    context.add_init_script = AsyncMock()
    manager = BrowserManager(DOCKER_RUNTIME)
    manager.vnc.allocate = AsyncMock(return_value=(100, 6100))
    manager.vnc.start_vnc = AsyncMock()
    manager._wait_for_cdp = AsyncMock()
    launch = AsyncMock(return_value=context)
    monkeypatch.setattr(module, "launch_persistent_context_async", launch)

    running = await manager.launch(_launch_profile(tmp_path))
    background_tasks = [
        task for task in (running.download_task, running.screenshot_task) if task is not None
    ]
    for task in background_tasks:
        task.cancel()
    await asyncio.gather(*background_tasks, return_exceptions=True)

    assert running.display == 100
    assert running.ws_port == 6100
    manager.vnc.start_vnc.assert_awaited_once()
    context.add_init_script.assert_awaited_once()
    options = launch.await_args.kwargs
    assert options["env"]["DISPLAY"] == ":100"
    assert options["viewport"] == {"width": 1920, "height": 947}
    assert "--use-angle=swiftshader" in options["args"]


@pytest.mark.asyncio
async def test_launch_passes_license_config(monkeypatch, tmp_path: Path):
    from backend import browser_manager as module

    context = MagicMock(pages=[])
    context.add_init_script = AsyncMock()
    manager = BrowserManager(
        NATIVE_RUNTIME, license_key="cb_test", release_channel="preview"
    )
    manager._wait_for_cdp = AsyncMock()
    launch = AsyncMock(return_value=context)
    monkeypatch.setattr(module, "launch_persistent_context_async", launch)

    profile = _launch_profile(tmp_path)
    profile["extension_paths"] = ["/tmp/extension"]
    profile["launch_args"] = ["--raw-flag"]
    await manager.launch(profile)

    options = launch.await_args.kwargs
    assert options["license_key"] == "cb_test"
    assert options["release_channel"] == "preview"
    assert options["extension_paths"] == ["/tmp/extension"]
    assert options["args"].index("--raw-flag") > options["args"].index("--fingerprint-platform=windows")


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_launch_gates_search_engine_on_flag(monkeypatch, tmp_path: Path, enabled: bool):
    from backend import browser_manager as module

    context = MagicMock(pages=[])
    context.add_init_script = AsyncMock()
    manager = BrowserManager(NATIVE_RUNTIME)
    manager._wait_for_cdp = AsyncMock()
    manager._ensure_search_engine = AsyncMock()
    monkeypatch.setattr(
        module, "launch_persistent_context_async", AsyncMock(return_value=context)
    )

    profile = _launch_profile(tmp_path)
    profile["set_google_default"] = enabled
    await manager.launch(profile)

    if enabled:
        manager._ensure_search_engine.assert_awaited_once()
    else:
        manager._ensure_search_engine.assert_not_awaited()


@pytest.mark.asyncio
async def test_launch_retries_failed_cdp_and_closes_first_context(
    monkeypatch,
    tmp_path: Path,
):
    from backend import browser_manager as module

    first_context = MagicMock(pages=[])
    first_context.close = AsyncMock()
    second_context = MagicMock(pages=[])
    second_context.add_init_script = AsyncMock()
    second_context.close = AsyncMock()
    manager = BrowserManager(NATIVE_RUNTIME)
    manager._wait_for_cdp = AsyncMock(side_effect=[TimeoutError("busy"), None])
    launch = AsyncMock(side_effect=[first_context, second_context])
    monkeypatch.setattr(module, "launch_persistent_context_async", launch)

    running = await manager.launch(_launch_profile(tmp_path))

    assert launch.await_count == 2
    first_context.close.assert_awaited_once()
    assert running.cdp_port in manager._cdp_ports


@pytest.mark.asyncio
async def test_failed_launch_stays_active_until_its_context_is_closed(monkeypatch, tmp_path: Path):
    """A launch that fails AFTER the browser is up must keep the profile active
    while the half-launched context is being closed — the failure path used to
    drop _launching before awaiting the close, leaving a window in which
    hold_stopped() (duplicate / reset / delete) would touch a live profile dir."""
    from backend import browser_manager as module

    context = MagicMock(pages=[])
    context.on = MagicMock(side_effect=RuntimeError("post-startup wiring failed"))
    closing, finish = asyncio.Event(), asyncio.Event()

    async def blocked_close(*_args):
        closing.set()
        await finish.wait()

    manager = BrowserManager(NATIVE_RUNTIME)
    manager._wait_for_cdp = AsyncMock()
    manager._ensure_search_engine = AsyncMock()
    monkeypatch.setattr(manager, "_close_context", blocked_close)
    monkeypatch.setattr(module, "launch_persistent_context_async", AsyncMock(return_value=context))
    profile = _launch_profile(tmp_path)
    pid = profile["id"]

    task = asyncio.create_task(manager.launch(profile))
    await asyncio.wait_for(closing.wait(), 2)
    try:
        assert pid not in manager._launching  # the exact window: cleanup pending
        assert manager.is_active(pid)
        with pytest.raises(ProfileBusyError):
            async with manager.hold_stopped(pid):
                pass
    finally:
        finish.set()
    with pytest.raises(RuntimeError, match="post-startup wiring failed"):
        await task

    # Once cleanup has finished the profile is genuinely stopped again
    assert not manager.is_active(pid)
    async with manager.hold_stopped(pid):
        pass


@pytest.mark.asyncio
async def test_launch_is_refused_while_the_previous_browser_is_still_closing(monkeypatch):
    """A relaunch during shutdown used to be admitted, and if it then failed its
    cleanup cleared the shutdown's `_stopping` entry — leaving the still-closing
    profile inactive to hold_stopped(). Now the relaunch is refused outright."""
    manager = BrowserManager(DOCKER_RUNTIME)
    closing, finish = asyncio.Event(), asyncio.Event()

    async def blocked_close(*_args):
        closing.set()
        await finish.wait()

    monkeypatch.setattr(manager, "_close_context", blocked_close)
    manager.vnc.allocate = AsyncMock(return_value=(101, 6101))
    manager.vnc.start_vnc = AsyncMock(side_effect=RuntimeError("Xvnc failed to start"))
    manager.vnc.stop_vnc = AsyncMock()
    manager.running["profile-1"] = RunningProfile("profile-1", object(), 19001, capture_preview=False)

    stop = asyncio.create_task(manager.stop("profile-1"))
    await asyncio.wait_for(closing.wait(), 2)
    try:
        assert manager.is_active("profile-1")
        with pytest.raises(ProfileBusyError, match="still closing"):
            await manager.launch({"id": "profile-1", "user_data_dir": "/nonexistent"})
        # The refused launch touched nothing: the shutdown reservation is intact
        assert not stop.done()
        assert manager.is_active("profile-1")
        manager.vnc.allocate.assert_not_awaited()
    finally:
        finish.set()
        await stop
    assert not manager.is_active("profile-1")


def test_stopping_reservations_are_counted_per_operation():
    """One operation's cleanup must never release another's reservation."""
    manager = BrowserManager(NATIVE_RUNTIME)
    manager._reserve_stopping("p")
    manager._reserve_stopping("p")
    manager._release_stopping("p")
    assert manager.is_active("p")
    manager._release_stopping("p")
    assert not manager.is_active("p")
    manager._release_stopping("p")  # over-release is harmless
    assert not manager.is_active("p")


@pytest.mark.asyncio
async def test_stop_releases_native_cdp_port():
    manager = BrowserManager(NATIVE_RUNTIME)
    context = MagicMock()
    context.close = AsyncMock()
    port = manager._reserve_cdp_port()
    manager.running["profile-1"] = module_running = RunningProfile(
        "profile-1", context, port
    )

    await manager.stop("profile-1")

    context.close.assert_awaited_once()
    assert module_running.cdp_port not in manager._cdp_ports
    assert "profile-1" not in manager.running


# ── CDP reservation and verification ─────────────────────────────────────────


def test_reserve_cdp_port_tracks_unique_ports():
    manager = BrowserManager(NATIVE_RUNTIME)
    first = manager._reserve_cdp_port()
    second = manager._reserve_cdp_port()
    assert first != second
    assert manager._cdp_ports == {first, second}


def test_release_cdp_port_is_idempotent():
    manager = BrowserManager(NATIVE_RUNTIME)
    port = manager._reserve_cdp_port()
    manager._release_cdp_port(port)
    manager._release_cdp_port(port)
    assert port not in manager._cdp_ports


@pytest.mark.asyncio
async def test_wait_for_cdp_verifies_debugger_port(monkeypatch):
    manager = BrowserManager(NATIVE_RUNTIME)
    fetch = AsyncMock(return_value={
        "webSocketDebuggerUrl": "ws://127.0.0.1:53123/devtools/browser/test",
    })
    monkeypatch.setattr(manager, "_fetch_cdp_version", fetch)
    await manager._wait_for_cdp(53123, timeout=0.1)


@pytest.mark.asyncio
async def test_wait_for_cdp_rejects_wrong_debugger_port(monkeypatch):
    manager = BrowserManager(NATIVE_RUNTIME)
    fetch = AsyncMock(return_value={
        "webSocketDebuggerUrl": "ws://127.0.0.1:53124/devtools/browser/test",
    })
    monkeypatch.setattr(manager, "_fetch_cdp_version", fetch)
    with pytest.raises(TimeoutError, match="was not ready"):
        await manager._wait_for_cdp(53123, timeout=0.01)


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


def test_init_creates_bookmarks_not_preferences(tmp_path: Path):
    _init_profile_defaults(tmp_path)
    # Bookmarks are seeded here.
    assert (tmp_path / "Default" / "Bookmarks").exists()
    # The default search engine is NOT set via Preferences (it can't stick — it
    # lives in MAC-protected Secure Preferences). That is handled once per profile
    # by BrowserManager._ensure_search_engine, not here.
    assert not (tmp_path / "Default" / "Preferences").exists()


def test_init_idempotent(tmp_path: Path):
    _init_profile_defaults(tmp_path)
    bookmarks_path = tmp_path / "Default" / "Bookmarks"
    original = bookmarks_path.read_text()

    # Write a sentinel to the file
    bookmarks_path.write_text("SENTINEL")

    # Second call should NOT overwrite (file already exists)
    _init_profile_defaults(tmp_path)
    assert bookmarks_path.read_text() == "SENTINEL"


@pytest.mark.asyncio
async def test_downloads_are_captured_only_under_docker(monkeypatch, tmp_path: Path):
    """Natively the browser writes to the user's own Downloads folder. Diverting that into
    the Manager's artifact store would take their files away from where they expect them."""
    from backend import browser_manager as module

    context = MagicMock(pages=[])
    context.add_init_script = AsyncMock()
    manager = BrowserManager(NATIVE_RUNTIME)
    manager._wait_for_cdp = AsyncMock()
    manager._ensure_search_engine = AsyncMock()
    monkeypatch.setattr(module, "launch_persistent_context_async", AsyncMock(return_value=context))
    watch = AsyncMock()
    monkeypatch.setattr(module.downloads, "watch", watch)

    running = await manager.launch(_launch_profile(tmp_path))

    assert running.download_task is None
    watch.assert_not_called()


@pytest.mark.asyncio
async def test_downloads_are_captured_under_docker(monkeypatch, tmp_db: Path, tmp_path: Path):
    from backend import browser_manager as module

    context = MagicMock(pages=[])
    context.add_init_script = AsyncMock()
    manager = BrowserManager(DOCKER_RUNTIME)
    manager._wait_for_cdp = AsyncMock()
    manager._ensure_search_engine = AsyncMock()
    manager.vnc.allocate = AsyncMock(return_value=(101, 6101))
    manager.vnc.start_vnc = AsyncMock()
    manager.vnc.stop_vnc = AsyncMock()
    monkeypatch.setattr(module, "launch_persistent_context_async", AsyncMock(return_value=context))
    monkeypatch.setattr(module.downloads, "watch", AsyncMock())

    running = await manager.launch(_launch_profile(tmp_path))
    try:
        assert running.download_task is not None
    finally:
        await manager.stop(running.profile_id)


@pytest.mark.asyncio
async def test_stopping_a_browser_settles_its_pending_downloads(
    monkeypatch, tmp_db: Path, tmp_path: Path
):
    """Reconciling only on the next launch strands the row while the profile sits stopped."""
    from backend import browser_manager as module
    from backend import database as db
    from backend import downloads

    context = MagicMock(pages=[])
    context.add_init_script = AsyncMock()
    manager = BrowserManager(DOCKER_RUNTIME)
    manager._wait_for_cdp = AsyncMock()
    manager._ensure_search_engine = AsyncMock()
    manager.vnc.allocate = AsyncMock(return_value=(101, 6101))
    manager.vnc.start_vnc = AsyncMock()
    manager.vnc.stop_vnc = AsyncMock()
    monkeypatch.setattr(module, "launch_persistent_context_async", AsyncMock(return_value=context))
    monkeypatch.setattr(module.downloads, "watch", AsyncMock())

    profile = db.create_profile(name="Downloader")
    launch_profile = _launch_profile(tmp_path)
    launch_profile["id"] = profile["id"]
    running = await manager.launch(launch_profile)
    artifact_id = downloads.DownloadTracker(profile["id"]).begin("guid-1", "half.csv")
    assert db.get_artifact(profile["id"], artifact_id)["state"] == "pending"

    await manager.stop(running.profile_id)

    assert db.get_artifact(profile["id"], artifact_id)["state"] == "failed"


def test_a_detaching_cdp_client_re_arms_download_capture():
    manager = BrowserManager(DOCKER_RUNTIME)
    running = RunningProfile("p1", MagicMock(), 9222, download_rearm=asyncio.Event())
    manager.running["p1"] = running
    manager.cdp_client_detached(running)
    assert running.download_rearm.is_set()
    # Running without capture (native): nothing to re-arm, nothing raised.
    native = RunningProfile("native", MagicMock(), 9223)
    manager.running["native"] = native
    manager.cdp_client_detached(native)


def test_a_late_detach_notification_does_not_touch_a_relaunched_browser():
    manager = BrowserManager(DOCKER_RUNTIME)
    stale = RunningProfile("p1", MagicMock(), 9222, download_rearm=asyncio.Event())
    current = RunningProfile("p1", MagicMock(), 9224, download_rearm=asyncio.Event())
    manager.running["p1"] = current  # the profile was stopped and launched again
    manager.cdp_client_detached(stale)
    assert not current.download_rearm.is_set()
    assert not stale.download_rearm.is_set()
    manager.running.pop("p1")
    manager.cdp_client_detached(current)  # nor a browser that is gone altogether
    assert not current.download_rearm.is_set()


# ── GeoIP resolved off the event loop ────────────────────────────────────────
# cloakbrowser's async launcher resolves GeoIP with a blocking call on the event
# loop (maybe_resolve_geoip, 20s timeout), once per CDP attempt. The Manager now
# resolves it once, in a thread, and launches with geoip=False plus the result.

_GEO = ("America/New_York", "en-US", "203.0.113.7")


def _geoip_profile(tmp_path: Path) -> dict:
    profile = _launch_profile(tmp_path)
    profile["geoip"] = True
    profile["proxy"] = "http://user:pass@proxy.example:8080"
    return profile


@contextlib.asynccontextmanager
async def _heartbeat():
    """Counts event-loop ticks while the body runs; a blocked loop barely ticks. The task is
    cancelled and awaited on every exit path, so it never outlives the test."""
    ticks = [0]

    async def beat():
        while True:
            await asyncio.sleep(0.02)
            ticks[0] += 1

    task = asyncio.create_task(beat())
    try:
        yield ticks
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _geo_manager(monkeypatch, resolve, launch=None, wait_for_cdp=None):
    from backend import browser_manager as module

    context = MagicMock(pages=[])
    context.add_init_script = AsyncMock()
    launch = launch or AsyncMock(return_value=context)
    manager = BrowserManager(NATIVE_RUNTIME)
    manager._wait_for_cdp = wait_for_cdp or AsyncMock()
    monkeypatch.setattr(module, "launch_persistent_context_async", launch)
    monkeypatch.setattr(module, "maybe_resolve_geoip", resolve)
    return manager, launch


@pytest.mark.asyncio
async def test_geoip_is_resolved_off_the_loop_and_the_library_skips_it(monkeypatch, tmp_path: Path):
    import threading

    calls = []

    def resolve(geoip, proxy, timezone, locale, args):
        calls.append((geoip, proxy, timezone, locale, threading.get_ident()))
        return _GEO

    manager, launch = _geo_manager(monkeypatch, resolve)
    await manager.launch(_geoip_profile(tmp_path))

    assert len(calls) == 1
    geoip, proxy, timezone, locale, thread_id = calls[0]
    assert geoip is True and proxy == "http://user:pass@proxy.example:8080"
    assert (timezone, locale) == (None, None)
    assert thread_id != threading.get_ident()  # not on the event loop's thread
    options = launch.await_args.kwargs
    assert options["geoip"] is False
    assert (options["timezone"], options["locale"]) == ("America/New_York", "en-US")
    assert "--fingerprint-webrtc-ip=203.0.113.7" in options["args"]


@pytest.mark.asyncio
async def test_a_slow_lookup_does_not_freeze_the_event_loop(monkeypatch, tmp_path: Path):
    """The symptom: /api/health (any coroutine) must keep running while GeoIP resolves."""
    import time

    def slow_resolve(*_args):
        time.sleep(0.6)
        return _GEO

    async def blocking_library_launch(**kwargs):
        # The real library blocks the loop when asked to resolve GeoIP itself.
        if kwargs.get("geoip"):
            time.sleep(0.6)
        context = MagicMock(pages=[])
        context.add_init_script = AsyncMock()
        return context

    manager, _ = _geo_manager(monkeypatch, slow_resolve, launch=AsyncMock(side_effect=blocking_library_launch))
    async with _heartbeat() as ticks:
        await manager.launch(_geoip_profile(tmp_path))
    assert ticks[0] >= 6  # ~30 expected over 0.6s at 20ms; a blocked loop gets ~0


@pytest.mark.asyncio
async def test_the_lookup_runs_once_across_cdp_retries(monkeypatch, tmp_path: Path):
    resolve = MagicMock(return_value=_GEO)
    contexts = [MagicMock(pages=[], close=AsyncMock()), MagicMock(pages=[], close=AsyncMock())]
    contexts[1].add_init_script = AsyncMock()
    manager, launch = _geo_manager(
        monkeypatch,
        resolve,
        launch=AsyncMock(side_effect=contexts),
        wait_for_cdp=AsyncMock(side_effect=[TimeoutError("busy"), None]),
    )
    await manager.launch(_geoip_profile(tmp_path))
    assert launch.await_count == 2
    resolve.assert_called_once()
    assert all(call.kwargs["geoip"] is False for call in launch.await_args_list)


@pytest.mark.asyncio
async def test_explicit_timezone_and_locale_reach_the_lookup(monkeypatch, tmp_path: Path):
    resolve = MagicMock(return_value=("Europe/Berlin", "de-DE", "203.0.113.7"))
    manager, launch = _geo_manager(monkeypatch, resolve)
    profile = _geoip_profile(tmp_path)
    profile.update(timezone="Europe/Berlin", locale="de-DE")
    await manager.launch(profile)
    assert resolve.call_args.args[2:4] == ("Europe/Berlin", "de-DE")
    assert launch.await_args.kwargs["timezone"] == "Europe/Berlin"


@pytest.mark.asyncio
async def test_a_user_webrtc_flag_is_not_overridden(monkeypatch, tmp_path: Path):
    resolve = MagicMock(return_value=_GEO)
    manager, launch = _geo_manager(monkeypatch, resolve)
    profile = _geoip_profile(tmp_path)
    profile["launch_args"] = ["--fingerprint-webrtc-ip=198.51.100.9"]
    await manager.launch(profile)
    webrtc = [a for a in launch.await_args.kwargs["args"] if a.startswith("--fingerprint-webrtc-ip")]
    assert webrtc == ["--fingerprint-webrtc-ip=198.51.100.9"]


@pytest.mark.asyncio
async def test_geoip_off_does_no_lookup(monkeypatch, tmp_path: Path):
    resolve = MagicMock(return_value=_GEO)
    manager, launch = _geo_manager(monkeypatch, resolve)
    profile = _geoip_profile(tmp_path)
    profile.update(geoip=False, timezone="America/Chicago", locale="en-US")
    await manager.launch(profile)
    resolve.assert_not_called()
    options = launch.await_args.kwargs
    assert options["geoip"] is False and options["timezone"] == "America/Chicago"
    assert not any(a.startswith("--fingerprint-webrtc-ip") for a in options["args"])


@pytest.mark.asyncio
async def test_a_failed_lookup_fails_the_launch_cleanly(monkeypatch, tmp_path: Path):
    resolve = MagicMock(side_effect=RuntimeError("GeoIP resolution timed out after 20.0s"))
    manager, launch = _geo_manager(monkeypatch, resolve)
    with pytest.raises(RuntimeError, match="GeoIP resolution timed out"):
        await manager.launch(_geoip_profile(tmp_path))
    # Retried as often as the old per-CDP-attempt lookups were, then given up.
    assert resolve.call_count == 3
    launch.assert_not_awaited()
    assert "profile-1" not in manager._launching and "profile-1" not in manager.running


@pytest.mark.asyncio
async def test_a_transient_lookup_failure_is_retried_and_the_launch_succeeds(monkeypatch, tmp_path: Path):
    """Parity with the old behaviour, where each CDP attempt redid the lookup: a proxy that is
    slow for one window and recovers still launches."""
    import threading

    threads = []

    def flaky(*_args):
        threads.append(threading.get_ident())
        if len(threads) == 1:
            raise RuntimeError("GeoIP resolution timed out after 20.0s")
        return _GEO

    manager, launch = _geo_manager(monkeypatch, flaky)
    await manager.launch(_geoip_profile(tmp_path))
    assert len(threads) == 2
    assert all(t != threading.get_ident() for t in threads)  # every try off the loop
    options = launch.await_args.kwargs
    assert options["timezone"] == "America/New_York" and "--fingerprint-webrtc-ip=203.0.113.7" in options["args"]


@pytest.mark.asyncio
async def test_retrying_a_slow_lookup_never_freezes_the_event_loop(monkeypatch, tmp_path: Path):
    import time

    def slow_then_fail(*_args):
        time.sleep(0.25)
        raise RuntimeError("GeoIP resolution timed out after 20.0s")

    manager, _ = _geo_manager(monkeypatch, slow_then_fail)
    async with _heartbeat() as ticks:
        with pytest.raises(RuntimeError):
            await manager.launch(_geoip_profile(tmp_path))
    assert ticks[0] >= 6  # three 0.25s tries = 0.75s: ~37 ticks at 20ms; a blocked loop gets ~0


# ── --fingerprint-webrtc-ip=auto ─────────────────────────────────────────────
# The library resolves `auto` with its own blocking lookup (_resolve_webrtc_args), even with
# geoip=False. The Manager resolves it before launch so the library never sees `auto`.

_AUTO = "--fingerprint-webrtc-ip=auto"


@pytest.mark.asyncio
async def test_webrtc_auto_uses_the_geoip_exit_ip_without_a_second_lookup(monkeypatch, tmp_path: Path):
    from backend import browser_manager as module

    second_lookup = MagicMock()
    monkeypatch.setattr(module, "_resolve_webrtc_args", second_lookup)
    manager, launch = _geo_manager(monkeypatch, MagicMock(return_value=_GEO))
    profile = _geoip_profile(tmp_path)
    profile["launch_args"] = [_AUTO]
    await manager.launch(profile)

    second_lookup.assert_not_called()
    args = launch.await_args.kwargs["args"]
    assert _AUTO not in args
    assert [a for a in args if a.startswith("--fingerprint-webrtc-ip")] == ["--fingerprint-webrtc-ip=203.0.113.7"]


@pytest.mark.asyncio
async def test_webrtc_auto_without_geoip_is_resolved_off_the_loop(monkeypatch, tmp_path: Path):
    import threading
    import time

    from backend import browser_manager as module

    seen = []

    def slow_resolve_webrtc(args, proxy):
        seen.append((proxy, threading.get_ident()))
        time.sleep(0.6)
        return ["--fingerprint-webrtc-ip=198.51.100.4" if a == _AUTO else a for a in args]

    async def blocking_library_launch(**kwargs):
        # The real library blocks the loop when it is handed `auto` to resolve.
        if _AUTO in kwargs["args"]:
            time.sleep(0.6)
        context = MagicMock(pages=[])
        context.add_init_script = AsyncMock()
        return context

    monkeypatch.setattr(module, "_resolve_webrtc_args", slow_resolve_webrtc)
    resolve = MagicMock(return_value=_GEO)
    manager, launch = _geo_manager(monkeypatch, resolve, launch=AsyncMock(side_effect=blocking_library_launch))
    profile = _geoip_profile(tmp_path)
    profile.update(geoip=False, launch_args=[_AUTO])
    async with _heartbeat() as ticks:
        await manager.launch(profile)

    resolve.assert_not_called()
    assert len(seen) == 1 and seen[0][0] == "http://user:pass@proxy.example:8080"
    assert seen[0][1] != threading.get_ident()
    assert ticks[0] >= 6  # ~30 expected at 20ms; a blocked loop gets ~0
    args = launch.await_args.kwargs["args"]
    assert _AUTO not in args and "--fingerprint-webrtc-ip=198.51.100.4" in args


@pytest.mark.asyncio
async def test_a_failed_webrtc_auto_lookup_drops_the_flag_and_still_launches(monkeypatch, tmp_path: Path):
    from backend import browser_manager as module

    # The library's own rule on a failed lookup: remove `auto` rather than fail the launch.
    monkeypatch.setattr(module, "_resolve_webrtc_args", lambda args, proxy: [a for a in args if a != _AUTO])
    manager, launch = _geo_manager(monkeypatch, MagicMock(return_value=_GEO))
    profile = _geoip_profile(tmp_path)
    profile.update(geoip=False, launch_args=[_AUTO])
    await manager.launch(profile)
    assert not any(a.startswith("--fingerprint-webrtc-ip") for a in launch.await_args.kwargs["args"])


@pytest.mark.asyncio
async def test_webrtc_auto_without_a_proxy_defers_to_the_library_rule(monkeypatch, tmp_path: Path):
    from backend import browser_manager as module

    rule = MagicMock(side_effect=lambda args, proxy: [a for a in args if a != _AUTO])
    monkeypatch.setattr(module, "_resolve_webrtc_args", rule)
    manager, launch = _geo_manager(monkeypatch, MagicMock(return_value=_GEO))
    profile = _geoip_profile(tmp_path)
    profile.update(proxy=None, launch_args=[_AUTO])
    await manager.launch(profile)
    # With no proxy the machine's own IP is what sites see; the library just removes `auto`.
    rule.assert_called_once()
    assert _AUTO not in launch.await_args.kwargs["args"]


# The duplicate-flag tests below use conftest's FAITHFUL resolver (not a monkeypatch): the library
# handles only the first `auto`, and the fake launcher resolves any `auto` it is handed exactly as
# the real launcher does, on the event loop. Every lookup records its thread.


@pytest.fixture
def webrtc_resolver():
    import cloakbrowser.browser as stub

    stub.webrtc_lookups.clear()
    stub.webrtc_exit_ip = "198.51.100.4"
    yield stub
    stub.webrtc_lookups.clear()
    stub.webrtc_exit_ip = "198.51.100.4"


def _launcher_that_resolves_auto_on_the_loop(stub):
    async def launch(**kwargs):
        # The real launcher calls the resolver synchronously, i.e. on the event loop's thread.
        kwargs["args"] = stub._resolve_webrtc_args(kwargs["args"], kwargs.get("proxy"))
        context = MagicMock(pages=[])
        context.add_init_script = AsyncMock()
        return context

    return AsyncMock(side_effect=launch)


@pytest.mark.asyncio
@pytest.mark.parametrize("geoip", [False, True])
async def test_repeated_auto_flags_never_reach_the_launcher(monkeypatch, tmp_path: Path, webrtc_resolver, geoip):
    import threading

    launch = _launcher_that_resolves_auto_on_the_loop(webrtc_resolver)
    manager, launch = _geo_manager(monkeypatch, MagicMock(return_value=_GEO), launch=launch)
    profile = _geoip_profile(tmp_path)
    profile.update(geoip=geoip, launch_args=[_AUTO, "--x", _AUTO])
    await manager.launch(profile)

    args = launch.await_args.kwargs["args"]
    assert _AUTO not in args
    webrtc = [a for a in args if a.startswith("--fingerprint-webrtc-ip")]
    assert len(webrtc) == 1  # one flag, whichever branch resolved it
    assert webrtc == ["--fingerprint-webrtc-ip=" + ("203.0.113.7" if geoip else "198.51.100.4")]
    loop_thread = threading.get_ident()
    assert all(thread != loop_thread for _proxy, thread in webrtc_resolver.webrtc_lookups)
    assert len(webrtc_resolver.webrtc_lookups) == (0 if geoip else 1)


@pytest.mark.asyncio
async def test_a_failed_lookup_with_repeated_auto_flags_drops_all_of_them(monkeypatch, tmp_path: Path, webrtc_resolver):
    import threading

    webrtc_resolver.webrtc_exit_ip = None  # the lookup fails: the library drops the flag
    launch = _launcher_that_resolves_auto_on_the_loop(webrtc_resolver)
    manager, launch = _geo_manager(monkeypatch, MagicMock(return_value=_GEO), launch=launch)
    profile = _geoip_profile(tmp_path)
    profile.update(geoip=False, launch_args=[_AUTO, _AUTO])
    await manager.launch(profile)

    assert not any(a.startswith("--fingerprint-webrtc-ip") for a in launch.await_args.kwargs["args"])
    assert [t != threading.get_ident() for _p, t in webrtc_resolver.webrtc_lookups] == [True]



@pytest.mark.asyncio
async def test_every_failed_geoip_attempt_is_logged(monkeypatch, tmp_path: Path, caplog):
    import logging

    resolve = MagicMock(side_effect=RuntimeError("GeoIP resolution timed out after 20.0s"))
    manager, _ = _geo_manager(monkeypatch, resolve)
    with caplog.at_level(logging.WARNING, logger="cloakbrowser.manager.browser"):
        with pytest.raises(RuntimeError):
            await manager.launch(_geoip_profile(tmp_path))
    logged = [r.getMessage() for r in caplog.records if "GeoIP attempt" in r.getMessage()]
    assert [m.split(" failed")[0] for m in logged] == ["GeoIP attempt 1/3", "GeoIP attempt 2/3", "GeoIP attempt 3/3"]


@pytest.mark.asyncio
async def test_cancelling_a_launch_stops_further_geoip_attempts(monkeypatch, tmp_path: Path):
    """Each try runs in its own worker thread, so a cancelled launch starts no further tries (the
    in-flight one finishes in its thread, but nothing is retried after it)."""
    import threading

    started = threading.Event()
    calls = []

    def slow_failing(*_args):
        calls.append(1)
        started.set()
        time.sleep(0.3)
        raise RuntimeError("GeoIP resolution timed out after 20.0s")

    manager, launch = _geo_manager(monkeypatch, slow_failing)
    task = asyncio.create_task(manager.launch(_geoip_profile(tmp_path)))
    await asyncio.to_thread(started.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.8)  # long enough for tries 2 and 3 to have started if anything retried
    assert len(calls) == 1
    launch.assert_not_awaited()
    assert "profile-1" not in manager._launching
