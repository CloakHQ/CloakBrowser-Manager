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

# Quality presets (Xvnc CLI flags; every spelling and range checked against
# `Xvnc -help` and common/rfb/ServerCore.cxx for KasmVNC 1.5.0). Whether these
# or the client's own Kasm settings win is decided by KASM_ENCODING_POLICY.
#
# -RectThreads is deliberately absent. It still parses in 1.5.0, but nothing
# reads rfb::Server::rectThreads any more: the OpenMP loop it drove in 1.3.3
# (EncodeManager.cxx:1133) was replaced by an unconditional oneTBB arena sized
# to cpu_info::cores_count (EncodeManager.cxx:233), per EncodeManager and so
# per connected client. A grep of the 1.5.0 tree finds the parameter only in
# its own declaration/definition, the perl vncserver wrapper and the man page.
# Passing it caps nothing, so we do not pretend otherwise — the only remaining
# lever on encoder threads is process-level CPU limiting (cgroup cpuset /
# `--cpus`), not a VNC parameter.
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
    ],
}


def _quality_preset_name() -> str:
    """Active preset from KASM_QUALITY_PRESET (default 'balanced')."""
    name = os.environ.get("KASM_QUALITY_PRESET", "balanced").strip().lower()
    if name not in QUALITY_PRESETS:
        logger.warning("Unknown KASM_QUALITY_PRESET=%r, falling back to 'balanced'", name)
        return "balanced"
    return name


def _xvnc_log_level() -> int:
    """Xvnc -Log verbosity from KASM_XVNC_LOG_LEVEL (0-100, default 30)."""
    raw = os.environ.get("KASM_XVNC_LOG_LEVEL")
    if raw is None:
        return 30
    try:
        level = int(raw)
    except ValueError:
        logger.warning("Invalid KASM_XVNC_LOG_LEVEL=%r, using 30", raw)
        return 30
    if not 0 <= level <= 100:
        logger.warning("KASM_XVNC_LOG_LEVEL=%r out of range 0-100, using 30", raw)
        return 30
    return level


def _quality_flags(preset: str, drop: tuple[str, ...] = ()) -> list[str]:
    """Preset flags, plus a warning for the KASM_RECT_THREADS knob we removed.

    Operators who set it on an older image would otherwise turn the dial on a
    loaded multi-tenant host and observe nothing change, with no clue why.

    `drop` removes flag/value pairs the active encoding policy makes inert.
    Emitting a flag the server will ignore is how KASM_QUALITY_PRESET=low ends
    up encoding full 1080p anyway: the preset looks applied and is not.
    """
    if os.environ.get("KASM_RECT_THREADS") is not None:
        logger.warning(
            "KASM_RECT_THREADS is ignored: -RectThreads is a dead parameter in "
            "KasmVNC 1.5.0 (oneTBB sizes the encoder arena to the core count). "
            "Cap encoder CPU with a cgroup/--cpus limit instead."
        )
    flags = list(QUALITY_PRESETS[preset])
    if not drop:
        return flags
    kept: list[str] = []
    skip_value = False
    for flag in flags:
        if skip_value:
            skip_value = False
            continue
        if flag in drop:
            skip_value = True
            continue
        kept.append(flag)
    logger.warning(
        "Quality preset %r: %s dropped — inert under the active encoding policy, "
        "so passing them would misreport the effective limits.",
        preset, " / ".join(drop),
    )
    return kept


# -IgnoreClientSettingsKasm and -videoCodec are MUTUALLY EXCLUSIVE in KasmVNC
# 1.5.0, which neither flag's documentation says. ConnParams.cxx:164-165 derives
# `can_apply = !ignoreClientSettingsKasm && canChangeKasmSettings()`, and the
# only writer of cp.encoder_config is inside `if (can_apply)`
# (ConnParams.cxx:374-386); EncodeManager.cxx:444 gates video mode on that field
# being something other than `unavailable`. So with -IgnoreClientSettingsKasm
# every streaming-mode pseudo-encoding the client offers is dropped ("CP: Client
# sent config param Encoder -1027, ignored due to -IgnoreClientSettingsKasm"),
# video mode is false on every frame, and -videoCodec cannot do anything at all
# — the WebCodecs H.264/H.265/AV1 path is dead however capable FFmpeg or the GPU
# is. Verified live against the shipped 1.5.0 Xvnc: the same run logs "applied"
# for those pseudo-encodings once the flag is dropped.
#
# There is no third option upstream — one boolean gates both — so the choice is
# a policy knob, and both flags are emitted from _encoding_flags() alone.
# Nothing else in this module (and nothing in QUALITY_PRESETS) may add either,
# which is what makes the exclusion structurally unbreakable.
ENCODING_POLICY_SERVER = "server-authoritative"
ENCODING_POLICY_VIDEO = "video"
ENCODING_POLICIES = (ENCODING_POLICY_SERVER, ENCODING_POLICY_VIDEO)

# The binary's default is the EMPTY string (= video mode off,
# EncodeManager.cxx:199) despite the man page claiming "auto", so a codec has to
# be passed explicitly to mean anything.
#
# NOT "auto". "auto" hands the CHOICE to the client: it widens the probed set to
# every encoder the build advertises, and the client then picks by offering a
# streaming-mode pseudo-encoding. Measured live on this image (no GPU, so the
# software tier is all that is reachable): a client advertising -1037 selected
# libsvtav1, which spent 364 ms encoding a single 1080p keyframe and then errored
# out, after which the session silently fell back to Tight for its entire
# lifetime — worse than never enabling video mode, and invisible in the default
# logs. The upstream claim that software AV1 is excluded does not hold for a
# client-driven selection.
#
# So the server names the encoder it wants. IMPORTANT, measured: this narrows
# the server's own probe ("Using CLI-specified video codecs (supported subset):
# libx264") but it does NOT restrict what a client may select — ConnParams.cxx
# still honours a streaming-mode pseudo-encoding outside the list, so a client
# offering -1037 gets software AV1 anyway. KasmVNC 1.5.0 exposes no enforcement
# point for this. h264 is therefore the best DEFAULT, not a guarantee, and it is
# the reason the `video` policy is opt-in and the default policy is not.
_VIDEO_CODEC_DEFAULT = "h264"
# Xkasmvnc(1) 1.5.0: "Supported options: auto, h264, h264_vaapi, h265,
# h265_vaapi, av1, av1_vaapi". "auto" is deliberately NOT in this set — see above.
_VIDEO_CODECS_ALLOWED = frozenset({
    "h264", "h264_vaapi", "h265", "h265_vaapi", "av1", "av1_vaapi",
})


def _video_codec() -> str:
    """Encoder(s) offered under the `video` policy, from KASM_VIDEO_CODEC."""
    raw = os.environ.get("KASM_VIDEO_CODEC")
    if raw is None:
        return _VIDEO_CODEC_DEFAULT
    codec = raw.strip().lower()
    if codec == "auto":
        logger.warning(
            "KASM_VIDEO_CODEC=auto is refused: it lets the client select a "
            "software AV1 encoder that stalls a core for ~364ms per keyframe and "
            "then fails the session over to Tight. Using %r instead.",
            _VIDEO_CODEC_DEFAULT,
        )
        return _VIDEO_CODEC_DEFAULT
    if codec not in _VIDEO_CODECS_ALLOWED:
        logger.warning(
            "Unknown KASM_VIDEO_CODEC=%r (accepted: %s), using %r",
            raw, ", ".join(sorted(_VIDEO_CODECS_ALLOWED)), _VIDEO_CODEC_DEFAULT,
        )
        return _VIDEO_CODEC_DEFAULT
    return codec


# The one flag the `video` policy makes inert, and must therefore not pretend
# to set. In video mode EncodeManager.cxx:1242 takes the client's resolution
# rather than -MaxVideoResolution, so emitting it is how KASM_QUALITY_PRESET=low
# silently still encodes full 1080p.
#
# -TreatLossless is deliberately NOT in this list. It looked inert, but it is
# not: it governs the Tight path (EncodeManager.cxx:742), which is still the
# path taken for every frame in which the client has not selected a video
# encoder — i.e. most of them. Dropping it silently reverted the preset value
# to the binary default of 10 (off).
_INERT_UNDER_VIDEO_POLICY = ("-MaxVideoResolution",)


def _encoding_policy_name() -> str:
    """Active policy from KASM_ENCODING_POLICY (default 'server-authoritative')."""
    name = os.environ.get("KASM_ENCODING_POLICY", ENCODING_POLICY_SERVER).strip().lower()
    if name not in ENCODING_POLICIES:
        logger.warning(
            "Unknown KASM_ENCODING_POLICY=%r, falling back to %r",
            name, ENCODING_POLICY_SERVER,
        )
        return ENCODING_POLICY_SERVER
    return name


def _encoding_flags(policy: str) -> list[str]:
    """The one place either -IgnoreClientSettingsKasm or -videoCodec is emitted.

    Returning them from a single function is the enforcement mechanism for the
    mutual exclusion documented above: there is no code path that can produce
    both, so the dead-`-videoCodec` combination cannot be reintroduced by
    editing a preset or adding a flag next to an unrelated one.
    """
    if policy == ENCODING_POLICY_VIDEO:
        codec = _video_codec()
        logger.warning(
            "KasmVNC encoding policy 'video': WebCodecs streaming can negotiate "
            "with -videoCodec %s. Two limits you are opting into. (1) Without "
            "-IgnoreClientSettingsKasm the client's own Kasm settings "
            "(DynamicQuality*, VideoTime/VideoArea, framerate) override the "
            "quality preset, and %s is inert. (2) -videoCodec narrows the "
            "server's probe but does NOT restrict the client's choice — 1.5.0 "
            "has no enforcement point, so a client offering another "
            "streaming-mode pseudo-encoding still gets that encoder, including "
            "software AV1 (measured: ~0.4s of a core per 1080p keyframe, then a "
            "silent fallback to Tight for the session). This is why the default "
            "policy is 'server-authoritative'.",
            codec, " / ".join(_INERT_UNDER_VIDEO_POLICY),
        )
        return ["-videoCodec", codec]
    logger.info(
        "KasmVNC encoding policy 'server-authoritative': the quality preset is "
        "the whole policy and clients cannot override it. Trade-off: this also "
        "suppresses the client's codec selection, so in-band H.264/H.265/AV1 is "
        "unavailable and rects stay JPEG/WebP. Set KASM_ENCODING_POLICY=video "
        "to swap which half you get."
    )
    return ["-IgnoreClientSettingsKasm"]


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
        # Closed-source NVIDIA lacks DRI3 (kasmweb.com/kasmvnc/docs/latest/
        # gpu_acceleration.html: "nouveau2 drivers only"); unresolvable driver
        # counts as OK. Getting this wrong is fatal, not degrading:
        # xvnc_init_dri3() FatalError()s if open()/gbm_create_device()/
        # dri3_screen_init() fail (dri3.c:372-397), so Xvnc dies at startup,
        # _wait_until_listening times out and POST /launch 500s. That asymmetry
        # — silently slower vs. no profile at all — is why 'auto' is the default
        # rather than 'force'.
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
    and we do not pass -DisableBasicAuth — so starting Xvnc without a password
    file yields a profile that reports itself perfectly healthy while every
    viewer request 401s, which the reconnect machine can only loop against.
    Failing the launch is the honest outcome.

    -DisableBasicAuth is not the escape hatch it looks like. Kasm only assigns
    the internal `owner` flag inside the `if (!disablebasicauth)` branch
    (websocket.c:1917, 1970-1976) while /api dispatch is `if (owner) ... else
    401` (websocket.c:2024-2043), so the flag simultaneously opens the client to
    anyone who reaches the port AND hard-401s the management API even for the
    correct owner credentials — which would silently kill /kasm-stats.
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

        # -httpd serves the KasmVNC web client (index.html + assets) over the
        # same websocket port; the viewer iframe 404s without it. It does NOT
        # gate the WebSocket upgrade — websocket.c only consults httpdir in the
        # `parse_handshake` FAILED branch (websocket.c:2044-2045), and a live
        # run with no -httpd still answers 101 on /websockify. The path has to
        # be explicit because the binary's compiled default is
        # /usr/local/share/kasmvnc/www while the Debian package installs to
        # /usr/share/kasmvnc/www.
        httpd_dir = "/usr/share/kasmvnc/www"

        # No -rfbport: it would be a no-op anyway. vncExtInit.cc:281-296 only
        # reaches the raw-RFB listener in the `else` of `if (!noWebsocket)`, and
        # noWebsocket defaults to false, so KasmVNC 1.5.0 opens the websocket
        # listener and nothing else. Verified live — `-rfbport 5999` produces no
        # listener on 5999. The old `-rfbport -1` claimed to disable a port that
        # was never opened; if you ever need a raw RFB port you must pass
        # -noWebsocket, which breaks the viewer entirely.
        cmd = [
            xvnc_bin,
            f":{display}",
            "-websocketPort", str(ws_port),
            "-geometry", f"{width}x{height}",
            "-depth", "24",
            "-SecurityTypes", "None",  # no RFB-layer auth; HTTP Basic guards the port
            "-interface", "127.0.0.1",  # internal only, proxied by nginx
            "-AlwaysShared",
            "-httpd", httpd_dir,
        ]

        # Quality preset (KASM_QUALITY_PRESET) plus the one flag that decides
        # who owns encoding policy (KASM_ENCODING_POLICY) — see _encoding_flags.
        preset = _quality_preset_name()
        policy = _encoding_policy_name()
        cmd += _encoding_flags(policy) + _quality_flags(
            preset, drop=_INERT_UNDER_VIDEO_POLICY if policy == ENCODING_POLICY_VIDEO else (),
        )

        # Without this the applied/ignored codec decision and the encoder probe
        # results are DEBUG-only, so a session that fell back to Tight because
        # the chosen encoder failed to open looks identical in the logs to one
        # streaming correctly. Level 30 keeps the ordinary INFO lines;
        # KASM_XVNC_LOG_LEVEL=100 surfaces the per-connection encoder decisions.
        cmd += ["-Log", f"*:stdout:{_xvnc_log_level()}"]

        # Mandatory, not a privacy preference. getPublicIP() (iceip.cxx:157-190)
        # runs unconditionally at extension init, and with no -PublicIP it walks
        # seven hardcoded Google/VoIP STUN servers and then exit(1)s if none
        # answer. Verified: `--network none` without this flag exits rc=1, so
        # _wait_until_listening times out and POST /launch 500s. Supplying it
        # short-circuits the lookup entirely ("ICE: Using public IP ... from
        # args") — no egress, and Cloudflare Tunnel carries WSS only, so the
        # UDP/WebRTC listener Kasm still opens on the websocket port is unusable
        # for negotiation.
        cmd += ["-PublicIP", "127.0.0.1"]

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
            "Starting Xvnc on :%d (ws_port=%d, preset=%s, encoding_policy=%s) log=%s",
            display, ws_port, preset, policy, log_path,
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
