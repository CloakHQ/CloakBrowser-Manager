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


# ── Auto-restart on crash ────────────────────────────────────────────────────


def test_consume_restart_budget_allows_up_to_the_max(monkeypatch):
    mgr = BrowserManager()
    clock = [0.0]
    monkeypatch.setattr(bm.time, "monotonic", lambda: clock[0])

    for expected in range(1, bm.AUTO_RESTART_MAX_ATTEMPTS + 1):
        assert mgr._consume_restart_budget("p1") == expected
    assert mgr._consume_restart_budget("p1") is None


def test_consume_restart_budget_is_per_profile(monkeypatch):
    mgr = BrowserManager()
    clock = [0.0]
    monkeypatch.setattr(bm.time, "monotonic", lambda: clock[0])

    for _ in range(bm.AUTO_RESTART_MAX_ATTEMPTS):
        mgr._consume_restart_budget("p1")
    assert mgr._consume_restart_budget("p1") is None
    assert mgr._consume_restart_budget("p2") == 1  # untouched budget


def test_consume_restart_budget_recovers_after_the_window(monkeypatch):
    mgr = BrowserManager()
    clock = [0.0]
    monkeypatch.setattr(bm.time, "monotonic", lambda: clock[0])

    for _ in range(bm.AUTO_RESTART_MAX_ATTEMPTS):
        mgr._consume_restart_budget("p1")
    assert mgr._consume_restart_budget("p1") is None

    clock[0] += bm.AUTO_RESTART_WINDOW_S + 1
    assert mgr._consume_restart_budget("p1") == 1  # fresh window, back to attempt 1


def test_consume_global_restart_budget_allows_up_to_the_max(monkeypatch):
    mgr = BrowserManager()
    clock = [0.0]
    monkeypatch.setattr(bm.time, "monotonic", lambda: clock[0])

    for _ in range(bm.GLOBAL_AUTO_RESTART_MAX_ATTEMPTS):
        assert mgr._consume_global_restart_budget() is True
    assert mgr._consume_global_restart_budget() is False


def test_consume_global_restart_budget_is_shared_across_profiles(monkeypatch):
    # Unlike the per-profile budget: one profile's crashes can exhaust the
    # container-wide breaker on their own, which is the point — a single
    # profile crash-looping fast enough to hit this is itself worth pausing.
    mgr = BrowserManager()
    clock = [0.0]
    monkeypatch.setattr(bm.time, "monotonic", lambda: clock[0])

    for _ in range(bm.GLOBAL_AUTO_RESTART_MAX_ATTEMPTS):
        assert mgr._consume_global_restart_budget() is True
    assert mgr._consume_global_restart_budget() is False


def test_consume_global_restart_budget_recovers_after_the_window(monkeypatch):
    mgr = BrowserManager()
    clock = [0.0]
    monkeypatch.setattr(bm.time, "monotonic", lambda: clock[0])

    for _ in range(bm.GLOBAL_AUTO_RESTART_MAX_ATTEMPTS):
        mgr._consume_global_restart_budget()
    assert mgr._consume_global_restart_budget() is False

    clock[0] += bm.GLOBAL_AUTO_RESTART_WINDOW_S + 1
    assert mgr._consume_global_restart_budget() is True


def test_restart_delay_grows_with_attempt_number_and_is_capped(monkeypatch):
    mgr = BrowserManager()
    monkeypatch.setattr(bm.random, "uniform", lambda a, b: 0.0)  # strip jitter

    assert mgr._restart_delay_s(1) == bm.AUTO_RESTART_BACKOFF_BASE_S
    assert mgr._restart_delay_s(2) == bm.AUTO_RESTART_BACKOFF_BASE_S * 2
    assert mgr._restart_delay_s(3) == bm.AUTO_RESTART_BACKOFF_BASE_S * 4
    # A high enough attempt number must not exceed the cap
    assert mgr._restart_delay_s(20) == bm.AUTO_RESTART_BACKOFF_MAX_S


def test_restart_delay_applies_positive_jitter_on_top_of_the_base(monkeypatch):
    mgr = BrowserManager()
    monkeypatch.setattr(bm.random, "uniform", lambda a, b: b)  # max jitter

    delay = mgr._restart_delay_s(1)
    assert delay == pytest.approx(
        bm.AUTO_RESTART_BACKOFF_BASE_S * (1 + bm.AUTO_RESTART_BACKOFF_JITTER_FRACTION),
    )


def test_auto_restart_budget_state_reports_not_exhausted_when_untouched():
    mgr = BrowserManager()
    state = mgr.auto_restart_budget_state("p1")
    assert state == {"exhausted": False, "attempts_used": 0, "retry_after_s": None}


def test_auto_restart_budget_state_reports_exhausted_with_a_retry_deadline(monkeypatch):
    mgr = BrowserManager()
    clock = [0.0]
    monkeypatch.setattr(bm.time, "monotonic", lambda: clock[0])

    for _ in range(bm.AUTO_RESTART_MAX_ATTEMPTS):
        mgr._consume_restart_budget("p1")
    clock[0] += 20.0  # some time passes after the last attempt

    state = mgr.auto_restart_budget_state("p1")
    assert state["exhausted"] is True
    assert state["attempts_used"] == bm.AUTO_RESTART_MAX_ATTEMPTS
    assert state["retry_after_s"] == pytest.approx(bm.AUTO_RESTART_WINDOW_S - 20.0)


def test_auto_restart_budget_state_is_read_only(monkeypatch):
    """Checking exhaustion must never itself consume budget."""
    mgr = BrowserManager()
    clock = [0.0]
    monkeypatch.setattr(bm.time, "monotonic", lambda: clock[0])

    mgr.auto_restart_budget_state("p1")
    mgr.auto_restart_budget_state("p1")
    mgr.auto_restart_budget_state("p1")

    for expected in range(1, bm.AUTO_RESTART_MAX_ATTEMPTS + 1):
        assert mgr._consume_restart_budget("p1") == expected  # full budget intact


@pytest.mark.asyncio
async def test_maybe_auto_restart_does_nothing_for_a_deleted_profile(monkeypatch, tmp_db):
    mgr = BrowserManager()
    launch = AsyncMock()
    monkeypatch.setattr(mgr, "launch", launch)
    monkeypatch.setattr(mgr, "_restart_delay_s", lambda n: 0)

    await mgr._maybe_auto_restart("nonexistent-profile-id")

    launch.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_auto_restart_does_nothing_when_auto_restart_disabled(monkeypatch, tmp_db):
    from backend import database as db

    pid = db.create_profile(name="NoAutoRestart", auto_restart=False)["id"]
    mgr = BrowserManager()
    launch = AsyncMock()
    monkeypatch.setattr(mgr, "launch", launch)
    monkeypatch.setattr(mgr, "_restart_delay_s", lambda n: 0)

    await mgr._maybe_auto_restart(pid)

    launch.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_auto_restart_launches_when_enabled(monkeypatch, tmp_db):
    from backend import database as db

    pid = db.create_profile(name="CrashesAndRestarts", auto_restart=True)["id"]
    mgr = BrowserManager()
    launch = AsyncMock()
    monkeypatch.setattr(mgr, "launch", launch)
    monkeypatch.setattr(mgr, "_restart_delay_s", lambda n: 0)

    await mgr._maybe_auto_restart(pid)

    launch.assert_awaited_once()
    launched_profile = launch.await_args.args[0]
    assert launched_profile["id"] == pid
    assert launch.await_args.kwargs == {"_is_auto_restart": True}


@pytest.mark.asyncio
async def test_maybe_auto_restart_skips_launch_if_already_running_after_the_delay(monkeypatch, tmp_db):
    """A manual relaunch (or a second crash-restart) that wins the race in the
    delay window must not be followed by a redundant second launch() call."""
    from backend import database as db

    pid = db.create_profile(name="RacedBack", auto_restart=True)["id"]
    mgr = BrowserManager()
    launch = AsyncMock()
    monkeypatch.setattr(mgr, "launch", launch)
    monkeypatch.setattr(mgr, "_restart_delay_s", lambda n: 0)
    mgr.running[pid] = MagicMock()  # already back up by the time the delay elapses

    await mgr._maybe_auto_restart(pid)

    launch.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_auto_restart_skips_launch_if_deleted_during_the_delay(monkeypatch, tmp_db):
    """The profile must be RE-fetched after the delay, not reused from before
    it — a delete mid-sleep must not launch a browser onto a directory a
    concurrent DELETE is (or already has finished) rmtree-ing.

    _restart_delay_s() itself stays synchronous in production (it is called,
    then its plain float result is handed to asyncio.sleep), so the side
    effect has to happen INSIDE that sleep — patching asyncio.sleep itself is
    what actually lands it in the delay window asyncio.sleep(_restart_delay_s
    (...)) creates, rather than before or after it.
    """
    from backend import database as db

    pid = db.create_profile(name="DeletedDuringDelay", auto_restart=True)["id"]
    mgr = BrowserManager()
    launch = AsyncMock()
    monkeypatch.setattr(mgr, "launch", launch)

    async def sleep_and_delete(delay):
        db.delete_profile(pid)

    monkeypatch.setattr(bm.asyncio, "sleep", sleep_and_delete)

    await mgr._maybe_auto_restart(pid)

    launch.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_auto_restart_skips_launch_if_claimed_for_delete_during_the_delay(
    monkeypatch, tmp_db,
):
    """claim_for_delete() is taken before the DB row is gone — a restart
    landing in that window must still back off, not race the rmtree."""
    from backend import database as db

    pid = db.create_profile(name="DeleteClaimedDuringDelay", auto_restart=True)["id"]
    mgr = BrowserManager()
    launch = AsyncMock()
    monkeypatch.setattr(mgr, "launch", launch)

    async def sleep_and_claim(delay):
        mgr.claim_for_delete(pid)

    monkeypatch.setattr(bm.asyncio, "sleep", sleep_and_claim)

    await mgr._maybe_auto_restart(pid)

    launch.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_auto_restart_skips_launch_if_turned_off_during_the_delay(monkeypatch, tmp_db):
    from backend import database as db

    pid = db.create_profile(name="TurnedOffDuringDelay", auto_restart=True)["id"]
    mgr = BrowserManager()
    launch = AsyncMock()
    monkeypatch.setattr(mgr, "launch", launch)

    async def sleep_and_disable(delay):
        db.update_profile(pid, auto_restart=False)

    monkeypatch.setattr(bm.asyncio, "sleep", sleep_and_disable)

    await mgr._maybe_auto_restart(pid)

    launch.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_auto_restart_uses_the_freshest_profile_settings_after_the_delay(
    monkeypatch, tmp_db,
):
    """An edit (e.g. fixing the proxy that caused the crash) made during the
    delay must be what actually launches, not a stale pre-delay snapshot."""
    from backend import database as db

    pid = db.create_profile(
        name="EditedDuringDelay", auto_restart=True, proxy="http://bad:1@h:1",
    )["id"]
    mgr = BrowserManager()
    launch = AsyncMock()
    monkeypatch.setattr(mgr, "launch", launch)

    async def sleep_and_fix_proxy(delay):
        db.update_profile(pid, proxy=None)

    monkeypatch.setattr(bm.asyncio, "sleep", sleep_and_fix_proxy)

    await mgr._maybe_auto_restart(pid)

    launch.assert_awaited_once()
    assert launch.await_args.args[0]["proxy"] is None


@pytest.mark.asyncio
async def test_maybe_auto_restart_absorbs_profile_already_running(monkeypatch, tmp_db):
    """launch() itself raises ProfileAlreadyRunning for the same race under
    the lock — that must not propagate out of a fire-and-forget task."""
    from backend import database as db

    pid = db.create_profile(name="RaceUnderLock", auto_restart=True)["id"]
    mgr = BrowserManager()
    monkeypatch.setattr(
        mgr, "launch", AsyncMock(side_effect=bm.ProfileAlreadyRunning("already running")),
    )
    monkeypatch.setattr(mgr, "_restart_delay_s", lambda n: 0)

    await mgr._maybe_auto_restart(pid)  # must not raise


@pytest.mark.asyncio
async def test_maybe_auto_restart_absorbs_a_launch_failure(monkeypatch, tmp_db):
    """A genuinely broken profile (bad proxy, missing extension) must not
    crash the caller — _on_browser_closed schedules this fire-and-forget."""
    from backend import database as db

    pid = db.create_profile(name="BrokenProxy", auto_restart=True)["id"]
    mgr = BrowserManager()
    monkeypatch.setattr(mgr, "launch", AsyncMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(mgr, "_restart_delay_s", lambda n: 0)

    await mgr._maybe_auto_restart(pid)  # must not raise


@pytest.mark.asyncio
async def test_maybe_auto_restart_respects_the_crash_loop_budget(monkeypatch, tmp_db):
    from backend import database as db

    pid = db.create_profile(name="CrashLoop", auto_restart=True)["id"]
    mgr = BrowserManager()
    launch = AsyncMock()
    monkeypatch.setattr(mgr, "launch", launch)
    monkeypatch.setattr(mgr, "_restart_delay_s", lambda n: 0)
    clock = [0.0]
    monkeypatch.setattr(bm.time, "monotonic", lambda: clock[0])

    for _ in range(bm.AUTO_RESTART_MAX_ATTEMPTS):
        await mgr._maybe_auto_restart(pid)
    assert launch.await_count == bm.AUTO_RESTART_MAX_ATTEMPTS

    await mgr._maybe_auto_restart(pid)  # one crash too many
    assert launch.await_count == bm.AUTO_RESTART_MAX_ATTEMPTS  # unchanged


@pytest.mark.asyncio
async def test_maybe_auto_restart_respects_the_global_crash_loop_budget(monkeypatch, tmp_db):
    """A single profile crashing fast enough can exhaust the container-wide
    breaker on its own — that IS the point, not a bug: this many restarts
    this fast, from anywhere, is itself the signal something systemic is
    wrong, so it must stop regardless of whose per-profile budget it is."""
    from backend import database as db

    pid = db.create_profile(name="SystemicCrash", auto_restart=True)["id"]
    mgr = BrowserManager()
    launch = AsyncMock()
    monkeypatch.setattr(mgr, "launch", launch)
    monkeypatch.setattr(mgr, "_restart_delay_s", lambda n: 0)
    clock = [0.0]
    monkeypatch.setattr(bm.time, "monotonic", lambda: clock[0])
    # Exhaust the global budget from OTHER profiles first, well within each
    # of their own per-profile budgets.
    for _ in range(bm.GLOBAL_AUTO_RESTART_MAX_ATTEMPTS):
        mgr._consume_global_restart_budget()

    await mgr._maybe_auto_restart(pid)

    launch.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_auto_restart_global_budget_does_not_block_an_isolated_crash(
    monkeypatch, tmp_db,
):
    from backend import database as db

    pid = db.create_profile(name="IsolatedCrash", auto_restart=True)["id"]
    mgr = BrowserManager()
    launch = AsyncMock()
    monkeypatch.setattr(mgr, "launch", launch)
    monkeypatch.setattr(mgr, "_restart_delay_s", lambda n: 0)

    await mgr._maybe_auto_restart(pid)

    launch.assert_awaited_once()


@pytest.mark.asyncio
async def test_maybe_auto_restart_logs_a_fast_crash_distinctly(monkeypatch, tmp_db, caplog):
    from backend import database as db

    pid = db.create_profile(name="FastCrash", auto_restart=True)["id"]
    mgr = BrowserManager()
    monkeypatch.setattr(mgr, "launch", AsyncMock())
    monkeypatch.setattr(mgr, "_restart_delay_s", lambda n: 0)

    with caplog.at_level("WARNING", logger="cloakbrowser.manager.browser"):
        await mgr._maybe_auto_restart(pid, ran_for_s=1.5)

    assert any("persistent configuration problem" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_maybe_auto_restart_does_not_log_fast_crash_for_a_long_lived_instance(
    monkeypatch, tmp_db, caplog,
):
    from backend import database as db

    pid = db.create_profile(name="SlowCrash", auto_restart=True)["id"]
    mgr = BrowserManager()
    monkeypatch.setattr(mgr, "launch", AsyncMock())
    monkeypatch.setattr(mgr, "_restart_delay_s", lambda n: 0)

    with caplog.at_level("WARNING", logger="cloakbrowser.manager.browser"):
        await mgr._maybe_auto_restart(pid, ran_for_s=3600.0)

    assert not any("persistent configuration problem" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_a_successful_manual_launch_resets_the_crash_budget(monkeypatch, tmp_db):
    """The whole point of the reset: a profile that burned its budget crash-
    looping earlier must get a full budget again once it has PROVEN it can
    come up cleanly — not stay silently unprotected until the window rolls
    off or the container restarts. headless=True sidesteps the Xvnc
    allocate/start_vnc path entirely, which is not what this test is about."""
    from backend import database as db

    pid = db.create_profile(name="Recovered", auto_restart=True, headless=True)["id"]
    mgr = BrowserManager()
    for _ in range(bm.AUTO_RESTART_MAX_ATTEMPTS):
        mgr._consume_restart_budget(pid)
    assert mgr.auto_restart_budget_state(pid)["exhausted"] is True

    profile = db.get_profile(pid)
    monkeypatch.setattr(
        bm, "launch_persistent_context_async", AsyncMock(side_effect=RuntimeError("boom")),
    )
    # A failed manual launch must NOT reset the budget (it is not proof of
    # anything) — only a launch that actually comes up does.
    with pytest.raises(Exception):
        await mgr.launch(profile)
    assert mgr.auto_restart_budget_state(pid)["exhausted"] is True


def _mock_successful_context() -> MagicMock:
    context = MagicMock()
    context.is_closed.return_value = False
    context.pages = []
    context.on = MagicMock()
    context.add_init_script = AsyncMock()
    return context


@pytest.mark.asyncio
async def test_a_successful_manual_launch_actually_resets_the_budget(monkeypatch, tmp_db):
    """The other half of the pair above: a launch that DOES come up clears
    the history rather than merely not incrementing it."""
    from backend import database as db

    pid = db.create_profile(name="ActuallyRecovered", auto_restart=True, headless=True)["id"]
    mgr = BrowserManager()
    for _ in range(bm.AUTO_RESTART_MAX_ATTEMPTS):
        mgr._consume_restart_budget(pid)
    assert mgr.auto_restart_budget_state(pid)["exhausted"] is True
    profile = db.get_profile(pid)

    async def fake_discover(*args, **kwargs):
        return bm.BrowserProcess(pid=os.getpid(), starttime=0, user_data_dir="/tmp", cdp_port=5100)

    monkeypatch.setattr(bm, "discover_browser_process_async", fake_discover)
    monkeypatch.setattr(
        bm, "launch_persistent_context_async",
        AsyncMock(return_value=_mock_successful_context()),
    )

    await mgr.launch(profile)  # a manual launch — NOT _is_auto_restart

    state = mgr.auto_restart_budget_state(pid)
    assert state["exhausted"] is False
    assert state["attempts_used"] == 0


@pytest.mark.asyncio
async def test_an_auto_restart_success_does_not_reset_its_own_budget(monkeypatch, tmp_db):
    """_is_auto_restart=True launches must not erase the very history that
    limits them — otherwise a profile that crashes, is auto-restarted, and
    crashes again immediately would never actually exhaust the budget."""
    from backend import database as db

    pid = db.create_profile(name="AutoRestartLoop", auto_restart=True, headless=True)["id"]
    mgr = BrowserManager()
    mgr._consume_restart_budget(pid)
    mgr._consume_restart_budget(pid)
    profile = db.get_profile(pid)

    async def fake_discover(*args, **kwargs):
        return bm.BrowserProcess(pid=os.getpid(), starttime=0, user_data_dir="/tmp", cdp_port=5100)

    monkeypatch.setattr(bm, "discover_browser_process_async", fake_discover)
    monkeypatch.setattr(
        bm, "launch_persistent_context_async",
        AsyncMock(return_value=_mock_successful_context()),
    )

    await mgr.launch(profile, _is_auto_restart=True)

    assert mgr.auto_restart_budget_state(pid)["attempts_used"] == 2  # untouched


@pytest.mark.asyncio
async def test_is_starting_is_true_for_the_whole_auto_restart_backoff_window(monkeypatch, tmp_db):
    """The exact fix for the frontend viewer treating every auto-restarted
    crash as terminal: without this, get_status() falls through to "stopped"
    during the backoff sleep, and the viewer's reconnect machine ends the
    session outright on a control-plane-terminal "stopped" verdict on the
    FIRST probe — showing "Browser session ended" within ~250ms of a crash
    auto-restart exists to make invisible."""
    from backend import database as db

    pid = db.create_profile(name="StatusDuringBackoff", auto_restart=True, headless=True)["id"]
    mgr = BrowserManager()

    async def fake_discover(*args, **kwargs):
        return bm.BrowserProcess(pid=os.getpid(), starttime=0, user_data_dir="/tmp", cdp_port=5100)

    monkeypatch.setattr(bm, "discover_browser_process_async", fake_discover)
    monkeypatch.setattr(
        bm, "launch_persistent_context_async",
        AsyncMock(return_value=_mock_successful_context()),
    )

    delay_started = asyncio.Event()
    release_delay = asyncio.Event()

    async def held_sleep(delay):
        delay_started.set()
        await release_delay.wait()

    monkeypatch.setattr(bm.asyncio, "sleep", held_sleep)

    assert mgr.is_starting(pid) is False
    task = asyncio.ensure_future(mgr._maybe_auto_restart(pid))
    await delay_started.wait()

    # Mid-backoff: this is exactly the window that used to report "stopped".
    assert mgr.is_starting(pid) is True
    assert mgr.get_status(pid)["status"] == "starting"

    release_delay.set()
    await task

    # Restart completed (launched) — no longer pending.
    assert mgr.is_starting(pid) is False


@pytest.mark.asyncio
async def test_is_starting_clears_even_if_the_restart_is_refused(monkeypatch, tmp_db):
    from backend import database as db

    pid = db.create_profile(name="RefusedRestart", auto_restart=True)["id"]
    mgr = BrowserManager()
    monkeypatch.setattr(mgr, "_restart_delay_s", lambda n: 0)
    mgr.running[pid] = MagicMock()  # already back up by the time the delay elapses

    await mgr._maybe_auto_restart(pid)

    assert mgr.is_starting(pid) is False


@pytest.mark.asyncio
async def test_a_restart_aborted_after_the_delay_does_not_consume_either_budget(
    monkeypatch, tmp_db,
):
    """Budgets must only be spent on restarts that actually reach launch() —
    not on ones aborted by a post-sleep check (already running, deleted,
    turned off). Consuming up front charged both budgets for restarts that
    never happened at all."""
    from backend import database as db

    pid = db.create_profile(name="AbortedAfterDelay", auto_restart=True)["id"]
    mgr = BrowserManager()
    launch = AsyncMock()
    monkeypatch.setattr(mgr, "launch", launch)

    async def sleep_and_win_the_race(delay):
        mgr.running[pid] = MagicMock()  # a manual launch wins during the delay

    monkeypatch.setattr(bm.asyncio, "sleep", sleep_and_win_the_race)

    await mgr._maybe_auto_restart(pid)

    launch.assert_not_awaited()
    state = mgr.auto_restart_budget_state(pid)
    assert state["attempts_used"] == 0
    assert len(mgr._global_restart_history) == 0


@pytest.mark.asyncio
async def test_a_globally_refused_restart_does_not_consume_the_per_profile_budget(
    monkeypatch, tmp_db,
):
    """The per-profile budget must not be charged for a restart the global
    breaker refuses — otherwise a profile that was never actually
    auto-restarted once ends up individually locked out too, for the rest of
    AUTO_RESTART_WINDOW_S, after an unrelated systemic storm clears."""
    from backend import database as db

    pid = db.create_profile(name="GloballyRefused", auto_restart=True)["id"]
    mgr = BrowserManager()
    monkeypatch.setattr(mgr, "_restart_delay_s", lambda n: 0)
    launch = AsyncMock()
    monkeypatch.setattr(mgr, "launch", launch)
    clock = [0.0]
    monkeypatch.setattr(bm.time, "monotonic", lambda: clock[0])
    for _ in range(bm.GLOBAL_AUTO_RESTART_MAX_ATTEMPTS):
        mgr._consume_global_restart_budget()

    for _ in range(3):
        await mgr._maybe_auto_restart(pid)

    launch.assert_not_awaited()
    assert mgr.auto_restart_budget_state(pid)["attempts_used"] == 0


@pytest.mark.asyncio
async def test_ten_crashes_deleted_during_delay_do_not_trip_the_global_breaker(
    monkeypatch, tmp_db,
):
    """Ten restart ATTEMPTS that never actually happen (each profile deleted
    mid-backoff) must not exhaust the container-wide breaker — it counts
    restarts that fire, not crashes considered."""
    from backend import database as db

    pids = [
        db.create_profile(name=f"Deleted{i}", auto_restart=True)["id"] for i in range(10)
    ]
    mgr = BrowserManager()
    launch = AsyncMock()
    monkeypatch.setattr(mgr, "launch", launch)

    for pid in pids:
        async def sleep_and_delete(delay, pid=pid):
            db.delete_profile(pid)

        monkeypatch.setattr(bm.asyncio, "sleep", sleep_and_delete)
        await mgr._maybe_auto_restart(pid)

    launch.assert_not_awaited()
    assert len(mgr._global_restart_history) == 0

    # The breaker must still have full budget for a real crash afterward.
    new_pid = db.create_profile(name="RealCrash", auto_restart=True)["id"]

    async def noop_sleep(delay):
        pass

    monkeypatch.setattr(bm.asyncio, "sleep", noop_sleep)
    await mgr._maybe_auto_restart(new_pid)
    launch.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_new_restart_is_scheduled_once_shutdown_has_started(monkeypatch, tmp_db):
    from backend import database as db

    pid = db.create_profile(name="DuringShutdown", auto_restart=True)["id"]
    mgr = BrowserManager()
    monkeypatch.setattr(mgr.vnc, "stop_vnc", AsyncMock())
    restart = AsyncMock()
    monkeypatch.setattr(mgr, "_maybe_auto_restart", restart)
    mgr._shutting_down = True

    context = MagicMock()
    mgr.running[pid] = bm.RunningProfile(
        profile_id=pid, context=context, display=100, ws_port=6100, cdp_port=5100,
    )

    await mgr._on_browser_closed(pid, context)
    await asyncio.sleep(0)

    restart.assert_not_awaited()
    assert mgr._restart_tasks == set()


@pytest.mark.asyncio
async def test_on_browser_closed_schedules_a_restart_for_a_genuine_crash(monkeypatch, tmp_db):
    """The exact hook _on_browser_closed uses: `running` still in self.running
    when the close arrives means nobody called stop() first."""
    from backend import database as db

    pid = db.create_profile(name="GenuineCrash", auto_restart=True)["id"]
    mgr = BrowserManager()
    monkeypatch.setattr(mgr.vnc, "stop_vnc", AsyncMock())
    restart = AsyncMock()
    monkeypatch.setattr(mgr, "_maybe_auto_restart", restart)

    context = MagicMock()
    mgr.running[pid] = bm.RunningProfile(
        profile_id=pid, context=context, display=100, ws_port=6100, cdp_port=5100,
    )

    await mgr._on_browser_closed(pid, context)
    await asyncio.sleep(0)  # let the fire-and-forget task actually start

    restart.assert_awaited_once()
    call_args = restart.await_args.args
    assert call_args[0] == pid
    assert isinstance(call_args[1], float)  # ran_for_s, non-negative elapsed time
    assert call_args[1] >= 0


@pytest.mark.asyncio
async def test_on_browser_closed_tracks_the_restart_task_and_forgets_it_when_done(
    monkeypatch, tmp_db,
):
    from backend import database as db

    pid = db.create_profile(name="Tracked", auto_restart=True)["id"]
    mgr = BrowserManager()
    monkeypatch.setattr(mgr.vnc, "stop_vnc", AsyncMock())
    monkeypatch.setattr(mgr, "_maybe_auto_restart", AsyncMock())

    context = MagicMock()
    mgr.running[pid] = bm.RunningProfile(
        profile_id=pid, context=context, display=100, ws_port=6100, cdp_port=5100,
    )

    await mgr._on_browser_closed(pid, context)
    assert len(mgr._restart_tasks) == 1
    await asyncio.sleep(0)  # let the task run to completion...
    await asyncio.sleep(0)  # ...and its done-callback (call_soon) actually fire
    assert len(mgr._restart_tasks) == 0


@pytest.mark.asyncio
async def test_on_browser_closed_after_stop_does_not_schedule_a_restart(monkeypatch, tmp_db):
    """stop() pops `running` BEFORE closing — by the time this fires, there is
    nothing left to distinguish it from "already handled"."""
    from backend import database as db

    pid = db.create_profile(name="DeliberateStop", auto_restart=True)["id"]
    mgr = BrowserManager()
    monkeypatch.setattr(mgr.vnc, "stop_vnc", AsyncMock())
    restart = AsyncMock()
    monkeypatch.setattr(mgr, "_maybe_auto_restart", restart)

    context = MagicMock()
    # Deliberately NOT added to mgr.running — mirrors stop()'s own pop
    # happening before the context actually finishes closing.

    await mgr._on_browser_closed(pid, context)
    await asyncio.sleep(0)

    restart.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_browser_closed_from_a_superseded_context_does_not_schedule_a_restart(monkeypatch, tmp_db):
    """A stale close from an old instance must not restart the NEW one that
    already owns this profile id."""
    from backend import database as db

    pid = db.create_profile(name="Superseded", auto_restart=True)["id"]
    mgr = BrowserManager()
    monkeypatch.setattr(mgr.vnc, "stop_vnc", AsyncMock())
    restart = AsyncMock()
    monkeypatch.setattr(mgr, "_maybe_auto_restart", restart)

    old_context = MagicMock()
    new_context = MagicMock()
    mgr.running[pid] = bm.RunningProfile(
        profile_id=pid, context=new_context, display=100, ws_port=6100, cdp_port=5100,
    )

    await mgr._on_browser_closed(pid, old_context)
    await asyncio.sleep(0)

    restart.assert_not_awaited()
    assert mgr.running[pid].context is new_context  # untouched


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
async def test_cleanup_all_cancels_a_pending_auto_restart(monkeypatch):
    """Untracked, a fire-and-forget auto-restart sleeping through shutdown
    used to wake up AFTER cleanup_all() returned and launch a brand new,
    totally unsupervised Chromium while nginx and the event loop were
    themselves being torn down. cleanup_all() must cancel it instead."""
    mgr = BrowserManager()
    launched = False

    async def slow_restart():
        nonlocal launched
        await asyncio.sleep(60)  # would only return after a real shutdown
        launched = True  # never reached if cancelled, as it must be

    task = asyncio.ensure_future(slow_restart())
    mgr._restart_tasks.add(task)
    monkeypatch.setattr(mgr.vnc, "cleanup_all", AsyncMock())

    await mgr.cleanup_all()

    assert task.cancelled()
    assert launched is False
    assert mgr._restart_tasks == set()
    assert mgr._shutting_down is True


@pytest.mark.asyncio
async def test_cleanup_all_sweeps_teardown_claims_after_cancelling_restart_tasks(monkeypatch):
    """A restart task cancelled WHILE INSIDE launch_persistent_context_async
    (before a context exists) leaves its _closing claim in place with no
    identified process — normally resolved by the maintenance loop's own
    sweep, but main.py's lifespan cancels that loop's task BEFORE calling
    cleanup_all(). One explicit sweep pass here catches the fast, common case
    (the browser already provably gone) instead of leaving the claim dangling
    for the rest of this short-lived process."""
    mgr = BrowserManager()
    monkeypatch.setattr(mgr.vnc, "cleanup_all", AsyncMock())
    sweep = AsyncMock()
    monkeypatch.setattr(mgr, "sweep_teardown_claims", sweep)

    task = asyncio.ensure_future(asyncio.sleep(60))
    mgr._restart_tasks.add(task)

    await mgr.cleanup_all()

    sweep.assert_awaited_once()


@pytest.mark.asyncio
async def test_cleanup_all_skips_the_sweep_when_nothing_was_pending(monkeypatch):
    mgr = BrowserManager()
    monkeypatch.setattr(mgr.vnc, "cleanup_all", AsyncMock())
    sweep = AsyncMock()
    monkeypatch.setattr(mgr, "sweep_teardown_claims", sweep)

    await mgr.cleanup_all()

    sweep.assert_not_awaited()


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
