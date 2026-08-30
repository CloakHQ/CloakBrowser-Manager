"""Tests for VNCManager — allocation logic and get_ws_port."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, mock_open

import pytest

from backend.runtime import RuntimeConfig
from backend.vnc_manager import VNCInstance, VNCManager

DOCKER_RUNTIME = RuntimeConfig("linux", "docker", "vnc", Path("/data"))


@pytest.fixture()
def vnc() -> VNCManager:
    return VNCManager()


# ── allocate ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_allocate_rejects_native_runtime():
    manager = VNCManager(enabled=False)
    with pytest.raises(RuntimeError, match="Linux Docker runtime"):
        await manager.allocate()


def test_docker_runtime_requires_kasmvnc(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("backend.vnc_manager.shutil.which", lambda _: None)
    with pytest.raises(RuntimeError, match="Docker image with KasmVNC"):
        VNCManager(enabled=True).validate_available()


def test_native_runtime_does_not_require_kasmvnc(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("backend.vnc_manager.shutil.which", lambda _: None)
    VNCManager(enabled=False).validate_available()


@pytest.mark.asyncio
async def test_allocate_first(vnc: VNCManager):
    display, ws_port = await vnc.allocate()
    assert display == 100
    assert ws_port == 6100


@pytest.mark.asyncio
async def test_allocate_sequential(vnc: VNCManager):
    d1, p1 = await vnc.allocate()
    d2, p2 = await vnc.allocate()
    d3, p3 = await vnc.allocate()
    assert (d1, d2, d3) == (100, 101, 102)
    assert (p1, p2, p3) == (6100, 6101, 6102)


@pytest.mark.asyncio
async def test_allocate_fills_gap(vnc: VNCManager):
    """After freeing display 100, next allocate should reuse it."""
    await vnc.allocate()  # 100
    await vnc.allocate()  # 101
    # Simulate freeing display 100 (like stop_vnc would)
    vnc._allocated.pop(100)
    d, p = await vnc.allocate()
    assert d == 100  # gap filled
    assert p == 6100


@pytest.mark.asyncio
async def test_allocate_tracks_instances(vnc: VNCManager):
    await vnc.allocate()
    await vnc.allocate()
    assert len(vnc._allocated) == 2
    assert 100 in vnc._allocated
    assert 101 in vnc._allocated


@pytest.mark.asyncio
async def test_allocate_instance_fields(vnc: VNCManager):
    await vnc.allocate()
    instance = vnc._allocated[100]
    assert isinstance(instance, VNCInstance)
    assert instance.display == 100
    assert instance.ws_port == 6100
    assert instance.process is None  # not started yet


# ── start_vnc ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_vnc_uses_loopback_public_ip(monkeypatch: pytest.MonkeyPatch):
    process = MagicMock()
    process.poll.return_value = None
    popen = MagicMock(return_value=process)
    monkeypatch.setattr("backend.vnc_manager.shutil.which", lambda _: "/usr/bin/Xvnc")
    monkeypatch.setattr("backend.vnc_manager.subprocess.Popen", popen)
    monkeypatch.setattr("backend.vnc_manager.asyncio.sleep", AsyncMock())
    monkeypatch.setattr("builtins.open", mock_open())

    await VNCManager().start_vnc(display=100, ws_port=6100)

    command = popen.call_args.args[0]
    public_ip_index = command.index("-publicIP")
    assert command[public_ip_index + 1] == "127.0.0.1"


# ── get_ws_port ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_ws_port_allocated(vnc: VNCManager):
    await vnc.allocate()
    assert vnc.get_ws_port(100) == 6100


def test_get_ws_port_not_allocated(vnc: VNCManager):
    assert vnc.get_ws_port(999) is None


# ── active_displays ──────────────────────────────────────────────────────────


def test_active_displays_empty(vnc: VNCManager):
    assert vnc.active_displays == []


@pytest.mark.asyncio
async def test_active_displays_after_allocate(vnc: VNCManager):
    await vnc.allocate()
    await vnc.allocate()
    assert sorted(vnc.active_displays) == [100, 101]


# ── BrowserManager.get_status ────────────────────────────────────────────────


def test_get_status_stopped():
    from backend.browser_manager import BrowserManager
    mgr = BrowserManager(DOCKER_RUNTIME)
    status = mgr.get_status("nonexistent")
    assert status == {
        "status": "stopped",
        "runtime_mode": "docker",
        "viewer_mode": "vnc",
        "vnc_ws_port": None,
        "display": None,
        "cdp_url": None,
        "last_error": None,
    }


def test_get_status_running():
    from backend.browser_manager import BrowserManager, RunningProfile
    from unittest.mock import MagicMock
    mgr = BrowserManager(DOCKER_RUNTIME)
    mgr.running["abc"] = RunningProfile(
        profile_id="abc",
        context=MagicMock(),
        display=100,
        ws_port=6100,
        cdp_port=5100,
    )
    status = mgr.get_status("abc")
    assert status == {
        "status": "running",
        "runtime_mode": "docker",
        "viewer_mode": "vnc",
        "vnc_ws_port": 6100,
        "display": ":100",
        "cdp_url": "/api/profiles/abc/cdp",
        "last_error": None,
    }
