"""Tests for VNCManager — allocation logic, quality presets, hw3d, get_ws_port."""

from __future__ import annotations

import atexit
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from backend import browser_manager as bm
from backend import vnc_manager
from backend.vnc_manager import VNCInstance, VNCManager


@pytest.fixture()
def vnc() -> VNCManager:
    return VNCManager()


# ── teardown-claim identities ────────────────────────────────────────────────
# The guard now keys on (pid, /proc/<pid>/stat starttime), so tests need real
# processes rather than sentinels: a MagicMock cannot be signalled, cannot go
# zombie and cannot be found by the /proc scan, so none of the behaviours the
# design turns on are observable against one.

_SPARE_PROCESSES: list[subprocess.Popen] = []


def _spawn(*argv: str) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(3600)", *argv],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    _SPARE_PROCESSES.append(proc)
    return proc


@atexit.register
def _reap_spares() -> None:
    for proc in _SPARE_PROCESSES:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def _spare_process() -> subprocess.Popen:
    """A real, live child to stand in for a browser.

    NEVER this process: escalation really calls os.kill, so recording our own
    pid as the browser would have a test SIGTERM the test runner.
    """
    for proc in _SPARE_PROCESSES:
        if proc.poll() is None:
            return proc
    return _spawn()


def _ident(pid: int, cdp_port: int = 5100, udd: str = "/tmp/udd") -> bm.BrowserProcess:
    _state, _ppid, starttime = bm._proc_stat(pid)
    return bm.BrowserProcess(
        pid=pid, starttime=starttime, user_data_dir=udd, cdp_port=cdp_port,
    )


def _live_proc(cdp_port: int = 5100, udd: str = "/tmp/udd") -> bm.BrowserProcess:
    return _ident(_spare_process().pid, cdp_port, udd)


def _dead_proc(cdp_port: int = 5100, udd: str = "/tmp/udd") -> bm.BrowserProcess:
    # Same pid, different starttime: models both a browser that exited and a
    # pid the kernel has since handed to somebody else.
    live = _live_proc(cdp_port, udd)
    return bm.BrowserProcess(
        pid=live.pid, starttime=live.starttime + 1,
        user_data_dir=udd, cdp_port=cdp_port,
    )


def _claim(proc: bm.BrowserProcess | None, context=None, **kw) -> bm.ClosingClaim:
    return bm.ClosingClaim(
        context=context, proc=proc,
        user_data_dir=proc.user_data_dir if proc else "/tmp/udd",
        cdp_port=proc.cdp_port if proc else None,
        claimed_at=kw.pop("claimed_at", time.monotonic()), **kw,
    )


def _discovers(proc: bm.BrowserProcess | None):
    """Stand-in for the /proc scan, for tests whose context is a MagicMock."""
    async def _discover(_udd, _port):
        return proc
    return _discover


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


def test_browser_alive_falls_back_to_the_port_without_a_recorded_process():
    """No proc recorded (unreachable in production) -> the port is all we have."""
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
        assert mgr.running["abc"].proc is None
        assert mgr.get_liveness("abc")["browser_alive"] is True
    finally:
        listener.close()

    # port gone -> the browser is gone. context.pages could never report this:
    # Playwright implements it as a local list copy that cannot raise.
    assert mgr.get_liveness("abc")["browser_alive"] is False


def _live_browser_process(cdp_port: int, pid: int | None = None, starttime=None):
    """A BrowserProcess describing this test process (which is genuinely alive)."""
    import os as os_mod
    from backend.browser_manager import BrowserProcess, _proc_stat

    me = os_mod.getpid()
    stat = _proc_stat(me)
    assert stat is not None
    return BrowserProcess(
        pid=me if pid is None else pid,
        starttime=stat[2] if starttime is None else starttime,
        user_data_dir="/data/profiles/abc",
        cdp_port=cdp_port,
    )


def test_browser_alive_reports_dead_when_the_cdp_port_was_recycled():
    """The harmful direction: something else on our old port is not our browser.

    A false ALIVE keeps the viewer out of its browser-dead classification, so
    it burns the whole MAX_ALIVE_RECONNECTS budget minting viewer tokens for a
    Chromium that no longer exists.
    """
    import socket as socket_mod
    from backend.browser_manager import BrowserManager, RunningProfile
    from unittest.mock import MagicMock

    listener = socket_mod.socket(socket_mod.AF_INET, socket_mod.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        mgr = BrowserManager()
        # our pid, but a starttime that is not ours: the textbook recycled pid
        mgr.running["abc"] = RunningProfile(
            profile_id="abc", context=MagicMock(), display=100, ws_port=6100,
            cdp_port=port, proc=_live_browser_process(port, starttime=1),
        )
        assert _port_is_listening_helper(port) is True
        assert mgr.get_liveness("abc")["browser_alive"] is False
    finally:
        listener.close()


def test_browser_alive_reports_alive_before_the_port_is_up():
    """A live process with nothing listening yet is starting, not dead."""
    import socket as socket_mod
    from backend.browser_manager import BrowserManager, RunningProfile
    from unittest.mock import MagicMock

    # a port nobody is listening on
    probe = socket_mod.socket(socket_mod.AF_INET, socket_mod.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    closed_port = probe.getsockname()[1]
    probe.close()

    mgr = BrowserManager()
    mgr.running["abc"] = RunningProfile(
        profile_id="abc", context=MagicMock(), display=100, ws_port=6100,
        cdp_port=closed_port, proc=_live_browser_process(closed_port),
    )
    assert _port_is_listening_helper(closed_port) is False
    assert mgr.get_liveness("abc")["browser_alive"] is True


def _port_is_listening_helper(port: int) -> bool:
    from backend.browser_manager import _port_is_listening

    return _port_is_listening(port)


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


# ── -RectThreads is dead upstream ────────────────────────────────────────────


@pytest.mark.parametrize("name", ["text", "balanced", "low", "motion"])
def test_no_preset_passes_rect_threads(name: str):
    """rfb::Server::rectThreads is never read in 1.5.0 (oneTBB replaced OpenMP).

    Re-adding it would put a knob back on the box that caps nothing, which is
    exactly how an operator ends up tuning a dial against a loaded host and
    watching nothing happen.
    """
    assert "-RectThreads" not in vnc_manager.QUALITY_PRESETS[name]


def test_rect_threads_env_is_inert_and_says_so(monkeypatch, caplog):
    """The old KASM_RECT_THREADS knob must not silently pretend to work."""
    monkeypatch.setenv("KASM_RECT_THREADS", "4")
    with caplog.at_level("WARNING", logger="cloakbrowser.manager.vnc"):
        flags = vnc_manager._quality_flags("balanced")
    assert "-RectThreads" not in flags
    assert "KASM_RECT_THREADS is ignored" in caplog.text


def test_rect_threads_unset_is_silent(monkeypatch, caplog):
    monkeypatch.delenv("KASM_RECT_THREADS", raising=False)
    with caplog.at_level("WARNING", logger="cloakbrowser.manager.vnc"):
        vnc_manager._quality_flags("balanced")
    assert "KASM_RECT_THREADS" not in caplog.text


# ── encoding policy (-IgnoreClientSettingsKasm vs -videoCodec) ───────────────


def test_encoding_policy_default_is_server_authoritative(monkeypatch):
    monkeypatch.delenv("KASM_ENCODING_POLICY", raising=False)
    assert vnc_manager._encoding_policy_name() == "server-authoritative"


@pytest.mark.parametrize("name", ["server-authoritative", "video"])
def test_encoding_policy_from_env(monkeypatch, name: str):
    monkeypatch.setenv("KASM_ENCODING_POLICY", f"  {name.upper()}  ")
    assert vnc_manager._encoding_policy_name() == name


def test_encoding_policy_unknown_falls_back(monkeypatch, caplog):
    monkeypatch.setenv("KASM_ENCODING_POLICY", "h264-please")
    with caplog.at_level("WARNING", logger="cloakbrowser.manager.vnc"):
        assert vnc_manager._encoding_policy_name() == "server-authoritative"
    assert "Unknown KASM_ENCODING_POLICY" in caplog.text


def test_server_authoritative_policy_drops_the_dead_video_codec():
    """-IgnoreClientSettingsKasm makes -videoCodec unreachable, so don't pass it.

    can_apply = !ignoreClientSettingsKasm gates the ONLY writer of
    cp.encoder_config, and EncodeManager gates video mode on that field, so the
    pair together advertises a codec path that can never engage.
    """
    flags = vnc_manager._encoding_flags("server-authoritative")
    assert flags == ["-IgnoreClientSettingsKasm"]
    assert "-videoCodec" not in flags


def test_video_policy_drops_ignore_client_settings():
    flags = vnc_manager._encoding_flags("video")
    assert flags == ["-videoCodec", "h264"]
    assert "-IgnoreClientSettingsKasm" not in flags


def test_video_policy_never_offers_auto(monkeypatch: pytest.MonkeyPatch):
    """"auto" hands encoder choice to the client, which is not safe here.

    Measured live on this image: a client advertising -1037 selected software
    AV1 (libsvtav1), spent 364ms on one 1080p keyframe, then errored and the
    session fell back to Tight for its whole lifetime — worse than never
    enabling video mode, and invisible at the default log level.
    """
    monkeypatch.delenv("KASM_VIDEO_CODEC", raising=False)
    assert "auto" not in vnc_manager._encoding_flags("video")


def test_explicit_auto_is_refused(monkeypatch: pytest.MonkeyPatch, caplog):
    monkeypatch.setenv("KASM_VIDEO_CODEC", "auto")
    with caplog.at_level("WARNING", logger="cloakbrowser.manager.vnc"):
        assert vnc_manager._video_codec() == "h264"
    assert "refused" in caplog.text


@pytest.mark.parametrize("codec", ["h264_vaapi", "h265", "av1_vaapi"])
def test_known_codecs_pass_through(monkeypatch: pytest.MonkeyPatch, codec: str):
    monkeypatch.setenv("KASM_VIDEO_CODEC", codec)
    assert vnc_manager._video_codec() == codec


def test_unknown_codec_falls_back(monkeypatch: pytest.MonkeyPatch, caplog):
    monkeypatch.setenv("KASM_VIDEO_CODEC", "vp9")
    with caplog.at_level("WARNING", logger="cloakbrowser.manager.vnc"):
        assert vnc_manager._video_codec() == "h264"
    assert "Unknown KASM_VIDEO_CODEC" in caplog.text


def test_inert_preset_flags_are_dropped_under_the_video_policy(caplog):
    """A flag the server ignores must not be emitted as if it applied.

    In video mode EncodeManager takes the client's resolution rather than
    -MaxVideoResolution and skips the -TreatLossless promotion, so
    KASM_QUALITY_PRESET=low would silently still encode full 1080p.
    """
    full = vnc_manager._quality_flags("low")
    assert "-MaxVideoResolution" in full and "-TreatLossless" in full

    with caplog.at_level("WARNING", logger="cloakbrowser.manager.vnc"):
        trimmed = vnc_manager._quality_flags(
            "low", drop=vnc_manager._INERT_UNDER_VIDEO_POLICY,
        )
    for flag in vnc_manager._INERT_UNDER_VIDEO_POLICY:
        assert flag not in trimmed
    assert "-TreatLossless" in trimmed  # inert-looking, but load-bearing
    # the flag's VALUE must go with it, or the next flag inherits it as its own
    assert "1280x720" not in trimmed
    # everything else survives with its own value still attached
    assert trimmed[trimmed.index("-FrameRate") + 1] == "24"
    assert trimmed[trimmed.index("-VideoScaling") + 1] == "2"
    assert "inert" in caplog.text


@pytest.mark.parametrize("policy", ["server-authoritative", "video"])
def test_encoding_flags_are_mutually_exclusive(policy: str):
    """Neither policy may ever emit both halves of the exclusive pair."""
    flags = vnc_manager._encoding_flags(policy)
    assert ("-IgnoreClientSettingsKasm" in flags) != ("-videoCodec" in flags)


@pytest.mark.parametrize("name", ["text", "balanced", "low", "motion"])
def test_presets_never_carry_an_encoding_policy_flag(name: str):
    """_encoding_flags must stay the single emitter, or the exclusion breaks."""
    preset = vnc_manager.QUALITY_PRESETS[name]
    assert "-IgnoreClientSettingsKasm" not in preset
    assert "-videoCodec" not in preset


@pytest.mark.parametrize(
    "policy,needle",
    [
        ("server-authoritative", "in-band H.264/H.265/AV1 is"),
        ("video", "override the quality preset"),
    ],
)
def test_encoding_policy_logs_its_trade_off(caplog, policy: str, needle: str):
    """The operator must see what the active policy costs, not just its name."""
    with caplog.at_level("INFO", logger="cloakbrowser.manager.vnc"):
        vnc_manager._encoding_flags(policy)
    assert needle in caplog.text


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
    # -rfbport is not passed: KasmVNC 1.5.0 takes the raw-RFB listener branch
    # only under -noWebsocket, so the flag is inert either way and claiming it
    # "disables the raw port" describes a control that does not exist.
    assert "-rfbport" not in cmd
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
    monkeypatch.delenv("KASM_ENCODING_POLICY", raising=False)
    await vnc.start_vnc(100, 6100)
    cmd = xvnc_cmd["cmd"]
    assert "-IgnoreClientSettingsKasm" in cmd  # server owns encoding policy
    assert _flag_value(cmd, "-FrameRate") == "30"
    assert _flag_value(cmd, "-MaxVideoResolution") == "1600x900"
    assert "-RectThreads" not in cmd
    assert "-hw3d" not in cmd
    # -videoCodec would be dead weight next to -IgnoreClientSettingsKasm: the
    # server refuses the client's streaming-mode pseudo-encodings, so
    # cp.encoder_config stays `unavailable` and video mode never engages.
    assert "-videoCodec" not in cmd


@pytest.mark.asyncio
async def test_start_vnc_video_policy_swaps_the_exclusive_pair(
    vnc: VNCManager, xvnc_cmd, monkeypatch,
):
    """KASM_ENCODING_POLICY=video is the only way to reach WebCodecs H.264."""
    monkeypatch.setenv("KASM_HW3D", "0")
    monkeypatch.setenv("KASM_ENCODING_POLICY", "video")
    await vnc.start_vnc(100, 6100)
    cmd = xvnc_cmd["cmd"]
    assert _flag_value(cmd, "-videoCodec") == "h264"
    assert "-IgnoreClientSettingsKasm" not in cmd
    # The preset still ships; only who may override it changed.
    assert _flag_value(cmd, "-FrameRate") == "30"
    # ...except -MaxVideoResolution, which this policy makes inert and which
    # must NOT be passed as if it still capped anything.
    assert "-MaxVideoResolution" not in cmd
    # -TreatLossless is NOT inert here: it governs the Tight path, which is
    # still taken for every frame the client has not put in video mode.
    # Dropping it silently reverted the preset to the binary default (off).
    assert _flag_value(cmd, "-TreatLossless") == "8"


@pytest.mark.asyncio
async def test_start_vnc_logs_at_a_level_that_shows_codec_decisions(
    vnc: VNCManager, xvnc_cmd, monkeypatch,
):
    """Without -Log the applied/ignored codec lines are DEBUG-only.

    A session that fell back to Tight because the chosen encoder failed to open
    then looks identical in the shipped logs to one streaming correctly.
    """
    monkeypatch.setenv("KASM_HW3D", "0")
    monkeypatch.delenv("KASM_XVNC_LOG_LEVEL", raising=False)
    await vnc.start_vnc(100, 6100)
    assert _flag_value(xvnc_cmd["cmd"], "-Log") == "*:stdout:30"

    monkeypatch.setenv("KASM_XVNC_LOG_LEVEL", "100")
    await vnc.start_vnc(101, 6101)
    assert _flag_value(xvnc_cmd["cmd"], "-Log") == "*:stdout:100"

    monkeypatch.setenv("KASM_XVNC_LOG_LEVEL", "nonsense")
    await vnc.start_vnc(102, 6102)
    assert _flag_value(xvnc_cmd["cmd"], "-Log") == "*:stdout:30"


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["server-authoritative", "video", "nonsense"])
async def test_start_vnc_never_passes_both_encoding_flags(
    vnc: VNCManager, xvnc_cmd, monkeypatch, policy: str,
):
    """The pair is mutually exclusive in KasmVNC 1.5.0 — including on fallback."""
    monkeypatch.setenv("KASM_HW3D", "0")
    monkeypatch.setenv("KASM_ENCODING_POLICY", policy)
    await vnc.start_vnc(100, 6100)
    cmd = xvnc_cmd["cmd"]
    assert ("-IgnoreClientSettingsKasm" in cmd) != ("-videoCodec" in cmd)


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
    # launch() fails closed when it cannot identify the Chromium it just
    # started; a MagicMock context has no process behind it.
    monkeypatch.setattr(bm, "discover_browser_process_async", _discovers(_live_proc()))
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
    # launch() fails closed when it cannot identify the Chromium it just
    # started; a MagicMock context has no process behind it.
    monkeypatch.setattr(bm, "discover_browser_process_async", _discovers(_live_proc()))
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
    # launch() fails closed when it cannot identify the Chromium it just
    # started; a MagicMock context has no process behind it.
    monkeypatch.setattr(bm, "discover_browser_process_async", _discovers(_live_proc()))
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
    # launch() fails closed when it cannot identify the Chromium it just
    # started; a MagicMock context has no process behind it.
    monkeypatch.setattr(bm, "discover_browser_process_async", _discovers(_live_proc()))
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
        user_data_dir=str(tmp_path / "p1"), proc=_live_proc(),
    )
    monkeypatch.setattr(mgr.vnc, "stop_vnc", AsyncMock())

    assert await mgr.stop("p1") is False
    assert await mgr.check_wedged("p1") is True

    # a relaunch must be refused while the old Chromium is still alive
    with pytest.raises(bm.ProfileAlreadyRunning):
        await mgr.launch({"id": "p1", "user_data_dir": str(tmp_path / "p1")})

    # ...and allowed again once the close finally lands
    await mgr._on_browser_closed("p1", context)
    assert await mgr.check_wedged("p1") is False


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
    # launch() fails closed when it cannot identify the Chromium it just
    # started; a MagicMock context has no process behind it.
    monkeypatch.setattr(bm, "discover_browser_process_async", _discovers(_live_proc()))
    monkeypatch.setattr(mgr.vnc, "start_vnc", AsyncMock())
    monkeypatch.setattr(mgr.vnc, "stop_vnc", AsyncMock())

    with pytest.raises(aio.TimeoutError):
        await aio.wait_for(
            mgr.launch({"id": "p1", "user_data_dir": str(tmp_path / "p1")}), timeout=0.05,
        )

    assert mgr.is_starting("p1") is False
    assert "p1" not in mgr.running
    assert mgr.peek_wedged("p1") is True        # gated, not forgotten
    assert handlers, "close handler must be registered before the setup awaits"

    # the handler registered up-front is what eventually clears it
    await mgr._on_browser_closed("p1", context)
    assert mgr.peek_wedged("p1") is False


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
    mgr._closing["p1"] = _claim(_live_proc(), context=wedged_context)
    # some other instance now owns the id
    mgr.running["p1"] = bm.RunningProfile(
        profile_id="p1", context=MagicMock(name="B"), display=101, ws_port=6101, cdp_port=5101,
    )
    monkeypatch.setattr(mgr.vnc, "stop_vnc", AsyncMock())

    await mgr._on_browser_closed("p1", wedged_context)

    assert mgr.peek_wedged("p1") is False      # resolved
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
        proc=_live_proc(),
    )
    monkeypatch.setattr(mgr.vnc, "stop_vnc", AsyncMock())

    task = aio.ensure_future(mgr.stop("p1"))
    await aio.sleep(0)  # let stop() reach the close
    await aio.sleep(0)

    # mid-close: gone from running, but must NOT look free — and the read path
    # must say so, because "stopped" here offered the user a Launch button for
    # a browser that is still alive.
    assert "p1" not in mgr.running
    assert mgr.peek_wedged("p1") is True
    assert mgr.get_status("p1")["status"] == "stopping"

    release.set()
    assert await task is True                 # closed cleanly in the end
    assert mgr.peek_wedged("p1") is False     # and the claim is released
    assert mgr.get_status("p1")["status"] == "stopped"


@pytest.mark.asyncio
async def test_the_guard_releases_on_evidence_of_death_not_on_a_timer():
    """The release condition is "that process is gone", with no ceiling.

    The old guard released on elapsed time, which is wrong in one direction
    whatever the timer is set to: too short hands a live browser's
    user_data_dir to a delete, too long bricks the profile. (pid, starttime)
    answers the question directly, so neither trade-off is needed.
    """
    mgr = bm.BrowserManager()

    # a live process holds the guard however long the claim has been held; the
    # only thing elapsed time drives is escalation, stubbed out here
    signals: list[int] = []
    monkeypatch_kill = signals.append
    mgr._closing["p1"] = _claim(
        _live_proc(), claimed_at=time.monotonic() - 100_000,
    )
    original_kill = bm.os.kill
    try:
        bm.os.kill = lambda _pid, sig: monkeypatch_kill(sig)
        assert await mgr.check_wedged("p1") is True
    finally:
        bm.os.kill = original_kill
    assert "p1" in mgr._closing

    # ...and a claim whose process is gone releases at once, however fresh
    mgr._closing["p2"] = _claim(_dead_proc(), claimed_at=time.monotonic())
    assert await mgr.check_wedged("p2") is False
    assert "p2" not in mgr._closing


@pytest.mark.asyncio
async def test_a_stored_identity_whose_starttime_moved_is_never_signalled():
    """pid reuse must not turn escalation into killing a bystander."""
    mgr = bm.BrowserManager()
    killed: list[tuple[int, int]] = []
    # long past both escalation deadlines, so only the identity check can stop it
    mgr._closing["p1"] = _claim(
        _dead_proc(),
        claimed_at=time.monotonic() - (bm.CLOSING_SIGTERM_AFTER_S + 100),
    )

    original_kill = bm.os.kill
    try:
        bm.os.kill = lambda pid, sig: killed.append((pid, sig))
        assert await mgr.check_wedged("p1") is False
    finally:
        bm.os.kill = original_kill

    assert killed == []                        # the pid was recycled: hands off
    assert "p1" not in mgr._closing


@pytest.mark.asyncio
async def test_a_browser_that_will_not_close_is_escalated_then_released():
    """SIGTERM, then SIGKILL, then release only once the process is gone.

    This is what replaces the ceiling: the manager ENDS the teardown it is
    waiting on instead of choosing between bricking the profile and releasing
    the guard under a live Chromium. Run against a REAL process that ignores
    SIGTERM, because that is the case the two-step escalation exists for and
    the one a mock cannot express.
    """
    import asyncio as aio

    child = subprocess.Popen(
        [sys.executable, "-c",
         "import signal, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
         "sys.stdout.write('ready\\n'); sys.stdout.flush(); time.sleep(60)"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    # Handshake, not a sleep: interpreter start-up is slower than the first
    # SIGTERM, and a race here would silently test the wrong thing.
    assert child.stdout.readline().strip() == "ready"
    try:
        mgr = bm.BrowserManager()
        claim = _claim(
            _ident(child.pid),
            claimed_at=time.monotonic() - bm.CLOSING_SIGTERM_AFTER_S,
        )
        mgr._closing["p1"] = claim

        assert await mgr.check_wedged("p1") is True
        assert claim.sigterm_at is not None
        await aio.sleep(0.2)
        assert child.poll() is None            # SIGTERM ignored, still alive
        assert await mgr.check_wedged("p1") is True   # guard held, not released

        # past the SIGKILL grace period the manager stops asking nicely
        claim.sigterm_at = time.monotonic() - bm.CLOSING_SIGKILL_AFTER_S
        assert await mgr.check_wedged("p1") is True
        assert claim.sigkill_at is not None

        for _ in range(100):
            await aio.sleep(0.02)
            if not await mgr.check_wedged("p1"):
                break
        assert "p1" not in mgr._closing        # released on proof of death
        assert child.poll() is not None
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()


@pytest.mark.asyncio
async def test_a_launch_in_flight_is_never_escalated():
    """A launch holds a claim for its whole duration; it is not a teardown.

    Without this exemption a launch slower than CLOSING_SIGTERM_AFTER_S (the
    ceiling is LAUNCH_TIMEOUT_S = 60s) would have its own healthy Chromium
    SIGTERMed by the sweeper.
    """
    mgr = bm.BrowserManager()
    mgr._launching.add("p1")
    mgr._closing["p1"] = _claim(
        _live_proc(),
        claimed_at=time.monotonic() - (bm.CLOSING_SIGTERM_AFTER_S + 100),
    )

    signals: list[int] = []
    original_kill = bm.os.kill
    try:
        bm.os.kill = lambda pid, sig: signals.append(sig)
        assert await mgr.check_wedged("p1") is True
    finally:
        bm.os.kill = original_kill

    assert signals == []
    assert "p1" in mgr._closing        # and the entry is dropped


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
    # launch() fails closed when it cannot identify the Chromium it just
    # started; a MagicMock context has no process behind it.
    monkeypatch.setattr(bm, "discover_browser_process_async", _discovers(_live_proc()))
    monkeypatch.setattr(mgr.vnc, "start_vnc", AsyncMock())
    monkeypatch.setattr(mgr.vnc, "stop_vnc", AsyncMock())

    task = aio.ensure_future(mgr.launch({"id": "p1", "user_data_dir": str(tmp_path / "p1")}))
    for _ in range(8):
        await aio.sleep(0)          # let it reach the abort's close

    assert mgr.is_starting("p1") is False   # already out of _launching
    assert mgr.peek_wedged("p1") is True    # but NOT free

    release.set()
    with pytest.raises(RuntimeError, match="setup failed"):
        await task
    assert mgr.peek_wedged("p1") is False   # claim released once closed


@pytest.mark.asyncio
async def test_claim_is_not_released_while_the_browser_is_still_alive():
    """A headless profile survives stop_vnc(), so time says nothing.

    Killing the display tells us nothing about a headless Chromium; releasing
    on elapsed time hands a live browser's user_data_dir to the next launch or
    delete. Only the process itself can answer.
    """
    mgr = bm.BrowserManager()
    claim = _claim(_live_proc())
    mgr._closing["p1"] = claim
    assert await mgr.check_wedged("p1") is True
    assert mgr._closing["p1"] is claim         # held, and not rewritten

    mgr._closing["p1"] = _claim(_dead_proc())
    assert await mgr.check_wedged("p1") is False
    assert "p1" not in mgr._closing


@pytest.mark.asyncio
async def test_peek_wedged_never_probes_and_never_mutates():
    """The status path runs on an executor thread; it must be inert.

    get_liveness_async() runs get_liveness -> get_status in a thread pool. A
    probing, mutating check there raced the loop's own writes: a `del` raised
    KeyError when the close handler removed the entry first, and a re-write
    resurrected a claim stop() had already released, re-wedging a cleanly
    closed profile.
    """
    from unittest.mock import patch

    mgr = bm.BrowserManager()
    claim = _claim(_dead_proc(), claimed_at=time.monotonic() - 100_000)
    mgr._closing["abc"] = claim

    with patch("backend.browser_manager.socket.socket") as sock, \
            patch("backend.browser_manager.os.listdir") as listdir:
        for _ in range(5):
            assert mgr.peek_wedged("abc") is True
            assert mgr.get_status("abc")["status"] == "stopping"
            assert mgr.get_liveness("abc")["status"] == "stopping"
        sock.assert_not_called()
        listdir.assert_not_called()

    assert mgr._closing["abc"] is claim        # byte-identical: nothing moved


@pytest.mark.asyncio
async def test_resolve_does_not_clobber_a_replacement_claim():
    """The resurrection race: a verdict may only apply to the claim it probed."""
    mgr = bm.BrowserManager()
    probed = _claim(_dead_proc())
    replacement = _claim(_live_proc())
    mgr._closing["p1"] = replacement

    # a verdict computed for `probed` arrives late
    assert mgr._apply_claim_verdict("p1", probed, alive=False, discovered=None) is True
    assert mgr._closing["p1"] is replacement


@pytest.mark.asyncio
async def test_check_wedged_probes_off_the_event_loop():
    """A blocking probe under BrowserManager._lock froze the loop for 254ms.

    That loop also serves nginx's viewer auth_request subrequests for every
    live session, so an unrelated relaunch stalled every connected viewer.
    """
    import asyncio as aio

    mgr = bm.BrowserManager()
    mgr._closing["p1"] = _claim(_live_proc())
    seen: dict[str, bool] = {}
    original = bm.BrowserManager._claim_evidence

    def recording(claim):
        try:
            aio.get_running_loop()
            seen["on_loop"] = True
        except RuntimeError:
            seen["on_loop"] = False
        return original(claim)

    mgr._claim_evidence = staticmethod(recording)
    await mgr.check_wedged("p1")
    assert seen.get("on_loop") is False


@pytest.mark.asyncio
async def test_launch_does_not_probe_under_the_lock():
    """check_wedged must run before _lock is taken, not inside it."""
    import asyncio as aio

    mgr = bm.BrowserManager()
    mgr._closing["p1"] = _claim(_dead_proc())
    held: list[bool] = []

    def recording(claim):
        held.append(mgr._lock.locked())
        return (False, None)

    mgr._claim_evidence = staticmethod(recording)
    with pytest.raises(Exception):
        # the claim resolves, so the launch proceeds and fails later on I/O
        await mgr.launch({"id": "p1", "user_data_dir": "/nonexistent/x"})
    assert held == [False]


@pytest.mark.asyncio
async def test_the_sweeper_releases_a_claim_nobody_asks_about():
    """With a pure peek in the status path, nothing else opens the valve.

    Otherwise a profile whose browser exited quietly reports "stopping" with
    launch/stop/delete all refusing, forever — and the UI renders that as a
    disabled button, so there is no user action left that could release it.
    """
    mgr = bm.BrowserManager()
    mgr._closing["p1"] = _claim(_dead_proc())
    mgr._closing["p2"] = _claim(_live_proc())

    await mgr.sweep_teardown_claims()

    assert mgr.get_status("p1")["status"] == "stopped"
    assert mgr.get_status("p2")["status"] == "stopping"


@pytest.mark.asyncio
async def test_the_sweeper_survives_a_claim_that_raises():
    """One bad claim must not stop every other one from being resolved."""
    mgr = bm.BrowserManager()
    mgr._closing["bad"] = _claim(_live_proc())
    mgr._closing["good"] = _claim(_dead_proc())

    async def explode(profile_id):
        if profile_id == "bad":
            raise RuntimeError("boom")
        return mgr._apply_claim_verdict(
            profile_id, mgr._closing[profile_id], alive=False, discovered=None,
        )

    mgr.check_wedged = explode  # type: ignore[assignment]
    await mgr.sweep_teardown_claims()          # must not raise

    assert "good" not in mgr._closing


@pytest.mark.asyncio
async def test_the_reaper_retires_a_browser_whose_driver_died(monkeypatch):
    """Playwright emits NO close event when its node driver dies.

    The event is driven by a message FROM the driver, so killing the driver
    takes Chromium with it while is_closed() stays False and nothing fires.
    _on_browser_closed() then never runs: the profile reports "running"
    forever, /launch answers 409 forever, and the display, ws_port and
    password file are held for the life of the container.
    """
    from unittest.mock import AsyncMock

    mgr = bm.BrowserManager()
    context = MagicMock()
    context.is_closed.return_value = False     # exactly what the driver death shows
    mgr.running["p1"] = bm.RunningProfile(
        profile_id="p1", context=context, display=100, ws_port=6100, cdp_port=5100,
        proc=_dead_proc(),
    )
    stop_vnc = AsyncMock()
    monkeypatch.setattr(mgr.vnc, "stop_vnc", stop_vnc)

    await mgr.reap_dead_browsers()

    assert "p1" not in mgr.running
    stop_vnc.assert_awaited_once_with(100)
    assert mgr.get_status("p1")["status"] == "stopped"


@pytest.mark.asyncio
async def test_the_reaper_leaves_a_live_browser_alone(monkeypatch):
    from unittest.mock import AsyncMock

    mgr = bm.BrowserManager()
    mgr.running["p1"] = bm.RunningProfile(
        profile_id="p1", context=MagicMock(), display=100, ws_port=6100, cdp_port=5100,
        proc=_live_proc(),
    )
    stop_vnc = AsyncMock()
    monkeypatch.setattr(mgr.vnc, "stop_vnc", stop_vnc)

    await mgr.reap_dead_browsers()

    assert "p1" in mgr.running
    stop_vnc.assert_not_awaited()


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
    # launch() fails closed when it cannot identify the Chromium it just
    # started; a MagicMock context has no process behind it.
    monkeypatch.setattr(bm, "discover_browser_process_async", _discovers(_live_proc()))
    monkeypatch.setattr(mgr.vnc, "start_vnc", AsyncMock())
    stop_vnc = AsyncMock()
    monkeypatch.setattr(mgr.vnc, "stop_vnc", stop_vnc)

    with pytest.raises(RuntimeError, match="exited during launch"):
        await mgr.launch({"id": "p1", "user_data_dir": str(tmp_path / "p1")})

    assert stop_vnc.await_count == 1          # exactly once, not twice


@pytest.mark.asyncio
async def test_cancelling_stop_keeps_the_guard(monkeypatch):
    """The close is shielded, so a cancelled stop() must not free the profile.

    stop_vnc must be a coroutine that actually SUSPENDS and the task must be
    cancelled TWICE. With an AsyncMock (which never reaches a suspension
    point) and a single wait_for cancellation, the pending cancellation is
    never re-delivered inside the finally, so the shield the test is named
    after is a no-op and removing it leaves the suite green.
    """
    import asyncio as aio

    mgr = bm.BrowserManager()
    context = MagicMock()
    context.is_closed.return_value = False

    async def slow_close():
        await aio.sleep(3600)

    context.close = slow_close
    mgr.running["p1"] = bm.RunningProfile(
        profile_id="p1", context=context, display=100, ws_port=6100, cdp_port=5100,
        proc=_live_proc(),
    )

    released: list[int] = []

    async def suspending_stop_vnc(display: int):
        await aio.sleep(0.01)                  # a real stop_vnc takes a lock
        released.append(display)

    monkeypatch.setattr(mgr.vnc, "stop_vnc", suspending_stop_vnc)

    task = aio.ensure_future(mgr.stop("p1"))
    for _ in range(4):
        await aio.sleep(0)                     # let it reach the bounded close
    task.cancel()                              # 1: unwinds into the finally
    await aio.sleep(0)
    task.cancel()                              # 2: lands ON the stop_vnc await
    with pytest.raises(aio.CancelledError):
        await task
    await aio.sleep(0.05)                      # let the shielded task finish

    assert released == [100]                   # display, ws_port, passwd freed
    assert mgr.peek_wedged("p1") is True       # and still guarded


@pytest.mark.asyncio
async def test_a_recycled_pid_cannot_brick_a_profile():
    """A recycled pid is a DIFFERENT process, and the claim must release.

    The old guard keyed on "is anything listening on the remembered CDP port",
    which a later profile's Chromium could satisfy — so it needed an absolute
    ceiling to escape, and that ceiling then released real wedges early.
    (pid, starttime) cannot alias, so neither the aliasing nor the ceiling
    that compensated for it exists any more.
    """
    mgr = bm.BrowserManager()
    proc = _live_proc()
    mgr._closing["p1"] = _claim(proc)
    assert await mgr.check_wedged("p1") is True

    # same pid, different starttime: the pid was reused after our browser died
    mgr._closing["p1"] = _claim(
        bm.BrowserProcess(
            pid=proc.pid, starttime=proc.starttime + 1,
            user_data_dir=proc.user_data_dir, cdp_port=proc.cdp_port,
        ),
    )
    assert await mgr.check_wedged("p1") is False
    assert "p1" not in mgr._closing


@pytest.mark.asyncio
async def test_a_launch_cancelled_inside_playwright_still_guards_the_profile(
    monkeypatch, tmp_path,
):
    """Cancellation there is Python-side only; the driver keeps launching.

    LAUNCH_TIMEOUT_S is exactly this case. With the claim taken only once
    `context` existed, the Chromium the node driver went on to start had NO
    owner: the next launch unlinked the Singleton locks and opened a SECOND
    browser on the same live user_data_dir, and DELETE rmtree'd underneath it.
    """
    import asyncio as aio
    from unittest.mock import AsyncMock

    mgr = bm.BrowserManager()

    async def never_returns(*_a, **_k):
        await aio.sleep(3600)

    monkeypatch.setattr(bm, "launch_persistent_context_async", never_returns)
    monkeypatch.setattr(mgr.vnc, "start_vnc", AsyncMock())
    monkeypatch.setattr(mgr.vnc, "stop_vnc", AsyncMock())
    # the driver went on to start a Chromium the abandoned coroutine never saw
    monkeypatch.setattr(bm, "discover_browser_process", lambda *_a: _live_proc())

    with pytest.raises(aio.TimeoutError):
        await aio.wait_for(
            mgr.launch({"id": "p1", "user_data_dir": str(tmp_path / "p1")}),
            timeout=0.05,
        )

    assert mgr.is_starting("p1") is False
    assert "p1" not in mgr.running
    assert mgr.peek_wedged("p1") is True        # the orphan has an owner
    claim = mgr._closing["p1"]
    assert claim.context is None                # nothing to close: rediscover
    assert claim.user_data_dir == str(tmp_path / "p1")
    assert claim.cdp_port is not None

    # a relaunch is refused until the orphan is proven gone. Bounded, because
    # without the guard this call reaches the hanging Playwright stub and the
    # failure mode would be a hung suite rather than a failed assertion.
    with pytest.raises(bm.ProfileAlreadyRunning):
        await aio.wait_for(
            mgr.launch({"id": "p1", "user_data_dir": str(tmp_path / "p1")}),
            timeout=2.0,
        )


@pytest.mark.asyncio
async def test_an_identity_less_claim_adopts_the_orphan_the_scan_finds(monkeypatch):
    """A claim with no context resolves by looking for the process itself."""
    mgr = bm.BrowserManager()
    orphan = _live_proc(cdp_port=5177, udd="/tmp/orphan")
    mgr._closing["p1"] = bm.ClosingClaim(
        context=None, proc=None, user_data_dir="/tmp/orphan", cdp_port=5177,
        claimed_at=time.monotonic(),
    )
    monkeypatch.setattr(bm, "discover_browser_process", lambda *_a: orphan)

    assert await mgr.check_wedged("p1") is True
    assert mgr._closing["p1"].proc == orphan    # identity adopted, guard held


@pytest.mark.asyncio
async def test_an_identity_less_claim_releases_when_no_orphan_exists(monkeypatch):
    mgr = bm.BrowserManager()
    mgr._closing["p1"] = bm.ClosingClaim(
        context=None, proc=None, user_data_dir="/tmp/orphan", cdp_port=5177,
        claimed_at=time.monotonic(),
    )
    monkeypatch.setattr(bm, "discover_browser_process", lambda *_a: None)

    assert await mgr.check_wedged("p1") is False
    assert "p1" not in mgr._closing


@pytest.mark.asyncio
async def test_launch_fails_closed_when_the_browser_cannot_be_identified(
    monkeypatch, tmp_path,
):
    """An unidentifiable browser can be neither proven dead nor escalated.

    Registering one would put the teardown guard back on guesswork, which is
    the thing the pid identity exists to remove.
    """
    from unittest.mock import AsyncMock

    mgr = bm.BrowserManager()
    context = MagicMock()
    context.is_closed.return_value = False
    context.pages = []
    context.on = MagicMock()
    context.close = AsyncMock()
    context.add_init_script = AsyncMock()

    monkeypatch.setattr(bm, "launch_persistent_context_async", AsyncMock(return_value=context))
    monkeypatch.setattr(bm, "discover_browser_process_async", _discovers(None))
    monkeypatch.setattr(mgr.vnc, "start_vnc", AsyncMock())
    monkeypatch.setattr(mgr.vnc, "stop_vnc", AsyncMock())

    with pytest.raises(RuntimeError, match="Could not identify"):
        await mgr.launch({"id": "p1", "user_data_dir": str(tmp_path / "p1")})

    assert "p1" not in mgr.running
    context.close.assert_awaited()              # and the browser was closed


@pytest.mark.asyncio
async def test_a_successful_launch_drops_the_launch_phase_claim(monkeypatch, tmp_path):
    """A live profile must not read as "stopping" or refuse stop/delete."""
    from unittest.mock import AsyncMock

    mgr = bm.BrowserManager()
    context = MagicMock()
    context.is_closed.return_value = False
    context.pages = []
    context.on = MagicMock()
    context.add_init_script = AsyncMock()

    proc = _live_proc()
    monkeypatch.setattr(bm, "launch_persistent_context_async", AsyncMock(return_value=context))
    monkeypatch.setattr(bm, "discover_browser_process_async", _discovers(proc))
    monkeypatch.setattr(mgr.vnc, "start_vnc", AsyncMock())
    monkeypatch.setattr(mgr.vnc, "stop_vnc", AsyncMock())

    running = await mgr.launch({"id": "p1", "user_data_dir": str(tmp_path / "p1")})

    assert running.proc == proc
    assert running.session_epoch                # a nonce, not a recycled port
    assert mgr.peek_wedged("p1") is False
    assert mgr.get_status("p1")["status"] == "running"


@pytest.mark.asyncio
async def test_stop_reports_false_when_it_loses_the_pop_race(monkeypatch):
    """The documented contract is what DELETE trusts before it rmtrees.

    The early return used to answer True for any caller that found nothing in
    `running` — including one racing a teardown that is still awaiting a close
    which may never land.
    """
    import asyncio as aio
    from unittest.mock import AsyncMock

    mgr = bm.BrowserManager()
    release = aio.Event()
    context = MagicMock()
    context.is_closed.return_value = False

    async def slow_close():
        await release.wait()

    context.close = slow_close
    mgr.running["p1"] = bm.RunningProfile(
        profile_id="p1", context=context, display=100, ws_port=6100, cdp_port=5100,
        proc=_live_proc(),
    )
    monkeypatch.setattr(mgr.vnc, "stop_vnc", AsyncMock())

    first = aio.ensure_future(mgr.stop("p1"))
    for _ in range(4):
        await aio.sleep(0)
    assert mgr.peek_wedged("p1") is True

    assert await mgr.stop("p1") is False        # lost the race, and says so

    release.set()
    assert await first is True


@pytest.mark.asyncio
async def test_on_browser_closed_cannot_clear_a_claim_it_does_not_own(monkeypatch):
    """The identity check used to be bypassed whenever context was omitted.

    Correct for its single caller, and a silent way for any second caller to
    hand a live Chromium's user_data_dir to the next launch.
    """
    from unittest.mock import AsyncMock

    mgr = bm.BrowserManager()
    other_context = MagicMock(name="A")
    mgr._closing["p1"] = _claim(_live_proc(), context=other_context)
    mgr.running["p1"] = bm.RunningProfile(
        profile_id="p1", context=MagicMock(name="B"), display=101, ws_port=6101,
        cdp_port=5101, proc=_live_proc(),
    )
    monkeypatch.setattr(mgr.vnc, "stop_vnc", AsyncMock())

    await mgr._on_browser_closed("p1", mgr.running["p1"].context)

    assert mgr.peek_wedged("p1") is True        # A's claim survives B's close
    assert mgr._closing["p1"].context is other_context


# ── headless profiles never allocate a display ───────────────────────────────


def _launchable_context():
    """A context that survives launch()'s registration re-check."""
    from unittest.mock import AsyncMock
    context = MagicMock()
    context.is_closed.return_value = False
    context.pages = []
    context.add_init_script = AsyncMock()
    context.close = AsyncMock()
    context.on = MagicMock()
    return context


@pytest.mark.asyncio
async def test_headless_launch_allocates_no_display_and_starts_no_xvnc(
    monkeypatch, tmp_path,
):
    """A headless Chromium never draws to X, so an Xvnc for it is pure waste.

    Twenty headless scraping profiles used to mean twenty idle Xvnc servers,
    twenty displays, twenty ws ports and twenty password files that no pixel
    would ever traverse — plus a viewer affordance onto an empty root window.
    """
    from unittest.mock import AsyncMock
    from backend import browser_manager as bm

    mgr = bm.BrowserManager()
    monkeypatch.setattr(
        bm, "launch_persistent_context_async", AsyncMock(return_value=_launchable_context()),
    )
    monkeypatch.setattr(bm, "discover_browser_process_async", _discovers(_live_proc()))
    allocate = AsyncMock(side_effect=AssertionError("allocate() must not be called"))
    start_vnc = AsyncMock(side_effect=AssertionError("start_vnc() must not be called"))
    monkeypatch.setattr(mgr.vnc, "allocate", allocate)
    monkeypatch.setattr(mgr.vnc, "start_vnc", start_vnc)

    running = await mgr.launch(
        {"id": "p1", "user_data_dir": str(tmp_path / "p1"), "headless": True},
    )

    assert running.display is None
    assert running.ws_port is None
    assert mgr.vnc.active_displays == []
    status = mgr.get_status("p1")
    assert status["display"] is None and status["vnc_ws_port"] is None
    assert mgr.get_liveness("p1")["xvnc_alive"] is None


@pytest.mark.asyncio
async def test_headed_launch_still_allocates_a_display(monkeypatch, tmp_path):
    """The control case: skipping Xvnc must be conditional, not unconditional."""
    from unittest.mock import AsyncMock
    from backend import browser_manager as bm

    mgr = bm.BrowserManager()
    monkeypatch.setattr(
        bm, "launch_persistent_context_async", AsyncMock(return_value=_launchable_context()),
    )
    monkeypatch.setattr(bm, "discover_browser_process_async", _discovers(_live_proc()))
    start_vnc = AsyncMock()
    monkeypatch.setattr(mgr.vnc, "start_vnc", start_vnc)

    running = await mgr.launch(
        {"id": "p1", "user_data_dir": str(tmp_path / "p1"), "headless": False},
    )

    assert running.display == 100
    assert running.ws_port == 6100
    start_vnc.assert_awaited_once()
    assert mgr.get_status("p1")["display"] == ":100"


@pytest.mark.asyncio
async def test_stopping_a_headless_profile_needs_no_display_teardown(
    monkeypatch, tmp_path,
):
    """Every teardown path funnels through _release_display; None is normal."""
    from unittest.mock import AsyncMock
    from backend import browser_manager as bm

    mgr = bm.BrowserManager()
    monkeypatch.setattr(
        bm, "launch_persistent_context_async", AsyncMock(return_value=_launchable_context()),
    )
    monkeypatch.setattr(bm, "discover_browser_process_async", _discovers(_live_proc()))
    monkeypatch.setattr(mgr.vnc, "allocate", AsyncMock(side_effect=AssertionError("no")))
    stop_vnc = AsyncMock()
    monkeypatch.setattr(mgr.vnc, "stop_vnc", stop_vnc)

    await mgr.launch({"id": "p1", "user_data_dir": str(tmp_path / "p1"), "headless": True})
    assert await mgr.stop("p1") is True
    assert "p1" not in mgr.running
    stop_vnc.assert_not_awaited()   # nothing was ever allocated to stop
