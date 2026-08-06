"""Tests for browser_manager pure functions — proxy parsing, fingerprint args, profile defaults."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import socket

from backend import browser_manager as bm
from backend.vnc_manager import DRI_RENDER_NODE_DEFAULT
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


def test_build_args_always_includes_base(monkeypatch: pytest.MonkeyPatch):
    # Pin the GL backend explicitly. Left on 'auto' this assertion depends on
    # whether the machine running the tests happens to have /dev/nvidiactl —
    # it passes on CI and fails on any developer box with an NVIDIA GPU.
    monkeypatch.setenv("CHROME_GPU_ACCEL", "0")
    args = _mgr._build_fingerprint_args({})
    assert "--disable-infobars" in args
    assert "--test-type" in args
    assert "--use-angle=swiftshader" in args


def test_build_args_uses_gpu_flags_when_enabled(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CHROME_GPU_ACCEL", "nvidia")
    args = _mgr._build_fingerprint_args({})
    assert "--use-angle=vulkan" in args
    assert "--use-gl=angle" in args
    # Chromium takes the LAST --use-angle, so emitting both would make the
    # backend depend on argv order.
    assert "--use-angle=swiftshader" not in args


def test_gpu_env_only_set_in_gpu_mode(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CHROME_GPU_ACCEL", "0")
    assert bm._chrome_gpu_env() == {}
    monkeypatch.setenv("CHROME_GPU_ACCEL", "nvidia")
    assert bm._chrome_gpu_env()["__EGL_VENDOR_LIBRARY_FILENAMES"].endswith(
        "10_nvidia.json"
    )


# ── _chrome_gpu_mode / _chrome_gpu_flags ─────────────────────────────────────


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "swiftshader"])
def test_gpu_mode_disabled_values(monkeypatch: pytest.MonkeyPatch, value: str):
    monkeypatch.setenv("CHROME_GPU_ACCEL", value)
    assert bm._chrome_gpu_mode() == bm._GPU_MODE_SWIFTSHADER


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "nvidia"])
def test_gpu_mode_forced_values_skip_device_detection(
    monkeypatch: pytest.MonkeyPatch, value: str,
):
    """Forcing must not consult the device node.

    A GPU can be passed in ways this detection does not recognise, and an
    operator who says "yes, it is there" should not be overruled by it.
    """
    monkeypatch.setenv("CHROME_GPU_ACCEL", value)
    monkeypatch.setattr(bm.os.path, "exists", lambda _: False)
    assert bm._chrome_gpu_mode() == bm._GPU_MODE_NVIDIA


def test_gpu_mode_auto_detects_nvidia_device(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CHROME_GPU_ACCEL", "auto")
    monkeypatch.setattr(
        bm.os.path, "exists", lambda p: p == bm._NVIDIA_CONTROL_DEVICE,
    )
    assert bm._chrome_gpu_mode() == bm._GPU_MODE_NVIDIA


def test_gpu_mode_auto_without_device_stays_software(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CHROME_GPU_ACCEL", "auto")
    monkeypatch.setattr(bm.os.path, "exists", lambda _: False)
    assert bm._chrome_gpu_mode() == bm._GPU_MODE_SWIFTSHADER
    assert bm._chrome_gpu_flags() == ["--use-angle=swiftshader"]


def test_gpu_mode_defaults_to_auto_when_unset(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CHROME_GPU_ACCEL", raising=False)
    monkeypatch.setattr(bm.os.path, "exists", lambda _: False)
    assert bm._chrome_gpu_mode() == bm._GPU_MODE_SWIFTSHADER


def test_gpu_mode_unknown_value_warns_and_falls_back_to_auto(
    monkeypatch: pytest.MonkeyPatch, caplog,
):
    """A typo must not silently pick a branch.

    'auto' is the safe landing spot: it still enables the GPU when one is
    actually present, so a misspelling degrades to detection rather than to a
    hard software fallback the operator did not ask for.
    """
    monkeypatch.setenv("CHROME_GPU_ACCEL", "nvida")
    monkeypatch.setattr(
        bm.os.path, "exists", lambda p: p == bm._NVIDIA_CONTROL_DEVICE,
    )
    with caplog.at_level("WARNING", logger="cloakbrowser.manager.browser"):
        assert bm._chrome_gpu_mode() == bm._GPU_MODE_NVIDIA
    assert "Unknown CHROME_GPU_ACCEL" in caplog.text


def test_nvidia_flags_never_include_the_deprecated_use_gl_egl():
    """--use-gl=egl silently DISABLES WebGL on modern Chromium.

    It reads like the flag that turns on EGL, which is why it keeps coming
    back; CloakBrowser PR #476 exists because of it.
    """
    flags = list(bm._NVIDIA_GPU_FLAGS)
    assert "--use-gl=egl" not in flags
    assert "--use-gl=angle" in flags


def test_nvidia_backend_is_vulkan_not_gl_egl():
    """gl-egl is the SOFTWARE path here, despite looking like the GPU one.

    Measured headed on KasmVNC's Xvnc, same host: --use-angle=gl-egl binds
    "ANGLE (Mesa, llvmpipe)" because NVIDIA's EGL declines the X11 platform on
    an X server it does not drive and GLVND falls through to Mesa. Pinning the
    NVIDIA EGL manifest recovers the renderer but leaves
    gpu_compositing=disabled_software. Only the Vulkan backend gets both, so
    this asserts the distinction rather than merely "some GPU flag is present".
    """
    assert "--use-angle=vulkan" in bm._NVIDIA_GPU_FLAGS
    assert "--use-angle=gl-egl" not in bm._NVIDIA_GPU_FLAGS


# ── the Mesa / integrated-GPU mode ───────────────────────────────────────────


@pytest.mark.parametrize("value", ["igpu", "vaapi", "mesa", "intel", "amd"])
def test_gpu_mode_igpu_forced_values_skip_device_detection(
    monkeypatch: pytest.MonkeyPatch, value: str,
):
    monkeypatch.setenv("CHROME_GPU_ACCEL", value)
    monkeypatch.setattr(bm.os.path, "exists", lambda _: False)
    assert bm._chrome_gpu_mode() == bm._GPU_MODE_IGPU


@pytest.mark.parametrize("driver", ["amdgpu", "i915", "xe", None])
def test_gpu_mode_auto_detects_a_mesa_render_node(
    monkeypatch: pytest.MonkeyPatch, driver: str | None,
):
    """An unresolvable driver counts as Mesa.

    /sys is often not introspectable from inside a container, and the only
    driver this has to exclude is nvidia — whose runtime always creates
    /dev/nvidiactl, which is checked first.
    """
    monkeypatch.setenv("CHROME_GPU_ACCEL", "auto")
    monkeypatch.setattr(
        bm.os.path, "exists", lambda p: p == DRI_RENDER_NODE_DEFAULT,
    )
    monkeypatch.setattr(bm, "_dri_driver", lambda _n: driver)
    assert bm._chrome_gpu_mode() == bm._GPU_MODE_IGPU


def test_gpu_mode_auto_prefers_nvidia_when_both_devices_exist(
    monkeypatch: pytest.MonkeyPatch,
):
    """The NVIDIA runtime injects a render node too.

    So an NVIDIA host satisfies the DRI check as well, and testing it second is
    what stops it being handed the Mesa flag set — which on the closed driver
    means llvmpipe with the GPU idle.
    """
    monkeypatch.setenv("CHROME_GPU_ACCEL", "auto")
    monkeypatch.setattr(bm.os.path, "exists", lambda _: True)
    monkeypatch.setattr(bm, "_dri_driver", lambda _n: "amdgpu")
    assert bm._chrome_gpu_mode() == bm._GPU_MODE_NVIDIA


def test_gpu_mode_auto_nvidia_render_node_without_the_control_device(
    monkeypatch: pytest.MonkeyPatch,
):
    """A render node Mesa cannot drive, and no injected NVIDIA userspace.

    Neither path can reach this GPU, so claiming acceleration would be the
    silent-llvmpipe failure rather than a degraded version of it.
    """
    monkeypatch.setenv("CHROME_GPU_ACCEL", "auto")
    monkeypatch.setattr(
        bm.os.path, "exists", lambda p: p == DRI_RENDER_NODE_DEFAULT,
    )
    monkeypatch.setattr(bm, "_dri_driver", lambda _n: "nvidia")
    assert bm._chrome_gpu_mode() == bm._GPU_MODE_SWIFTSHADER


def test_gpu_mode_auto_honours_kasm_drinode(monkeypatch: pytest.MonkeyPatch):
    """Chromium must probe the SAME node Xvnc encodes on.

    Two GPUs and two different nodes would look accelerated on both halves
    while paying a cross-device copy per frame, which is why one resolver
    (vnc_manager._dri_render_node) serves both.
    """
    monkeypatch.setenv("CHROME_GPU_ACCEL", "auto")
    monkeypatch.setenv("KASM_DRINODE", "/dev/dri/renderD129")
    monkeypatch.setattr(
        bm.os.path, "exists", lambda p: p == "/dev/dri/renderD129",
    )
    monkeypatch.setattr(bm, "_dri_driver", lambda _n: "amdgpu")
    assert bm._chrome_gpu_mode() == bm._GPU_MODE_IGPU


def test_igpu_flags_default_to_vulkan(monkeypatch: pytest.MonkeyPatch):
    """gl-egl reaches the GPU here and still composites through the CPU.

    Measured on an AMD Raphael iGPU under Xvnc with -hw3d, Mesa 25.0.7:
    gl-egl bound radeonsi but reported webgl=enabled_readback and
    gpu_compositing=disabled_software; vulkan bound radv and reported
    webgl=enabled with gpu_compositing=enabled. So the default is not the
    "obvious" Mesa EGL path — asserting the exact list is what keeps a
    plausible-looking edit from quietly halving the acceleration.
    """
    monkeypatch.setenv("CHROME_GPU_ACCEL", "igpu")
    monkeypatch.delenv("CHROME_ANGLE_BACKEND", raising=False)
    flags = bm._chrome_gpu_flags()
    assert flags == [
        "--use-gl=angle", "--use-angle=vulkan",
        "--enable-gpu-rasterization", "--ignore-gpu-blocklist",
    ]


@pytest.mark.parametrize("backend", ["gl-egl", "vulkan", "gl"])
def test_igpu_angle_backend_is_selectable(
    monkeypatch: pytest.MonkeyPatch, backend: str,
):
    """Which backend reaches the GPU varies by driver and Mesa version.

    And the wrong one fails silently, so it has to be changeable on a host
    without a rebuild.
    """
    monkeypatch.setenv("CHROME_GPU_ACCEL", "igpu")
    monkeypatch.setenv("CHROME_ANGLE_BACKEND", backend)
    flags = bm._chrome_gpu_flags()
    assert f"--use-angle={backend}" in flags
    # Chromium takes the LAST --use-angle, so exactly one may be emitted.
    assert len([f for f in flags if f.startswith("--use-angle=")]) == 1


def test_igpu_angle_backend_refuses_swiftshader(
    monkeypatch: pytest.MonkeyPatch, caplog,
):
    """The one value that would make CHROME_GPU_ACCEL=igpu a lie.

    swiftshader is a legal --use-angle value, so without the allow-list it
    would be passed through and every frame would rasterise on the CPU under a
    configuration that says otherwise.
    """
    monkeypatch.setenv("CHROME_GPU_ACCEL", "igpu")
    monkeypatch.setenv("CHROME_ANGLE_BACKEND", "swiftshader")
    with caplog.at_level("WARNING", logger="cloakbrowser.manager.browser"):
        flags = bm._chrome_gpu_flags()
    assert "--use-angle=swiftshader" not in flags
    assert f"--use-angle={bm._IGPU_ANGLE_BACKEND_DEFAULT}" in flags
    assert "Unknown CHROME_ANGLE_BACKEND" in caplog.text


def test_igpu_mode_sets_no_egl_vendor_override(monkeypatch: pytest.MonkeyPatch):
    """NVIDIA's pin exists because its ICD lands beside Mesa's.

    The iGPU image installs no second EGL vendor, so pinning a manifest it did
    not install could only break EGL outright.
    """
    monkeypatch.setenv("CHROME_GPU_ACCEL", "igpu")
    assert bm._chrome_gpu_env() == {}


def test_build_args_uses_igpu_flags_when_selected(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CHROME_GPU_ACCEL", "igpu")
    args = _mgr._build_fingerprint_args({})
    assert "--use-gl=angle" in args
    assert "--use-angle=swiftshader" not in args


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


def test_build_args_empty_profile(monkeypatch: pytest.MonkeyPatch):
    """An empty profile contributes nothing beyond the base args.

    The GL backend is pinned because it is no longer a fixed single flag —
    on 'auto' the count varies with whether the host running the tests has an
    NVIDIA device, which is not something this test is trying to assert.
    """
    monkeypatch.setenv("CHROME_GPU_ACCEL", "0")
    args = _mgr._build_fingerprint_args({})
    assert args == ["--disable-infobars", "--test-type", "--use-angle=swiftshader"]


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
    assert "Google" in data["default_search_provider_data"]["template_url_data"]["short_name"]
    assert data["default_search_provider"]["enabled"] is True


def test_init_idempotent(tmp_path: Path):
    _init_profile_defaults(tmp_path)
    bookmarks_path = tmp_path / "Default" / "Bookmarks"
    original = bookmarks_path.read_text()

    # Write a sentinel to the file
    bookmarks_path.write_text("SENTINEL")

    # Second call should NOT overwrite (file already exists)
    _init_profile_defaults(tmp_path)
    assert bookmarks_path.read_text() == "SENTINEL"


# ── /proc identity ───────────────────────────────────────────────────────────


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_proc_argv_normalises_chromiums_space_joined_cmdline(monkeypatch, tmp_path: Path):
    """Chromium rewrites argv into ONE space-joined buffer, not NUL-separated.

    The textbook split(b"\\0") returns a single blob, so the obvious scan finds
    nothing against a real browser while passing every test written against a
    plain Python child — which uses the normal encoding. Feed the reader the
    encoding a real Chromium produces, because a subprocess decoy cannot.
    """
    import builtins

    blob = tmp_path / "cmdline"
    blob.write_bytes(
        b"/opt/chrome --headless=new --no-sandbox "
        b"--user-data-dir=/data/profiles/abc --remote-debugging-port=5100\0"
    )
    real_open = builtins.open
    monkeypatch.setattr(
        builtins, "open",
        lambda path, *a, **k: real_open(
            blob if str(path).endswith("/cmdline") else path, *a, **k
        ),
    )

    assert bm._proc_argv(4242) == [
        "/opt/chrome", "--headless=new", "--no-sandbox",
        "--user-data-dir=/data/profiles/abc", "--remote-debugging-port=5100",
    ]


def test_proc_argv_reads_the_ordinary_nul_separated_form():
    """The normalisation must not break the encoding every other process uses."""
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", "--marker=zzz"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            argv = bm._proc_argv(child.pid)
            if "--marker=zzz" in argv:
                break
            time.sleep(0.02)
        assert argv[-1] == "--marker=zzz"
        assert argv[0].endswith("python") or "python" in argv[0]
    finally:
        child.kill()
        child.wait()


def test_discover_finds_the_browser_and_skips_renderers_and_strangers(tmp_path: Path):
    """Renderers carry the SAME --user-data-dir and --remote-debugging-port.

    Excluding --type= is mandatory, not defensive: without it the scan returns
    a renderer, and every later liveness check and every escalation targets
    the wrong process.
    """
    udd = str(tmp_path / "profile")
    port = _free_port()
    children = [
        subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)", *argv],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for argv in (
            [f"--user-data-dir={udd}", f"--remote-debugging-port={port}"],
            [f"--user-data-dir={udd}", f"--remote-debugging-port={port}",
             "--type=renderer"],
            [f"--user-data-dir={udd}-other", f"--remote-debugging-port={port}"],
            [f"--user-data-dir={udd}", f"--remote-debugging-port={port + 1}"],
        )
    ]
    browser = children[0]
    try:
        for _ in range(100):
            found = bm.discover_browser_process(udd, port, os.getpid())
            if found is not None:
                break
            time.sleep(0.02)
        assert found is not None
        assert found.pid == browser.pid
        assert found.starttime == bm._proc_stat(browser.pid)[2]
    finally:
        for child in children:
            child.kill()
            child.wait()

    assert bm.discover_browser_process(udd, port, os.getpid()) is None


def test_a_renderer_is_never_mistaken_for_the_browser(tmp_path: Path):
    """Renderers inherit --user-data-dir AND --remote-debugging-port.

    With only a renderer running, the scan must find nothing: returning it
    would make every later liveness check and every escalation target a child
    that dies and respawns independently of the browser.
    """
    udd = str(tmp_path / "profile")
    port = _free_port()
    renderer = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)",
         f"--user-data-dir={udd}", f"--remote-debugging-port={port}",
         "--type=renderer"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            if f"--user-data-dir={udd}" in bm._proc_argv(renderer.pid):
                break
            time.sleep(0.02)
        assert f"--type=renderer" in bm._proc_argv(renderer.pid)
        assert bm.discover_browser_process(udd, port, os.getpid()) is None
    finally:
        renderer.kill()
        renderer.wait()


def test_discover_ignores_a_match_outside_our_process_tree(tmp_path: Path):
    """The manager must never be able to signal a process it did not start."""
    udd = str(tmp_path / "profile")
    port = _free_port()
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)",
         f"--user-data-dir={udd}", f"--remote-debugging-port={port}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        assert bm.discover_browser_process(udd, port, os.getpid()) is not None
        # same predicate, a root we are not below: no match
        assert bm.discover_browser_process(udd, port, root_pid=-1) is None
    finally:
        child.kill()
        child.wait()


def test_process_is_alive_treats_a_zombie_as_dead():
    """PID 1 here is a shell with no reaping, so a killed browser can sit in Z.

    Counting Z as alive would mean the teardown guard never releases.
    """
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.exit(0)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        proc = bm.BrowserProcess(
            pid=child.pid, starttime=bm._proc_stat(child.pid)[2],
            user_data_dir="/tmp/x", cdp_port=5100,
        )
        for _ in range(200):
            stat = bm._proc_stat(child.pid)
            if stat is not None and stat[0] == "Z":
                break
            time.sleep(0.01)
        assert stat is not None and stat[0] == "Z", "needed a zombie to test"
        assert bm.process_is_alive(proc) is False
    finally:
        child.wait()


def test_process_is_alive_rejects_a_recycled_pid():
    """pid alone is not an identity; starttime is what makes it one."""
    stat = bm._proc_stat(os.getpid())
    same_pid_other_process = bm.BrowserProcess(
        pid=os.getpid(), starttime=stat[2] + 1, user_data_dir="/tmp/x", cdp_port=5100,
    )
    assert bm.process_is_alive(same_pid_other_process) is False


def test_signal_process_refuses_a_recycled_pid(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(bm.os, "kill", lambda pid, sig: calls.append(pid))
    stat = bm._proc_stat(os.getpid())
    stale = bm.BrowserProcess(
        pid=os.getpid(), starttime=stat[2] + 1, user_data_dir="/tmp/x", cdp_port=5100,
    )
    assert bm._signal_process(stale, signal.SIGTERM) is False
    assert calls == []


# ── _close_context_bounded ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_close_context_bounded_does_not_trust_is_closed(monkeypatch):
    """playwright sets _closing_or_closed BEFORE its first await.

    is_closed() therefore means "closing or closed", so short-circuiting on it
    reported success for a close that was merely in flight — and the caller
    then dropped the teardown guard while Chromium was still writing to
    user_data_dir.
    """
    monkeypatch.setattr(bm, "CONTEXT_CLOSE_TIMEOUT_S", 0.05)
    state = {"closed": False}

    class HangingContext:
        def is_closed(self):
            return state["closed"]

        async def close(self):
            state["closed"] = True     # exactly what playwright does, up front
            await asyncio.sleep(3600)

    context = HangingContext()
    assert await bm._close_context_bounded(context, "p1") is False
    assert context.is_closed() is True          # ...and yet still alive
    assert await bm._close_context_bounded(context, "p1") is False


# ── viewer-token revocation ordering ─────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["stop", "closed"])
async def test_teardown_revokes_before_the_profile_can_be_relaunched(monkeypatch, path):
    """No teardown may leave a live token behind once `running` is clear.

    This is the premise that makes /api/viewer-auth's epoch check unreachable,
    and it is the property that actually protects the session: a token that
    outlived its launch would be authorized against the NEXT one, handed that
    session's upstream port and that display's Kasm credentials.

    Asserted at the moment of removal, not afterwards, because "revoked
    eventually" is not the same guarantee — a launch racing into the gap is
    precisely the case the epoch check exists to backstop.
    """
    from backend.viewer_tokens import viewer_tokens

    mgr = BrowserManager()
    context = MagicMock()
    context.is_closed.return_value = False
    context.close = AsyncMock()
    monkeypatch.setattr(mgr.vnc, "stop_vnc", AsyncMock())

    running = bm.RunningProfile(
        profile_id="p1", context=context, display=100, ws_port=6100, cdp_port=5100,
    )
    mgr.running["p1"] = running
    token = viewer_tokens.issue("p1", 6100, session_epoch=running.session_epoch)
    assert viewer_tokens.validate(token) is not None

    # Record whether the profile was still registered when revocation ran, so a
    # future reorder that revokes too late is caught here rather than in prod.
    observed: list[bool] = []
    real_revoke = viewer_tokens.revoke_profile

    def spy(profile_id: str) -> None:
        observed.append(profile_id in mgr.running)
        real_revoke(profile_id)

    monkeypatch.setattr(viewer_tokens, "revoke_profile", spy)

    try:
        if path == "stop":
            await mgr.stop("p1")
        else:
            await mgr._on_browser_closed("p1", context)

        assert observed, "teardown did not revoke this profile's viewer tokens"
        assert observed[-1] is False, "revoked while still registered as running"
        assert "p1" not in mgr.running
        assert viewer_tokens.validate(token) is None
    finally:
        monkeypatch.setattr(viewer_tokens, "revoke_profile", real_revoke)
        viewer_tokens.revoke_profile("p1")


# ── cleanup_all / auto_launch_all ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cleanup_all_stops_every_profile_concurrently(monkeypatch):
    """Shutdown cost must be the slowest profile, not the sum of all of them.

    Sequentially it is the SUM of every CONTEXT_CLOSE_TIMEOUT_S plus Xvnc
    teardown, so a handful of profiles exceeds Docker's stop grace period and
    the container is SIGKILLed mid-cleanup — killing uncleanly the very
    browsers the ordered shutdown exists to protect. conftest mocks this
    method away in every app fixture, so it must be exercised directly.
    """
    mgr = BrowserManager()
    windows: list[tuple[str, float]] = []

    def make_context(name: str):
        context = MagicMock()
        context.is_closed.return_value = False

        async def close():
            windows.append((f"start-{name}", time.monotonic()))
            await asyncio.sleep(0.05)
            windows.append((f"end-{name}", time.monotonic()))

        context.close = close
        return context

    for index in range(4):
        mgr.running[f"p{index}"] = bm.RunningProfile(
            profile_id=f"p{index}", context=make_context(str(index)),
            display=100 + index, ws_port=6100 + index, cdp_port=5100 + index,
        )
    monkeypatch.setattr(mgr.vnc, "stop_vnc", AsyncMock())
    vnc_cleanup = AsyncMock()
    monkeypatch.setattr(mgr.vnc, "cleanup_all", vnc_cleanup)

    started = time.monotonic()
    await mgr.cleanup_all()
    elapsed = time.monotonic() - started

    assert mgr.running == {}
    vnc_cleanup.assert_awaited_once()
    assert elapsed < 0.15                       # not 4 x 0.05 in series
    assert [w[0] for w in windows[:4]] == [f"start-{i}" for i in range(4)]


@pytest.mark.asyncio
async def test_cleanup_all_survives_one_profile_that_raises(monkeypatch):
    """One broken teardown must not strand the others or the vnc cleanup."""
    mgr = BrowserManager()
    stopped: list[str] = []

    async def stop(profile_id: str) -> bool:
        if profile_id == "bad":
            raise RuntimeError("boom")
        stopped.append(profile_id)
        return True

    mgr.running["bad"] = MagicMock()
    mgr.running["good"] = MagicMock()
    monkeypatch.setattr(mgr, "stop", stop)
    vnc_cleanup = AsyncMock()
    monkeypatch.setattr(mgr.vnc, "cleanup_all", vnc_cleanup)

    await mgr.cleanup_all()

    assert stopped == ["good"]
    vnc_cleanup.assert_awaited_once()


@pytest.mark.asyncio
async def test_auto_launch_claims_the_whole_queue_up_front(monkeypatch, tmp_db):
    """Queued profiles must report "starting", never "stopped".

    Launches are sequential and the last one can be minutes away. An open
    viewer reads "stopped" as terminal, so without the up-front claim a
    container restart ends every session that was about to come back.
    """
    from backend import database as db

    ids = [db.create_profile(name=f"auto{i}", auto_launch=True)["id"] for i in range(3)]
    mgr = BrowserManager()
    gate = asyncio.Event()
    seen: list[list[bool]] = []

    async def launch(profile):
        # while the FIRST profile is launching, the other two must read starting
        seen.append([mgr.is_starting(other) for other in ids])
        await gate.wait()

    monkeypatch.setattr(mgr, "launch", launch)
    task = asyncio.ensure_future(mgr.auto_launch_all())
    for _ in range(10):
        await asyncio.sleep(0)
        if seen:
            break

    assert seen[0] == [True, True, True]

    gate.set()
    await task
    assert [mgr.is_starting(pid) for pid in ids] == [False, False, False]


@pytest.mark.asyncio
async def test_auto_launch_bounds_each_launch(monkeypatch, tmp_db):
    """Without the per-launch ceiling one wedged profile blocks the whole queue."""
    from backend import database as db

    ids = [db.create_profile(name=f"auto{i}", auto_launch=True)["id"] for i in range(2)]
    monkeypatch.setattr(bm, "LAUNCH_TIMEOUT_S", 0.05)
    mgr = BrowserManager()
    reached: list[str] = []

    async def launch(profile):
        reached.append(profile["id"])
        if profile["id"] == ids[0]:
            await asyncio.sleep(3600)

    monkeypatch.setattr(mgr, "launch", launch)
    await asyncio.wait_for(mgr.auto_launch_all(), timeout=3.0)

    # both were reached: the hang did not block the queue behind it
    assert sorted(reached) == sorted(ids)
    assert mgr.is_starting(ids[0]) is False
    assert mgr.is_starting(ids[1]) is False


@pytest.mark.asyncio
async def test_auto_launch_cancellation_does_not_strand_starting(monkeypatch, tmp_db):
    """Shutdown cancels this task; a leftover claim is a permanent 409."""
    from backend import database as db

    pid = db.create_profile(name="auto", auto_launch=True)["id"]
    mgr = BrowserManager()

    async def launch(profile):
        await asyncio.sleep(3600)

    monkeypatch.setattr(mgr, "launch", launch)
    task = asyncio.ensure_future(mgr.auto_launch_all())
    for _ in range(10):
        await asyncio.sleep(0)
        if mgr.is_starting(pid):
            break
    assert mgr.is_starting(pid) is True

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert mgr.is_starting(pid) is False


# ── driver identity and the SIGKILL leg ──────────────────────────────────────


def test_discovery_records_the_driver_that_owns_the_browser(tmp_path: Path):
    """The browser's ppid at discovery is the Playwright node driver.

    Recording it is what makes the SIGKILL leg able to clean up the driver too;
    without it a forced teardown leaves the driver blocked on a pipe read from
    a dead browser, holding it as a zombie for the container's lifetime.
    """
    udd = str(tmp_path / "profile")
    port = _free_port()
    # A parent that spawns the "browser" so the browser has a non-init ppid,
    # mirroring node -> chrome.
    # The flags reach the parent through the ENVIRONMENT, never its argv: a
    # parent carrying them too would match the scan predicate itself and be
    # discovered instead of its child, which is what a driver never does.
    parent = subprocess.Popen(
        [
            sys.executable, "-c",
            "import os,subprocess,sys,time;"
            "c=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)',"
            "os.environ['T_UDD'],os.environ['T_PORT']]);"
            "print(c.pid,flush=True); time.sleep(30)",
        ],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        env={
            **os.environ,
            "T_UDD": f"--user-data-dir={udd}",
            "T_PORT": f"--remote-debugging-port={port}",
        },
    )
    try:
        browser_pid = int(parent.stdout.readline().strip())
        found = None
        for _ in range(150):
            found = bm.discover_browser_process(udd, port, os.getpid())
            if found is not None:
                break
            time.sleep(0.02)
        assert found is not None, "browser not discovered"
        assert found.pid == browser_pid
        assert found.driver_pid == parent.pid
        assert found.driver_starttime == bm._proc_stat(parent.pid)[2]
    finally:
        parent.kill()
        parent.wait()


def test_signal_driver_kills_the_real_driver_process(tmp_path: Path):
    driver = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        stat = bm._proc_stat(driver.pid)
        assert stat is not None
        proc = bm.BrowserProcess(
            pid=os.getpid(), starttime=bm._proc_stat(os.getpid())[2],
            user_data_dir=str(tmp_path), cdp_port=1,
            driver_pid=driver.pid, driver_starttime=stat[2],
        )
        assert bm._signal_driver(proc, signal.SIGKILL) is True
        driver.wait(timeout=5)
        assert driver.poll() is not None
    finally:
        if driver.poll() is None:
            driver.kill()
            driver.wait()


def test_signal_driver_refuses_a_recycled_driver_pid(tmp_path: Path):
    """A driver pid whose starttime moved belongs to somebody else now."""
    victim = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        stat = bm._proc_stat(victim.pid)
        proc = bm.BrowserProcess(
            pid=os.getpid(), starttime=bm._proc_stat(os.getpid())[2],
            user_data_dir=str(tmp_path), cdp_port=1,
            driver_pid=victim.pid, driver_starttime=stat[2] + 1,  # recycled
        )
        assert bm._signal_driver(proc, signal.SIGKILL) is False
        time.sleep(0.2)
        assert victim.poll() is None, "an unrelated process was killed"
    finally:
        victim.kill()
        victim.wait()


def test_signal_driver_is_a_noop_when_no_driver_was_recorded(tmp_path: Path):
    proc = bm.BrowserProcess(
        pid=os.getpid(), starttime=bm._proc_stat(os.getpid())[2],
        user_data_dir=str(tmp_path), cdp_port=1,
    )
    assert bm._signal_driver(proc, signal.SIGKILL) is False


def test_sigterm_leg_fires_before_playwrights_own_force_kill():
    """Playwright force-kills a frozen Chromium at its own 30s deadline.

    Measured live: with the SIGTERM leg at 30s the sweeper first evaluated it at
    ~33.5s and Playwright always won, so the manager's polite signal was
    unreachable and every wedge was really a silent SIGKILL by the driver. The
    worst-case moment the leg can fire is the threshold plus one sweep.
    """
    assert bm.CLOSING_SIGTERM_AFTER_S + bm.CLAIM_SWEEP_INTERVAL_S < 30.0


# ── an unattributable claim must fail closed ─────────────────────────────────


def test_a_claim_with_no_identity_is_held_while_its_cdp_port_is_bound(tmp_path: Path):
    """"Discovery found nothing" is not "the browser is gone".

    A Chromium reparented to PID 1 (driver OOM-killed mid-write) fails the
    descendant check and is invisible to the scan. Releasing then hands its live
    user_data_dir to a relaunch or an rmtree.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    claim = bm.ClosingClaim(
        context=None, proc=None, user_data_dir=str(tmp_path / "nope"),
        cdp_port=port, claimed_at=time.monotonic(),
    )
    try:
        alive, discovered = bm.BrowserManager._claim_evidence(claim)
        assert alive is True, "guard released with the CDP port still bound"
        assert discovered is None
    finally:
        listener.close()

    # ...and once nothing holds the port, the claim resolves.
    alive, discovered = bm.BrowserManager._claim_evidence(claim)
    assert alive is False
    assert discovered is None


@pytest.mark.asyncio
async def test_an_unattributable_claim_reports_itself_once(tmp_path: Path, caplog):
    """It cannot be signalled, so it must at least be visible in the log."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    mgr = BrowserManager()
    claim = bm.ClosingClaim(
        context=None, proc=None, user_data_dir=str(tmp_path / "nope"),
        cdp_port=port, claimed_at=time.monotonic(),
    )
    mgr._closing["p1"] = claim
    try:
        with caplog.at_level("ERROR", logger="cloakbrowser.manager.browser"):
            assert await mgr.check_wedged("p1") is True
            assert await mgr.check_wedged("p1") is True
        assert caplog.text.count("cannot be signalled") == 1
    finally:
        listener.close()
