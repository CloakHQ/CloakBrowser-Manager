<p align="center">
<img src="https://i.imgur.com/cqkp6fG.png" width="500" alt="CloakBrowser">
</p>

<h3 align="center">Browser Profile Manager for CloakBrowser</h3>

<p align="center">
Create, manage, and launch isolated browser profiles with unique fingerprints.<br>
Free, self-hosted alternative to Multilogin, GoLogin, and AdsPower.
</p>

<p align="center">
<a href="https://github.com/CloakHQ/CloakBrowser"><img src="https://img.shields.io/github/stars/cloakhq/cloakbrowser?label=CloakBrowser" alt="Stars"></a>
<a href="https://hub.docker.com/r/cloakhq/cloakbrowser-manager"><img src="https://img.shields.io/docker/pulls/cloakhq/cloakbrowser-manager?label=docker&logo=docker&logoColor=white" alt="Docker Pulls"></a>
<a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="License"></a>
</p>

---

<p align="center">
<img src="https://i.imgur.com/twdX81Q.png" width="800" alt="CloakBrowser Manager — Browser View">
<br>
<img src="https://i.imgur.com/XFYn1qY.png" width="800" alt="CloakBrowser Manager — Profile Settings">
</p>

Each profile is an isolated CloakBrowser instance with its own fingerprint, proxy, cookies, and session data. Profiles persist across restarts. Everything runs in one Docker container.

```bash
docker run -p 8080:8080 -v cloakprofiles:/data cloakhq/cloakbrowser-manager
```

Or build from source:

```bash
git clone https://github.com/CloakHQ/CloakBrowser-Manager.git
cd CloakBrowser-Manager
docker compose up --build
```

Open [http://localhost:8080](http://localhost:8080) in your browser. Create a profile. Click Launch. Done.

> **Early alpha** — this project is under active development. Expect bugs. If you find one, please [open an issue](https://github.com/CloakHQ/CloakBrowser-Manager/issues).

## Why Not Just Use a VPN?

A VPN only changes your IP. Incognito only clears cookies. Chrome profiles share the same hardware fingerprint underneath. Platforms use 50+ signals to link your accounts — canvas, WebGL, audio, GPU, fonts, screen size, timezone.

Each CloakBrowser profile generates a completely different device identity. To the website, each profile looks like a different computer.

| Solution | What it changes | Accounts linked? |
|----------|----------------|-----------------|
| VPN | IP address only | Yes — same fingerprint |
| Incognito | Clears cookies | Yes — same fingerprint |
| Chrome profiles | Separate bookmarks/cookies | Yes — same hardware fingerprint |
| **CloakBrowser** | **Everything — full device identity per profile** | **No** |

## Features

- **Profile management** — create, edit, delete browser profiles with unique fingerprints
- **Per-profile settings** — fingerprint seed, proxy, timezone, locale, user agent, screen size, platform, CloakBrowser license key override
- **One-click launch/stop** — each profile runs as an isolated CloakBrowser instance
- **Session persistence** — cookies, localStorage, and cache survive browser restarts
- **In-browser viewing** — interact with launched browsers via KasmVNC's native web client, directly in the web GUI (server-authoritative JPEG/WebP by default, opt-in H.264/H.265/AV1 WebCodecs streaming)
- **Playwright/Puppeteer API** — connect to any running profile programmatically via CDP, while still watching it live in the browser
- **Optional authentication** — protect the web UI and API with a single token, or run wide open locally
- **Powered by CloakBrowser** — 32 source-level C++ patches, passes Cloudflare Turnstile, 0.9 reCAPTCHA v3 score

## Stack

- **Backend**: FastAPI (Python) — control plane only (profiles, lifecycle, auth, CDP proxy)
- **Frontend**: React + Tailwind CSS
- **Browser viewer**: KasmVNC 1.5 native web client (matched server/client release)
- **Data plane**: nginx — proxies the KasmVNC HTTP/WebSocket path directly; no framebuffer bytes pass through Python
- **Database**: SQLite
- **Browser engine**: [CloakBrowser](https://github.com/CloakHQ/CloakBrowser) (stealth Chromium binary)

## Architecture

```
CloakBrowser/Chromium
  → KasmVNC 1.5 Xvnc (virtual X11 display, loopback-only WebSocket/HTTP)
  → nginx :8080
      ├── /api/*, SPA      → FastAPI (control plane, 127.0.0.1:8081)
      └── /viewer/<token>/* → that profile's KasmVNC port (auth_request-gated)
  → your browser (React SPA + native KasmVNC client in an iframe)
```

- The native KasmVNC client and server come from the **same pinned 1.5.0 package** — no protocol translation anywhere.
- Viewer access uses short-lived, per-profile opaque tokens issued by `POST /api/profiles/<id>/viewer-token`; nginx validates every viewer request (page, assets, WebSocket upgrade) through FastAPI's `/api/viewer-auth`. Kasm ports never leave loopback.
- The Manager owns a reconnect state machine (backoff + jitter, offline/visibility handling, session-status classification). A viewer disconnect never stops the browser — reconnecting returns you to the same running session.
- Encoding policy is a single knob (`KASM_ENCODING_POLICY`), because KasmVNC 1.5.0 gates video-codec negotiation and client quality overrides behind the *same* switch. `server-authoritative` keeps the server in charge — 30 FPS cap, dynamic JPEG/WebP quality, video-mode downscale under motion — and no H.264/H.265/AV1 codec is negotiated. `video` enables WebCodecs streaming and makes every quality setting client-overridable.
- Under `video`, `KASM_VIDEO_CODEC` decides the encoder for the bundled viewer: it narrows the server's probe, the server advertises only that probed set, and the client can only select from what it is offered. It is not *enforced*, though — KasmVNC accepts any streaming-mode pseudo-encoding a client sends, even for an encoder it rejected at probe time, so a client other than the bundled one can still land on software AV1 (roughly 0.4s of a CPU core per 1080p keyframe, then a silent drop back to Tight). `video` is opt-in for the other reason: it also hands every quality setting to the client, so `KASM_QUALITY_PRESET` stops binding.

### Headless profiles

A profile with **Headless** enabled starts no Xvnc and allocates no display or WebSocket port — there is nothing to view, so the viewer is not offered and `POST /api/profiles/<id>/viewer-token` answers `409`. Drive it over CDP instead (`/api/profiles/<id>/cdp`). Everything else — profiles, proxies, fingerprints, lifecycle — behaves identically.

### Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `AUTH_TOKEN` | *(unset = open)* | Protect the web UI + API with a token |
| `TLS_SANS` | *(unset)* | Extra names/IPs for the self-signed cert on `:8443`, comma separated. `localhost` and `127.0.0.1` are always included. Read only when the cert is first issued — delete `/data/tls` to re-issue. |
| `CF_TUNNEL_TOKEN` | *(unset = quick tunnel)* | With `docker-compose.tunnel.yml`: run a **named** Cloudflare tunnel with a stable hostname. Unset gives a free `*.trycloudflare.com` quick tunnel that changes every restart. |
| `CF_TUNNEL_ORIGIN` | `http://manager:8080` | What the tunnel points at inside the compose network. |
| `TUNNEL_ALLOW_NO_AUTH` | *(unset)* | Allow the tunnel sidecar to start with an empty `AUTH_TOKEN`, i.e. publish an unauthenticated Manager to the internet. |
| `KASM_QUALITY_PRESET` | `balanced` | Encoding preset: `text`, `balanced`, `low`, `motion` |
| `KASM_ENCODING_POLICY` | `server-authoritative` | `server-authoritative`: clients cannot override encoding/quality; JPEG/WebP only, no video codec can engage. `video`: in-band H.264/H.265/AV1 WebCodecs streaming, at the cost of client-authoritative quality settings — under it `KASM_QUALITY_PRESET` does not bind. See the note above. |
| `KASM_VIDEO_CODEC` | `h264` | Encoder offered under `KASM_ENCODING_POLICY=video`. Software: `h264`, `h265`, `av1`. VAAPI (Intel/AMD): `h264_vaapi`, `h265_vaapi`, `av1_vaapi`. NVENC (NVIDIA): `h264_nvenc`, `h265_nvenc`/`hevc_nvenc`, `av1_nvenc`. `auto` is refused — it lets the client pick, including software AV1. |
| `KASM_XVNC_LOG_LEVEL` | `30` | Xvnc log verbosity (0-100). Raise to `100` to see per-connection encoder decisions in `/tmp/xvnc-<display>.log`. |
| `KASM_HW3D` | `auto` | DRI3 GPU acceleration: `auto` (enable unless NVIDIA proprietary), `1` (force), `0` (disable) |
| `KASM_DRINODE` | `/dev/dri/renderD128` | GPU render node for DRI3/VAAPI. Chromium probes the same node, so both halves stay on one GPU. |
| `CHROME_GPU_ACCEL` | `auto` | Chromium GL backend. `auto` picks the NVIDIA path when `/dev/nvidiactl` is present, else the Mesa path when `KASM_DRINODE` exists and is not driven by `nvidia`, else SwiftShader. `1`/`nvidia` forces the NVIDIA path without the device check; `igpu`/`vaapi`/`mesa`/`intel`/`amd` force the Mesa one; `0` forces SwiftShader. |
| `CHROME_ANGLE_BACKEND` | `vulkan` | ANGLE backend for the Mesa path only: `vulkan`, `gl-egl`, `gl`. Which one reaches the GPU is per-driver and fails silently — settle it with `gpu_probe.py --vendor igpu --angle-backend <name>`. `swiftshader` is refused. |
| `KASM_RECT_THREADS` | *(ignored)* | No effect on KasmVNC 1.5.0 — `-RectThreads` parses but nothing reads it; encoder threads are sized to the host core count. Cap encoder CPU with the container's `--cpus`/cpuset. |

### GPU acceleration (optional)

Pass the host GPU into the container to enable KasmVNC DRI3 screen capture (AMD/Intel open-source drivers; closed-source NVIDIA does not support DRI3):

```bash
docker run --device /dev/dri:/dev/dri -p 8080:8080 -v cloakprofiles:/data cloakhq/cloakbrowser-manager
```

With Compose, GPU passthrough is an opt-in overlay (Docker refuses to create a container when a mapped device node is missing, so the default file stays GPU-free):

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

Without a GPU everything still works (software encoding).

This overlay buys DRI3 screen **capture** and nothing else. Hardware *video encode*
needs `KASM_ENCODING_POLICY=video` (no codec is negotiated at all under the
default policy), and Chromium needs Mesa userspace the base image does not carry
— use `docker-compose.igpu.yml` below for either.

### Intel / AMD integrated GPU (VAAPI encode + Chromium acceleration)

Needs a `/dev/dri` render node driven by the open-source stack — `i915`/`xe` for
Intel, `amdgpu` for AMD. Nothing else on the host: unlike the NVIDIA path, the
whole userspace is baked into the image and reaches the GPU through the mapped
render node.

```bash
docker compose -f docker-compose.yml -f docker-compose.igpu.yml up -d --build
```

This overlay defaults to `KASM_ENCODING_POLICY=video` and
`KASM_VIDEO_CODEC=h264_vaapi,h264`, because under the repo-wide default policy no
video codec is ever negotiated and the GPU encoder could not engage at all.

What it turns on:

| | Accelerated? | Notes |
|---|---|---|
| KasmVNC video encode | **Yes — VAAPI** | Xvnc encodes the framebuffer on the GPU (`h264_vaapi`). Needs `VAProfileH264*` + `VAEntrypointEncSlice` on the device — check with `vainfo`. |
| KasmVNC `-hw3d` screen capture | **Yes — DRI3** | Implemented by the open-source drivers, so `KASM_HW3D=auto` enables it. This is also what gives the X server the DRI3 that ANGLE's `gl-egl` backend needs. |
| Chromium WebGL / raster / compositing | **Yes — ANGLE + Mesa** | Backend selected by `CHROME_ANGLE_BACKEND`; see below. |
| Chromium video *decode* | No | Left off deliberately — VA-API decode in Chromium wants to import the decoded frame for compositing, and through a virtual X server that trades working playback for a benchmark. |

Two things to know, and both fail silently:

- **`gl-egl` reaches the GPU and still composites on the CPU.** Measured headed on
  Xvnc with `-hw3d`, AMD Raphael iGPU, Mesa 25.0.7 — all three backends, same host:

  | `CHROME_ANGLE_BACKEND` | renderer bound | `webgl` | `gpu_compositing` |
  |---|---|---|---|
  | `gl-egl` | `ANGLE (AMD, … radeonsi raphael_mendocino …, OpenGL ES 3.2 Mesa)` | `enabled_readback` | `disabled_software` |
  | `vulkan` (default) | `ANGLE (AMD, Vulkan 1.4.305 (RADV RAPHAEL_MENDOCINO), radv-25.0.7)` | `enabled` | `enabled` |
  | `gl` | Chromium never reached CDP (45s timeout) | — | — |

  Unlike the NVIDIA path, `gl-egl` is not a software trap here — DRI3 genuinely
  exists on an open-source driver, which is what `-hw3d` supplies — it just stops
  one step short. `vulkan` gets both halves. Keep it unless the probe says
  otherwise on your hardware; a Mesa build with no Vulkan driver falls back to
  llvmpipe on the CPU, not to `gl-egl`.
- **`llvmpipe` is Mesa too.** Any check that looks for "Mesa" in the renderer
  string passes on the software rasteriser. The probe keys on the vendor name
  (`AMD`, `Intel`) *and* the absence of a software marker.

Verify it end to end (expected to fail without a render node):

```bash
docker run --rm --device /dev/dri:/dev/dri -v "$PWD":/repo:ro \
  --entrypoint python cloakbrowser-manager-manager:latest \
  /repo/scripts/gpu_probe.py --vendor igpu
```

It asserts libva and the EGL loader, that the driver has an H.264 **encode**
entrypoint (`vainfo`), the encoder KasmVNC actually selected, and the renderer
Chromium actually bound. Add `--angle-backend vulkan` (or `gl`) to compare
backends on a host where the default lands on llvmpipe.

Two live checks on a running session:

```bash
docker exec <container> grep "acceleration capability" /tmp/xvnc-100.log   # must not say "none"
docker exec <container> grep -c FFMPEGHWEncoder /tmp/xvnc-100.log          # frames on the GPU encoder
```

### NVIDIA GPU (NVENC encode + Chromium acceleration)

Needs an NVIDIA GPU and the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) on the host. One command builds both the base image and the NVIDIA layer:

```bash
docker compose -f docker-compose.yml -f docker-compose.nvidia.yml up -d --build
```

This overlay defaults to `KASM_ENCODING_POLICY=video` and `KASM_VIDEO_CODEC=h264_nvenc,h264`, because under the repo-wide default policy no video codec is ever negotiated and the GPU encoder could not engage at all.

What it turns on:

| | Accelerated? | Notes |
|---|---|---|
| KasmVNC video encode | **Yes — NVENC** | Xvnc encodes the framebuffer on the GPU. Undocumented in `Xvnc -help` but fully implemented in 1.5.0. |
| Chromium WebGL / raster / compositing | **Yes — ANGLE + Vulkan** | `--use-angle=vulkan`. Vulkan is the only backend that works headed on a virtual X server (see below). |
| Chromium video *decode* | No | NVDEC via VA-API needs DRI3 to import the decoded frame for compositing; no virtual X server has it. Enabling it breaks playback rather than accelerating it. |
| KasmVNC `-hw3d` screen capture | No | DRI3, which the closed NVIDIA driver does not implement. Auto-detected and left off — forcing it (`KASM_HW3D=1`) makes Xvnc exit at startup. NVENC does not need it. |

Two things are easy to get wrong here, and both fail silently:

- **`--use-angle=gl-egl` is the software path.** NVIDIA's EGL declines the X11 platform on an X server it does not drive, so GLVND falls through to Mesa and you get llvmpipe on the CPU with the GPU attached and idle. On the same host `gl-egl` gives `ANGLE (Mesa, llvmpipe)` while `vulkan` gives `ANGLE (NVIDIA, Vulkan 1.4.312 (RTX 3080 Ti))` with GPU compositing enabled. `--use-gl=egl` is worse still — it is deprecated and silently disables WebGL.
- **The bundled KasmVNC client cannot select NVENC by itself.** Its automatic candidate list is hardcoded to the VAAPI and software variants, so against an NVENC server it quietly settles on JPEG/WebP while Xvnc still reports `capability: h264_nvenc`. The Manager works around this by sending `kasmvnc_mode_preference` in the viewer URL, derived from `KASM_VIDEO_CODEC`.

Verify it end to end with the bundled probe (expected to fail without a GPU):

```bash
docker run --rm --gpus all -v "$PWD":/repo:ro \
  --entrypoint python cloakbrowser-manager-manager:latest \
  /repo/scripts/gpu_probe.py --vendor nvidia
```

It asserts the driver libraries, the EGL loader, the encoder KasmVNC actually selected, and the renderer Chromium actually bound — not `chrome://gpu`'s feature table, which reports "enabled" for llvmpipe and SwiftShader alike.

Two live checks on a running session:

```bash
docker exec <container> grep "acceleration capability" /tmp/xvnc-100.log   # must not say "none"
nvidia-smi --query-gpu=utilization.encoder,encoder.stats.sessionCount --format=csv
```

Caveats: `av1_nvenc` needs Ada (RTX 40-series) or newer — on Ampere it probes out to `capability: none`. Consumer GeForce cards cap concurrent NVENC sessions (8 on recent drivers), and beyond that KasmVNC falls back to software encoding per session; datacenter cards are unrestricted.

## Development

### Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8080
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

### Docker

```bash
docker compose up --build
```

### Data plane (nginx + entrypoint)

`docker/nginx.conf` and `entrypoint.sh` have their own test suite. The static
config lint runs anywhere; the behavioural half boots the **shipped** config and
entrypoint inside the image against stub upstreams, and is skipped when the
image is not built.

```bash
docker build -t cloakbrowser-manager:kasm15 .
python -m pytest backend/tests/test_dataplane.py -v      # ~25s
```

It covers the auth_request status mapping, the `/viewer/<token>` 308 and its
relative `Location`, the 403 on Kasm's management `/api`, the token-prefix
strip, WebSocket upgrade passthrough on both legs, the no-idle-reaping
guarantee for CDP tunnels, and entrypoint.sh's signal handling. Run the probe
directly for the full transcript, or point it at another image with
`DATAPLANE_IMAGE=`:

```bash
docker run --rm --network none -v "$PWD":/repo:ro \
  --entrypoint python cloakbrowser-manager:kasm15 \
  /repo/scripts/dataplane_probe.py --repo /repo
```

## Requirements

- Docker (20.10+)
- ~2 GB disk (image + binary)
- ~512 MB RAM per running profile

## Updating

Pull the latest image and restart:

```bash
docker pull cloakhq/cloakbrowser-manager
docker stop <container-id>
docker run -p 8080:8080 -v cloakprofiles:/data cloakhq/cloakbrowser-manager
```

Your profiles and session data are stored in the `cloakprofiles` volume and persist across updates.

## Automation API

Every running profile exposes a CDP (Chrome DevTools Protocol) endpoint. Connect Playwright or Puppeteer to automate a profile while watching it live in the browser.

```python
from playwright.async_api import async_playwright

async with async_playwright() as pw:
    browser = await pw.chromium.connect_over_cdp(
        "http://localhost:8080/api/profiles/<profile-id>/cdp"
    )
    page = browser.contexts[0].pages[0]
    await page.goto("https://example.com")
```

```javascript
const { chromium } = require("playwright");

const browser = await chromium.connectOverCDP(
  "http://localhost:8080/api/profiles/<profile-id>/cdp"
);
const page = browser.contexts()[0].pages()[0];
await page.goto("https://example.com");
```

The CDP URL is available in the toolbar (code icon) when a profile is running. The same browser session is accessible both visually through VNC and programmatically through the API.

## Remote Access

Compose publishes on **all interfaces**: `8080` plain HTTP and `8443` HTTPS with
a self-signed certificate. Nothing else is exposed — the Xvnc WebSocket and the
FastAPI control plane stay on loopback inside the container, reached only
through nginx.

Set `AUTH_TOKEN` before doing this on an untrusted network: it is the only access
control, and unset means open (see [Authentication](#authentication)).

### Use HTTPS for any viewer that is not on localhost

This is not a preference. The KasmVNC client will not negotiate **any** video
codec — NVENC, VAAPI, or even software H.264 — outside a *secure context*,
because it gates them on WebCodecs:

```js
if (!("VideoDecoder" in window)) return "WebCodecs API not available";
```

Browsers expose WebCodecs only over `https://` or to `localhost`. A viewer on
`http://<lan-ip>:8080` therefore reports no decodable codecs, silently settles on
JPEG/WebP, and the GPU encoder never runs however the server is configured. The
origin alone decides it, for the same browser and the same viewer URL:

| Origin | Negotiated encoder | NVENC sessions |
|---|---|---|
| `http://<lan-ip>:8080` | `-1025` JPEG/WebP | 0 |
| `https://<lan-ip>:8443` or a tunnel | `-1029` h264_nvenc | 1 |

The certificate is generated on first boot and kept in `/data/tls`, so it
survives restarts and the browser exception you grant stays valid. Put the
address you actually connect to in the SAN list — the container cannot discover
the host's LAN address by itself:

```bash
TLS_SANS=192.168.1.50,manager.lan docker compose up -d
```

Delete `/data/tls` to re-issue. Being self-signed, the browser still shows a
one-time "unknown issuer" warning to click through.

### Cloudflare Tunnel (real certificate, no open ports)

Avoids both the self-signed warning and publishing any port:

```bash
docker compose -f docker-compose.yml -f docker-compose.tunnel.yml up -d --build
docker compose logs tunnel | grep trycloudflare.com   # the public URL
```

With no `CF_TUNNEL_TOKEN` this opens a **free quick tunnel** on a random
`*.trycloudflare.com` name that is regenerated on every restart — no Cloudflare
account required. Set `CF_TUNNEL_TOKEN` to run a named tunnel instead, with a
stable hostname and ingress rules from the Cloudflare dashboard. The overlay
also drops the manager's published ports, since the tunnel reaches it over the
compose network. Combine with GPU support by listing both overlays, tunnel last:

```bash
docker compose -f docker-compose.yml -f docker-compose.nvidia.yml \
               -f docker-compose.tunnel.yml up -d --build

# or, on an Intel/AMD integrated GPU
docker compose -f docker-compose.yml -f docker-compose.igpu.yml \
               -f docker-compose.tunnel.yml up -d --build
```

The sidecar **refuses to start when `AUTH_TOKEN` is empty** — a tunnel publishes
the Manager to the internet, and this API launches browsers and proxies CDP into
them. Override with `TUNNEL_ALLOW_NO_AUTH=1` only if you genuinely want an open
endpoint.

### Keeping it private

Narrow the bindings in `docker-compose.yml` and use a tunnel:

```yaml
ports:
  - "127.0.0.1:8080:8080"
```

```bash
ssh -L 8080:localhost:8080 your-server
```

Reaching it as `http://localhost:8080` this way is also a secure context, so
hardware encoding works over an SSH tunnel without TLS.

## Authentication

By default, there is no authentication (ideal for local use). To protect the web UI and API when hosting on a network, set the `AUTH_TOKEN` environment variable:

```bash
docker run -p 8080:8080 -v cloakprofiles:/data -e AUTH_TOKEN=your-secret-token cloakhq/cloakbrowser-manager
```

Or in `docker-compose.yml`:

```yaml
environment:
  - AUTH_TOKEN=your-secret-token
```

When `AUTH_TOKEN` is set:

- The web UI shows a login page. Enter the token to unlock.
- API consumers pass the token via `Authorization: Bearer <token>` header.
- Viewer page + WebSocket connections are authorized through short-lived per-profile viewer tokens issued after login.
- The `/api/status` endpoint remains unauthenticated (for Docker healthcheck).
- **`/api/profiles/<id>/...` is a capability URL** — see below.

### Per-profile capability URLs

Every route under `/api/profiles/<id>/` authenticates on the profile id alone
and needs no token. The id is a `uuid4`, so 122 random bits, and it is the
credential exactly as it is in a cloud provider's presigned URL.

This is what makes the CDP endpoint work with every CDP client:

```bash
# no token anywhere
agent-browser connect "wss://<host>/api/profiles/<id>/cdp"
playwright.chromium.connect_over_cdp("https://<host>/api/profiles/<id>/cdp")
```

A WebSocket handshake from Playwright, Puppeteer or chrome-remote-interface
cannot carry an `Authorization` header, so a bearer token on that route means a
patched client.

The routes that would hand out ids are the ones the token still guards:

| Route | Without token |
|---|---|
| `GET /api/profiles` (list) | **401** |
| `POST /api/profiles` (create) | **401** |
| `GET /api/profiles/<id>` | 200 |
| `GET /api/profiles/<id>/cdp`, `…/status`, `…/viewer-token` | 200 |
| `GET /api/profiles/<unknown-uuid>` | 404 |
| `GET /api/profiles/not-a-uuid` | **401** |

Treat the URL as the secret it is:

- The id grants **full control of that profile** — CDP is arbitrary code
  execution in the browser, plus its cookies and storage — and it also covers
  `DELETE`.
- It does not expire and cannot be revoked without deleting the profile.
- nginx redacts it from the access log (`/api/profiles/_/…`), as it does viewer
  tokens. nginx's *error* log format is not configurable, so a request that
  errors can still record the full path there.
- Anything that sees your URLs sees the credential: browser history, `Referer`,
  shared terminal scrollback, a pasted curl command.

Cross-origin WebSocket connections are refused regardless (`Origin` must match
`Host`), so a hostile page cannot drive the CDP socket even knowing the id.

> **Note**: The auth token is transmitted in cleartext over HTTP. If you expose the Manager to the internet, put it behind a reverse proxy with HTTPS (Caddy, nginx, Traefik).

## License

- **This application** (GUI source code) — MIT. See [LICENSE](LICENSE).
- **CloakBrowser binary** (compiled Chromium) — free to use, no redistribution. See [BINARY-LICENSE.md](BINARY-LICENSE.md).

The GUI application requires the CloakBrowser Chromium binary to function. The binary is automatically downloaded on first launch and is governed by its own license terms. If you fork or redistribute this application, your users must comply with the [CloakBrowser Binary License](BINARY-LICENSE.md).

The Docker images also install third-party components (KasmVNC, cloudflared, Microsoft core fonts) under their own licenses — see [ACKNOWLEDGEMENTS.md](ACKNOWLEDGEMENTS.md).

## Contributing

Contributions are welcome. Please [open an issue](https://github.com/CloakHQ/CloakBrowser-Manager/issues) first to discuss what you'd like to change.

## Links

- **CloakBrowser** — [github.com/CloakHQ/CloakBrowser](https://github.com/CloakHQ/CloakBrowser)
- **Website** — [cloakbrowser.dev](https://cloakbrowser.dev)
- **Bug reports** — [GitHub Issues](https://github.com/CloakHQ/CloakBrowser-Manager/issues)
- **Contact** — cloakhq@pm.me
