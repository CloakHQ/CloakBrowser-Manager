"""Launch/stop/track CloakBrowser instances per profile."""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import secrets
import signal
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from cloakbrowser import launch_persistent_context_async
from cloakbrowser.download import ensure_binary

from . import binary_status
from .extensions import extension_paths_for

# _dri_driver reads the driver behind a render node, and the node itself comes
# from the same KASM_DRINODE that Xvnc's -drinode uses. Imported rather than
# reimplemented precisely so the two cannot disagree about which GPU is in play:
# Chromium drawing on one device while Xvnc captures and encodes on another is a
# silent half-accelerated session.
from .vnc_manager import VNCManager, _dri_driver, _dri_render_node
from .viewer_tokens import viewer_tokens

logger = logging.getLogger("cloakbrowser.manager.browser")

# Parses the "cloakbrowser" logger's download-progress messages into
# binary_status's in-memory state; see that module for why (no callback hook
# on ensure_binary()). Installed once at import time — this module is only
# ever imported once, by main.py at process startup.
binary_status.install()


class ProfileAlreadyRunning(RuntimeError):
    """A launch lost the race to another launch (or to a delete in progress)."""


# Chromium's GL backend. SwiftShader (CPU) is the correct default: the base
# image has no GPU, and a headed Chromium that cannot reach one draws nothing at
# all unless it is told to rasterise in software.
#
# --use-angle=swiftshader alone is NOT interchangeable with "no GPU flags". With
# it removed on a GPU-less box Chromium probes for a GL driver, fails, and falls
# back anyway — but only after a multi-second GPU-process crash-and-retry cycle
# that shows up as a blank viewer, so the flag stays the explicit default.
_GPU_MODE_SWIFTSHADER = "swiftshader"
_GPU_MODE_NVIDIA = "nvidia"
# Intel and AMD integrated (or discrete) GPUs driven by the open-source Mesa
# stack. One mode for both vendors because nothing below this line differs
# between them: the same ANGLE backend, the same GLVND loader, the same DRI3
# render node, and — unlike NVIDIA — the same VAAPI encoder name in KasmVNC
# (h264_vaapi). Only the userspace driver the loader picks up differs (iHD or
# i965 for Intel, radeonsi for AMD), and that is chosen by the kernel driver
# behind the render node, not by us.
_GPU_MODE_IGPU = "igpu"

# Presence of the NVIDIA control device is the detection signal. It is created
# by the NVIDIA container runtime as part of the same injection that supplies
# libEGL_nvidia/libnvidia-encode, so it is true exactly when the GPU is actually
# reachable from inside the container — unlike a host-level nvidia-smi check, or
# probing /dev/dri (which exists on any host with an integrated GPU and says
# nothing about which vendor drives it).
_NVIDIA_CONTROL_DEVICE = "/dev/nvidiactl"

# Chromium 146 flags for the NVIDIA path. Deliberately short — every one is here
# for a reason that was MEASURED on an RTX 3080 Ti under KasmVNC's Xvnc, because
# the failure mode of a wrong GL flag is silent (SwiftShader or llvmpipe, with
# the GPU attached and idle) rather than loud.
#
#   --use-gl=angle --use-angle=vulkan
#       The backend that actually reaches the GPU for a HEADED browser here.
#       ANGLE's Vulkan backend does not need the X server to expose a
#       GPU-capable GLX/EGL, which is the whole problem under Xvnc: the closed
#       NVIDIA driver has no DRI3 and no NV-GLX there.
#
#       This is NOT the gl-egl pairing that CloakBrowser PR #476 landed, and
#       the difference is the point. Measured, same host, same X server:
#         gl-egl, as-is        -> ANGLE (Mesa, llvmpipe ...)      SOFTWARE
#         gl-egl + NVIDIA ICD  -> ANGLE (NVIDIA ... RTX 3080 Ti)
#                                 but webgl=enabled_readback and
#                                 gpu_compositing=DISABLED_SOFTWARE
#         vulkan               -> ANGLE (NVIDIA, Vulkan 1.4.312 ...)
#                                 webgl=enabled, gpu_compositing=ENABLED,
#                                 rasterization=enabled_force, webgpu=enabled
#       So gl-egl either lands on llvmpipe or wins the renderer and then loses
#       the compositor to a readback path; Vulkan is the only one of the three
#       that gets both. PR #476 concluded headed sessions stay software-rendered
#       under a virtual X server — that holds for the EGL path it changed, and
#       is what --use-angle=vulkan gets past.
#
#       --use-gl=egl is not an option at all: it is DEPRECATED in modern
#       Chromium and silently DISABLES WebGL rather than enabling the GPU, which
#       is the defect PR #476 exists to fix. It reads like the right flag, so it
#       has its own regression test.
#   --enable-gpu-rasterization
#       Rasterise on the GPU, not just composite there. Measured as
#       rasterization=enabled_force; without it tile raster stays on the CPU,
#       which is most of the win on a page that is not WebGL.
#   --ignore-gpu-blocklist
#       Chromium blocklists drivers it cannot identify, and an X server it does
#       not recognise is a reliable way to land on that path.
#
# Not included, and why: --disable-gpu-sandbox is unnecessary because the
# stealth defaults already pass --no-sandbox; --disable-gpu-driver-bug-workarounds
# turns off correctness fixes for a benchmark number nobody is reading here.
#
# Fingerprinting is unaffected, which was checked rather than assumed: with
# these flags JS still reads the SPOOFED renderer (measured: a page asking for
# UNMASKED_RENDERER_WEBGL got "NVIDIA GeForce RTX 5080 Laptop GPU ... 565.77" on
# a host whose real GPU is a 3080 Ti on driver 580.173.02).
_NVIDIA_GPU_FLAGS = (
    "--use-gl=angle",
    "--use-angle=vulkan",
    "--enable-gpu-rasterization",
    "--ignore-gpu-blocklist",
)

# Insurance for the fallback, not the primary path. If Vulkan is unavailable
# (older driver, missing /etc/vulkan/icd.d/nvidia_icd.json) ANGLE falls back to
# its GL/EGL backend, and there the GLVND loader picks a vendor from
# /usr/share/glvnd/egl_vendor.d — where Mesa's 50_mesa.json sits next to the
# injected 10_nvidia.json. Measured: left to itself it chose Mesa and bound
# llvmpipe. Pinning the NVIDIA manifest makes the degraded path "GPU with
# readback" instead of "silently on the CPU". Ignored entirely when Vulkan works.
_NVIDIA_GPU_ENV = {
    "__EGL_VENDOR_LIBRARY_FILENAMES": "/usr/share/glvnd/egl_vendor.d/10_nvidia.json",
}

# Chromium flags for the Mesa path (Intel/AMD). The shape mirrors the NVIDIA set
# and the single difference is the ANGLE backend — which lands on the SAME answer
# as NVIDIA for a different reason, and that is worth knowing because the obvious
# expectation is the opposite.
#
# Measured headed on KasmVNC's Xvnc (-hw3d on), AMD Raphael iGPU, Mesa 25.0.7,
# via CDP SystemInfo.getInfo — all three ANGLE backends, same host, same session:
#
#   gl-egl   ANGLE (AMD, ... radeonsi raphael_mendocino ..., OpenGL ES 3.2 Mesa)
#            webgl=enabled_READBACK, gpu_compositing=DISABLED_SOFTWARE
#   vulkan   ANGLE (AMD, Vulkan 1.4.305 (RADV RAPHAEL_MENDOCINO), radv-25.0.7)
#            webgl=enabled,          gpu_compositing=ENABLED,
#            rasterization=enabled_force, webgpu=enabled
#   gl       Chromium never reached CDP at all — the probe timed out at 45s
#
# So gl-egl is not the silent-llvmpipe trap here that it is on NVIDIA: DRI3 does
# exist on an open-source driver (that is what -hw3d supplies, and _hw3d_flags()
# enables it by default on any render node that is not the closed NVIDIA one), so
# EGL's X11 platform genuinely reaches radeonsi. It just stops one step short —
# the renderer is the GPU and the compositor still reads every frame back through
# the CPU. Vulkan gets both halves, exactly as on NVIDIA, so it is the default.
#
# It stays a knob rather than becoming a constant because the failure mode is
# SILENT and the answer is per-host: a GPU whose Mesa build has no Vulkan driver
# does not fall back to gl-egl, it falls back to llvmpipe on the CPU — which
# chrome://gpu still reports as "enabled" (see _SOFTWARE_RENDERER_MARKERS in
# scripts/gpu_probe.py). `gpu_probe.py --vendor igpu --angle-backend <name>` is
# how a new host gets settled before it is trusted. gl is kept selectable despite
# failing outright above: that failure is LOUD (no browser at all), which makes
# it a safe thing to leave available and a useless thing to fall back to.
_IGPU_ANGLE_BACKEND_DEFAULT = "vulkan"
# swiftshader is deliberately NOT accepted here. It is a valid --use-angle value
# and would be taken by Chromium, so allowing it would let a deployment claim
# CHROME_GPU_ACCEL=igpu while rasterising every frame on the CPU — the exact
# failure this whole module is arranged to make impossible to have by accident.
_IGPU_ANGLE_BACKENDS = ("gl-egl", "vulkan", "gl")

# No environment overlay for this mode, unlike NVIDIA's. The NVIDIA one exists
# because the container runtime injects 10_nvidia.json NEXT TO Mesa's
# 50_mesa.json and GLVND then has a real choice to get wrong. The iGPU image
# installs no second EGL vendor, and a host that has both a Mesa render node and
# an NVIDIA card resolves to _GPU_MODE_NVIDIA below before reaching this path —
# so there is nothing to disambiguate, and pinning a manifest we did not install
# would only be able to break EGL outright.
_IGPU_GPU_ENV: dict[str, str] = {}

# Values that name a mode outright. The truthy generic spellings stay bound to
# NVIDIA for backwards compatibility — that is what they have always meant, and
# repointing them at the Mesa path would silently change the backend on an
# existing NVIDIA deployment that spells it CHROME_GPU_ACCEL=1.
_GPU_MODE_ALIASES = {
    "0": _GPU_MODE_SWIFTSHADER, "false": _GPU_MODE_SWIFTSHADER,
    "no": _GPU_MODE_SWIFTSHADER, "off": _GPU_MODE_SWIFTSHADER,
    _GPU_MODE_SWIFTSHADER: _GPU_MODE_SWIFTSHADER,
    "1": _GPU_MODE_NVIDIA, "true": _GPU_MODE_NVIDIA, "yes": _GPU_MODE_NVIDIA,
    "on": _GPU_MODE_NVIDIA, _GPU_MODE_NVIDIA: _GPU_MODE_NVIDIA,
    _GPU_MODE_IGPU: _GPU_MODE_IGPU, "vaapi": _GPU_MODE_IGPU,
    "mesa": _GPU_MODE_IGPU, "intel": _GPU_MODE_IGPU, "amd": _GPU_MODE_IGPU,
}


def _igpu_angle_backend() -> str:
    """ANGLE backend for the Mesa path, from CHROME_ANGLE_BACKEND."""
    raw = os.environ.get(
        "CHROME_ANGLE_BACKEND", _IGPU_ANGLE_BACKEND_DEFAULT,
    ).strip().lower()
    if raw not in _IGPU_ANGLE_BACKENDS:
        logger.warning(
            "Unknown CHROME_ANGLE_BACKEND=%r (accepted: %s), using %r",
            raw, ", ".join(_IGPU_ANGLE_BACKENDS), _IGPU_ANGLE_BACKEND_DEFAULT,
        )
        return _IGPU_ANGLE_BACKEND_DEFAULT
    return raw


def _chrome_gpu_mode() -> str:
    """Resolve CHROME_GPU_ACCEL to a mode: swiftshader, nvidia or igpu."""
    raw = os.environ.get("CHROME_GPU_ACCEL", "auto").strip().lower()
    forced = _GPU_MODE_ALIASES.get(raw)
    if forced == _GPU_MODE_SWIFTSHADER:
        logger.info("Chromium GPU acceleration disabled (CHROME_GPU_ACCEL=%s)", raw)
        return _GPU_MODE_SWIFTSHADER
    if forced is not None:
        # Forced: no device check. Someone who passes the GPU in a way this
        # detection does not recognise should still be able to say so.
        logger.info(
            "Chromium %s GPU acceleration forced (CHROME_GPU_ACCEL=%s)", forced, raw,
        )
        return forced
    if raw != "auto":
        # An unrecognised value used to be impossible to notice: it would fall
        # through to whichever branch happened to be last.
        logger.warning("Unknown CHROME_GPU_ACCEL=%r, falling back to 'auto'", raw)
    if os.path.exists(_NVIDIA_CONTROL_DEVICE):
        # Tested FIRST, and not merely for tidiness: the NVIDIA container runtime
        # injects a render node too, so an NVIDIA host also satisfies the DRI
        # check below — and would then be handed the Mesa flag set, which on the
        # closed driver means llvmpipe.
        logger.info(
            "Chromium NVIDIA GPU acceleration enabled (auto: %s present)",
            _NVIDIA_CONTROL_DEVICE,
        )
        return _GPU_MODE_NVIDIA
    node = _dri_render_node()
    if os.path.exists(node):
        # An unresolvable driver counts as Mesa. /sys is frequently not
        # introspectable from inside a container, and the one driver this has to
        # exclude — nvidia — was already excluded by the device check above,
        # which the runtime that injects that driver always satisfies.
        driver = _dri_driver(node)
        if driver != "nvidia":
            logger.info(
                "Chromium integrated-GPU acceleration enabled (auto: %s present, "
                "driver=%s, ANGLE backend=%s)",
                node, driver or "unknown", _igpu_angle_backend(),
            )
            return _GPU_MODE_IGPU
        logger.info(
            "Chromium GPU acceleration off (auto: %s is driven by the closed "
            "nvidia driver but %s is absent, so Mesa cannot use it and the "
            "NVIDIA userspace was not injected)",
            node, _NVIDIA_CONTROL_DEVICE,
        )
        return _GPU_MODE_SWIFTSHADER
    logger.info(
        "Chromium GPU acceleration off (auto: neither %s nor %s, so no GPU was "
        "passed in)", _NVIDIA_CONTROL_DEVICE, node,
    )
    return _GPU_MODE_SWIFTSHADER


def _chrome_gpu_flags() -> list[str]:
    """Chromium GL flags for the resolved GPU mode."""
    mode = _chrome_gpu_mode()
    if mode == _GPU_MODE_NVIDIA:
        return list(_NVIDIA_GPU_FLAGS)
    if mode == _GPU_MODE_IGPU:
        return [
            "--use-gl=angle",
            f"--use-angle={_igpu_angle_backend()}",
            "--enable-gpu-rasterization",
            "--ignore-gpu-blocklist",
        ]
    return ["--use-angle=swiftshader"]


def _chrome_gpu_env() -> dict[str, str]:
    """Environment overlay for the resolved GPU mode (see _NVIDIA_GPU_ENV)."""
    mode = _chrome_gpu_mode()
    if mode == _GPU_MODE_NVIDIA:
        return dict(_NVIDIA_GPU_ENV)
    if mode == _GPU_MODE_IGPU:
        return dict(_IGPU_GPU_ENV)
    return {}


def _normalize_proxy(raw: str) -> str:
    """Convert common proxy formats to http://user:pass@host:port.

    Accepts:
      - http://user:pass@host:port  (already valid)
      - host:port:user:pass
      - host:port
    """
    if raw.startswith(("http://", "https://", "socks5://")):
        return raw
    parts = raw.split(":")
    if len(parts) == 4:
        host, port, user, passwd = parts
        return f"http://{user}:{passwd}@{host}:{port}"
    if len(parts) == 2:
        return f"http://{raw}"
    return raw


def _validate_proxy(url: str) -> None:
    """Validate that a normalized proxy URL has scheme, host, and port."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https", "socks5"):
        raise ValueError(
            f"Invalid proxy scheme '{parsed.scheme}'. Must be http, https, or socks5."
        )
    if not parsed.hostname:
        raise ValueError(f"Proxy URL missing hostname: {url}")
    if not parsed.port:
        raise ValueError(f"Proxy URL missing port: {url}")


def _init_profile_defaults(user_data_dir: Path) -> None:
    """Set up bookmarks and Google search (Chrome's own default) on first launch."""
    default_dir = user_data_dir / "Default"
    default_dir.mkdir(parents=True, exist_ok=True)

    # --- Bookmarks (only on first launch) ---
    bookmarks_path = default_dir / "Bookmarks"
    if not bookmarks_path.exists():
        ts = str(int(time.time() * 1_000_000))  # Chrome timestamp format
        _id = 1

        def bm(name: str, url: str) -> dict:
            nonlocal _id
            _id += 1
            return {"type": "url", "id": str(_id), "name": name, "url": url, "date_added": ts}

        def folder(name: str, children: list) -> dict:
            nonlocal _id
            _id += 1
            return {"type": "folder", "id": str(_id), "name": name, "children": children, "date_added": ts, "date_modified": ts}

        bookmarks = {
            "checksum": "",
            "roots": {
                "bookmark_bar": {
                    "type": "folder", "id": "1", "name": "Bookmarks bar",
                    "date_added": ts, "date_modified": ts,
                    "children": [
                        folder("Detection Tests", [
                            bm("Rebrowser Bot Detector", "https://bot-detector.rebrowser.net/"),
                            bm("Incolumitas", "https://bot.incolumitas.com/"),
                            bm("SannySort", "https://bot.sannysoft.com/"),
                            bm("BrowserScan Bot", "https://www.browserscan.net/bot-detection"),
                            bm("FingerprintJS Demo", "https://demo.fingerprint.com/web-scraping"),
                            bm("Pixelscan", "https://pixelscan.net/fingerprint-check"),
                            bm("CreepJS", "https://abrahamjuliot.github.io/creepjs/"),
                            bm("fingerprint-scan", "https://fingerprint-scan.com/"),
                            bm("DeviceInfo Bot", "https://deviceandbrowserinfo.com/are_you_a_bot"),
                        ]),
                        folder("Fingerprint", [
                            bm("BrowserLeaks Canvas", "https://browserleaks.com/canvas"),
                            bm("BrowserLeaks WebGL", "https://browserleaks.com/webgl"),
                            bm("BrowserLeaks Fonts", "https://browserleaks.com/fonts"),
                            bm("BrowserLeaks JS", "https://browserleaks.com/javascript"),
                            bm("FingerprintJS OSS", "https://fingerprintjs.github.io/fingerprintjs/"),
                            bm("Audio FP", "https://audiofingerprint.openwpm.com/"),
                            bm("DeviceInfo", "https://deviceandbrowserinfo.com/info_device"),
                        ]),
                        folder("Headers & TLS", [
                            bm("httpbin headers", "https://httpbin.org/headers"),
                            bm("httpbin IP", "https://httpbin.org/ip"),
                            bm("TLS Fingerprint", "https://tls.browserleaks.com/"),
                        ]),
                        folder("reCAPTCHA", [
                            bm("Google v3 Demo", "https://recaptcha-demo.appspot.com/recaptcha-v3-request-scores.php"),
                            bm("2captcha v3", "https://2captcha.com/demo/recaptcha-v3"),
                            bm("Turnstile", "https://peet.ws/turnstile-test/non-interactive.html"),
                        ]),
                    ],
                },
                "other": {"type": "folder", "id": "2", "name": "Other bookmarks", "children": []},
                "synced": {"type": "folder", "id": "3", "name": "Mobile bookmarks", "children": []},
            },
            "version": 1,
        }
        bookmarks_path.write_text(json.dumps(bookmarks, indent=2))
        logger.info("Created default bookmarks for %s", user_data_dir.name)

    # --- Google as default search engine, same as an out-of-the-box Chrome ---
    prefs_path = default_dir / "Preferences"
    if not prefs_path.exists():
        prefs = {
            "default_search_provider_data": {
                "template_url_data": {
                    "keyword": "google.com",
                    "short_name": "Google",
                    "url": "https://www.google.com/search?q={searchTerms}",
                    "suggestions_url": "https://www.google.com/complete/search?output=chrome&q={searchTerms}",
                    "favicon_url": "https://www.google.com/favicon.ico",
                }
            },
            "default_search_provider": {
                "enabled": True,
            },
        }
        prefs_path.write_text(json.dumps(prefs, indent=2))
        logger.info("Set Google as default search for %s", user_data_dir.name)


def _dedupe_download_path(downloads_dir: Path, filename: str) -> Path:
    """Chrome's own collision convention: file.txt, file (1).txt, file (2).txt…

    Two downloads of the same suggested filename in one profile must not
    silently overwrite each other — save_as() would happily clobber an
    existing file otherwise.
    """
    candidate = downloads_dir / filename
    if not candidate.exists():
        return candidate
    stem, suffix = Path(filename).stem, Path(filename).suffix
    n = 1
    while True:
        candidate = downloads_dir / f"{stem} ({n}){suffix}"
        if not candidate.exists():
            return candidate
        n += 1


async def _finalize_download(download: Any, downloads_dir: Path) -> None:
    """Copy a Playwright download to its real filename in `downloads_dir`.

    Playwright's own download.path() is a random GUID with no extension —
    intentional on their side (see the downloads_path docs: "the downloads
    are deleted when the browser context they were created in is closed",
    even when downloads_path is set) — so without this, every download a
    user makes through the VNC-visible browser turns into an unnamed file
    that vanishes the moment the profile stops. Never raises: a broken
    download must not take down the page's event loop.

    Verified live for a headed profile with no other CDP client attached
    (the pure human-via-VNC case this exists for): real filename, real
    bytes, correct permissions. A HEADLESS profile driven ONLY by a raw CDP
    client with no Playwright driver ever attached can report the download
    "canceled" — a Chromium headless-mode behavior this file does not
    control, not a regression (there was no working download path for that
    narrow combination before this either). A real automation script
    connecting via connect_over_cdp() should manage its own downloads with
    Playwright's own API rather than rely on this handler, since a second
    Playwright driver connecting resets the browser's download destination
    to its own default artifacts directory.
    """
    try:
        dest = _dedupe_download_path(downloads_dir, download.suggested_filename)
        await download.save_as(dest)
        logger.info("Download saved: %s", dest)
    except Exception as exc:
        logger.warning(
            "Failed to save download %r: %s",
            getattr(download, "suggested_filename", "<unknown>"), exc,
        )


# A wedged Playwright connection makes context.close() hang. Cleanup paths run
# under cancellation (auto_launch_all's wait_for) and under Docker's stop
# deadline, so a best-effort close must never be able to outlive either.
CONTEXT_CLOSE_TIMEOUT_S = 10.0
# Ceiling for a single launch. Auto-launch has always enforced this; the API
# path needs it too, or a wedged Playwright call leaves the request hanging and
# the id stuck in `_launching` — which is_starting() turns into a permanent 409
# on launch, stop and delete alike.
LAUNCH_TIMEOUT_S = 60
# A teardown claim is released on positive evidence that the identified
# Chromium is gone, never on elapsed time — so there is no TTL and no ceiling
# to tune. What IS time-driven is escalation: a browser that has not exited
# this long after the claim was taken gets SIGTERM, and one that survives that
# gets SIGKILL. Both are logged at ERROR: needing either is a bug report.
# SIGTERM first with a real grace period, because SIGKILL leaves the profile
# dirty (unflushed Preferences/Cookies/LevelDB, "Chrome didn't shut down
# correctly" on the next start).
# 20s, NOT 30s: Playwright's own gracefullyClose deadline is 30s, and measured
# live it force-kills the frozen Chromium at exactly +30.0s. At 30 the sweeper
# (which only evaluates every CLAIM_SWEEP_INTERVAL_S) reached the SIGTERM leg at
# ~33.5s and lost the race every time, so the polite signal was unreachable in
# the ordinary wedge and every teardown was really a silent SIGKILL by the
# driver. 20s fires by ~25s at the latest, ahead of Playwright.
CLOSING_SIGTERM_AFTER_S = 20.0
CLOSING_SIGKILL_AFTER_S = 15.0
# The status path is a pure membership peek, so nothing else re-examines a
# claim on its own. Without this loop a claim whose browser exited quietly
# would sit in _closing forever, reporting "stopping" with launch/stop/delete
# all refusing, until the user happened to click something.
CLAIM_SWEEP_INTERVAL_S = 5.0

# A profile with no VNC viewer attached and no CDP traffic for this long is
# stopped automatically — releasing its CloakBrowser license claim rather than
# holding a Chromium (and a GPU/display) open indefinitely. Per-profile
# idle_timeout_seconds overrides this; 0 disables idle timeout for that
# profile. See reap_idle_profiles().
DEFAULT_IDLE_TIMEOUT_S = int(os.environ.get("PROFILE_IDLE_TIMEOUT_SECONDS", "3600"))
# The viewer-liveness probe hits 127.0.0.1 and Kasm answers it in ~3ms without
# touching the frame clock (see _viewer_attached below), so anything slower
# means the control plane is sick, not busy — give up rather than let one
# stuck profile stall the whole idle sweep.
KASM_VIEWER_PROBE_TIMEOUT_S = 2.0

# Crash-loop guard for auto_restart=True profiles: at most this many
# automatic relaunches within the trailing window, per profile. A profile
# that crashes on launch itself (bad proxy, missing extension, whatever)
# would otherwise retry as fast as launch()+crash can cycle, burning CPU and
# CloakBrowser license claims forever. See _on_browser_closed's genuine-crash
# branch and _auto_restart_after_crash.
AUTO_RESTART_MAX_ATTEMPTS = 3
AUTO_RESTART_WINDOW_S = 300.0
# Deliberately not instant: gives a transient resource blip (proxy hiccup,
# a GPU device momentarily busy from the crash itself) a moment to clear
# before the retry, same spirit as any other backoff.
AUTO_RESTART_DELAY_S = 3.0


def _viewer_attached(bottleneck: Any) -> bool | None:
    """Whether a viewer WebSocket is attached, or None if we cannot tell.

    /api/get_bottleneck_stats is the only exact liveness signal Kasm exposes:
    entries are keyed by peerEndpoint, written per client per framebuffer
    update (VNCSConnectionST::sendStats) and erased in ~VNCSConnectionST, so
    the object goes empty the moment the last viewer socket dies. Verified
    live going empty -> populated -> empty across a connect/disconnect while
    /api/get_sessions stayed stale on the same run.

    None means "no answer" — a 401/5xx, an unreachable port or a body we
    could not parse. Callers must not treat that as "no viewer": only a
    definitively empty object is evidence of a dead socket, and acting on a
    control-plane blip would tear down a perfectly healthy session (or, for
    the idle reaper, stop a profile someone is actively watching).
    """
    if not isinstance(bottleneck, dict):
        return None
    return len(bottleneck) > 0

BASE_CDP_PORT = 5100
CDP_PORT_RANGE = 100  # cycle through 5100-5199 to avoid TIME_WAIT collisions
# Loopback liveness probe: a live Chromium answers instantly, a dead one
# refuses instantly. The timeout only bounds pathological cases.
_BROWSER_PROBE_TIMEOUT_S = 0.25
# /proc/<pid>/stat field 22 (1-based) is the process start time in clock ticks
# since boot. Everything after the comm field is whitespace-separated, and comm
# is the only field that can contain spaces or parens, so slicing at the LAST
# ')' is the only safe way to reach it.
_STAT_STARTTIME_OFFSET = 19  # index into the fields after comm: field 22
_STAT_PPID_OFFSET = 1        # field 4
# A browser cannot be more than this many hops below us; bounds the ancestry
# walk so a corrupted /proc (or a ppid cycle) cannot spin.
_MAX_ANCESTRY_DEPTH = 32


def _port_is_listening(port: int) -> bool:
    """Whether something still accepts on 127.0.0.1:port."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(_BROWSER_PROBE_TIMEOUT_S)
            return probe.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        return False


@dataclass(frozen=True)
class BrowserProcess:
    """A Chromium process this manager launched, identified precisely.

    pid alone is not an identity: pids are recycled, and signalling a recycled
    one kills an unrelated process. (pid, starttime) cannot alias — starttime
    is assigned by the kernel at exec and never changes — so every liveness
    check and every kill re-verifies both.
    """

    pid: int
    starttime: int
    user_data_dir: str
    cdp_port: int
    # The Playwright node driver that spawned this browser — the browser's ppid
    # at discovery time. Recorded because killing Chromium alone is not enough:
    # measured live, a forced teardown leaves the driver resident holding the
    # dead browser as an unreaped zombie, and PID 1 here is the entrypoint shell
    # which does not reap adopted children. Identified by (pid, starttime) like
    # the browser, for the same anti-recycling reason.
    driver_pid: int | None = None
    driver_starttime: int | None = None


def _proc_argv(pid: int) -> list[str]:
    """argv of a process, tolerating Chromium's non-standard encoding.

    Chromium rewrites its own argv into ONE space-joined NUL-terminated buffer
    instead of the usual NUL-separated form, so the textbook `split(b"\\0")`
    returns a single blob and every predicate below silently fails to match.
    A unit test built on subprocess.Popen cannot catch that — a plain child
    uses the normal encoding. Normalising both ways is what makes the scan
    work against a real browser.
    """
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            raw = handle.read()
    except OSError:
        return []
    return raw.replace(b"\0", b" ").decode("utf-8", "replace").split()


def _proc_stat(pid: int) -> tuple[str, int, int] | None:
    """(state, ppid, starttime) from /proc/<pid>/stat, or None if it is gone.

    stat rather than cmdline for the recurring liveness check: reading stat
    does not touch the task's mm, so it cannot block behind a process wedged
    holding mmap_lock — which is exactly the process this has to answer for.
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            raw = handle.read().decode("utf-8", "replace")
    except OSError:
        return None
    close = raw.rfind(")")
    if close < 0:
        return None
    fields = raw[close + 1:].split()
    if len(fields) <= _STAT_STARTTIME_OFFSET:
        return None
    try:
        return (
            fields[0],
            int(fields[_STAT_PPID_OFFSET]),
            int(fields[_STAT_STARTTIME_OFFSET]),
        )
    except ValueError:
        return None


def _is_descendant_of(pid: int, root_pid: int) -> bool:
    """Whether pid sits below root_pid in the process tree.

    Every candidate must pass this before it can be recorded or signalled: the
    argv predicate alone would let the manager SIGKILL a process outside its
    own tree if anything else on the host ever carried the same flags.
    """
    current = pid
    for _ in range(_MAX_ANCESTRY_DEPTH):
        if current == root_pid:
            return True
        if current <= 1:
            return False
        stat = _proc_stat(current)
        if stat is None:
            return False
        current = stat[1]
    return False


def discover_browser_process(
    user_data_dir: str, cdp_port: int, root_pid: int,
) -> BrowserProcess | None:
    """Find the top-level Chromium for a profile by scanning /proc.

    Playwright exposes no browser pid at all (there is no `process` attribute
    anywhere in the Python package), and cloakbrowser hands back the raw
    context, so a scan is the only route to a real identity. The predicate is
    exact: Playwright always injects --user-data-dir and launch() appends
    --remote-debugging-port, the directory is a UUID under /data/profiles, and
    renderers/GPU/utility children inherit BOTH flags — which is why the
    --type= exclusion is mandatory, not defensive.
    """
    wanted_dir = f"--user-data-dir={user_data_dir}"
    wanted_port = f"--remote-debugging-port={cdp_port}"
    try:
        entries = os.listdir("/proc")
    except OSError:
        return None
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        argv = _proc_argv(pid)
        if wanted_dir not in argv or wanted_port not in argv:
            continue
        if any(arg.startswith("--type=") for arg in argv):
            continue  # renderer/gpu/utility child, not the browser
        if not _is_descendant_of(pid, root_pid):
            continue
        stat = _proc_stat(pid)
        if stat is None or stat[0] == "Z":
            continue
        driver = _proc_stat(stat[1])
        return BrowserProcess(
            pid=pid, starttime=stat[2], user_data_dir=user_data_dir, cdp_port=cdp_port,
            driver_pid=stat[1] if driver is not None else None,
            driver_starttime=driver[2] if driver is not None else None,
        )
    return None


def process_is_alive(proc: BrowserProcess) -> bool:
    """Whether THAT process — not merely that pid — is still running.

    A zombie counts as dead: PID 1 in this container is the entrypoint shell
    with no reaping of adopted children, so a killed Chromium can sit in state
    'Z' indefinitely. Treating 'Z' as alive would mean the guard never
    releases. A starttime mismatch also counts as dead: the pid was recycled
    and our browser is gone.
    """
    stat = _proc_stat(proc.pid)
    if stat is None:
        return False
    state, _ppid, starttime = stat
    return state != "Z" and starttime == proc.starttime


async def discover_browser_process_async(
    user_data_dir: str, cdp_port: int,
) -> BrowserProcess | None:
    """discover_browser_process() with the /proc walk off the event loop.

    The scan reads cmdline for every pid on the host (~10 ms for 400 pids),
    and the loop it would otherwise block also serves nginx's viewer
    auth_request subrequests for every live session.
    """
    return await asyncio.get_running_loop().run_in_executor(
        None, discover_browser_process, user_data_dir, cdp_port, os.getpid(),
    )


def _signal_pid(pid: int | None, starttime: int | None, sig: int, what: str) -> bool:
    """Signal a pid only if it is still the process we recorded.

    The identity re-check is inside the same function as the kill on purpose:
    a check at the call site leaves a window in which the pid is recycled, and
    the manager would then SIGKILL an unrelated process.
    """
    if pid is None or starttime is None:
        return False
    stat = _proc_stat(pid)
    if stat is None or stat[0] == "Z" or stat[2] != starttime:
        return False
    try:
        os.kill(pid, sig)
    except OSError as exc:
        logger.warning("Could not signal %s pid %d: %s", what, pid, exc)
        return False
    return True


def _signal_process(proc: BrowserProcess, sig: int) -> bool:
    """Signal the browser only if it is still the one we recorded."""
    return _signal_pid(proc.pid, proc.starttime, sig, "browser")


def _signal_driver(proc: BrowserProcess, sig: int) -> bool:
    """Signal the Playwright node driver that owns this browser.

    Only ever used after the browser itself has been SIGKILLed. Killing the
    browser alone leaves the driver blocked on a pipe read from a process that
    will never answer, holding the dead child as a zombie forever — verified
    live: the driver survived a forced teardown and the zombie persisted
    because PID 1 in this container does not reap adopted children.
    """
    return _signal_pid(proc.driver_pid, proc.driver_starttime, sig, "playwright driver")


async def _close_context_bounded(context: Any, profile_id: str) -> bool:
    """Best-effort context.close() that cannot outlive its caller.

    Shielded so a cancellation of *this* task does not abandon the close
    half-done, and bounded so a wedged Playwright connection cannot hold a
    cancellation (or a container shutdown) open indefinitely.
    """
    # No is_closed() short-circuit: playwright sets _closing_or_closed at the
    # TOP of close(), before its first await, and is_closed() returns that same
    # flag — so "closed" and "closing, possibly hung forever" are
    # indistinguishable. Short-circuiting on it reports success for a close
    # that is merely in flight, and the caller then drops the guard while
    # Chromium is still writing to user_data_dir. close() already early-returns
    # on an already-closed context, so the check only ever saved a no-op await.
    if context is None:
        return True
    closing = asyncio.ensure_future(context.close())
    try:
        # shield: a cancellation of *this* task leaves the close running rather
        # than abandoning the browser half-closed.
        await asyncio.wait_for(asyncio.shield(closing), timeout=CONTEXT_CLOSE_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.warning(
            "Timed out closing browser context for %s after %.0fs; continuing",
            profile_id, CONTEXT_CLOSE_TIMEOUT_S,
        )
        # Let it finish in the background, but consume the eventual result so a
        # later failure is not reported as a never-retrieved exception.
        closing.add_done_callback(
            lambda task: task.cancelled() or task.exception() is not None
        )
        return False
    except Exception as exc:
        logger.warning("Error closing context for %s: %s", profile_id, exc)
        return False
    return True


@dataclass
class RunningProfile:
    profile_id: str
    context: Any  # Playwright BrowserContext
    # None for a headless profile: there is no X server and no viewer. Every
    # consumer must treat these as optional rather than assuming a display
    # exists — a headless profile that allocated one would burn a display, a
    # ws_port, a password file and an idle Xvnc per profile to render a root
    # window no pixel ever leaves.
    display: int | None
    ws_port: int | None
    cdp_port: int
    user_data_dir: str = ""
    # The Chromium this profile owns. launch() fails closed when discovery
    # returns None, so in production this is never None for a registered
    # profile — that is precisely what lets the teardown guard release on
    # evidence instead of on a timer.
    proc: BrowserProcess | None = None
    # Identifies THIS launch. display and ws_port are deterministically
    # recycled (allocate() gap-fills from BASE_DISPLAY up), so a stop+relaunch
    # hands back the identical pair and a stale viewer token would authorise
    # against the new session. A nonce cannot be recycled.
    session_epoch: str = field(default_factory=lambda: secrets.token_hex(8))
    # Resolved once at launch from the profile's idle_timeout_seconds (or
    # DEFAULT_IDLE_TIMEOUT_S) — a later edit takes effect on the next launch,
    # the same restart-required contract extensions.py already documents.
    # <= 0 disables idle timeout for this running instance.
    idle_timeout_seconds: int = DEFAULT_IDLE_TIMEOUT_S
    # Bumped by touch_activity() on real VNC-viewer or CDP traffic; compared
    # against idle_timeout_seconds by reap_idle_profiles(). monotonic, not
    # wall-clock, so it cannot be upset by a clock step.
    last_active: float = field(default_factory=time.monotonic)


@dataclass
class ClosingClaim:
    """Ownership of a profile whose browser has not finished exiting.

    Held for the WHOLE teardown, not just after one fails: the profile is out
    of `running` but Chromium is still alive and writing to user_data_dir, so
    a launch or a delete must not treat the directory as free.

    `proc` may be None for a claim taken before the Playwright call returned
    (a launch cancelled inside launch_persistent_context_async still leaves a
    live Chromium the driver started). Resolution then re-runs discovery from
    user_data_dir + cdp_port, which is why both are stored here rather than
    only on RunningProfile.
    """

    context: Any | None
    proc: BrowserProcess | None
    user_data_dir: str
    cdp_port: int | None
    claimed_at: float
    sigterm_at: float | None = None
    sigkill_at: float | None = None
    # Set once the claim has been held open by an unidentifiable process, so
    # the 5s sweeper reports that state exactly once instead of every tick.
    warned_unidentified: bool = False


class BrowserManager:
    def __init__(self):
        self.running: dict[str, RunningProfile] = {}
        self._launching: set[str] = set()  # profile IDs currently being launched
        # Auto-launch profiles queued at startup but not yet reached. Without
        # this, every profile behind the one currently launching would report
        # "stopped" — which an open viewer reads as a terminal session end.
        self._pending_auto_launch: set[str] = set()
        # Profiles being deleted. A launch must not claim one mid-delete: the
        # rmtree would then run under a starting Chromium.
        self._deleting: set[str] = set()
        # Profiles whose browser teardown has not completed. Written ONLY from
        # the event loop thread: get_status() runs in an executor via
        # get_liveness_async(), so anything it touches must be read-only or the
        # two threads race (a `del` there raises KeyError when the close
        # handler removes the same entry first, and a re-write resurrects a
        # claim stop() already released).
        self._closing: dict[str, ClosingClaim] = {}
        self.vnc = VNCManager()
        self._lock = asyncio.Lock()
        self._next_cdp_port = BASE_CDP_PORT
        self._auto_launch_task: asyncio.Task | None = None
        self._sweep_task: asyncio.Task | None = None
        # Notified with a display number once its Xvnc is gone. Anything bound
        # to a display (main.py's xclip relay) must be torn down here, or it
        # outlives its X server and leaks for the container's lifetime.
        self._display_released_hooks: list[Callable[[int], None]] = []
        # monotonic timestamps of recent auto-restart attempts per profile id,
        # for AUTO_RESTART_MAX_ATTEMPTS/AUTO_RESTART_WINDOW_S — see
        # _consume_restart_budget.
        self._crash_restart_history: dict[str, list[float]] = {}

    def add_display_released_hook(self, hook: Callable[[int], None]) -> None:
        self._display_released_hooks.append(hook)

    async def _release_display(self, display: int | None) -> None:
        """Stop Xvnc for a display and notify everything bound to it.

        A headless profile never allocated one, so None is a normal input
        rather than a bug — every teardown path funnels through here.
        """
        if display is None:
            return
        await self.vnc.stop_vnc(display)
        for hook in self._display_released_hooks:
            try:
                hook(display)
            except Exception as exc:
                logger.warning("Display-released hook failed for :%d: %s", display, exc)

    async def launch(self, profile: dict[str, Any]) -> RunningProfile:
        """Launch a browser instance for the given profile."""
        profile_id = profile["id"]

        # Resolve any teardown claim BEFORE taking the lock. The check probes
        # /proc in an executor; doing it under _lock stalled the single event
        # loop for the whole probe — measured at 254 ms with the old blocking
        # socket connect — and that loop also serves nginx's viewer
        # auth_request subrequests for every live session.
        await self.check_wedged(profile_id)

        async with self._lock:
            if (
                profile_id in self.running
                or profile_id in self._launching
                or profile_id in self._deleting
                or self.peek_wedged(profile_id)
            ):
                raise ProfileAlreadyRunning(f"Profile {profile_id} is already running")
            self._launching.add(profile_id)

        # One handler covers everything after the claim above. The filesystem
        # setup below is the most failure-prone part of the function (a full or
        # read-only /data, EACCES on a lock file) and used to sit outside it —
        # an exception there left the id in `_launching` forever, and
        # is_starting() makes that permanent and total: /launch, /stop and
        # DELETE all answer 409, /viewer-token 503, and the display is never
        # reclaimed. Only a container restart cleared it.
        display: int | None = None
        ws_port: int | None = None
        context = None
        # A headless Chromium never draws to X, so an Xvnc for it is pure
        # overhead and the viewer it advertises can only ever show an empty
        # root window. Skip the allocation entirely rather than starting a
        # server nothing will connect to.
        headless = bool(profile.get("headless", False))
        # Per-profile override. cloakbrowser resolves a license key as
        # explicit param > CLOAKBROWSER_LICENSE_KEY env > cached key file, so
        # a set override cleanly beats the container-wide env var — but an
        # UNSET one (None) falls through to that env var same as before, it
        # does NOT force free tier. Forcing free despite a container-wide key
        # would mean blanking a process-wide env var for the duration of
        # ensure_binary()/launch (which can include a first-time download),
        # serializing every concurrent launch that needs a different key
        # against it — not a trade worth making for what's otherwise a rare
        # case. Leave the field blank to inherit the container's default.
        profile_license_key = profile.get("license_key") or None
        try:
            # ensure_binary() is a blocking, synchronous call (network I/O),
            # and launch_persistent_context_async() below calls it again
            # internally with no way to make it async. Off the event loop
            # first: on a warm cache this is a cheap no-op (the second,
            # internal call just re-hits the same cache), but on a first-ever
            # launch it keeps every other request — including the frontend's
            # download-progress poll — served while the ~200-460MB binary
            # downloads, instead of freezing the whole API for that duration.
            # binary_status only flips its banner on when a download actually
            # starts, so a cache hit here never shows one. mark_idle() in the
            # finally covers the one case the module's own log-parsing can't:
            # a failed download, which never reaches the "Binary ready" line
            # that would otherwise clear it, leaving the banner stuck on.
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, functools.partial(ensure_binary, license_key=profile_license_key)
                )
            finally:
                binary_status.mark_idle()

            if not headless:
                display, ws_port = await self.vnc.allocate()
            cdp_port = self._allocate_cdp_port()

            # Clean stale Chromium lock files (left by previous container crashes)
            user_data_dir = Path(profile["user_data_dir"])
            for lock_file in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
                lock_path = user_data_dir / lock_file
                lock_path.unlink(missing_ok=True)

            # Set up bookmarks and search engine on first launch
            _init_profile_defaults(user_data_dir)

            # Downloads land in <user_data_dir>/Downloads with their real
            # filename — see the download handler wired up after launch below
            # for why a plain downloads_path alone is not enough. chmod 777
            # rather than relying on the container's default umask: this
            # directory is bind-mounted onto the host (the whole /data volume
            # is), and a host whose Docker is not user-namespace-remapped
            # would otherwise hand back root-owned files the host user can
            # read but not delete.
            downloads_dir = user_data_dir / "Downloads"
            downloads_staging_dir = user_data_dir / ".pw-downloads"
            downloads_dir.mkdir(exist_ok=True)
            downloads_staging_dir.mkdir(exist_ok=True)
            os.chmod(downloads_dir, 0o777)
            os.chmod(downloads_staging_dir, 0o777)

            # Start KasmVNC on the allocated display (headed profiles only)
            if display is not None:
                await self.vnc.start_vnc(
                    display,
                    ws_port,
                    width=profile.get("screen_width", 1920),
                    height=profile.get("screen_height", 1080),
                )

            # Build fingerprint args from profile settings
            extra_args = self._build_fingerprint_args(profile)
            extra_args += profile.get("launch_args") or []
            extra_args.append(f"--remote-debugging-port={cdp_port}")

            # Resolved against the set extensions.py discovered at startup —
            # an enabled id for an extension since removed (or not yet
            # picked up because the container hasn't restarted) is silently
            # dropped rather than erroring the launch. See extensions.py.
            extension_paths = extension_paths_for(profile.get("enabled_extensions") or [])

            # Normalize proxy format (host:port:user:pass → http://user:pass@host:port)
            raw_proxy = profile.get("proxy") or None
            proxy = _normalize_proxy(raw_proxy) if raw_proxy else None
            if proxy:
                _validate_proxy(proxy)

            # Claim the profile BEFORE the Playwright call, not after it
            # returns. Cancellation inside launch_persistent_context_async (the
            # LAUNCH_TIMEOUT_S case this function exists to survive) is purely
            # Python-side: the node driver has already been told to launch and
            # finishes doing so, leaving a live Chromium on user_data_dir. With
            # the claim taken only once `context` exists, that browser had no
            # owner at all — the next launch would unlink the Singleton locks
            # and start a SECOND Chromium on it, and a DELETE would rmtree
            # under it. The claim carries the discovery key rather than a
            # context, because there is no context yet.
            self._closing[profile_id] = ClosingClaim(
                context=None, proc=None, user_data_dir=profile["user_data_dir"],
                cdp_port=cdp_port, claimed_at=time.monotonic(),
            )

            # Launch CloakBrowser on that display
            # DISPLAY is passed via env kwarg to avoid process-wide os.environ mutation
            context = await launch_persistent_context_async(
                user_data_dir=profile["user_data_dir"],
                headless=bool(profile.get("headless", False)),
                proxy=proxy,
                args=extra_args,
                timezone=profile.get("timezone") or None,
                locale=profile.get("locale") or None,
                humanize=bool(profile.get("humanize", False)),
                human_preset=profile.get("human_preset", "default"),
                geoip=bool(profile.get("geoip", False)),
                color_scheme=profile.get("color_scheme") or None,
                license_key=profile_license_key,
                extension_paths=extension_paths,
                downloads_path=str(downloads_staging_dir),
                user_agent=profile.get("user_agent") or None,
                viewport={
                    "width": profile.get("screen_width", 1920),
                    "height": profile.get("screen_height", 1080) - 133,
                },
                # No DISPLAY for headless: there is no X server, and passing
                # a dead one makes Chromium retry an X connection it cannot make.
                # _chrome_gpu_env() only pins the EGL vendor for the GPU
                # fallback path — see _NVIDIA_GPU_ENV.
                env=({**os.environ, **_chrome_gpu_env(), "DISPLAY": f":{display}"}
                     if display is not None
                     else {**os.environ, **_chrome_gpu_env()}),
            )

            # Give the claim the identity it was missing. Discovery is the only
            # route to the Chromium pid — Playwright exposes none — and it is
            # what lets the teardown guard release on "that process is gone"
            # instead of on a timer that is wrong in one direction whatever it
            # is set to.
            proc = await discover_browser_process_async(
                profile["user_data_dir"], cdp_port,
            )
            if proc is None:
                # Fail closed. An unidentifiable browser can be neither proven
                # dead nor escalated, so registering it would reintroduce the
                # guesswork the pid design removes. The abort path below closes
                # the context we just opened.
                raise RuntimeError(
                    f"Could not identify the Chromium process for {profile_id}"
                )
            claim = self._closing.get(profile_id)
            if claim is not None:
                claim.context = context
                claim.proc = proc

            # Register the close handler NOW, not after the setup below. The
            # setup awaits, so a context that dies (or is cancelled) in that
            # window would otherwise have no handler at all — and nothing would
            # ever clear a wedged entry recorded for it.
            closed_context = context
            context.on("close", lambda: asyncio.ensure_future(
                self._on_browser_closed(profile_id, closed_context)
            ))

            # Inject clipboard listener: captures copied text on every page
            # so the GET /clipboard endpoint can read it via page.evaluate()
            _clipboard_init_js = """
                window.__clipboardText = '';
                document.addEventListener('copy', () => {
                    const sel = window.getSelection();
                    if (sel) window.__clipboardText = sel.toString();
                });
                document.addEventListener('keydown', (e) => {
                    if ((e.ctrlKey || e.metaKey) && e.key === 'c' && !e.altKey && !e.shiftKey) {
                        const sel = window.getSelection();
                        if (sel && sel.toString()) window.__clipboardText = sel.toString();
                    }
                });
            """
            await context.add_init_script(_clipboard_init_js)
            # Also inject into already-open pages (about:blank created before init_script)
            for p in context.pages:
                try:
                    await p.evaluate(_clipboard_init_js)
                except Exception as exc:
                    logger.debug("Clipboard init failed on existing page: %s", exc)

            # Finalize downloads with their real filename in the visible
            # Downloads folder. Playwright intercepts every download via CDP
            # once a browser is under its control — including ones a human
            # triggers by clicking in the VNC-visible window, not just
            # automation — and downloads_path alone only relocates the
            # GUID-named artifact (still deleted when this context closes);
            # it does not rename it. _finalize_download does that rename by
            # copying it out to `downloads_dir` under its suggested filename.
            # "page" covers tabs opened after this point; the loop covers
            # about:blank created before it — same two-part pattern as the
            # clipboard init script just above.
            context.on("page", lambda new_page: new_page.on(
                "download", lambda download: asyncio.ensure_future(
                    _finalize_download(download, downloads_dir)
                ),
            ))
            for p in context.pages:
                p.on("download", lambda download: asyncio.ensure_future(
                    _finalize_download(download, downloads_dir)
                ))

            idle_timeout_seconds = profile.get("idle_timeout_seconds")
            if idle_timeout_seconds is None:
                idle_timeout_seconds = DEFAULT_IDLE_TIMEOUT_S

            running = RunningProfile(
                profile_id=profile_id,
                context=context,
                display=display,
                ws_port=ws_port,
                cdp_port=cdp_port,
                user_data_dir=profile["user_data_dir"],
                proc=proc,
                idle_timeout_seconds=idle_timeout_seconds,
            )

            async with self._lock:
                self.running[profile_id] = running
                self._launching.discard(profile_id)
                # The launch-phase claim has done its job: ownership now lives
                # in `running`. Leaving it would report "stopping" for a live
                # profile and make stop/delete answer 409.
                self._closing.pop(profile_id, None)

            # The close event can fire between registering the handler above
            # and this point (there are awaits in between). _on_browser_closed
            # would then have found nothing in `running`, skipped stop_vnc, and
            # returned — leaving an orphaned Xvnc holding the display/ws_port
            # while we register a dead context as "running" forever. Re-check
            # now that we are visible to the handler.
            if running.context.is_closed():
                logger.warning(
                    "Browser for profile %s exited during launch registration", profile_id,
                )
                # _on_browser_closed() has already reclaimed the display. Clear
                # our handle so the except-path finally does NOT tear it down a
                # second time: by then a concurrent relaunch may have been
                # allocated that same display, and the duplicate stop_vnc would
                # pop its allocation and unlink its password file.
                # Pass the context: _on_browser_closed's identity check exists
                # precisely so a caller cannot clear a claim it does not own,
                # and this caller has the object in hand.
                await self._on_browser_closed(profile_id, closed_context)
                display = None
                raise RuntimeError("Browser exited during launch")

            logger.info(
                "Launched profile %s (%s, cdp_port=%d)", profile_id,
                f"display :{display}, ws_port={ws_port}" if display is not None
                else "headless, no display", cdp_port,
            )

            return running

        except BaseException:
            async with self._lock:
                self._launching.discard(profile_id)
            try:
                # Close the browser we opened. The context.on("close") handler
                # only fires if Chromium closes itself, and nothing here asks
                # it to — so aborting after launch_persistent_context_async()
                # returned would leave a live Chromium with its X server killed
                # out from under it, holding cdp_port (permanently skipped by
                # _allocate_cdp_port) and still writing to user_data_dir. A
                # later relaunch deletes the Singleton* locks and opens a
                # SECOND Chromium on the same profile.
                # The claim was already taken before the Playwright call, so
                # the whole abort window is owned even when `context` is None
                # (cancelled mid-launch). Re-stamp it: escalation is measured
                # from the moment the teardown started, and the launch phase
                # itself is deliberately exempt (see _apply_claim_verdict), so
                # a slow-but-healthy launch is never SIGTERMed.
                claim = self._closing.get(profile_id)
                if claim is not None:
                    claim.claimed_at = time.monotonic()
                    if context is not None:
                        claim.context = context
                closed = await _close_context_bounded(context, profile_id)
                # `context is None` is NOT evidence that no browser exists: the
                # driver may have finished launching one after we were
                # cancelled. Only a close we actually performed releases the
                # claim; otherwise the sweeper decides, by looking for the
                # process itself.
                if context is not None and closed:
                    self._closing.pop(profile_id, None)
                elif claim is not None:
                    logger.warning(
                        "Aborted launch for %s left a browser that may still be "
                        "running; holding the guard until it is proven gone",
                        profile_id,
                    )
            finally:
                # In a finally: if the close hangs past its bound (or we are
                # being cancelled) the display must still be reclaimed, or
                # wait_for() never returns and every queued auto-launch profile
                # stays stuck reporting "starting".
                if display is not None:
                    await self._release_display(display)
            raise

    async def _on_browser_closed(self, profile_id: str, context: Any):
        """Called when a browser exits (crash, closed via VNC, or stop()).

        `context` identifies which instance closed and is REQUIRED. It used to
        default to None, and that branch cleared whatever claim happened to be
        recorded for the profile — correct for its one caller, and a silent
        way to hand a live Chromium's user_data_dir to the next launch for
        any second caller. A late close from a superseded context must also be
        ignored: acting on profile_id alone would pop and tear down the
        replacement that now owns this id.
        """
        # This browser is genuinely gone now. Resolve its claim FIRST — before
        # the superseded-context check below, which returns early — otherwise a
        # wedged context whose close lands while another instance holds the id
        # would leave the profile blocked forever.
        claim = self._closing.get(profile_id)
        if claim is not None and claim.context is context:
            logger.info("Browser for profile %s finished closing", profile_id)
            # pop, never del: the sweeper may have resolved the same claim on
            # this loop turn.
            self._closing.pop(profile_id, None)

        async with self._lock:
            current = self.running.get(profile_id)
            if current is not None and current.context is not context:
                logger.info(
                    "Ignoring close from a superseded context for profile %s", profile_id,
                )
                return
            running = self.running.pop(profile_id, None)

        viewer_tokens.revoke_profile(profile_id)

        if running:
            logger.info("Browser closed for profile %s, cleaning up", profile_id)
            await self._release_display(running.display)
            # `running` was still in self.running when this close arrived —
            # nobody called stop() first (that pops it BEFORE closing, see
            # stop()'s own comment), so this is an unrequested closure: a
            # crash, an OOM kill, the user closing Chromium some other way.
            # Fire-and-forget: this handler must not block on a relaunch.
            asyncio.ensure_future(self._maybe_auto_restart(profile_id))

    def _consume_restart_budget(self, profile_id: str) -> bool:
        """True and records an attempt if `profile_id` has budget left in the
        trailing AUTO_RESTART_WINDOW_S; False (no side effect) otherwise."""
        now = time.monotonic()
        history = self._crash_restart_history.setdefault(profile_id, [])
        history[:] = [t for t in history if now - t < AUTO_RESTART_WINDOW_S]
        if len(history) >= AUTO_RESTART_MAX_ATTEMPTS:
            return False
        history.append(now)
        return True

    async def _maybe_auto_restart(self, profile_id: str) -> None:
        """Relaunch `profile_id` after an unrequested closure, if it opted in
        and still has restart budget. Never raises — this runs detached from
        whatever noticed the crash (_on_browser_closed or reap_dead_browsers,
        itself called from the maintenance loop), so an uncaught exception
        here would either vanish into "Task exception was never retrieved" or,
        in the maintenance loop's case, kill the loop that also runs the
        teardown-claim sweep and the idle reaper.
        """
        from . import database as db

        profile = db.get_profile(profile_id)
        if profile is None or not profile.get("auto_restart"):
            return
        if not self._consume_restart_budget(profile_id):
            logger.error(
                "Profile %s crashed again but has used its %d auto-restarts "
                "in the last %.0fs — giving up until it is launched manually",
                profile_id, AUTO_RESTART_MAX_ATTEMPTS, AUTO_RESTART_WINDOW_S,
            )
            return

        await asyncio.sleep(AUTO_RESTART_DELAY_S)
        # Re-check after the delay: a manual launch (or another auto-restart
        # racing this one, though _lock inside launch() itself prevents two
        # actually starting) may have already made this moot.
        if profile_id in self.running or profile_id in self._launching:
            return
        logger.warning(
            "Auto-restarting profile %s (%s) after an unrequested close",
            profile.get("name", "?"), profile_id,
        )
        try:
            await asyncio.wait_for(self.launch(profile), timeout=LAUNCH_TIMEOUT_S)
            logger.info("Auto-restart succeeded for profile %s", profile_id)
        except ProfileAlreadyRunning:
            pass  # relaunched by something else while we were waiting
        except Exception as exc:
            logger.error("Auto-restart failed for profile %s: %s", profile_id, exc)

    async def stop(self, profile_id: str) -> bool:
        """Stop a running browser instance.

        Returns whether the browser actually closed. False means Chromium is
        still alive (a wedged teardown) — callers must not then treat the
        profile directory as free to delete.
        """
        # Pop before close so _on_browser_closed() finds nothing to clean up
        async with self._lock:
            running = self.running.pop(profile_id, None)

        viewer_tokens.revoke_profile(profile_id)

        if not running:
            # Lost the pop race to another teardown. Reporting True here would
            # tell the caller the browser closed while another stop() is still
            # awaiting a close that may never land — and the documented
            # contract ("False means Chromium is still alive") is what DELETE
            # relies on before it rmtrees user_data_dir.
            return not self.peek_wedged(profile_id)

        logger.info("Stopping profile %s", profile_id)

        # Claim BEFORE closing, not after it fails. The profile is already out
        # of `running` and the close below takes up to CONTEXT_CLOSE_TIMEOUT_S,
        # so recording it only on failure leaves the entire teardown window
        # unguarded — a concurrent DELETE would rmtree user_data_dir, and a
        # concurrent launch would start a second Chromium, while the first is
        # still closing. The claim simply persists if the close never lands.
        # NOTHING may await between the pop above and this assignment: an
        # uncontended asyncio.Lock does not yield, so pop+claim is atomic with
        # respect to the loop today, and that is the only reason a concurrent
        # DELETE cannot slip through the gap and rmtree under a live browser.
        self._closing[profile_id] = ClosingClaim(
            context=running.context, proc=running.proc,
            user_data_dir=running.user_data_dir, cdp_port=running.cdp_port,
            claimed_at=time.monotonic(),
        )
        try:
            # Bounded: cleanup_all() runs this for every profile inside Docker's
            # stop grace period, so one wedged context must not hold it open.
            closed = await _close_context_bounded(running.context, profile_id)
            if closed:
                self._closing.pop(profile_id, None)
            else:
                logger.warning(
                    "Browser for profile %s did not close; blocking launch/delete until it does",
                    profile_id,
                )
        except BaseException:
            # The guard is deliberately NOT released here. The close is
            # shielded, so on cancellation it keeps running and Chromium may
            # still be alive — dropping it now would let a relaunch or delete
            # race a live browser. The claim is self-clearing: the close
            # handler resolves it, and failing that the sweeper escalates to
            # SIGTERM/SIGKILL and releases once the process is provably gone.
            raise
        finally:
            # Xvnc is ours regardless of how the close went, and reclaiming it
            # is independent of whether the browser exited. Shielded so a
            # cancelled stop() still frees the display, its ws_port and its
            # password file instead of leaking all three for the process
            # lifetime.
            await asyncio.shield(
                asyncio.ensure_future(self._release_display(running.display))
            )
        # The close may have landed while Xvnc was being torn down; report what
        # is true now rather than what was true a moment ago.
        return not await self.check_wedged(profile_id)

    def claim_for_delete(self, profile_id: str) -> bool:
        """Block launches for the duration of a delete.

        Exclusive: a second concurrent delete must not share the claim, or the
        first to finish would release it while the second is still running and
        reopen the launch window this exists to close.
        """
        if profile_id in self._deleting:
            return False
        self._deleting.add(profile_id)
        return True

    def release_delete_claim(self, profile_id: str) -> None:
        self._deleting.discard(profile_id)

    def peek_wedged(self, profile_id: str) -> bool:
        """Is a teardown claim recorded for this profile?

        Pure: one dict membership test, no probing, no mutation. Safe on every
        3s poll and from any thread, including get_liveness_async()'s executor.
        Mirrors exactly what launch() enforces under the lock, so "the UI says
        stopping" and "a mutation will refuse" cannot disagree. Use
        check_wedged() wherever a DECISION is made.
        """
        return profile_id in self._closing

    @staticmethod
    def _claim_evidence(claim: ClosingClaim) -> tuple[bool, BrowserProcess | None]:
        """(is the browser alive, newly discovered identity) — /proc only.

        Runs in an executor, so it must not touch any BrowserManager dict.
        A claim with no identity re-runs discovery: it was taken before the
        Playwright call returned, and the driver may have finished launching a
        Chromium after the awaiting coroutine was cancelled.
        """
        if claim.proc is None:
            if claim.cdp_port is None:
                return (False, None)
            found = discover_browser_process(
                claim.user_data_dir, claim.cdp_port, os.getpid(),
            )
            if found is not None:
                return (True, found)
            # Discovery came back empty — but "we cannot see it" is not "it is
            # gone". A Chromium reparented to PID 1 (driver OOM-killed while the
            # browser was mid-write) fails the descendant check and is invisible
            # here, and releasing the claim would hand its live user_data_dir to
            # a relaunch or an rmtree. Widening the descendant check is not the
            # answer — that is what keeps impostors out — so fail CLOSED on the
            # one piece of evidence still available: something is holding the
            # CDP port this profile was launched with.
            if _port_is_listening(claim.cdp_port):
                return (True, None)
            return (False, None)
        return (process_is_alive(claim.proc), None)

    def _apply_claim_verdict(
        self, profile_id: str, claim: ClosingClaim,
        alive: bool, discovered: BrowserProcess | None,
    ) -> bool:
        """Apply a probe verdict. EVENT LOOP THREAD ONLY.

        `claim` is the object the probe was started for; if _closing no longer
        holds THAT object another task has already decided and we must not
        clobber it — that is the race that used to resurrect a claim stop() had
        just released, re-wedging a cleanly-closed profile.
        """
        current = self._closing.get(profile_id)
        if current is None:
            return False                       # released while we probed
        if current is not claim:
            return True                        # superseded: still guarded
        if profile_id in self._launching:
            # A launch holds a claim for its whole duration so a cancellation
            # inside the Playwright call cannot orphan a browser. It is not a
            # teardown: neither "no process yet" nor "process alive at +30s"
            # says anything, and escalating here would SIGTERM a healthy
            # browser mid-launch.
            return True
        if discovered is not None:
            claim.proc = discovered
            logger.error(
                "Adopted orphaned Chromium pid %d for profile %s (launch aborted "
                "after the driver started it)", discovered.pid, profile_id,
            )
        if not alive:
            logger.info("Browser for profile %s is gone; releasing the guard", profile_id)
            self._closing.pop(profile_id, None)  # pop, never del: idempotent
            return False
        if claim.proc is None:
            # Held open by evidence we cannot attribute to a process (the CDP
            # port is still bound but discovery finds nothing below us). The
            # guard is correct — something owns that directory — but nothing can
            # end it, so say so loudly rather than sitting silent forever.
            if not claim.warned_unidentified:
                claim.warned_unidentified = True
                logger.error(
                    "Profile %s is held by a browser on cdp port %s that is not "
                    "below this process; it cannot be signalled and the profile "
                    "stays 'stopping' until that process exits",
                    profile_id, claim.cdp_port,
                )
            return True
        self._escalate(profile_id, claim)
        return True

    def _escalate(self, profile_id: str, claim: ClosingClaim) -> None:
        """SIGTERM then SIGKILL a browser that will not exit on its own.

        This is what removes the old dilemma between bricking the profile
        forever and releasing the guard under a live browser: the manager can
        now END the teardown it is waiting on. Both signals re-verify
        (pid, starttime) inside _signal_process, so a recycled pid is never
        touched, and both kill the whole `--type=` child tree with the browser.
        """
        if claim.proc is None:
            return
        now = time.monotonic()
        if claim.sigterm_at is None:
            if now - claim.claimed_at < CLOSING_SIGTERM_AFTER_S:
                return
            logger.error(
                "Browser for profile %s (pid %d) has not exited %.0fs after its "
                "teardown began; sending SIGTERM",
                profile_id, claim.proc.pid, now - claim.claimed_at,
            )
            _signal_process(claim.proc, signal.SIGTERM)
            claim.sigterm_at = now
            return
        if claim.sigkill_at is None and now - claim.sigterm_at >= CLOSING_SIGKILL_AFTER_S:
            logger.error(
                "Browser for profile %s (pid %d) survived SIGTERM; sending SIGKILL. "
                "The profile directory may be left dirty.",
                profile_id, claim.proc.pid,
            )
            _signal_process(claim.proc, signal.SIGKILL)
            # And the driver that owns it. If the teardown wedged because the
            # PLAYWRIGHT connection hung — the case this whole machinery exists
            # for — the driver is still blocked on a pipe read from a browser
            # that is now dead, and it will hold the zombie for the container's
            # lifetime. Killing it lets the kernel reparent and reap the child.
            if _signal_driver(claim.proc, signal.SIGKILL):
                logger.error(
                    "Also killed the Playwright driver (pid %s) for profile %s; it "
                    "outlived the browser it was waiting on",
                    claim.proc.driver_pid, profile_id,
                )
            claim.sigkill_at = now

    async def check_wedged(self, profile_id: str) -> bool:
        """True while a stopped profile's Chromium has not finished exiting.

        Authoritative: probes /proc off the event loop and mutates _closing
        only on the loop thread. Never call this from an executor — use
        peek_wedged() there.
        """
        claim = self._closing.get(profile_id)
        if claim is None:
            return False
        alive, discovered = await asyncio.get_running_loop().run_in_executor(
            None, self._claim_evidence, claim,
        )
        return self._apply_claim_verdict(profile_id, claim, alive, discovered)

    async def sweep_teardown_claims(self) -> None:
        """One pass over every teardown claim.

        Mandatory, not optional: the status path is a pure peek, so without
        this nothing releases a claim whose browser exited quietly and the
        profile would report "stopping" with every mutation refused until the
        user clicked something — which the UI renders as a disabled button.
        """
        # list(): never iterate a dict that the loop's own callbacks mutate.
        for profile_id in list(self._closing):
            try:
                await self.check_wedged(profile_id)
            except Exception as exc:
                # One bad claim must not kill the pass for every other one.
                logger.warning("Claim sweep failed for %s: %s", profile_id, exc)

    async def reap_dead_browsers(self) -> None:
        """Retire running profiles whose Chromium is gone but never said so.

        Playwright's context 'close' event is driven exclusively by a message
        FROM the node driver. Kill the driver — an OOM kill is enough — and
        Chromium dies with it while `is_closed()` stays False, `is_connected()`
        stays True and NO event is emitted. _on_browser_closed() then never
        runs: the profile reports "running" forever, /launch answers 409
        forever, and its display, ws_port and password file are held for the
        life of the container. Verified end to end.
        """
        for profile_id, running in list(self.running.items()):
            if running.proc is None:
                continue  # no identity to check (only reachable in tests)
            alive = await asyncio.get_running_loop().run_in_executor(
                None, process_is_alive, running.proc,
            )
            if alive or self.running.get(profile_id) is not running:
                continue
            logger.error(
                "Browser for profile %s (pid %d) is gone but emitted no close "
                "event; reclaiming its display", profile_id, running.proc.pid,
            )
            # Safe to share with the event handler: it is context-identity
            # checked, so a concurrent real close is idempotent.
            await self._on_browser_closed(profile_id, running.context)

    def touch_activity(self, profile_id: str) -> None:
        """Reset a running profile's idle clock. Safe to call for anything.

        Called from main.py's CDP proxy on every message forwarded in either
        direction (a real command or event, never just a protocol-level
        ping/pong — those are handled below the app layer and never reach
        here) and from _probe_viewer_activity below on a live VNC viewer.
        """
        running = self.running.get(profile_id)
        if running is not None:
            running.last_active = time.monotonic()

    async def _probe_viewer_activity(self, running: RunningProfile) -> None:
        """Touch activity for `running` if a VNC viewer is attached right now.

        Headless profiles and VNC traffic in general never reach this process
        — nginx proxies /viewer/ straight to Kasm's ws_port (see
        docker/nginx.conf) — so this is the only way the idle reaper can see
        a viewer at all. Reuses the exact probe /api/viewer-attached already
        uses, on the theory that a second definition of "attached" would
        eventually disagree with the first.
        """
        if running.display is None or running.ws_port is None:
            return
        try:
            auth = self.vnc.get_api_credentials(running.display)
            async with httpx.AsyncClient(
                timeout=KASM_VIEWER_PROBE_TIMEOUT_S, auth=auth,
            ) as client:
                resp = await client.get(
                    f"http://127.0.0.1:{running.ws_port}/api/get_bottleneck_stats"
                )
            if resp.status_code >= 400:
                return
            bottleneck = resp.json()
        except Exception as exc:
            logger.debug(
                "Idle sweep: viewer probe unavailable for %s: %s", running.profile_id, exc,
            )
            return
        if _viewer_attached(bottleneck):
            self.touch_activity(running.profile_id)

    async def reap_idle_profiles(self) -> None:
        """Stop profiles idle past their timeout, releasing their license claim.

        "Idle" is the absence of BOTH signals this manager can see: a VNC
        viewer attached (probed fresh every pass, since that traffic never
        reaches this process) and CDP traffic (touched directly by the proxy
        as it happens). Neither implies the other — a profile driven purely
        by Playwright has no viewer, and one only ever watched over VNC has no
        CDP client — so a profile is idle only when both have been silent.
        """
        for profile_id, running in list(self.running.items()):
            if running.idle_timeout_seconds <= 0:
                continue  # 0 disables idle timeout for this profile
            try:
                await self._probe_viewer_activity(running)
            except Exception as exc:
                logger.warning("Idle sweep: viewer probe failed for %s: %s", profile_id, exc)
            if self.running.get(profile_id) is not running:
                continue  # stopped/relaunched by something else mid-probe
            idle_for = time.monotonic() - running.last_active
            if idle_for < running.idle_timeout_seconds:
                continue
            logger.info(
                "Profile %s idle for %.0fs (timeout %ds) — stopping to release "
                "its license claim", profile_id, idle_for, running.idle_timeout_seconds,
            )
            try:
                await self.stop(profile_id)
            except Exception as exc:
                logger.warning("Idle sweep: stop failed for %s: %s", profile_id, exc)

    async def run_maintenance(self) -> None:
        """Background loop: resolve teardown claims, reap dead/idle browsers."""
        while True:
            await asyncio.sleep(CLAIM_SWEEP_INTERVAL_S)
            try:
                await self.sweep_teardown_claims()
                await self.reap_dead_browsers()
                await self.reap_idle_profiles()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Maintenance pass failed: %s", exc)

    def is_starting(self, profile_id: str) -> bool:
        """True while a launch is in flight or queued behind auto-launch."""
        return profile_id in self._launching or profile_id in self._pending_auto_launch

    def get_status(self, profile_id: str) -> dict[str, Any]:
        """Cheap lifecycle status: running | starting | stopping | stopped.

        Deliberately does no process probing and no mutation — the profile list
        polls this for every profile every 3 seconds and discards liveness, and
        it reaches here on an executor thread via get_liveness_async(). Use
        get_liveness() where liveness matters and check_wedged() where a
        decision is made.
        """
        running = self.running.get(profile_id)
        if running:
            return {
                "status": "running",
                # Both stay null for a headless profile: it has no X server
                # and no viewer, and reporting a display it does not own is
                # what made the UI offer a Connect affordance onto nothing.
                "vnc_ws_port": running.ws_port,
                "display": f":{running.display}" if running.display is not None else None,
                "cdp_url": f"/api/profiles/{profile_id}/cdp",
            }
        # "starting" is distinct from "stopped" on purpose: an open viewer
        # treats "stopped" as terminal, so reporting it during a container
        # restart would kill a session that is about to come back.
        if self.is_starting(profile_id):
            # MUST be tested first: a launch holds a teardown claim for its
            # whole duration, and auto_launch_all keeps the id in
            # _pending_auto_launch until after launch() raises, so both can be
            # true at once. main.py checks is_starting first on every mutation
            # path, and the status must agree with the refusal the user gets.
            status = "starting"
        elif self.peek_wedged(profile_id):
            # "stopped" here was a lie on EVERY ordinary stop, not just a
            # wedged one: stop() pops from `running` before awaiting a close
            # bounded at CONTEXT_CLOSE_TIMEOUT_S, so the UI offered Launch for
            # a browser that was still alive and the click answered 409.
            status = "stopping"
        else:
            status = "stopped"
        return {
            "status": status,
            "vnc_ws_port": None,
            "display": None,
            "cdp_url": None,
        }

    async def get_liveness_async(self, profile_id: str) -> dict[str, Any]:
        """get_liveness() with the blocking probes off the event loop.

        These are filesystem and socket probes, not awaits: _browser_alive()
        reads /proc (and still opens a TCP connection on its no-proc fallback),
        and the Xvnc check probes its own process. On loopback and procfs that
        is normally instantaneous, but "normally" is not "always" and this runs
        in a request handler on the single event loop that also serves nginx's
        viewer auth_request subrequests.

        Everything reachable from here must be read-only with respect to
        BrowserManager's dicts. The process probes it performs (Popen.poll,
        connect_ex) are the point of the endpoint and are internally
        thread-safe; mutating `running`/`_launching`/`_closing` from this
        thread is what is forbidden.
        """
        return await asyncio.get_running_loop().run_in_executor(
            None, self.get_liveness, profile_id,
        )

    def get_liveness(self, profile_id: str) -> dict[str, Any]:
        """Lifecycle status plus real Xvnc/browser liveness.

        Drives the viewer's reconnect classification, so both flags have to
        observe the actual processes rather than local bookkeeping.
        """
        status = self.get_status(profile_id)
        running = self.running.get(profile_id)
        return {
            **status,
            # None, not False, for a headless profile: there is no Xvnc to be
            # alive or dead, and False reads as "the display server died",
            # which the viewer classifies as a terminal xvnc-dead session.
            "xvnc_alive": (
                self.vnc.is_alive(running.display)
                if running is not None and running.display is not None else None
            ),
            "browser_alive": self._browser_alive(running) if running else None,
        }

    @staticmethod
    def _browser_alive(running: RunningProfile) -> bool:
        """Whether THIS Chromium — not merely something on its port — is up.

        Contract: this distinguishes DEAD from NOT-DEAD, not healthy from
        hung. A SIGSTOPped Chromium is still a live process, so it reports
        alive — verified.

        Identity, not a port probe. A CDP port is an ordinary ephemeral port:
        once our Chromium dies the kernel is free to hand it to anything, and
        the manager itself rotates these ports across relaunches, so "something
        is listening there" and "our browser is running" are different claims.
        Answering the first when asked the second is a false ALIVE, which is
        the harmful direction — the viewer never classifies browser-dead and
        instead burns its whole MAX_ALIVE_RECONNECTS budget minting tokens
        against a browser that no longer exists.

        (pid, starttime) cannot be recycled that way, and process_is_alive
        counts a zombie as dead — which matters here because PID 1 in this
        container is the entrypoint shell and does not reap adopted children,
        so a killed Chromium can sit in state 'Z' indefinitely.

        `context.pages` is not usable for a different reason than the obvious
        one: Playwright implements it as `self._pages.copy()`, a local list
        only updated when the DRIVER delivers a page-close event. It empties
        correctly on a real crash, but lies exactly when the driver is
        unreachable — which is when liveness matters.
        """
        if running.proc is None:
            # Unreachable for a registered profile: launch fails closed when
            # discovery returns None, which is what lets the teardown guard
            # release on evidence. Kept because the field is Optional and a
            # port probe is a better answer than asserting.
            return _port_is_listening(running.cdp_port)
        return process_is_alive(running.proc)

    async def cleanup_all(self):
        """Stop all running profiles. Called on shutdown.

        Concurrently: profiles are independent, and the entrypoint waits for
        this before tearing down nginx, inside Docker's stop grace period.
        Sequentially, shutdown cost is the SUM of every context close plus Xvnc
        teardown, so a handful of profiles is enough to get the container
        SIGKILLed mid-cleanup — killing uncleanly the very browsers the ordered
        shutdown exists to protect.
        """
        async with self._lock:
            profile_ids = list(self.running.keys())

        results = await asyncio.gather(
            *(self.stop(pid) for pid in profile_ids), return_exceptions=True,
        )
        for pid, result in zip(profile_ids, results):
            if isinstance(result, BaseException):
                logger.warning("Error stopping profile %s during shutdown: %s", pid, result)

        await self.vnc.cleanup_all()

    async def cleanup_stale(self):
        """Kill orphan processes from previous container runs."""
        await self.vnc.cleanup_stale()

    async def auto_launch_all(self):
        """Launch all profiles with auto_launch=True. Called on startup."""
        from . import database as db

        profiles = db.list_profiles()
        auto_profiles = [p for p in profiles if p.get("auto_launch")]
        if not auto_profiles:
            logger.info("No profiles configured for auto-launch")
            return

        # Claim the whole queue up front so profiles waiting their turn report
        # "starting" rather than "stopped" — launches are sequential and the
        # last one can be minutes away.
        self._pending_auto_launch = {p["id"] for p in auto_profiles}

        logger.info("Auto-launching %d profile(s)...", len(auto_profiles))
        try:
            for profile in auto_profiles:
                try:
                    await asyncio.wait_for(self.launch(profile), timeout=LAUNCH_TIMEOUT_S)
                    logger.info("Auto-launched profile %s (%s)", profile["name"], profile["id"])
                except Exception as exc:
                    logger.error(
                        "Auto-launch failed for profile %s (%s): %s",
                        profile["name"], profile["id"], exc,
                    )
                finally:
                    self._pending_auto_launch.discard(profile["id"])
        finally:
            # Cancellation (shutdown) must not leave profiles stuck "starting".
            self._pending_auto_launch.clear()
        logger.info("Auto-launch complete: %d running", len(self.running))

    def _allocate_cdp_port(self) -> int:
        """Find a free CDP port using a rotating counter to avoid TIME_WAIT collisions."""
        for _ in range(CDP_PORT_RANGE):
            port = self._next_cdp_port
            self._next_cdp_port = BASE_CDP_PORT + (
                (self._next_cdp_port + 1 - BASE_CDP_PORT) % CDP_PORT_RANGE
            )
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind(("127.0.0.1", port))
                    return port
                except OSError:
                    continue
        raise ValueError("No free CDP ports available in range %d-%d" % (BASE_CDP_PORT, BASE_CDP_PORT + CDP_PORT_RANGE - 1))

    def _build_fingerprint_args(self, profile: dict[str, Any]) -> list[str]:
        """Build extra Chromium args from profile fingerprint settings."""
        args: list[str] = [
            "--disable-infobars",
            "--test-type",  # suppress "unsupported flag: --no-sandbox" bad flags warning
        ]
        # SwiftShader unless a GPU was actually passed into the container —
        # see _chrome_gpu_flags. These come first so a profile's own
        # launch_args can still override them (the launcher deduplicates by
        # flag key, last writer wins).
        args += _chrome_gpu_flags()

        seed = profile.get("fingerprint_seed")
        if seed is not None:
            args.append(f"--fingerprint={seed}")

        p = profile.get("platform")
        if p:
            # Map our "macos" to binary's "macos"
            args.append(f"--fingerprint-platform={p}")

        vendor = profile.get("gpu_vendor")
        if vendor:
            args.append(f"--fingerprint-gpu-vendor={vendor}")

        renderer = profile.get("gpu_renderer")
        if renderer:
            args.append(f"--fingerprint-gpu-renderer={renderer}")

        hw = profile.get("hardware_concurrency")
        if hw is not None:
            args.append(f"--fingerprint-hardware-concurrency={hw}")

        sw = profile.get("screen_width")
        sh = profile.get("screen_height")
        if sw:
            args.append(f"--fingerprint-screen-width={sw}")
        if sh:
            args.append(f"--fingerprint-screen-height={sh}")

        return args
