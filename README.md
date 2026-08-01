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
- **Per-profile settings** — fingerprint seed, proxy, timezone, locale, user agent, screen size, platform
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

- The native KasmVNC client and server come from the **same pinned 1.5.0 package** — no protocol translation anywhere (the old noVNC compatibility bridge is gone).
- Viewer access uses short-lived, per-profile opaque tokens issued by `POST /api/profiles/<id>/viewer-token`; nginx validates every viewer request (page, assets, WebSocket upgrade) through FastAPI's `/api/viewer-auth`. Kasm ports never leave loopback.
- The Manager owns a reconnect state machine (backoff + jitter, offline/visibility handling, session-status classification). A viewer disconnect never stops the browser — reconnecting returns you to the same running session.
- Encoding policy is a deliberate choice, not a default you can have both ways. KasmVNC 1.5.0 gates video-codec negotiation and client quality overrides behind the *same* switch, so the Manager exposes it as one knob (`KASM_ENCODING_POLICY`). The default keeps the server in charge — 30 FPS cap, dynamic JPEG/WebP quality, video-mode downscale under motion — and clients cannot change any of it, which also means no H.264/H.265/AV1 codec is ever negotiated. Opting into `video` enables real WebCodecs streaming and simultaneously makes every quality setting client-overridable.
- **Caveat on `video`.** `KASM_VIDEO_CODEC` narrows the *server's* encoder probe, but KasmVNC 1.5.0 offers no way to restrict what a **client** selects: a client that advertises another streaming-mode pseudo-encoding gets that encoder anyway, including software AV1 (measured at roughly 0.4s of a CPU core per 1080p keyframe, followed by a silent fallback to Tight for the rest of the session). That is why `video` is opt-in and the default policy is not.

### Headless profiles

A profile with **Headless** enabled starts no Xvnc and allocates no display or WebSocket port — there is nothing to view, so the viewer is not offered and `POST /api/profiles/<id>/viewer-token` answers `409`. Drive it over CDP instead (`/api/profiles/<id>/cdp`). Everything else — profiles, proxies, fingerprints, lifecycle — behaves identically.

### Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `AUTH_TOKEN` | *(unset = open)* | Protect the web UI + API with a token |
| `KASM_QUALITY_PRESET` | `balanced` | Encoding preset: `text`, `balanced`, `low`, `motion` |
| `KASM_ENCODING_POLICY` | `server-authoritative` | `server-authoritative`: clients cannot override encoding/quality; JPEG/WebP only, no video codec can engage. `video`: in-band H.264/H.265/AV1 WebCodecs streaming, at the cost of client-authoritative quality settings — see the caveat below. |
| `KASM_VIDEO_CODEC` | `h264` | Encoder offered under `KASM_ENCODING_POLICY=video`. One of `h264`, `h264_vaapi`, `h265`, `h265_vaapi`, `av1`, `av1_vaapi`. `auto` is refused on purpose (it lets the client pick, including software AV1). |
| `KASM_XVNC_LOG_LEVEL` | `30` | Xvnc log verbosity (0-100). Raise to `100` to see per-connection encoder decisions in `/tmp/xvnc-<display>.log`. |
| `KASM_HW3D` | `auto` | DRI3 GPU acceleration: `auto` (enable unless NVIDIA proprietary), `1` (force), `0` (disable) |
| `KASM_DRINODE` | `/dev/dri/renderD128` | GPU render node for DRI3/VAAPI |
| `KASM_RECT_THREADS` | `2` | **Inert on KasmVNC 1.5.0** — `-RectThreads` is still accepted but nothing reads it; the OpenMP loop it drove was replaced by a oneTBB arena sized to the host core count. Cap encoder CPU with the container's `--cpus`/cpuset instead. |

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

VAAPI hardware *video* encode only comes into play with `KASM_ENCODING_POLICY=video`; under the default policy the GPU accelerates screen capture only, because no video codec is negotiated.

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

The container binds to localhost only. To access from a remote server:

```bash
ssh -L 8080:localhost:8080 your-server
```

Then open `http://localhost:8080`.

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

> **Note**: The auth token is transmitted in cleartext over HTTP. If you expose the Manager to the internet, put it behind a reverse proxy with HTTPS (Caddy, nginx, Traefik).

## License

- **This application** (GUI source code) — MIT. See [LICENSE](LICENSE).
- **CloakBrowser binary** (compiled Chromium) — free to use, no redistribution. See [BINARY-LICENSE.md](BINARY-LICENSE.md).

The GUI application requires the CloakBrowser Chromium binary to function. The binary is automatically downloaded on first launch and is governed by its own license terms. If you fork or redistribute this application, your users must comply with the [CloakBrowser Binary License](BINARY-LICENSE.md).

## Contributing

Contributions are welcome. Please [open an issue](https://github.com/CloakHQ/CloakBrowser-Manager/issues) first to discuss what you'd like to change.

## Links

- **CloakBrowser** — [github.com/CloakHQ/CloakBrowser](https://github.com/CloakHQ/CloakBrowser)
- **Website** — [cloakbrowser.dev](https://cloakbrowser.dev)
- **Bug reports** — [GitHub Issues](https://github.com/CloakHQ/CloakBrowser-Manager/issues)
- **Contact** — cloakhq@pm.me
