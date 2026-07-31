"""Tests for VNCManager — allocation logic, quality presets, hw3d, get_ws_port."""

from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from backend import vnc_manager
from backend.vnc_manager import VNCInstance, VNCManager


@pytest.fixture()
def vnc() -> VNCManager:
    return VNCManager()


# ── allocate ─────────────────────────────────────────────────────────────────


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
    mgr = BrowserManager()
    assert mgr.get_status("nonexistent") == {
        "status": "stopped",
        "vnc_ws_port": None,
        "display": None,
        "cdp_url": None,
    }


def test_get_status_running():
    from backend.browser_manager import BrowserManager, RunningProfile
    from unittest.mock import MagicMock
    mgr = BrowserManager()
    mgr.running["abc"] = RunningProfile(
        profile_id="abc",
        context=MagicMock(),
        display=100,
        ws_port=6100,
        cdp_port=5100,
    )
    assert mgr.get_status("abc") == {
        "status": "running",
        "vnc_ws_port": 6100,
        "display": ":100",
        "cdp_url": "/api/profiles/abc/cdp",
    }


def test_get_status_does_not_probe_processes():
    """The 3s profile-list poll must not pay for liveness it discards."""
    from backend.browser_manager import BrowserManager, RunningProfile
    from unittest.mock import MagicMock, patch
    mgr = BrowserManager()
    mgr.running["abc"] = RunningProfile(
        profile_id="abc", context=MagicMock(), display=100, ws_port=6100, cdp_port=5100,
    )
    with patch("backend.browser_manager.socket.socket") as sock:
        mgr.get_status("abc")
        sock.assert_not_called()


def test_get_liveness_stopped():
    from backend.browser_manager import BrowserManager
    mgr = BrowserManager()
    assert mgr.get_liveness("nonexistent") == {
        "status": "stopped",
        "vnc_ws_port": None,
        "display": None,
        "cdp_url": None,
        "xvnc_alive": None,
        "browser_alive": None,
    }


def test_browser_alive_probes_the_cdp_port():
    """A live CDP listener means the Chromium process is up."""
    import socket as socket_mod
    from backend.browser_manager import BrowserManager, RunningProfile
    from unittest.mock import MagicMock

    listener = socket_mod.socket(socket_mod.AF_INET, socket_mod.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        mgr = BrowserManager()
        mgr.running["abc"] = RunningProfile(
            profile_id="abc", context=MagicMock(), display=100, ws_port=6100, cdp_port=port,
        )
        assert mgr.get_liveness("abc")["browser_alive"] is True
    finally:
        listener.close()

    # port gone -> the browser is gone. context.pages could never report this:
    # Playwright implements it as a local list copy that cannot raise.
    assert mgr.get_liveness("abc")["browser_alive"] is False


# ── quality presets ──────────────────────────────────────────────────────────


def _flag_value(flags: list[str], name: str) -> str:
    return flags[flags.index(name) + 1]


def test_preset_default_balanced(monkeypatch):
    monkeypatch.delenv("KASM_QUALITY_PRESET", raising=False)
    assert vnc_manager._quality_preset_name() == "balanced"


@pytest.mark.parametrize("name", ["text", "balanced", "low", "motion"])
def test_preset_from_env(monkeypatch, name: str):
    monkeypatch.setenv("KASM_QUALITY_PRESET", name)
    assert vnc_manager._quality_preset_name() == name
    assert vnc_manager._quality_flags(name) == vnc_manager.QUALITY_PRESETS[name]


def test_preset_unknown_falls_back(monkeypatch, caplog):
    monkeypatch.setenv("KASM_QUALITY_PRESET", "ultra")
    with caplog.at_level("WARNING", logger="cloakbrowser.manager.vnc"):
        assert vnc_manager._quality_preset_name() == "balanced"
    assert "Unknown KASM_QUALITY_PRESET" in caplog.text


def test_preset_values():
    """Spot-check the brief's balanced values (man-page verified spellings)."""
    flags = vnc_manager.QUALITY_PRESETS["balanced"]
    assert _flag_value(flags, "-FrameRate") == "30"
    assert _flag_value(flags, "-DynamicQualityMin") == "6"
    assert _flag_value(flags, "-DynamicQualityMax") == "8"
    assert _flag_value(flags, "-TreatLossless") == "8"
    assert _flag_value(flags, "-JpegVideoQuality") == "5"
    assert _flag_value(flags, "-WebpVideoQuality") == "5"
    assert _flag_value(flags, "-MaxVideoResolution") == "1600x900"
    assert _flag_value(flags, "-VideoTime") == "1"
    assert _flag_value(flags, "-VideoArea") == "30"
    assert _flag_value(flags, "-VideoOutTime") == "1"
    assert _flag_value(flags, "-VideoScaling") == "2"
    assert _flag_value(flags, "-webpEncodingTime") == "30"
    assert _flag_value(flags, "-CompareFB") == "2"
    assert _flag_value(flags, "-RectThreads") == "2"


def test_rect_threads_override(monkeypatch):
    monkeypatch.setenv("KASM_RECT_THREADS", "4")
    flags = vnc_manager._quality_flags("balanced")
    assert _flag_value(flags, "-RectThreads") == "4"


def test_rect_threads_invalid_keeps_default(monkeypatch, caplog):
    monkeypatch.setenv("KASM_RECT_THREADS", "lots")
    with caplog.at_level("WARNING", logger="cloakbrowser.manager.vnc"):
        flags = vnc_manager._quality_flags("balanced")
    assert _flag_value(flags, "-RectThreads") == "2"
    assert "Invalid KASM_RECT_THREADS" in caplog.text


def test_rect_threads_zero_warns(monkeypatch, caplog):
    monkeypatch.setenv("KASM_RECT_THREADS", "0")
    with caplog.at_level("WARNING", logger="cloakbrowser.manager.vnc"):
        flags = vnc_manager._quality_flags("balanced")
    assert _flag_value(flags, "-RectThreads") == "0"
    assert "unbounded" in caplog.text


# ── hw3d (DRI3) detection ────────────────────────────────────────────────────


@pytest.fixture()
def dri_present(monkeypatch):
    """Pretend /dev/dri/<node> exists; driver resolution per test."""
    real_exists = os.path.exists
    monkeypatch.setattr(
        vnc_manager.os.path, "exists",
        lambda p: p.startswith("/dev/dri/") or real_exists(p),
    )


def _fake_driver(monkeypatch, driver: str | None):
    def fake_readlink(path):
        if path.startswith("/sys/class/drm/"):
            if driver is None:
                raise OSError("no such device")
            return f"../../../../bus/pci/drivers/{driver}"
        return os.readlink(path)

    monkeypatch.setattr(vnc_manager.os, "readlink", fake_readlink)


def test_hw3d_disabled_by_env(monkeypatch, dri_present):
    monkeypatch.setenv("KASM_HW3D", "0")
    assert vnc_manager._hw3d_flags() == []


def test_hw3d_auto_no_device(monkeypatch):
    monkeypatch.delenv("KASM_HW3D", raising=False)
    monkeypatch.setattr(vnc_manager.os.path, "exists", lambda p: False)
    assert vnc_manager._hw3d_flags() == []


def test_hw3d_auto_non_nvidia(monkeypatch, dri_present):
    monkeypatch.delenv("KASM_HW3D", raising=False)
    monkeypatch.delenv("KASM_DRINODE", raising=False)
    _fake_driver(monkeypatch, "amdgpu")
    assert vnc_manager._hw3d_flags() == ["-hw3d", "-drinode", "/dev/dri/renderD128"]


def test_hw3d_auto_nvidia_skipped(monkeypatch, dri_present, caplog):
    monkeypatch.delenv("KASM_HW3D", raising=False)
    _fake_driver(monkeypatch, "nvidia")
    with caplog.at_level("INFO", logger="cloakbrowser.manager.vnc"):
        assert vnc_manager._hw3d_flags() == []
    assert "nvidia" in caplog.text


def test_hw3d_auto_unresolvable_driver(monkeypatch, dri_present):
    """Driver symlink may not resolve in-container — treated as not nvidia."""
    monkeypatch.delenv("KASM_HW3D", raising=False)
    _fake_driver(monkeypatch, None)
    assert vnc_manager._hw3d_flags() == ["-hw3d", "-drinode", "/dev/dri/renderD128"]


def test_hw3d_forced_on_nvidia(monkeypatch, dri_present):
    """KASM_HW3D=1 always enables when the device exists, even on nvidia."""
    monkeypatch.setenv("KASM_HW3D", "1")
    _fake_driver(monkeypatch, "nvidia")
    assert vnc_manager._hw3d_flags() == ["-hw3d", "-drinode", "/dev/dri/renderD128"]


def test_hw3d_drinode_override(monkeypatch, dri_present):
    monkeypatch.setenv("KASM_HW3D", "auto")
    monkeypatch.setenv("KASM_DRINODE", "/dev/dri/renderD129")
    _fake_driver(monkeypatch, "i915")
    assert vnc_manager._hw3d_flags() == ["-hw3d", "-drinode", "/dev/dri/renderD129"]


# ── start_vnc command assembly ───────────────────────────────────────────────


@pytest.fixture()
def xvnc_cmd(monkeypatch):
    """Capture the Xvnc command line without spawning a process.

    start_vnc() waits for the websocket port to accept before returning, so
    the stub reports the port as ready rather than pretending a sleep was
    long enough.
    """
    captured: dict[str, list[str]] = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        proc = MagicMock()
        proc.poll.return_value = None  # still running
        return proc

    async def ready_immediately(_port, _process, _timeout):
        return True

    async def fake_passwd(display, _password):
        return f"/tmp/kasmpasswd-{display}"

    monkeypatch.setattr(vnc_manager.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(vnc_manager, "_wait_until_listening", ready_immediately)
    monkeypatch.setattr(vnc_manager, "_write_kasm_passwd", fake_passwd)
    return captured


@pytest.mark.asyncio
async def test_start_vnc_base_command_unchanged(vnc: VNCManager, xvnc_cmd, monkeypatch):
    """Display/port scheme and connectivity flags stay as before."""
    monkeypatch.setenv("KASM_HW3D", "0")
    await vnc.start_vnc(100, 6100, width=1920, height=1080)
    cmd = xvnc_cmd["cmd"]
    assert ":100" in cmd
    assert _flag_value(cmd, "-websocketPort") == "6100"
    assert _flag_value(cmd, "-rfbport") == "-1"
    assert _flag_value(cmd, "-geometry") == "1920x1080"
    assert _flag_value(cmd, "-depth") == "24"
    assert _flag_value(cmd, "-interface") == "127.0.0.1"
    assert _flag_value(cmd, "-httpd") == "/usr/share/kasmvnc/www"
    assert "-SecurityTypes" in cmd and "-AlwaysShared" in cmd
    # Basic auth stays ENABLED (default) — Kasm's HTTP layer needs it for the
    # management API; nginx injects per-display creds for viewer traffic.
    assert "-DisableBasicAuth" not in cmd


@pytest.mark.asyncio
async def test_start_vnc_performance_flags(vnc: VNCManager, xvnc_cmd, monkeypatch):
    monkeypatch.setenv("KASM_HW3D", "0")
    monkeypatch.delenv("KASM_QUALITY_PRESET", raising=False)
    monkeypatch.delenv("KASM_RECT_THREADS", raising=False)
    await vnc.start_vnc(100, 6100)
    cmd = xvnc_cmd["cmd"]
    assert "-IgnoreClientSettingsKasm" in cmd  # server owns encoding policy
    assert _flag_value(cmd, "-FrameRate") == "30"
    assert _flag_value(cmd, "-MaxVideoResolution") == "1600x900"
    assert _flag_value(cmd, "-RectThreads") == "2"
    assert "-hw3d" not in cmd
    # Streaming codec must be set explicitly — the binary default is empty
    # (disabled), the man page's "auto" claim is wrong.
    assert _flag_value(cmd, "-videoCodec") == "auto"


@pytest.mark.asyncio
async def test_start_vnc_preset_from_env(vnc: VNCManager, xvnc_cmd, monkeypatch):
    monkeypatch.setenv("KASM_HW3D", "0")
    monkeypatch.setenv("KASM_QUALITY_PRESET", "low")
    await vnc.start_vnc(100, 6100)
    cmd = xvnc_cmd["cmd"]
    assert _flag_value(cmd, "-FrameRate") == "24"
    assert _flag_value(cmd, "-MaxVideoResolution") == "1280x720"


@pytest.mark.asyncio
async def test_start_vnc_hw3d_flags(vnc: VNCManager, xvnc_cmd, monkeypatch, dri_present):
    monkeypatch.setenv("KASM_HW3D", "1")
    await vnc.start_vnc(100, 6100)
    cmd = xvnc_cmd["cmd"]
    assert "-hw3d" in cmd
    assert _flag_value(cmd, "-drinode") == "/dev/dri/renderD128"


@pytest.mark.asyncio
async def test_start_vnc_public_ip_flag(vnc: VNCManager, xvnc_cmd, monkeypatch):
    """STUN public-IP discovery must be disabled (no outbound lookups)."""
    monkeypatch.setenv("KASM_HW3D", "0")
    await vnc.start_vnc(100, 6100)
    assert _flag_value(xvnc_cmd["cmd"], "-PublicIP") == "127.0.0.1"


@pytest.mark.asyncio
async def test_start_vnc_kasm_passwd(vnc: VNCManager, xvnc_cmd, monkeypatch):
    """API password file flag + credentials retrievable for the stats proxy."""
    monkeypatch.setenv("KASM_HW3D", "0")
    await vnc.allocate()  # start_vnc only stores creds on allocated displays
    await vnc.start_vnc(100, 6100)
    assert _flag_value(xvnc_cmd["cmd"], "-KasmPasswordFile") == "/tmp/kasmpasswd-100"
    creds = vnc.get_api_credentials(100)
    assert creds is not None
    assert creds[0] == "manager"
    assert len(creds[1]) == 32  # secrets.token_hex(16)


@pytest.mark.asyncio
async def test_start_vnc_fails_when_credentials_cannot_be_written(
    vnc: VNCManager, xvnc_cmd, monkeypatch,
):
    """No password file means a permanently 401 viewer — fail the launch."""
    monkeypatch.setenv("KASM_HW3D", "0")

    async def fail_passwd(_display, _password):
        raise RuntimeError("kasmvncpasswd not found; cannot create KasmVNC credentials")

    monkeypatch.setattr(vnc_manager, "_write_kasm_passwd", fail_passwd)
    await vnc.allocate()
    with pytest.raises(RuntimeError, match="kasmvncpasswd"):
        await vnc.start_vnc(100, 6100)
    # and no Xvnc was spawned for it
    assert xvnc_cmd.get("cmd") is None


@pytest.mark.asyncio
async def test_write_kasm_passwd_rejects_an_empty_file(monkeypatch):
    """kasmvncpasswd can exit 0 after a failed write — verify the artefact."""
    class WritesEmpty:
        returncode = 0

        async def communicate(self, _data):
            Path("/tmp/kasmpasswd-901").write_text("")
            return (b"", b"")

    monkeypatch.setattr(vnc_manager.shutil, "which", lambda _n: "/usr/bin/kasmvncpasswd")
    monkeypatch.setattr(
        vnc_manager.asyncio, "create_subprocess_exec",
        lambda *a, **k: _coro(WritesEmpty()),
    )
    try:
        with pytest.raises(RuntimeError, match="empty"):
            await vnc_manager._write_kasm_passwd(901, "pw")
    finally:
        Path("/tmp/kasmpasswd-901").unlink(missing_ok=True)


async def _coro(value):
    return value


@pytest.mark.asyncio
async def test_stop_vnc_removes_passwd_file(vnc: VNCManager, xvnc_cmd, monkeypatch, tmp_path):
    monkeypatch.setenv("KASM_HW3D", "0")
    await vnc.allocate()
    await vnc.start_vnc(100, 6100)
    passwd = Path("/tmp/kasmpasswd-100")
    passwd.write_text("dummy")
    assert passwd.exists()
    await vnc.stop_vnc(100)
    assert not passwd.exists()


@pytest.mark.asyncio
async def test_write_kasm_passwd_success(monkeypatch):
    """The helper pipes the password twice and returns the path."""
    calls: dict[str, object] = {}

    class FakeProc:
        returncode = 0

        async def communicate(self, data):
            calls["stdin"] = data
            Path("/tmp/kasmpasswd-100").write_text("manager:hash\n")  # like the real tool
            return (b"", b"")

    async def fake_exec(*args, **kwargs):
        calls["args"] = args
        return FakeProc()

    monkeypatch.setattr(vnc_manager.shutil, "which", lambda _n: "/usr/bin/kasmvncpasswd")
    monkeypatch.setattr(vnc_manager.asyncio, "create_subprocess_exec", fake_exec)
    try:
        path = await vnc_manager._write_kasm_passwd(100, "s3cret")
    finally:
        Path("/tmp/kasmpasswd-100").unlink(missing_ok=True)
    assert path == "/tmp/kasmpasswd-100"
    assert calls["args"][:3] == ("/usr/bin/kasmvncpasswd", "-u", "manager")
    assert "-wro" in calls["args"]
    assert calls["stdin"] == b"s3cret\ns3cret\n"


@pytest.mark.asyncio
async def test_write_kasm_passwd_missing_binary(monkeypatch):
    """No binary means no credentials means an unusable viewer — raise."""
    monkeypatch.setattr(vnc_manager.shutil, "which", lambda _n: None)
    with pytest.raises(RuntimeError, match="kasmvncpasswd not found"):
        await vnc_manager._write_kasm_passwd(100, "x")


@pytest.mark.asyncio
async def test_stop_vnc_releases_allocation_only_after_the_process_exits(vnc: VNCManager):
    """A gap-filling allocate() must not hand out a port Xvnc still holds."""
    from unittest.mock import MagicMock

    display, _ = await vnc.allocate()
    observed: dict[str, bool] = {}

    proc = MagicMock()

    def _wait(_timeout=None):
        observed["allocated_during_wait"] = display in vnc._allocated
        return 0

    proc.wait.side_effect = _wait
    vnc._allocated[display].process = proc

    await vnc.stop_vnc(display)

    assert observed["allocated_during_wait"] is True
    assert display not in vnc._allocated
    proc.terminate.assert_called_once()


@pytest.mark.asyncio
async def test_stop_vnc_waits_after_kill(vnc: VNCManager):
    """SIGKILL is followed by a wait so the port is actually released."""
    from unittest.mock import MagicMock

    display, _ = await vnc.allocate()
    proc = MagicMock()
    proc.wait.side_effect = [subprocess.TimeoutExpired("Xvnc", 5), 0]
    vnc._allocated[display].process = proc

    await vnc.stop_vnc(display)

    proc.kill.assert_called_once()
    assert proc.wait.call_count == 2
    assert display not in vnc._allocated


@pytest.mark.asyncio
async def test_stop_vnc_survives_a_broken_process_handle(vnc: VNCManager):
    """One bad handle must not strand the remaining displays in cleanup_all."""
    from unittest.mock import MagicMock

    display, _ = await vnc.allocate()
    proc = MagicMock()
    proc.terminate.side_effect = ProcessLookupError("no such process")
    vnc._allocated[display].process = proc

    await vnc.stop_vnc(display)  # must not raise

    assert display not in vnc._allocated


# ── VNCManager.is_alive ──────────────────────────────────────────────────────


def test_is_alive_unallocated_display(vnc: VNCManager):
    assert vnc.is_alive(999) is False


@pytest.mark.asyncio
async def test_is_alive_tracks_the_process(vnc: VNCManager):
    display, _ = await vnc.allocate()
    assert vnc.is_alive(display) is False  # allocated, not started

    proc = MagicMock()
    proc.poll.return_value = None  # running
    vnc._allocated[display].process = proc
    assert vnc.is_alive(display) is True

    proc.poll.return_value = 1  # exited
    assert vnc.is_alive(display) is False


# ── launch registration window ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_launch_reclaims_display_if_the_browser_dies_before_registration(
    monkeypatch, tmp_path,
):
    """A close during the registration window must not orphan Xvnc."""
    from unittest.mock import AsyncMock
    from backend import browser_manager as bm

    mgr = bm.BrowserManager()
    context = MagicMock()
    context.is_closed.return_value = True  # died while we were registering
    context.pages = []
    context.add_init_script = AsyncMock()
    context.close = AsyncMock()
    context.on = MagicMock()

    monkeypatch.setattr(bm, "launch_persistent_context_async", AsyncMock(return_value=context))
    monkeypatch.setattr(mgr.vnc, "start_vnc", AsyncMock())
    stop_vnc = AsyncMock()
    monkeypatch.setattr(mgr.vnc, "stop_vnc", stop_vnc)

    profile = {"id": "p1", "user_data_dir": str(tmp_path / "p1")}
    with pytest.raises(RuntimeError, match="exited during launch"):
        await mgr.launch(profile)

    assert "p1" not in mgr.running
    assert "p1" not in mgr._launching
    stop_vnc.assert_awaited()  # the display was reclaimed


# ── Xvnc readiness ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wait_until_listening_detects_a_bound_port():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    proc = MagicMock()
    proc.poll.return_value = None
    try:
        assert await vnc_manager._wait_until_listening(port, proc, 2.0) is True
    finally:
        listener.close()


@pytest.mark.asyncio
async def test_wait_until_listening_gives_up_when_the_process_dies():
    """A dead Xvnc is reported immediately, not after the full timeout."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    proc = MagicMock()
    proc.poll.return_value = 1  # exited
    started = time.monotonic()
    assert await vnc_manager._wait_until_listening(port, proc, 5.0) is False
    assert time.monotonic() - started < 1.0


@pytest.mark.asyncio
async def test_start_vnc_fails_when_the_port_never_opens(vnc: VNCManager, monkeypatch):
    """Launch must not proceed against an Xvnc that is not listening."""
    monkeypatch.setenv("KASM_HW3D", "0")

    waited: dict[str, bool] = {}

    def fake_popen(cmd, **kwargs):
        proc = MagicMock()
        proc.poll.return_value = None  # alive but never binds
        proc.wait.side_effect = lambda _t=None: waited.__setitem__("waited", True)
        return proc

    async def never_ready(_port, _process, _timeout):
        return False

    async def fake_passwd(display, _password):
        return f"/tmp/kasmpasswd-{display}"

    monkeypatch.setattr(vnc_manager.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(vnc_manager, "_wait_until_listening", never_ready)
    monkeypatch.setattr(vnc_manager, "_write_kasm_passwd", fake_passwd)

    await vnc.allocate()
    with pytest.raises(RuntimeError, match="failed to start"):
        await vnc.start_vnc(100, 6100)
    # the process is registered before the readiness check, so teardown goes
    # through _terminate: SIGTERM, then SIGKILL, and reaped either way
    assert vnc._allocated[100].process is not None
    assert waited.get("waited") is True


@pytest.mark.asyncio
async def test_launch_releases_everything_when_profile_setup_fails(monkeypatch, tmp_path):
    """A full/read-only volume must not brick the profile permanently.

    Without cleanup coverage over the whole setup, the id stays in `_launching`
    forever: is_starting() then makes /launch, /stop and DELETE all answer 409
    and the display is never reclaimed, until the container restarts.
    """
    from unittest.mock import AsyncMock
    from backend import browser_manager as bm

    mgr = bm.BrowserManager()
    monkeypatch.setattr(
        bm, "_init_profile_defaults",
        MagicMock(side_effect=OSError(28, "No space left on device")),
    )
    stop_vnc = AsyncMock()
    monkeypatch.setattr(mgr.vnc, "stop_vnc", stop_vnc)

    with pytest.raises(OSError):
        await mgr.launch({"id": "p1", "user_data_dir": str(tmp_path / "p1")})

    assert mgr.is_starting("p1") is False   # not bricked
    assert "p1" not in mgr.running
    stop_vnc.assert_awaited()               # display reclaimed


@pytest.mark.asyncio
async def test_launch_closes_the_context_when_a_later_step_fails(monkeypatch, tmp_path):
    """An aborted launch must not orphan a live Chromium.

    Xvnc gets torn down, so the browser would be left with no X server, still
    holding its CDP port and writing to user_data_dir — and the next relaunch
    clears the Singleton locks and opens a second Chromium on the same dir.
    """
    from unittest.mock import AsyncMock
    from backend import browser_manager as bm

    mgr = bm.BrowserManager()
    context = MagicMock()
    context.is_closed.return_value = False
    context.pages = []
    context.close = AsyncMock()
    context.on = MagicMock()
    context.add_init_script = AsyncMock(side_effect=RuntimeError("browser wedged"))

    monkeypatch.setattr(bm, "launch_persistent_context_async", AsyncMock(return_value=context))
    monkeypatch.setattr(mgr.vnc, "start_vnc", AsyncMock())
    monkeypatch.setattr(mgr.vnc, "stop_vnc", AsyncMock())

    with pytest.raises(RuntimeError, match="browser wedged"):
        await mgr.launch({"id": "p1", "user_data_dir": str(tmp_path / "p1")})

    context.close.assert_awaited()


@pytest.mark.asyncio
async def test_launch_closes_the_context_when_cancelled(monkeypatch, tmp_path):
    """auto_launch_all wraps launch() in wait_for; a timeout must not orphan."""
    import asyncio as aio
    from unittest.mock import AsyncMock
    from backend import browser_manager as bm

    mgr = bm.BrowserManager()
    context = MagicMock()
    context.is_closed.return_value = False
    context.pages = []
    context.close = AsyncMock()
    context.on = MagicMock()

    async def never_returns(*_a, **_k):
        await aio.sleep(3600)

    context.add_init_script = never_returns
    monkeypatch.setattr(bm, "launch_persistent_context_async", AsyncMock(return_value=context))
    monkeypatch.setattr(mgr.vnc, "start_vnc", AsyncMock())
    monkeypatch.setattr(mgr.vnc, "stop_vnc", AsyncMock())

    with pytest.raises(aio.TimeoutError):
        await aio.wait_for(
            mgr.launch({"id": "p1", "user_data_dir": str(tmp_path / "p1")}), timeout=0.05,
        )

    context.close.assert_awaited()


@pytest.mark.asyncio
async def test_write_kasm_passwd_ignores_a_stale_file_from_a_previous_run(monkeypatch):
    """/tmp survives `docker restart` — a leftover must not pass as success.

    Without the pre-unlink, a silently-failing kasmvncpasswd leaves the old
    file in place, the non-empty check passes, and Xvnc starts with credentials
    that do not match the password we just generated.
    """
    class WritesNothing:
        returncode = 0

        async def communicate(self, _data):
            return (b"", b"")

    stale = Path("/tmp/kasmpasswd-903")
    stale.write_text("manager:credentials-from-the-previous-container\n")
    monkeypatch.setattr(vnc_manager.shutil, "which", lambda _n: "/usr/bin/kasmvncpasswd")
    monkeypatch.setattr(
        vnc_manager.asyncio, "create_subprocess_exec",
        lambda *a, **k: _coro(WritesNothing()),
    )
    try:
        with pytest.raises(RuntimeError, match="did not create"):
            await vnc_manager._write_kasm_passwd(903, "pw")
        assert not stale.exists()  # and the stale credentials are gone
    finally:
        stale.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_cleanup_all_stops_displays_concurrently(vnc: VNCManager):
    """Shutdown cost must be the slowest display, not the sum of all of them."""
    import asyncio as aio

    for _ in range(4):
        await vnc.allocate()

    order: list[str] = []

    async def slow_stop(display: int):
        order.append(f"start-{display}")
        await aio.sleep(0.05)
        order.append(f"done-{display}")
        vnc._allocated.pop(display, None)

    vnc.stop_vnc = slow_stop  # type: ignore[assignment]
    start = time.monotonic()
    await vnc.cleanup_all()
    elapsed = time.monotonic() - start

    assert elapsed < 0.15                      # not 4 x 0.05 in series
    assert order[:4] == [f"start-{d}" for d in (100, 101, 102, 103)]


@pytest.mark.asyncio
async def test_launch_cancellation_is_not_held_open_by_a_wedged_context(
    monkeypatch, tmp_path,
):
    """wait_for(launch, 60) must actually return when the context won't close.

    A wedged Playwright connection makes context.close() hang; awaiting it
    unbounded inside the cancellation path means auto_launch_all's timeout
    never fires, the display is never reclaimed, and every queued profile
    behind it stays stuck reporting "starting".
    """
    import asyncio as aio
    from unittest.mock import AsyncMock
    from backend import browser_manager as bm

    monkeypatch.setattr(bm, "CONTEXT_CLOSE_TIMEOUT_S", 0.05)

    mgr = bm.BrowserManager()
    context = MagicMock()
    context.is_closed.return_value = False
    context.pages = []
    context.on = MagicMock()

    async def never_closes():
        await aio.sleep(3600)

    async def never_returns(*_a, **_k):
        await aio.sleep(3600)

    context.close = never_closes
    context.add_init_script = never_returns
    monkeypatch.setattr(bm, "launch_persistent_context_async", AsyncMock(return_value=context))
    monkeypatch.setattr(mgr.vnc, "start_vnc", AsyncMock())
    stop_vnc = AsyncMock()
    monkeypatch.setattr(mgr.vnc, "stop_vnc", stop_vnc)

    async def cancel_a_launch():
        with pytest.raises(aio.TimeoutError):
            await aio.wait_for(
                mgr.launch({"id": "p1", "user_data_dir": str(tmp_path / "p1")}),
                timeout=0.05,
            )

    # Outer deadline so an unbounded close fails the test instead of hanging it.
    try:
        await aio.wait_for(cancel_a_launch(), timeout=3.0)
    except aio.TimeoutError:
        pytest.fail("cancellation was held open by the wedged context.close()")

    assert mgr.is_starting("p1") is False
    stop_vnc.assert_awaited()            # display reclaimed despite the wedge


@pytest.mark.asyncio
async def test_stop_vnc_keeps_the_display_when_the_process_survives_sigkill(
    vnc: VNCManager,
):
    """An unreaped Xvnc may still hold the port; don't hand it to the next launch."""
    from unittest.mock import MagicMock

    display, _ = await vnc.allocate()
    proc = MagicMock()
    proc.wait.side_effect = subprocess.TimeoutExpired("Xvnc", 5)  # never dies
    vnc._allocated[display].process = proc

    await vnc.stop_vnc(display)

    proc.kill.assert_called_once()
    assert display in vnc._allocated          # leaked on purpose, not reused
    assert (await vnc.allocate())[0] != display


@pytest.mark.asyncio
async def test_late_close_does_not_evict_the_replacement(monkeypatch, tmp_path):
    """A close arriving from a superseded context must be ignored.

    Otherwise a slow teardown that outlived its bounded wait pops whatever
    instance owns the profile id by then, killing a freshly launched session
    and orphaning its Chromium.
    """
    from unittest.mock import AsyncMock
    from backend import browser_manager as bm

    mgr = bm.BrowserManager()
    old_context = MagicMock(name="A")
    new_running = bm.RunningProfile(
        profile_id="p1", context=MagicMock(name="B"), display=101, ws_port=6101, cdp_port=5101,
    )
    mgr.running["p1"] = new_running
    stop_vnc = AsyncMock()
    monkeypatch.setattr(mgr.vnc, "stop_vnc", stop_vnc)

    # context A finally finishes closing, long after it was superseded
    await mgr._on_browser_closed("p1", old_context)

    assert mgr.running.get("p1") is new_running   # replacement untouched
    stop_vnc.assert_not_awaited()

    # the live context's own close still works
    await mgr._on_browser_closed("p1", new_running.context)
    assert "p1" not in mgr.running
    stop_vnc.assert_awaited()


@pytest.mark.asyncio
async def test_a_wedged_stop_blocks_relaunch_until_the_close_lands(monkeypatch, tmp_path):
    """stop() pops the profile, so the wedge must be recorded somewhere.

    Otherwise a relaunch starts a SECOND Chromium on the same live
    user_data_dir, and a delete rmtree's under the first one.
    """
    from unittest.mock import AsyncMock
    from backend import browser_manager as bm

    monkeypatch.setattr(bm, "CONTEXT_CLOSE_TIMEOUT_S", 0.05)
    mgr = bm.BrowserManager()
    context = MagicMock()
    context.is_closed.return_value = False

    async def never_closes():
        await __import__("asyncio").sleep(3600)

    context.close = never_closes
    mgr.running["p1"] = bm.RunningProfile(
        profile_id="p1", context=context, display=100, ws_port=6100, cdp_port=5100,
    )
    monkeypatch.setattr(mgr.vnc, "stop_vnc", AsyncMock())

    assert await mgr.stop("p1") is False
    assert mgr.is_wedged("p1") is True

    # a relaunch must be refused while the old Chromium is still alive
    with pytest.raises(bm.ProfileAlreadyRunning):
        await mgr.launch({"id": "p1", "user_data_dir": str(tmp_path / "p1")})

    # ...and allowed again once the close finally lands
    await mgr._on_browser_closed("p1", context)
    assert mgr.is_wedged("p1") is False


@pytest.mark.asyncio
async def test_aborted_launch_records_a_wedged_browser(monkeypatch, tmp_path):
    """A cancelled launch whose close also wedges must gate later mutations.

    The profile is in none of running/_launching, so without recording it a
    relaunch starts a second Chromium on the same live user_data_dir.
    """
    import asyncio as aio
    from unittest.mock import AsyncMock
    from backend import browser_manager as bm

    monkeypatch.setattr(bm, "CONTEXT_CLOSE_TIMEOUT_S", 0.05)
    mgr = bm.BrowserManager()
    handlers: list = []
    context = MagicMock()
    context.is_closed.return_value = False
    context.pages = []
    context.on = MagicMock(side_effect=lambda _evt, cb: handlers.append(cb))

    async def never(*_a, **_k):
        await aio.sleep(3600)

    context.close = never
    context.add_init_script = never
    monkeypatch.setattr(bm, "launch_persistent_context_async", AsyncMock(return_value=context))
    monkeypatch.setattr(mgr.vnc, "start_vnc", AsyncMock())
    monkeypatch.setattr(mgr.vnc, "stop_vnc", AsyncMock())

    with pytest.raises(aio.TimeoutError):
        await aio.wait_for(
            mgr.launch({"id": "p1", "user_data_dir": str(tmp_path / "p1")}), timeout=0.05,
        )

    assert mgr.is_starting("p1") is False
    assert "p1" not in mgr.running
    assert mgr.is_wedged("p1") is True          # gated, not forgotten
    assert handlers, "close handler must be registered before the setup awaits"

    # the handler registered up-front is what eventually clears it
    await mgr._on_browser_closed("p1", context)
    assert mgr.is_wedged("p1") is False


@pytest.mark.asyncio
async def test_wedge_clears_even_when_another_instance_holds_the_id(monkeypatch):
    """The wedged context's own close must resolve it.

    _on_browser_closed returns early for a superseded context, so clearing the
    wedge after that check would leave the profile blocked forever once any
    other instance held the id.
    """
    from unittest.mock import AsyncMock
    from backend import browser_manager as bm

    mgr = bm.BrowserManager()
    wedged_context = MagicMock(name="A")
    mgr._closing["p1"] = (wedged_context, time.monotonic() + 60, None, time.monotonic())
    # some other instance now owns the id
    mgr.running["p1"] = bm.RunningProfile(
        profile_id="p1", context=MagicMock(name="B"), display=101, ws_port=6101, cdp_port=5101,
    )
    monkeypatch.setattr(mgr.vnc, "stop_vnc", AsyncMock())

    await mgr._on_browser_closed("p1", wedged_context)

    assert mgr.is_wedged("p1") is False        # resolved
    assert "p1" in mgr.running                 # and the live instance untouched


@pytest.mark.asyncio
async def test_profile_is_guarded_for_the_whole_teardown_not_just_on_failure(
    monkeypatch,
):
    """The close window itself must be guarded.

    stop() pops the profile from `running` and then spends up to
    CONTEXT_CLOSE_TIMEOUT_S closing. Recording the browser only after that
    close FAILS leaves the entire window unguarded, so a DELETE arriving
    mid-close rmtree's user_data_dir under a live Chromium.
    """
    import asyncio as aio
    from unittest.mock import AsyncMock
    from backend import browser_manager as bm

    mgr = bm.BrowserManager()
    release = aio.Event()
    context = MagicMock()
    context.is_closed.return_value = False

    async def slow_close():
        await release.wait()

    context.close = slow_close
    mgr.running["p1"] = bm.RunningProfile(
        profile_id="p1", context=context, display=100, ws_port=6100, cdp_port=5100,
    )
    monkeypatch.setattr(mgr.vnc, "stop_vnc", AsyncMock())

    task = aio.ensure_future(mgr.stop("p1"))
    await aio.sleep(0)  # let stop() reach the close
    await aio.sleep(0)

    # mid-close: gone from running, but must NOT look free
    assert "p1" not in mgr.running
    assert mgr.is_wedged("p1") is True

    release.set()
    assert await task is True                 # closed cleanly in the end
    assert mgr.is_wedged("p1") is False       # and the claim is released


@pytest.mark.asyncio
async def test_a_browser_that_never_closes_does_not_brick_the_profile(monkeypatch):
    """The guard needs a release valve.

    Without a ceiling, a browser that never reports its close leaves launch
    409ing, delete 409ing and stop 404ing for the life of the container — the
    profile is simply unusable.
    """
    from backend import browser_manager as bm

    mgr = bm.BrowserManager()
    mgr._closing["p1"] = (MagicMock(), time.monotonic() + bm.CLOSING_CLAIM_TTL_S, None, time.monotonic())
    assert mgr.is_wedged("p1") is True

    # ...once the claim expires the profile is usable again
    mgr._closing["p1"] = (MagicMock(), time.monotonic() - 0.01, None, time.monotonic())
    assert mgr.is_wedged("p1") is False
    assert "p1" not in mgr._closing        # and the entry is dropped


def test_unknown_hw3d_value_falls_back_to_auto(monkeypatch, caplog):
    """A typo must not bypass the NVIDIA auto-detect and force -hw3d."""
    monkeypatch.setenv("KASM_HW3D", "enabled")          # not a recognised value
    monkeypatch.setenv("KASM_DRINODE", "/dev/dri/renderD128")
    monkeypatch.setattr(vnc_manager.os.path, "exists", lambda _p: True)
    monkeypatch.setattr(vnc_manager, "_dri_driver", lambda _n: "nvidia")

    with caplog.at_level("WARNING", logger="cloakbrowser.manager.vnc"):
        flags = vnc_manager._hw3d_flags()

    assert flags == []                                   # auto-detect applied
    assert "Unknown KASM_HW3D" in caplog.text


def test_explicit_hw3d_still_forces_past_the_nvidia_check(monkeypatch):
    monkeypatch.setenv("KASM_HW3D", "1")
    monkeypatch.setenv("KASM_DRINODE", "/dev/dri/renderD128")
    monkeypatch.setattr(vnc_manager.os.path, "exists", lambda _p: True)
    monkeypatch.setattr(vnc_manager, "_dri_driver", lambda _n: "nvidia")

    assert vnc_manager._hw3d_flags() == ["-hw3d", "-drinode", "/dev/dri/renderD128"]


@pytest.mark.asyncio
async def test_launch_abort_guards_the_profile_for_its_whole_cleanup(monkeypatch, tmp_path):
    """Same window stop() closes: the abort's close must be guarded too."""
    import asyncio as aio
    from unittest.mock import AsyncMock
    from backend import browser_manager as bm

    mgr = bm.BrowserManager()
    release = aio.Event()
    context = MagicMock()
    context.is_closed.return_value = False
    context.pages = []
    context.on = MagicMock()

    async def slow_close():
        await release.wait()

    async def boom(*_a, **_k):
        raise RuntimeError("setup failed")

    context.close = slow_close
    context.add_init_script = boom
    monkeypatch.setattr(bm, "launch_persistent_context_async", AsyncMock(return_value=context))
    monkeypatch.setattr(mgr.vnc, "start_vnc", AsyncMock())
    monkeypatch.setattr(mgr.vnc, "stop_vnc", AsyncMock())

    task = aio.ensure_future(mgr.launch({"id": "p1", "user_data_dir": str(tmp_path / "p1")}))
    for _ in range(8):
        await aio.sleep(0)          # let it reach the abort's close

    assert mgr.is_starting("p1") is False   # already out of _launching
    assert mgr.is_wedged("p1") is True      # but NOT free

    release.set()
    with pytest.raises(RuntimeError, match="setup failed"):
        await task
    assert mgr.is_wedged("p1") is False     # claim released once closed


@pytest.mark.asyncio
async def test_claim_is_not_released_while_the_browser_is_still_listening():
    """The TTL must decide on evidence, not on the X-server assumption.

    A headless profile keeps running after stop_vnc() kills the display, so
    releasing purely on elapsed time hands a live browser's user_data_dir to
    the next launch or delete.
    """
    import socket as socket_mod
    from backend import browser_manager as bm

    listener = socket_mod.socket(socket_mod.AF_INET, socket_mod.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    mgr = bm.BrowserManager()
    try:
        # claim already expired, but the browser is demonstrably alive
        mgr._closing["p1"] = (MagicMock(), time.monotonic() - 1, port, time.monotonic())
        assert mgr.is_wedged("p1") is True
        # and the deadline is pushed out rather than the claim dropped
        assert mgr._closing["p1"][1] > time.monotonic()
    finally:
        listener.close()

    # once it really is gone, the expired claim releases
    mgr._closing["p1"] = (MagicMock(), time.monotonic() - 1, port, time.monotonic())
    assert mgr.is_wedged("p1") is False
    assert "p1" not in mgr._closing


@pytest.mark.asyncio
async def test_browser_exit_during_registration_reclaims_the_display_once(
    monkeypatch, tmp_path,
):
    """A duplicate stop_vnc could pop a concurrent relaunch's fresh allocation."""
    from unittest.mock import AsyncMock
    from backend import browser_manager as bm

    mgr = bm.BrowserManager()
    context = MagicMock()
    context.is_closed.return_value = True     # died during registration
    context.pages = []
    context.add_init_script = AsyncMock()
    context.close = AsyncMock()
    context.on = MagicMock()

    monkeypatch.setattr(bm, "launch_persistent_context_async", AsyncMock(return_value=context))
    monkeypatch.setattr(mgr.vnc, "start_vnc", AsyncMock())
    stop_vnc = AsyncMock()
    monkeypatch.setattr(mgr.vnc, "stop_vnc", stop_vnc)

    with pytest.raises(RuntimeError, match="exited during launch"):
        await mgr.launch({"id": "p1", "user_data_dir": str(tmp_path / "p1")})

    assert stop_vnc.await_count == 1          # exactly once, not twice


@pytest.mark.asyncio
async def test_cancelling_stop_keeps_the_guard(monkeypatch):
    """The close is shielded, so a cancelled stop() must not free the profile."""
    import asyncio as aio
    from unittest.mock import AsyncMock
    from backend import browser_manager as bm

    mgr = bm.BrowserManager()
    context = MagicMock()
    context.is_closed.return_value = False

    async def slow_close():
        await aio.sleep(3600)

    context.close = slow_close
    mgr.running["p1"] = bm.RunningProfile(
        profile_id="p1", context=context, display=100, ws_port=6100, cdp_port=5100,
    )
    monkeypatch.setattr(mgr.vnc, "stop_vnc", AsyncMock())

    stop_vnc = AsyncMock()
    monkeypatch.setattr(mgr.vnc, "stop_vnc", stop_vnc)

    with pytest.raises(aio.TimeoutError):
        await aio.wait_for(mgr.stop("p1"), timeout=0.05)
    await aio.sleep(0)

    assert mgr.is_wedged("p1") is True         # still guarded
    stop_vnc.assert_awaited()                  # but the display is NOT leaked


@pytest.mark.asyncio
async def test_a_recycled_cdp_port_cannot_brick_a_profile_forever():
    """"Something answers" is not proof it is OUR browser.

    CDP ports cycle 5100-5199, so a later profile's Chromium can bind the port
    a stale claim remembers. Without an absolute ceiling that unrelated browser
    would extend the claim indefinitely.
    """
    import socket as socket_mod
    from backend import browser_manager as bm

    listener = socket_mod.socket(socket_mod.AF_INET, socket_mod.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    mgr = bm.BrowserManager()
    try:
        # held just under the ceiling: a listening port still extends it
        mgr._closing["p1"] = (
            MagicMock(), time.monotonic() - 1, port,
            time.monotonic() - (bm.CLOSING_CLAIM_MAX_S - 5),
        )
        assert mgr.is_wedged("p1") is True

        # past the ceiling: released despite the port still answering
        mgr._closing["p1"] = (
            MagicMock(), time.monotonic() - 1, port,
            time.monotonic() - (bm.CLOSING_CLAIM_MAX_S + 1),
        )
        assert mgr.is_wedged("p1") is False
        assert "p1" not in mgr._closing
    finally:
        listener.close()
