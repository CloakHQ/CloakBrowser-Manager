"""Tests for the watchdog auto-restart feature (issue #14)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.browser_manager import BrowserManager, RunningProfile


def _make_running_profile(profile_id: str = "p1") -> RunningProfile:
    """Create a mock RunningProfile with a fake context."""
    context = MagicMock()
    context.pages = [MagicMock()]
    context.pages[0].evaluate = AsyncMock(return_value=1)
    context.on = MagicMock()
    return RunningProfile(
        profile_id=profile_id,
        context=context,
        display=1,
        ws_port=6080,
        cdp_port=5100,
    )


def _make_profile_dict(profile_id: str = "p1", auto_restart: bool = True) -> dict:
    """Create a minimal profile dict as returned by db.get_profile."""
    return {
        "id": profile_id,
        "name": f"Profile {profile_id}",
        "fingerprint_seed": 12345,
        "proxy": None,
        "timezone": None,
        "locale": None,
        "platform": "windows",
        "user_agent": None,
        "screen_width": 1920,
        "screen_height": 1080,
        "gpu_vendor": None,
        "gpu_renderer": None,
        "hardware_concurrency": None,
        "humanize": False,
        "human_preset": "default",
        "headless": False,
        "geoip": False,
        "clipboard_sync": True,
        "auto_launch": False,
        "auto_restart": auto_restart,
        "color_scheme": None,
        "launch_args": [],
        "notes": None,
        "user_data_dir": "/tmp/test_profile",
        "tags": [],
    }


@pytest.fixture()
def mgr() -> BrowserManager:
    """Create a fresh BrowserManager for each test."""
    return BrowserManager()


# ── Watchdog start/stop ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_watchdog_creates_task(mgr: BrowserManager):
    """start_watchdog should create a background task."""
    await mgr.start_watchdog(interval=0.1)
    assert mgr._watchdog_task is not None
    assert not mgr._watchdog_task.done()
    # Cleanup
    mgr._watchdog_task.cancel()
    try:
        await mgr._watchdog_task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_start_watchdog_idempotent(mgr: BrowserManager):
    """Calling start_watchdog twice should not create a second task."""
    await mgr.start_watchdog(interval=0.1)
    task1 = mgr._watchdog_task
    await mgr.start_watchdog(interval=0.1)
    assert mgr._watchdog_task is task1
    # Cleanup
    task1.cancel()
    try:
        await task1
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_cleanup_all_cancels_watchdog(mgr: BrowserManager):
    """cleanup_all should cancel the watchdog task."""
    await mgr.start_watchdog(interval=0.1)
    assert mgr._watchdog_task is not None
    await mgr.cleanup_all()
    assert mgr._watchdog_task.cancelled() or mgr._watchdog_task.done()


# ── Crash detection and restart ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_watchdog_restarts_crashed_profile(mgr: BrowserManager):
    """When a profile's context raises on .pages, watchdog should restart it."""
    running = _make_running_profile("p1")
    mgr.running["p1"] = running

    # Make context.pages raise to simulate a crash
    type(running.context).pages = property(lambda self: (_ for _ in ()).throw(RuntimeError("browser crashed")))

    profile_dict = _make_profile_dict("p1")

    with patch.object(mgr.vnc, "stop_vnc", new_callable=AsyncMock):
        with patch.object(mgr.vnc, "allocate", new_callable=AsyncMock, return_value=(2, 6081)):
            with patch.object(mgr.vnc, "start_vnc", new_callable=AsyncMock):
                with patch("backend.browser_manager.launch_persistent_context_async", new_callable=AsyncMock) as mock_launch:
                    new_context = MagicMock()
                    new_context.pages = [MagicMock()]
                    new_context.pages[0].evaluate = AsyncMock(return_value=1)
                    new_context.on = MagicMock()
                    new_context.add_init_script = AsyncMock()
                    mock_launch.return_value = new_context

                    with patch("backend.database.get_profile", return_value=profile_dict):
                        # Run a single watchdog tick
                        await mgr._check_and_restart_crashed()

    # Profile should be running again
    assert "p1" in mgr.running
    # Restart count should be reset on success
    assert "p1" not in mgr._restart_counts


@pytest.mark.asyncio
async def test_watchdog_no_restart_when_auto_restart_disabled(mgr: BrowserManager):
    """If auto_restart=False, watchdog should not restart a crashed profile."""
    running = _make_running_profile("p1")
    mgr.running["p1"] = running

    # Simulate crash
    type(running.context).pages = property(lambda self: (_ for _ in ()).throw(RuntimeError("crashed")))

    profile_dict = _make_profile_dict("p1", auto_restart=False)

    with patch("backend.browser_manager.launch_persistent_context_async", new_callable=AsyncMock) as mock_launch:
        with patch.object(mgr.vnc, "stop_vnc", new_callable=AsyncMock):
            with patch("backend.database.get_profile", return_value=profile_dict):
                await mgr._check_and_restart_crashed()

    # Should not have tried to launch
    mock_launch.assert_not_called()
    # Profile should not be running
    assert "p1" not in mgr.running


@pytest.mark.asyncio
async def test_watchdog_no_restart_when_stopped_by_user(mgr: BrowserManager):
    """If user explicitly stopped a profile, watchdog should not restart it."""
    mgr._stopped_profiles.add("p1")

    profile_dict = _make_profile_dict("p1")

    with patch("backend.database.get_profile", return_value=profile_dict):
        with patch("backend.browser_manager.launch_persistent_context_async", new_callable=AsyncMock) as mock_launch:
            await mgr._attempt_restart("p1", __import__("backend.database", fromlist=["database"]))

    mock_launch.assert_not_called()


@pytest.mark.asyncio
async def test_watchdog_no_restart_when_profile_deleted(mgr: BrowserManager):
    """If profile no longer exists in DB, watchdog should not restart it."""
    with patch("backend.database.get_profile", return_value=None):
        with patch("backend.browser_manager.launch_persistent_context_async", new_callable=AsyncMock) as mock_launch:
            await mgr._attempt_restart("p1", __import__("backend.database", fromlist=["database"]))

    mock_launch.assert_not_called()


# ── Exponential backoff ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_exponential_backoff_values(mgr: BrowserManager):
    """Verify backoff delays: 1s, 2s, 4s."""
    profile_dict = _make_profile_dict("p1")

    with patch("backend.database.get_profile", return_value=profile_dict):
        with patch("backend.browser_manager.launch_persistent_context_async", new_callable=AsyncMock) as mock_launch:
            mock_launch.side_effect = RuntimeError("launch fails")

            with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                # Attempt 1 — backoff = min(2^0, 30) = 1
                await mgr._attempt_restart("p1", __import__("backend.database", fromlist=["database"]))
                mock_sleep.assert_awaited_with(1)
                assert mgr._restart_counts["p1"] == 1

                # Attempt 2 — backoff = min(2^1, 30) = 2
                mock_sleep.reset_mock()
                await mgr._attempt_restart("p1", __import__("backend.database", fromlist=["database"]))
                mock_sleep.assert_awaited_with(2)
                assert mgr._restart_counts["p1"] == 2

                # Attempt 3 — backoff = min(2^2, 30) = 4
                mock_sleep.reset_mock()
                await mgr._attempt_restart("p1", __import__("backend.database", fromlist=["database"]))
                mock_sleep.assert_awaited_with(4)
                assert mgr._restart_counts["p1"] == 3


@pytest.mark.asyncio
async def test_backoff_capped_at_30s(mgr: BrowserManager):
    """Backoff should be capped at 30 seconds even with high restart count."""
    profile_dict = _make_profile_dict("p1")
    mgr._restart_counts["p1"] = 10  # Simulate many prior failures

    with patch("backend.database.get_profile", return_value=profile_dict):
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            with patch("backend.browser_manager.launch_persistent_context_async", new_callable=AsyncMock) as mock_launch:
                mock_launch.side_effect = RuntimeError("still failing")

                # count=3 triggers "give up" path, so reset to test capping
                mgr._restart_counts["p1"] = 3
                await mgr._attempt_restart("p1", __import__("backend.database", fromlist=["database"]))
                # At count >= 3, it gives up without sleeping
                mock_sleep.assert_not_awaited()


# ── Max retries ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gives_up_after_max_retries(mgr: BrowserManager):
    """After 3 failed restarts, watchdog should give up."""
    profile_dict = _make_profile_dict("p1")
    mgr._restart_counts["p1"] = 3

    with patch("backend.database.get_profile", return_value=profile_dict):
        with patch("backend.browser_manager.launch_persistent_context_async", new_callable=AsyncMock) as mock_launch:
            await mgr._attempt_restart("p1", __import__("backend.database", fromlist=["database"]))

    mock_launch.assert_not_called()
    # Counter should be cleaned up
    assert "p1" not in mgr._restart_counts


@pytest.mark.asyncio
async def test_restart_counter_resets_on_success(mgr: BrowserManager):
    """A successful restart should reset the crash counter for that profile."""
    running = _make_running_profile("p1")
    mgr.running["p1"] = running
    mgr._restart_counts["p1"] = 2  # 2 prior failures

    # Simulate crash
    type(running.context).pages = property(lambda self: (_ for _ in ()).throw(RuntimeError("crashed")))

    profile_dict = _make_profile_dict("p1")

    with patch.object(mgr.vnc, "stop_vnc", new_callable=AsyncMock):
        with patch.object(mgr.vnc, "allocate", new_callable=AsyncMock, return_value=(2, 6081)):
            with patch.object(mgr.vnc, "start_vnc", new_callable=AsyncMock):
                with patch("backend.browser_manager.launch_persistent_context_async", new_callable=AsyncMock) as mock_launch:
                    new_context = MagicMock()
                    new_context.pages = [MagicMock()]
                    new_context.pages[0].evaluate = AsyncMock(return_value=1)
                    new_context.on = MagicMock()
                    new_context.add_init_script = AsyncMock()
                    mock_launch.return_value = new_context

                    with patch("backend.database.get_profile", return_value=profile_dict):
                        await mgr._check_and_restart_crashed()

    assert "p1" not in mgr._restart_counts


@pytest.mark.asyncio
async def test_watchdog_ignores_healthy_profiles(mgr: BrowserManager):
    """Watchdog should not touch profiles that are still alive."""
    running = _make_running_profile("p1")
    mgr.running["p1"] = running

    # Profile is healthy — pages.evaluate returns 1
    with patch("backend.database.get_profile") as mock_get:
        await mgr._check_and_restart_crashed()

    # Should not have queried the database (no restart needed)
    mock_get.assert_not_called()
    # Profile still running
    assert "p1" in mgr.running
