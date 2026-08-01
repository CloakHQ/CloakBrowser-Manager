#!/usr/bin/env python3
"""
GPU acceleration contract probe — runs INSIDE the cloakbrowser-manager image.

Both halves of GPU acceleration fail SILENTLY, which is the only reason this
script exists:

  * KasmVNC NVENC — if libnvidia-encode.so.1 is missing, or the codec name is
    one this GPU generation cannot encode, the probe simply drops the codec and
    Xvnc logs "Hardware video encoding acceleration capability: none" once, at
    INFO, from a different process. The session then encodes every frame on the
    CPU while looking completely healthy.
  * Chromium — ANGLE's response to a missing libEGL.so.1 is to fall back to
    SwiftShader, not to fail. A GPU can be attached, injected and idle while
    every frame is rasterised on the CPU, and nothing in the manager's logs
    says so.

So neither "it started" nor "it did not crash" is evidence of acceleration.
This asserts the observable consequences instead: the encoder KasmVNC actually
selected, and the GL renderer string Chromium actually bound.

Run it against a GPU (it is expected to FAIL without one):

  docker run --rm --gpus all -v "$PWD":/repo:ro \
    --entrypoint python cloakbrowser-manager-manager:latest \
    /repo/scripts/gpu_probe.py

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

# The line Xvnc emits at INFO once the encoder probe has settled. Capturing the
# value rather than matching a literal is deliberate: "none" is the failure we
# are hunting, so the check has to be able to report what it actually got.
CAPABILITY_RE = re.compile(
    r"Hardware video encoding acceleration capability:\s*(\S+)"
)

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


def check_driver_libraries() -> dict:
    """The driver userspace the container runtime is supposed to have injected.

    Checked by name rather than by path: the runtime places them wherever the
    image's loader looks, and hardcoding /usr/lib/x86_64-linux-gnu would make
    this probe wrong on arm64 and on any image with a different multiarch dir.
    """
    wanted = {
        "nvidia-encode": "NVENC encode (KasmVNC h264_nvenc)",
        "cuda": "CUDA driver API (FFmpeg's nvenc backend)",
        "EGL_nvidia": "NVIDIA EGL vendor driver (Chromium/ANGLE)",
    }
    found = {name: ctypes.util.find_library(name) for name in wanted}
    missing = sorted(name for name, path in found.items() if path is None)
    return {
        "ok": not missing,
        "detail": {
            "found": {k: v for k, v in found.items() if v},
            "missing": missing,
            "purpose": {k: wanted[k] for k in missing},
            "hint": (
                "Run with --gpus all and NVIDIA_DRIVER_CAPABILITIES covering "
                "'video' (libnvidia-encode) and 'graphics' (libEGL_nvidia)."
                if missing else ""
            ),
        },
    }


def check_egl_loader() -> dict:
    """The GLVND loader ANGLE dlopen()s, and the vendor manifest it reads.

    Separate from the driver check because the failure is different in kind:
    the driver library is injected at run time, whereas libEGL.so.1 has to be
    IN the image (Dockerfile.nvidia installs libegl1/libgles2). Missing it is a
    build defect, and it produces the same silent SwiftShader fallback.
    """
    loader = ctypes.util.find_library("EGL")
    icd_dir = "/usr/share/glvnd/egl_vendor.d"
    icds = sorted(os.listdir(icd_dir)) if os.path.isdir(icd_dir) else []
    nvidia_icd = [f for f in icds if "nvidia" in f.lower()]
    ok = bool(loader) and bool(nvidia_icd)
    return {
        "ok": ok,
        "detail": {
            "libEGL_loader": loader,
            "egl_vendor_icds": icds,
            "nvidia_icd": nvidia_icd,
            "hint": (
                "" if ok else
                "libEGL.so.1 comes from the libegl1 package (Dockerfile.nvidia); "
                "10_nvidia.json is injected by the NVIDIA container runtime."
            ),
        },
    }


def start_xvnc(codec: str) -> subprocess.Popen:
    """Start a throwaway Xvnc with the NVENC codec requested."""
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
    ]
    log = open(XVNC_LOG, "w")
    proc = subprocess.Popen(cmd, stdout=log, stderr=log)
    log.close()
    return proc


def check_kasm_nvenc(codec: str, timeout_s: float = 25.0) -> dict:
    """Assert KasmVNC actually SELECTED a hardware encoder, not merely started.

    Waits for the capability line rather than sleeping a fixed interval: the
    NVENC probe opens and closes a real encoder session per candidate codec,
    which takes appreciably longer than Xvnc's port bind.
    """
    proc = start_xvnc(codec)
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
        "ok": accelerated and "nvenc" in (capability or ""),
        "detail": {
            "requested_codec": codec,
            "capability": capability,
            "available_encoders": available,
            "xvnc_log": XVNC_LOG,
            "hint": (
                "" if accelerated else
                "capability 'none' means every candidate failed to open. "
                "av1_nvenc needs Ada or newer; on Ampere use h264_nvenc."
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
                       gpu_env: dict, timeout_s: float = 45.0) -> dict:
    """Launch a headed Chromium on the probe's Xvnc and ask it what it bound.

    Headed on a real Xvnc, not --headless and not Xvfb, because that is the
    configuration under test: the browser this manager runs draws into a
    virtual X server, and that is precisely what breaks the EGL path.
    """
    xvnc = start_xvnc("h264")  # codec irrelevant here; we just need the display
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
    software = any(marker in renderer for marker in _SOFTWARE_RENDERER_MARKERS)
    is_nvidia = "nvidia" in renderer
    # Reported for context, deliberately NOT part of the verdict — see the note
    # on _SOFTWARE_RENDERER_MARKERS. gpu_compositing is called out because it is
    # the one that separates the Vulkan backend from the EGL fallback: the
    # latter reaches the GPU but composites in software via readback.
    return {
        "ok": is_nvidia and not software,
        "detail": {
            "gl_renderer": info.get("glRenderer"),
            "gl_vendor": info.get("glVendor"),
            "gl_implementation": info.get("glImplementationParts"),
            "gpu_compositing": features.get("gpu_compositing"),
            "feature_status": features,
            "devices": info.get("devices"),
            "flags": gpu_flags,
            "chrome_log": CHROME_LOG,
            "error": info.get("error"),
            "hint": (
                "" if (is_nvidia and not software) else
                "A software renderer with the GPU attached means ANGLE never "
                "reached the driver. Check libvulkan1 is installed and "
                "/etc/vulkan/icd.d/nvidia_icd.json was injected; note that "
                "--use-angle=gl-egl lands on Mesa/llvmpipe under a virtual X "
                "server and is NOT a substitute for --use-angle=vulkan."
            ),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codec", default="h264_nvenc",
                        help="KASM video codec to assert (default: h264_nvenc)")
    parser.add_argument("--chrome-binary", default=None,
                        help="Chromium path (default: resolve via cloakbrowser)")
    parser.add_argument("--skip-chrome", action="store_true",
                        help="Only check the KasmVNC/NVENC half")
    args = parser.parse_args()

    started = time.monotonic()
    results: dict = {}
    results["driver_libraries"] = check_driver_libraries()
    results["egl_loader"] = check_egl_loader()
    results["kasm_nvenc"] = check_kasm_nvenc(args.codec)

    if not args.skip_chrome:
        binary = args.chrome_binary
        if not binary:
            from cloakbrowser.download import ensure_binary
            binary = str(ensure_binary())
        # Import the manager's own resolution rather than restating a flag list
        # here: a probe that asserts its own copy of the flags would keep
        # passing after browser_manager stopped emitting them.
        sys.path.insert(0, "/app")
        from backend.browser_manager import _chrome_gpu_env, _chrome_gpu_flags
        results["chromium_gpu"] = check_chromium_gpu(
            binary, _chrome_gpu_flags(), _chrome_gpu_env(),
        )

    return _emit(results, started)


if __name__ == "__main__":
    sys.exit(main())
