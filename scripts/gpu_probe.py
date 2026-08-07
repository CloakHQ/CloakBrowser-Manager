#!/usr/bin/env python3
"""
GPU acceleration contract probe — runs INSIDE the cloakbrowser-manager image.

Both halves of GPU acceleration fail SILENTLY, which is the only reason this
script exists:

  * KasmVNC hardware encode — if the driver's encode library is missing, or the
    codec name is one this GPU cannot encode, the probe simply drops the codec
    and Xvnc logs "Hardware video encoding acceleration capability: none" once,
    at INFO, from a different process. The session then encodes every frame on
    the CPU while looking completely healthy.
  * Chromium — ANGLE's response to a driver it cannot reach is to fall back to
    SwiftShader (or, on the EGL path, to let GLVND fall through to Mesa's
    llvmpipe), not to fail. A GPU can be attached, mapped and idle while every
    frame is rasterised on the CPU, and nothing in the manager's logs says so.

So neither "it started" nor "it did not crash" is evidence of acceleration.
This asserts the observable consequences instead: the encoder KasmVNC actually
selected, and the GL renderer string Chromium actually bound.

Two vendors, because the two stacks fail differently and are configured
differently — NVENC through an injected closed driver, VAAPI through Mesa
userspace baked into the image:

  # NVIDIA (docker-compose.nvidia.yml)
  docker run --rm --gpus all -v "$PWD":/repo:ro \
    --entrypoint python cloakbrowser-manager-manager:latest \
    /repo/scripts/gpu_probe.py --vendor nvidia

  # Intel / AMD integrated (docker-compose.igpu.yml)
  docker run --rm --device /dev/dri:/dev/dri -v "$PWD":/repo:ro \
    --entrypoint python cloakbrowser-manager-manager:latest \
    /repo/scripts/gpu_probe.py --vendor igpu

--angle-backend exists because the iGPU answer is per-host: gl-egl, vulkan and
gl all reach the GPU on some driver/Mesa combinations and land on llvmpipe on
others. Run the probe once per backend on a new host, then set the winner as
CHROME_ANGLE_BACKEND in the deployment.

Output is a JSON object of check -> {ok, detail}, framed by sentinel lines so a
caller can recover it from mixed stdout. Exit status is 0 only when every check
passed.
"""

import argparse
import asyncio
import ctypes.util
import json
import os
import re
import shutil
import subprocess
import sys
import time

JSON_BEGIN = "---GPUPROBE-JSON-BEGIN---"
JSON_END = "---GPUPROBE-JSON-END---"

DISPLAY_NUM = 121
WS_PORT = 6121
CDP_PORT = 9333
XVNC_LOG = f"/tmp/gpu-probe-xvnc-{DISPLAY_NUM}.log"
CHROME_LOG = f"/tmp/gpu-probe-chrome-{DISPLAY_NUM}.log"

# The manager's own modules live here in the image. Inserted at import time
# rather than inside main() because start_xvnc() also needs the manager's
# resolution now (see _hw3d_flags below) — a probe that restated the flags would
# keep passing after the manager stopped emitting them.
sys.path.insert(0, "/app")

VENDOR_NVIDIA = "nvidia"
VENDOR_IGPU = "igpu"

# Per-vendor expectations. Keyed on the vendor rather than branched inline so
# that adding one is a table entry and the checks below stay single-path.
#
#   capability_marker  what has to appear in Xvnc's "Hardware video encoding
#                      acceleration capability:" line. This is the encoder that
#                      was actually opened, not the one that was requested.
#   renderer_markers   substrings that identify the GPU in the renderer string
#                      the GPU process reports. Mesa spells the vendor out
#                      ("ANGLE (AMD, AMD Radeon Graphics (radeonsi, ...))",
#                      "ANGLE (Intel, Mesa Intel(R) Graphics ...)").
#   libraries          userspace that must be loadable, and what breaks without
#                      it. NVIDIA's is injected by the container runtime; the
#                      iGPU's is installed by Dockerfile.igpu.
VENDORS = {
    VENDOR_NVIDIA: {
        "codec": "h264_nvenc",
        "capability_marker": "nvenc",
        "renderer_markers": ("nvidia",),
        "libraries": {
            "nvidia-encode": "NVENC encode (KasmVNC h264_nvenc)",
            "cuda": "CUDA driver API (FFmpeg's nvenc backend)",
            "EGL_nvidia": "NVIDIA EGL vendor driver (Chromium/ANGLE)",
        },
        "egl_icd_marker": "nvidia",
        "gpu_accel": "nvidia",
        "hint": (
            "Run with --gpus all and NVIDIA_DRIVER_CAPABILITIES covering "
            "'video' (libnvidia-encode) and 'graphics' (libEGL_nvidia)."
        ),
    },
    VENDOR_IGPU: {
        "codec": "h264_vaapi",
        "capability_marker": "vaapi",
        "renderer_markers": ("amd", "radeon", "intel", "iris"),
        "libraries": {
            "va": "libva core (KasmVNC's VAAPI encoder)",
            "va-drm": "libva DRM backend (opens the render node)",
            "EGL": "GLVND EGL loader (ANGLE's gl-egl backend)",
        },
        "egl_icd_marker": "mesa",
        "gpu_accel": "igpu",
        "hint": (
            "Map the render node (--device /dev/dri:/dev/dri) and use the "
            "docker-compose.igpu.yml image: libva2/libva-drm2 come from the "
            "base, libegl1 from Dockerfile.igpu."
        ),
    },
}

# The line Xvnc emits at INFO once the encoder probe has settled. Capturing the
# value rather than matching a literal is deliberate: "none" is the failure we
# are hunting, so the check has to be able to report what it actually got.
CAPABILITY_RE = re.compile(
    r"Hardware video encoding acceleration capability:\s*(\S+)"
)

# VAAPI codec name -> the vainfo profile prefix whose EncSlice entrypoint it
# needs. This is the check that separates "no VAAPI at all" from "this GPU
# generation cannot encode THAT codec", which are one indistinguishable
# "capability: none" from Xvnc's side. Measured example of the latter: a
# Raphael-class AMD iGPU lists VAProfileAV1Profile0/VAEntrypointVLD (decode) and
# no AV1 EncSlice anywhere, so av1_vaapi cannot open on it while h264_vaapi can.
VAAPI_ENCODE_PROFILES = {
    "h264_vaapi": "VAProfileH264",
    "h265_vaapi": "VAProfileHEVC",
    "av1_vaapi": "VAProfileAV1",
}

# Renderer strings that mean "this is the CPU". The probe keys on GL_RENDERER
# and NOT on chrome://gpu's feature table, which is actively misleading here:
# measured, the table reported webgl=enabled / rasterization=enabled /
# gpu_compositing=enabled for BOTH llvmpipe and SwiftShader. A check written
# against featureStatus passes on a fully software stack.
#
# WebGL's UNMASKED_RENDERER_WEBGL is not usable either, for a reason specific to
# this image: CloakBrowser's fingerprint layer synthesizes it, so a page reads a
# plausible NVIDIA string on a software run (measured: an "RTX 5080 / driver
# 565.77" string on a host whose real GPU is a 3080 Ti on 580.173.02). Only the
# GPU process's own view, via CDP SystemInfo.getInfo, is ground truth.
#
# "llvmpipe" is why the iGPU verdict cannot simply look for "mesa": Mesa is the
# vendor string for BOTH the hardware Intel/AMD drivers and its software
# rasteriser, and on the gl-egl path a failure to reach the render node lands on
# the latter with everything else looking identical.
_SOFTWARE_RENDERER_MARKERS = ("swiftshader", "llvmpipe", "software", "softpipe")


def _emit(results: dict, started: float) -> int:
    """Print the framed JSON payload and return the process exit status."""
    failed = [name for name, r in results.items() if not r["ok"]]
    payload = {
        "checks": results,
        "failed": failed,
        "elapsed_s": round(time.monotonic() - started, 2),
    }
    print(JSON_BEGIN)
    print(json.dumps(payload, indent=2, sort_keys=True))
    print(JSON_END)
    return 1 if failed else 0


def check_driver_libraries(vendor: dict) -> dict:
    """The driver userspace this path needs, and what each absence costs.

    Checked by name rather than by path: on the NVIDIA path the container
    runtime places them wherever the image's loader looks, and hardcoding
    /usr/lib/x86_64-linux-gnu would make this probe wrong on arm64 and on any
    image with a different multiarch dir.
    """
    wanted = vendor["libraries"]
    found = {name: ctypes.util.find_library(name) for name in wanted}
    missing = sorted(name for name, path in found.items() if path is None)
    return {
        "ok": not missing,
        "detail": {
            "found": {k: v for k, v in found.items() if v},
            "missing": missing,
            "purpose": {k: wanted[k] for k in missing},
            # Reported, never required: only CHROME_ANGLE_BACKEND=vulkan needs
            # it, and failing a gl-egl deployment over a missing Vulkan loader
            # would be a false negative.
            "libvulkan": ctypes.util.find_library("vulkan"),
            "hint": vendor["hint"] if missing else "",
        },
    }


def check_egl_loader(vendor: dict) -> dict:
    """The GLVND loader ANGLE dlopen()s, and the vendor manifest it reads.

    Separate from the driver check because the failure is different in kind:
    on the NVIDIA path the driver library is injected at run time, whereas
    libEGL.so.1 has to be IN the image (Dockerfile.nvidia and Dockerfile.igpu
    both install libegl1). Missing it is a build defect, and it produces the
    same silent software fallback rather than an error.
    """
    loader = ctypes.util.find_library("EGL")
    icd_dir = "/usr/share/glvnd/egl_vendor.d"
    icds = sorted(os.listdir(icd_dir)) if os.path.isdir(icd_dir) else []
    marker = vendor["egl_icd_marker"]
    vendor_icd = [f for f in icds if marker in f.lower()]
    ok = bool(loader) and bool(vendor_icd)
    return {
        "ok": ok,
        "detail": {
            "libEGL_loader": loader,
            "egl_vendor_icds": icds,
            f"{marker}_icd": vendor_icd,
            "hint": (
                "" if ok else
                "libEGL.so.1 comes from the libegl1 package; the vendor "
                f"manifest matching {marker!r} comes from libegl-mesa0 (iGPU) "
                "or the NVIDIA container runtime's injection."
            ),
        },
    }


def check_vaapi_encode_profile(codec: str, node: str) -> dict:
    """Ask the driver directly whether it can ENCODE this codec.

    Xvnc cannot tell you this: a GPU that decodes H.264 but cannot encode it,
    and a container with no VAAPI driver at all, both come out as
    "capability: none". vainfo separates them, and its entrypoint list is the
    authority — VAEntrypointVLD is decode, VAEntrypointEncSlice is encode, and
    only the second one matters for KasmVNC.
    """
    # KASM_VIDEO_CODEC is a comma-separated availability fallback ("h264_vaapi,
    # h264"), so accept one here too and assert the hardware entry — the software
    # tail is exactly what this check exists to stop the deployment from landing
    # on unnoticed.
    requested = [c.strip() for c in codec.split(",") if c.strip()]
    hardware = next((c for c in requested if c in VAAPI_ENCODE_PROFILES), None)
    profile = VAAPI_ENCODE_PROFILES.get(hardware or "")
    binary = shutil.which("vainfo")
    if profile is None or binary is None:
        return {
            "ok": profile is None,  # a non-VAAPI codec is not this check's problem
            "detail": {
                "skipped": True,
                "reason": (
                    f"{codec} names no VAAPI codec" if profile is None
                    else "vainfo is not installed in this image"
                ),
            },
        }
    codec = hardware
    try:
        result = subprocess.run(
            [binary, "--display", "drm", "--device", node],
            capture_output=True, text=True, timeout=30,
        )
        output = f"{result.stdout}\n{result.stderr}"
    except (OSError, subprocess.SubprocessError) as exc:
        output = f"{type(exc).__name__}: {exc}"
    encoders = sorted({
        line.split(":")[0].strip()
        for line in output.splitlines()
        if "VAEntrypointEncSlice" in line
    })
    driver = next(
        (l.split(":", 1)[1].strip() for l in output.splitlines()
         if l.startswith("vainfo: Driver version")), None,
    )
    supported = [e for e in encoders if e.startswith(profile)]
    return {
        "ok": bool(supported),
        "detail": {
            "codec": codec,
            "render_node": node,
            "va_driver": driver,
            "encode_profiles": encoders,
            "matching": supported,
            "hint": (
                "" if supported else
                f"No {profile}* VAEntrypointEncSlice on this GPU. Run "
                f"`vainfo --display drm --device {node}` for the full list and "
                "pick a codec it can encode (h264_vaapi is the widest); AV1 "
                "encode needs Intel Arc/Meteor Lake or AMD RDNA3 and newer."
            ),
        },
    }


def start_xvnc(codec: str, hw3d: list[str]) -> subprocess.Popen:
    """Start a throwaway Xvnc with the hardware codec requested.

    `hw3d` comes from the manager's own _hw3d_flags() rather than being
    hardcoded, and it is load-bearing for the Chromium half on the iGPU path:
    -hw3d is what gives this X server DRI3, and DRI3 is how ANGLE's gl-egl
    backend reaches the render node. A probe that omitted it would measure
    llvmpipe and blame the flags.
    """
    passwd_path = f"/tmp/gpu-probe-kasmpasswd-{DISPLAY_NUM}"
    subprocess.run(
        [shutil.which("kasmvncpasswd") or "kasmvncpasswd",
         "-u", "probe", "-wro", passwd_path],
        input=b"probeprobe\nprobeprobe\n",
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
    )
    cmd = [
        shutil.which("Xvnc") or "Xvnc", f":{DISPLAY_NUM}",
        "-websocketPort", str(WS_PORT),
        "-geometry", "1280x720", "-depth", "24",
        "-SecurityTypes", "None", "-interface", "127.0.0.1", "-AlwaysShared",
        "-httpd", "/usr/share/kasmvnc/www",
        "-videoCodec", codec,
        # The capability line is INFO, but the EncoderProbe lines that explain a
        # 'none' verdict are DEBUG — and explaining the failure is the point.
        "-Log", "*:stdout:100",
        "-PublicIP", "127.0.0.1",
        "-KasmPasswordFile", passwd_path,
        *hw3d,
    ]
    log = open(XVNC_LOG, "w")
    proc = subprocess.Popen(cmd, stdout=log, stderr=log)
    log.close()
    return proc


def check_kasm_hw_encode(codec: str, marker: str, hw3d: list[str],
                         timeout_s: float = 25.0) -> dict:
    """Assert KasmVNC actually SELECTED a hardware encoder, not merely started.

    Waits for the capability line rather than sleeping a fixed interval: the
    probe opens and closes a real encoder session per candidate codec, which
    takes appreciably longer than Xvnc's port bind.
    """
    proc = start_xvnc(codec, hw3d)
    capability = None
    available = None
    deadline = time.monotonic() + timeout_s
    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            try:
                with open(XVNC_LOG) as f:
                    text = f.read()
            except OSError:
                text = ""
            match = CAPABILITY_RE.search(text)
            if match:
                capability = match.group(1)
                avail = re.search(r"Available encoders:\s*(.+)", text)
                available = avail.group(1).strip() if avail else None
                break
            time.sleep(0.25)
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(5)

    accelerated = bool(capability) and capability != "none"
    return {
        "ok": accelerated and marker in (capability or ""),
        "detail": {
            "requested_codec": codec,
            "capability": capability,
            "available_encoders": available,
            "xvnc_log": XVNC_LOG,
            "hw3d_flags": hw3d,
            "hint": (
                "" if accelerated else
                "capability 'none' means every candidate failed to open. On "
                "NVIDIA, av1_nvenc needs Ada or newer (use h264_nvenc on "
                "Ampere); on an iGPU, read the vaapi_encode_profile check "
                "above — it says whether this GPU can encode the codec at all."
            ),
        },
    }


async def _read_gpu_info(cdp_port: int, timeout_s: float) -> dict:
    """Ask the GPU process what it actually bound, via CDP SystemInfo.getInfo.

    Uses the BROWSER-level endpoint (/json/version), not a page target:
    SystemInfo is a browser-scoped domain and is not dispatched on a page
    session. Scraping chrome://gpu instead does not work — it renders the
    feature table into shadow DOM, so document.body.innerText comes back empty.
    """
    import httpx
    import websockets

    deadline = time.monotonic() + timeout_s
    browser_ws = None
    async with httpx.AsyncClient() as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.get(
                    f"http://127.0.0.1:{cdp_port}/json/version", timeout=5.0,
                )
                if resp.status_code == 200:
                    browser_ws = resp.json()["webSocketDebuggerUrl"]
                    break
            except Exception:
                pass
            await asyncio.sleep(0.5)
    if not browser_ws:
        raise RuntimeError(f"CDP never came up on {cdp_port}")

    async with websockets.connect(browser_ws, max_size=32 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"id": 1, "method": "SystemInfo.getInfo"}))
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("id") == 1:
                break
        gpu = msg.get("result", {}).get("gpu", {}) or {}
        aux = gpu.get("auxAttributes", {}) or {}
        return {
            "glRenderer": aux.get("glRenderer"),
            "glVendor": aux.get("glVendor"),
            "glImplementationParts": aux.get("glImplementationParts"),
            "featureStatus": gpu.get("featureStatus", {}) or {},
            "devices": gpu.get("devices", []) or [],
        }


def check_chromium_gpu(chrome_binary: str, gpu_flags: list[str],
                       gpu_env: dict, markers: tuple[str, ...],
                       hw3d: list[str], timeout_s: float = 45.0) -> dict:
    """Launch a headed Chromium on the probe's Xvnc and ask it what it bound.

    Headed on a real Xvnc, not --headless and not Xvfb, because that is the
    configuration under test: the browser this manager runs draws into a
    virtual X server, and that is precisely what breaks the EGL path.
    """
    xvnc = start_xvnc("h264", hw3d)  # codec irrelevant here; we need the display
    env = {**os.environ, **gpu_env, "DISPLAY": f":{DISPLAY_NUM}"}
    profile_dir = f"/tmp/gpu-probe-profile-{DISPLAY_NUM}"
    shutil.rmtree(profile_dir, ignore_errors=True)
    cmd = [
        chrome_binary,
        f"--user-data-dir={profile_dir}",
        f"--remote-debugging-port={CDP_PORT}",
        "--remote-allow-origins=*",
        "--no-sandbox", "--test-type", "--no-first-run",
        "--disable-infobars",
        *gpu_flags,
        "about:blank",
    ]
    log = open(CHROME_LOG, "w")
    chrome = subprocess.Popen(cmd, stdout=log, stderr=log, env=env)
    log.close()
    try:
        info = asyncio.run(_read_gpu_info(CDP_PORT, timeout_s))
    except Exception as exc:
        info = {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        for proc in (chrome, xvnc):
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(5)

    renderer = (info.get("glRenderer") or "").lower()
    features = info.get("featureStatus") or {}
    software = any(m in renderer for m in _SOFTWARE_RENDERER_MARKERS)
    named = any(m in renderer for m in markers)
    # Reported for context, deliberately NOT part of the verdict — see the note
    # on _SOFTWARE_RENDERER_MARKERS. gpu_compositing is called out because it is
    # the one that separates a full GPU path from an EGL fallback that reaches
    # the GPU and then composites in software via readback.
    return {
        "ok": named and not software,
        "detail": {
            "gl_renderer": info.get("glRenderer"),
            "gl_vendor": info.get("glVendor"),
            "gl_implementation": info.get("glImplementationParts"),
            "gpu_compositing": features.get("gpu_compositing"),
            "feature_status": features,
            "devices": info.get("devices"),
            "flags": gpu_flags,
            "expected_renderer_markers": list(markers),
            "chrome_log": CHROME_LOG,
            "error": info.get("error"),
            "hint": (
                "" if (named and not software) else
                "A software renderer with the GPU attached means ANGLE never "
                "reached the driver. On NVIDIA: check libvulkan1 and that "
                "/etc/vulkan/icd.d/nvidia_icd.json was injected — "
                "--use-angle=gl-egl lands on Mesa/llvmpipe under a virtual X "
                "server there and is NOT a substitute for --use-angle=vulkan. "
                "On an iGPU: re-run with --angle-backend vulkan and with gl; "
                "which one binds hardware is per-driver, and -hw3d must be on "
                "for gl-egl to have DRI3 to reach."
            ),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vendor", default=VENDOR_NVIDIA, choices=sorted(VENDORS),
                        help="GPU stack to assert (default: nvidia)")
    parser.add_argument("--codec", default=None,
                        help="KASM video codec to assert (default: per vendor)")
    parser.add_argument("--angle-backend", default=None,
                        choices=("gl-egl", "vulkan", "gl"),
                        help="Override CHROME_ANGLE_BACKEND for the iGPU path")
    parser.add_argument("--chrome-binary", default=None,
                        help="Chromium path (default: resolve via cloakbrowser)")
    parser.add_argument("--skip-chrome", action="store_true",
                        help="Only check the KasmVNC/encoder half")
    args = parser.parse_args()

    vendor = VENDORS[args.vendor]
    codec = args.codec or vendor["codec"]

    # Pin the manager's GPU resolution to the vendor under test instead of
    # letting it auto-detect. Auto is the right default for a deployment and the
    # wrong one for a probe: on a host with both stacks present it would resolve
    # to NVIDIA and this run would silently assert the other path.
    os.environ["CHROME_GPU_ACCEL"] = vendor["gpu_accel"]
    if args.angle_backend:
        os.environ["CHROME_ANGLE_BACKEND"] = args.angle_backend

    from backend.vnc_manager import _dri_render_node, _hw3d_flags

    started = time.monotonic()
    node = _dri_render_node()
    # Resolved once and passed down: _hw3d_flags() logs its decision, and the
    # Xvnc it configures has to be the same one in both halves of the probe.
    hw3d = _hw3d_flags()

    results: dict = {}
    results["driver_libraries"] = check_driver_libraries(vendor)
    results["egl_loader"] = check_egl_loader(vendor)
    if args.vendor == VENDOR_IGPU:
        results["vaapi_encode_profile"] = check_vaapi_encode_profile(codec, node)
    results["kasm_hw_encode"] = check_kasm_hw_encode(
        codec, vendor["capability_marker"], hw3d,
    )

    if not args.skip_chrome:
        binary = args.chrome_binary
        if not binary:
            from cloakbrowser.download import ensure_binary
            binary = str(ensure_binary())
        # Import the manager's own resolution rather than restating a flag list
        # here: a probe that asserts its own copy of the flags would keep
        # passing after browser_manager stopped emitting them.
        from backend.browser_manager import _chrome_gpu_env, _chrome_gpu_flags
        results["chromium_gpu"] = check_chromium_gpu(
            binary, _chrome_gpu_flags(), _chrome_gpu_env(),
            vendor["renderer_markers"], hw3d,
        )

    return _emit(results, started)


if __name__ == "__main__":
    sys.exit(main())
