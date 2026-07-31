"""KasmVNC display allocation and lifecycle management."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field

logger = logging.getLogger("cloakbrowser.manager.vnc")

# Server-authoritative quality presets (Xvnc CLI flags; spellings per
# Xkasmvnc(1) 1.5.0). Client-side quality settings are ignored via
# -IgnoreClientSettingsKasm, so the active preset is the whole policy.
# -videoCodec is set explicitly in start_vnc: the binary default is empty
# (streaming disabled) despite the man page claiming "auto".
QUALITY_PRESETS: dict[str, list[str]] = {
    # Crisp text/UI priority: high quality, reluctant to enter video mode.
    "text": [
        "-FrameRate", "30",
        "-DynamicQualityMin", "7", "-DynamicQualityMax", "9", "-TreatLossless", "9",
        "-JpegVideoQuality", "5", "-WebpVideoQuality", "5",
        "-MaxVideoResolution", "1920x1080",
        "-VideoTime", "2", "-VideoArea", "45", "-VideoOutTime", "1",
        "-VideoScaling", "2",  # progressive bilinear
        "-webpEncodingTime", "30",
        "-CompareFB", "2",  # auto
        "-RectThreads", "2",
    ],
    # Default: good text quality, quick switch to video mode on motion.
    "balanced": [
        "-FrameRate", "30",
        "-DynamicQualityMin", "6", "-DynamicQualityMax", "8", "-TreatLossless", "8",
        "-JpegVideoQuality", "5", "-WebpVideoQuality", "5",
        "-MaxVideoResolution", "1600x900",
        "-VideoTime", "1", "-VideoArea", "30", "-VideoOutTime", "1",
        "-VideoScaling", "2",
        "-webpEncodingTime", "30",
        "-CompareFB", "2",
        "-RectThreads", "2",
    ],
    # Bandwidth-saver: lower framerate/quality, capped video resolution.
    "low": [
        "-FrameRate", "24",
        "-DynamicQualityMin", "4", "-DynamicQualityMax", "7", "-TreatLossless", "8",
        "-JpegVideoQuality", "4", "-WebpVideoQuality", "4",
        "-MaxVideoResolution", "1280x720",
        "-VideoTime", "1", "-VideoArea", "20", "-VideoOutTime", "1",
        "-VideoScaling", "2",
        "-webpEncodingTime", "30",
        "-CompareFB", "2",
        "-RectThreads", "2",
    ],
    # Motion content (video/gaming): quick video-mode entry, fluid over sharp.
    "motion": [
        "-FrameRate", "30",
        "-DynamicQualityMin", "5", "-DynamicQualityMax", "8", "-TreatLossless", "8",
        "-JpegVideoQuality", "4", "-WebpVideoQuality", "5",
        "-MaxVideoResolution", "1280x720",
        "-VideoTime", "1", "-VideoArea", "20", "-VideoOutTime", "1",
        "-VideoScaling", "2",
        "-webpEncodingTime", "30",
        "-CompareFB", "2",
        "-RectThreads", "2",
    ],
}


def _quality_preset_name() -> str:
    """Active preset from KASM_QUALITY_PRESET (default 'balanced')."""
    name = os.environ.get("KASM_QUALITY_PRESET", "balanced").strip().lower()
    if name not in QUALITY_PRESETS:
        logger.warning("Unknown KASM_QUALITY_PRESET=%r, falling back to 'balanced'", name)
        return "balanced"
    return name


def _quality_flags(preset: str) -> list[str]:
    flags = list(QUALITY_PRESETS[preset])
    raw = os.environ.get("KASM_RECT_THREADS")
    if raw is not None:
        idx = flags.index("-RectThreads") + 1
        flags[idx] = _rect_threads_value(raw, flags[idx])
    return flags


def _rect_threads_value(raw: str, default: str) -> str:
    """Validate a KASM_RECT_THREADS override (0 = auto, see warning)."""
    try:
        threads = int(raw)
    except ValueError:
        logger.warning("Invalid KASM_RECT_THREADS=%r, using preset default %s", raw, default)
        return default
    if threads < 0:
        logger.warning("Invalid KASM_RECT_THREADS=%r, using preset default %s", raw, default)
        return default
    if threads == 0:
        logger.warning(
            "KASM_RECT_THREADS=0: unbounded automatic encoder threads — risky multi-tenant"
        )
    return str(threads)


def _dri_driver(node: str) -> str | None:
    """Driver name for a DRI render node, or None if unresolvable in-container."""
    try:
        return os.path.basename(os.readlink(f"/sys/class/drm/{os.path.basename(node)}/device/driver"))
    except OSError:
        return None


def _hw3d_flags() -> list[str]:
    """DRI3 hardware 3D flags per KASM_HW3D (auto/1/true/yes/0) + KASM_DRINODE."""
    raw = os.environ.get("KASM_HW3D", "auto").strip().lower()
    if raw in ("0", "false", "no"):
        logger.info("KasmVNC hw3d disabled (KASM_HW3D=%s)", raw)
        return []
    if raw in ("1", "true", "yes"):
        mode = "force"
    elif raw == "auto":
        mode = "auto"
    else:
        # Anything unrecognised used to fall through to the force branch, so a
        # typo silently bypassed the NVIDIA check and passed -hw3d to Xvnc on a
        # driver without DRI3.
        logger.warning("Unknown KASM_HW3D=%r, falling back to 'auto'", raw)
        mode = "auto"
    node = os.environ.get("KASM_DRINODE", "/dev/dri/renderD128")
    if not os.path.exists(node):
        logger.info("KasmVNC hw3d disabled: %s not present", node)
        return []
    if mode == "auto":
        # Closed-source NVIDIA lacks DRI3; unresolvable driver counts as OK.
        driver = _dri_driver(node)
        if driver == "nvidia":
            logger.info("KasmVNC hw3d disabled: %s uses the nvidia driver (no DRI3)", node)
            return []
        logger.info("KasmVNC hw3d enabled on %s (auto, driver=%s)", node, driver or "unknown")
    else:
        logger.info("KasmVNC hw3d enabled on %s (KASM_HW3D=%s)", node, raw)
    return ["-hw3d", "-drinode", node]


# Xvnc binds its websocket port within milliseconds; the ceiling only covers
# a heavily loaded host.
XVNC_READY_TIMEOUT_S = 15.0
_XVNC_POLL_INTERVAL_S = 0.05


async def _wait_until_listening(
    port: int, process: subprocess.Popen, timeout: float,
) -> bool:
    """Poll 127.0.0.1:port until Xvnc accepts, it dies, or we run out of time."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False  # exited during startup
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.25)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return True
        await asyncio.sleep(_XVNC_POLL_INTERVAL_S)
    return False


@dataclass
class VNCInstance:
    display: int
    ws_port: int
    process: subprocess.Popen | None = None
    api_password: str | None = None


# KasmVNC's HTTP layer (static client, WebSocket upgrade, management /api)
# requires Basic-auth credentials from the -KasmPasswordFile file. We generate
# a random per-display password at launch; nginx injects it for viewer traffic
# and the Manager uses it for the stats proxy. It never leaves the server.
KASM_API_USER = "manager"


async def _write_kasm_passwd(display: int, password: str) -> str:
    """Create /tmp/kasmpasswd-<display> with an owner user, or raise.

    These credentials are not optional. Kasm's HTTP layer requires Basic auth
    for the static client, the WebSocket upgrade and the management API alike,
    and there is no -DisableBasicAuth in the launch flags — so starting Xvnc
    without a password file yields a profile that reports itself perfectly
    healthy while every viewer request 401s, which the reconnect machine can
    only loop against. Failing the launch is the honest outcome.
    """
    path = f"/tmp/kasmpasswd-{display}"
    passwd_bin = shutil.which("kasmvncpasswd")
    if not passwd_bin:
        raise RuntimeError("kasmvncpasswd not found; cannot create KasmVNC credentials")
    # /tmp survives `docker restart`, so a file from a previous run can still be
    # here for this display. Remove it first: otherwise a silently-failing
    # kasmvncpasswd leaves the stale file behind, the non-empty check below
    # passes, and Xvnc starts with credentials that no longer match the password
    # we generated — the permanently-401 viewer this function exists to prevent.
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    proc = await asyncio.create_subprocess_exec(
        passwd_bin, "-u", KASM_API_USER, "-wro", path,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate(f"{password}\n{password}\n".encode())
    if proc.returncode != 0:
        raise RuntimeError(
            f"kasmvncpasswd failed: {stderr.decode(errors='replace').strip()}"
        )
    # It can exit 0 after a failed write/rename (e.g. /tmp full or read-only),
    # so verify the artefact rather than trusting the status.
    try:
        if os.path.getsize(path) == 0:
            raise RuntimeError(f"kasmvncpasswd wrote an empty {path}")
    except OSError as exc:
        raise RuntimeError(f"kasmvncpasswd did not create {path}: {exc}") from exc
    return path


class VNCManager:
    BASE_DISPLAY = 100
    BASE_WS_PORT = 6100

    def __init__(self):
        self._allocated: dict[int, VNCInstance] = {}
        self._lock = asyncio.Lock()

    async def allocate(self) -> tuple[int, int]:
        """Returns (display_number, ws_port) for a new profile."""
        async with self._lock:
            display = self.BASE_DISPLAY
            while display in self._allocated:
                display += 1
            ws_port = self.BASE_WS_PORT + (display - self.BASE_DISPLAY)
            self._allocated[display] = VNCInstance(display=display, ws_port=ws_port)
            return display, ws_port

    async def start_vnc(
        self,
        display: int,
        ws_port: int,
        width: int = 1920,
        height: int = 1080,
    ) -> subprocess.Popen:
        """Start Xvnc (KasmVNC) on the given display."""
        xvnc_bin = shutil.which("Xvnc") or "Xvnc"

        # KasmVNC requires -httpd to enable the WebSocket handler on the websocket port.
        # Without it, the port accepts TCP but won't do WebSocket upgrade.
        httpd_dir = "/usr/share/kasmvnc/www"

        cmd = [
            xvnc_bin,
            f":{display}",
            "-websocketPort", str(ws_port),
            "-rfbport", "-1",  # disable raw VNC TCP port — WebSocket only
            "-geometry", f"{width}x{height}",
            "-depth", "24",
            "-SecurityTypes", "None",  # no RFB-layer auth; HTTP Basic guards the port
            "-interface", "127.0.0.1",  # internal only, proxied by nginx
            "-AlwaysShared",
            "-httpd", httpd_dir,
        ]

        # Server-authoritative performance policy (KASM_QUALITY_PRESET);
        # clients cannot override quality/encoding settings.
        preset = _quality_preset_name()
        cmd += ["-IgnoreClientSettingsKasm"] + _quality_flags(preset)

        # Never query STUN for a public IP: UDP/WebRTC transit is out of
        # scope (Cloudflare Tunnel carries WSS only), and the lookup leaks.
        cmd += ["-PublicIP", "127.0.0.1"]

        # In-band H.264/H.265/AV1 streaming (WebCodecs over the same
        # WebSocket). The raw binary's default is EMPTY (= disabled) despite
        # the man page claiming "auto" — set it explicitly. "auto" prefers
        # VAAPI (when -hw3d's GPU is usable) and falls back to software
        # encoders via dlopen'd FFmpeg; without FFmpeg it silently stays on
        # JPEG/WebP rects.
        cmd += ["-videoCodec", "auto"]

        # Owner credentials guard Kasm's HTTP layer (static client, WS
        # upgrade, management /api). nginx injects them on behalf of
        # token-authorized viewer requests, so the browser never sees them
        # (and the Manager's stats proxy uses them directly).
        api_password = secrets.token_hex(16)
        cmd += ["-KasmPasswordFile", await _write_kasm_passwd(display, api_password)]

        # DRI3 GPU acceleration (KASM_HW3D / KASM_DRINODE)
        cmd += _hw3d_flags()

        log_path = f"/tmp/xvnc-{display}.log"
        logger.info(
            "Starting Xvnc on :%d (ws_port=%d, preset=%s) log=%s",
            display, ws_port, preset, log_path,
        )

        log_file = open(log_path, "w")
        proc = subprocess.Popen(
            cmd,
            stdout=log_file,
            stderr=log_file,
        )
        log_file.close()  # Popen inherited the fd, parent doesn't need it

        # Register immediately, not after the readiness check: otherwise a
        # startup failure leaves stop_vnc() with instance.process is None, so it
        # skips _terminate() entirely and pops the allocation with no wait() —
        # bypassing the very guarantee stop_vnc exists to provide. Every
        # teardown path should go through the same reaping code.
        async with self._lock:
            if display in self._allocated:
                self._allocated[display].process = proc
                self._allocated[display].api_password = api_password

        # Wait for the websocket port to actually accept, not a fixed sleep.
        # A sleep that is merely usually long enough lets launch() proceed
        # against an Xvnc that is not listening (or already dead), and the
        # viewer then loops reconnecting against a port nobody answers.
        if not await _wait_until_listening(ws_port, proc, XVNC_READY_TIMEOUT_S):
            try:
                with open(log_path) as f:
                    err = f.read()
            except Exception as exc:
                logger.debug("Failed to read Xvnc log %s: %s", log_path, exc)
                err = ""
            # Reap here too: a SIGKILLed X server does not remove
            # /tmp/.X<display>-lock, so leaving it unreaped can make the next
            # allocation of this display fail with "Server is already active".
            await self._terminate(proc, display)
            raise RuntimeError(f"Xvnc failed to start on :{display}: {err}")

        return proc

    async def stop_vnc(self, display: int):
        """Kill Xvnc for the given display, then release the allocation.

        Ordering matters. Releasing first lets a concurrent allocate() gap-fill
        the same display/ws_port while the old Xvnc is still holding the port —
        the new Xvnc then fails to bind and POST /launch answers 500 — and lets
        this call's password-file unlink delete the *new* instance's file. A
        routine stop->relaunch is enough to hit it.

        Never raises: cleanup_all() iterates over displays, and one bad process
        handle must not strand the rest.
        """
        async with self._lock:
            instance = self._allocated.get(display)

        reaped = True
        if instance and instance.process:
            logger.info("Stopping Xvnc on :%d", display)
            try:
                reaped = await self._terminate(instance.process, display)
            except Exception as exc:
                logger.warning("Error stopping Xvnc on :%d: %s", display, exc)
                # A handle that raises (ProcessLookupError and friends) usually
                # means the process is already gone; trust poll() over the
                # exception rather than leaking the display on a stale handle.
                reaped = instance.process.poll() is not None

        if not reaped:
            # The process outlived SIGKILL, so it may still hold the X display
            # and the websocket port. Releasing the allocation would hand both
            # to the next launch, which would then fail to bind. Leaking one
            # display is the lesser failure — and allocate() skips it.
            logger.error(
                "Xvnc on :%d could not be reaped; leaving the display allocated", display,
            )
            return

        async with self._lock:
            self._allocated.pop(display, None)

        # Remove the per-display API password file
        try:
            os.unlink(f"/tmp/kasmpasswd-{display}")
        except OSError:
            pass

    @staticmethod
    async def _terminate(process: subprocess.Popen, display: int) -> bool:
        """SIGTERM, then SIGKILL if it outlives the grace period.

        Returns whether the process was actually reaped. Both paths wait():
        reaping here is what makes "the port is free" true by the time the
        allocation is released, so a False result must block that release.
        """
        loop = asyncio.get_running_loop()
        process.terminate()
        try:
            await loop.run_in_executor(None, process.wait, 5)
            return True
        except subprocess.TimeoutExpired:
            logger.warning("Xvnc on :%d ignored SIGTERM; killing", display)
        process.kill()
        try:
            await loop.run_in_executor(None, process.wait, 5)
            return True
        except subprocess.TimeoutExpired:
            logger.error("Xvnc on :%d survived SIGKILL", display)
            return False

    def is_alive(self, display: int) -> bool:
        """Whether this display's Xvnc process is still running."""
        instance = self._allocated.get(display)
        return bool(instance and instance.process and instance.process.poll() is None)

    def get_api_credentials(self, display: int) -> tuple[str, str] | None:
        """Basic-auth credentials for Kasm's management API on this display."""
        instance = self._allocated.get(display)
        if instance and instance.api_password:
            return (KASM_API_USER, instance.api_password)
        return None

    async def cleanup_all(self):
        """Kill all managed Xvnc processes. Called on shutdown."""
        async with self._lock:
            displays = list(self._allocated.keys())

        # Concurrently, for the same reason as BrowserManager.cleanup_all():
        # each display's SIGTERM grace would otherwise be additive.
        await asyncio.gather(*(self.stop_vnc(d) for d in displays))

    async def cleanup_stale(self):
        """Kill orphan Xvnc processes from previous runs."""
        try:
            result = subprocess.run(
                ["pkill", "-f", r"Xvnc :[0-9]"],
                capture_output=True,
            )
            if result.returncode == 0:
                logger.info("Cleaned up stale Xvnc processes")
        except FileNotFoundError:
            logger.debug("pkill not found, skipping stale Xvnc cleanup")

    def get_ws_port(self, display: int) -> int | None:
        """Get WebSocket port for a display."""
        instance = self._allocated.get(display)
        return instance.ws_port if instance else None

    @property
    def active_displays(self) -> list[int]:
        return list(self._allocated.keys())
