<p align="center">
<img src="https://i.imgur.com/cqkp6fG.png" width="500" alt="CloakBrowser">
</p>

<h3 align="center">Browser Profile Manager for CloakBrowser</h3>

<p align="center">
A self-hosted browser for managing unlimited accounts and profiles,<br>
each one a genuinely separate machine: its own fingerprint, GPU, proxy, cookies, and history.<br>
Powered by CloakBrowser, the stealth engine that passes Cloudflare Turnstile, reCAPTCHA v3, FingerprintJS and BrowserScan.<br>
The identities don't just look different. They hold up.
</p>

<p align="center">
Self-hosted alternative to Multilogin, GoLogin, and AdsPower.<br>
Start free with one concurrent browser, scale to more on a paid plan.
</p>

<p align="center">
<a href="https://github.com/CloakHQ/CloakBrowser"><img src="https://img.shields.io/github/stars/cloakhq/cloakbrowser?label=CloakBrowser" alt="Stars"></a>
<a href="https://hub.docker.com/r/cloakhq/cloakbrowser-manager"><img src="https://img.shields.io/docker/pulls/cloakhq/cloakbrowser-manager?label=docker&logo=docker&logoColor=white" alt="Docker Pulls"></a>
<a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="License"></a>
</p>

---

<p align="center">
<img src="https://raw.githubusercontent.com/CloakHQ/CloakBrowser-Manager/main/assets/manager-macos.png" width="800" alt="CloakBrowser Manager on macOS">
</p>

Open a profile and you don't get a new tab, you get a different computer: its own browser fingerprint, GPU, screen, timezone, proxy, cookies, and history. Nothing bleeds between profiles, so your accounts never link back to each other, or to you. Close a profile and reopen it next day, it's the same person, warmed up and ready.

Windows and macOS launch browsers directly in native desktop windows; Linux keeps the Docker/KasmVNC server experience.

### Windows and macOS

Download the installer from the [latest release](https://github.com/CloakHQ/CloakBrowser-Manager/releases) and run it:

- **macOS** — open the `.dmg` and drag **CloakBrowser Manager** into Applications. (Unsigned during early access — on first launch, run `xattr -rc "/Applications/CloakBrowser Manager.app"` in Terminal, or approve it under **System Settings → Privacy & Security → Open Anyway**.)
- **Windows** — run the setup `.exe`. (Unsigned during early access — if SmartScreen warns, click **More info → Run anyway**.)

No Python, Node, or git required. The Manager starts on `127.0.0.1:8080` and opens in your default browser. On first launch it downloads the CloakBrowser engine. Profiles are stored in `%LOCALAPPDATA%\CloakBrowser Manager` on Windows and `~/Library/Application Support/CloakBrowser Manager` on macOS; a `logs/manager.log` in that folder records what happened if you need it.

Open **Settings** (gear icon, top right) to add your license key and pick the Stable or Preview channel.

#### Run from source (developers)

```bash
git clone https://github.com/CloakHQ/CloakBrowser-Manager.git
cd CloakBrowser-Manager
./run-macos.sh      # macOS  (or run-windows.bat on Windows)
```

This path requires Python 3.10+ and Node 18+; the first run creates a local Python environment, installs dependencies, and builds the React UI before starting the Manager.

### Linux server

```bash
docker run -p 127.0.0.1:8080:8080 -v cloakprofiles:/data cloakhq/cloakbrowser-manager
```

Or build from source:

```bash
git clone https://github.com/CloakHQ/CloakBrowser-Manager.git
cd CloakBrowser-Manager
docker compose up --build
```

Open [http://localhost:8080](http://localhost:8080), create a profile, and click Launch.

> **Early alpha** — this project is under active development. Expect bugs. If you find one, please [open an issue](https://github.com/CloakHQ/CloakBrowser-Manager/issues) and attach the log so we can help. On Windows/macOS it's `logs/manager.log` in the data folder (`%LOCALAPPDATA%\CloakBrowser Manager` / `~/Library/Application Support/CloakBrowser Manager`); on Linux/Docker use `docker logs <container>`.

## CloakBrowser license key

The Manager runs on the CloakBrowser engine, so it needs a key.<br>
[Get a free one with GitHub](https://cloakbrowser.dev/free) to run one profile at a time on the current build.<br>
[Paid plans](https://cloakbrowser.dev) raise how many profiles run at the same time, from a handful to thousands.

Add your key once and every profile uses it.

**Native app (Windows/macOS):** open **Settings** (gear icon, top right), paste your key, choose the Stable or Preview channel, and Save. It applies immediately, no restart. The badge in the top bar shows which tier and binary version are active.

**Docker or run-from-source:** set it in a manager-root `.env` instead:

```bash
cp .env.example .env
```

```bash
# .env
CLOAKBROWSER_LICENSE_KEY=cb_your_key_here
CLOAKBROWSER_RELEASE_CHANNEL=stable   # or: preview
```

The file is loaded automatically at startup. Restart the Manager after changing it. With Docker, you can also pass the key with `-e CLOAKBROWSER_LICENSE_KEY=...`. An environment variable overrides the in-app setting.

## Why Not a Cloud Anti-Detect Browser?

The popular anti-detect browsers solve the fingerprint, then hand you a new problem: every account you own, every cookie, every session, sits on someone else's servers. And the disguise increasingly doesn't survive real detection, shortcuts in how fingerprints are faked, GPU and WebGL values that don't add up, identities that pass a test page and fail the real site.

CloakBrowser Manager runs on your own machine, and every profile inherits the CloakBrowser engine, so the identities actually hold up.

| | Typical cloud anti-detect browser | **CloakBrowser Manager** |
|---|---|---|
| Pricing model | Per profile + per seat, forced up-tiering | **Flat by concurrency, unlimited profiles** |
| Where profiles live | Their cloud | **Your machine** |
| Fingerprinting | JS-injected into a stock browser | **Source-level C++ patched engine** |
| The app | Closed box | **Open-source GUI (MIT)** |
| Native desktop windows | Rare | **Windows + macOS** |
| Automation API | Add-on / higher tier | **CDP built in, every profile** |
| Cost of idle accounts | Counts against your limit | **Free** |

## Features

- **Unlimited profiles, no per-profile tax** — create as many identities as you want. You pay only for how many run at the same time, not how many you keep. Dormant accounts cost nothing.
- **Each profile is a different machine** — its own fingerprint seed, GPU family, screen, cookies, localStorage, cache, and history, persistent across restarts
- **Per-profile network and locale** — proxy, GeoIP, timezone, locale, and screen, per profile; timezone and language follow the proxy exit IP automatically
- **Platform-aware hardware profiles** — automatic Apple Silicon selection and configurable Windows GPU families, coherent within each profile
- **Profile organization** — create, search, tag, edit, auto-launch, and delete profiles
- **Platform-native browsing** — Windows and macOS profiles open in normal desktop windows
- **Linux server viewing** — interact with Docker-launched browsers through KasmVNC in the web GUI
- **Playwright/Puppeteer API** — connect to any running profile through CDP while watching the same session live
- **Humanized interaction** — optional human-like mouse, keyboard, and scrolling behavior
- **Compatibility controls** — unpacked extensions, third-party-cookie support, and advanced Chromium arguments
- **Clipboard sync** — copy and paste between the Manager and Linux VNC browser profiles
- **License and system status** — see the active tier, binary version, and Windows font health in the top bar
- **Optional authentication** — protect the web UI and API with a single token, or run locally without authentication
- **Powered by CloakBrowser** — the identities don't just look different, they hold up: a source-level C++ patched Chromium engine tested against Cloudflare Turnstile, reCAPTCHA v3, FingerprintJS, and BrowserScan

## Stack

- **Backend**: FastAPI (Python)
- **Frontend**: React + Tailwind CSS
- **Browser viewer**: native windows on Windows/macOS; noVNC/KasmVNC on Linux Docker
- **Database**: SQLite
- **Browser engine**: [CloakBrowser](https://github.com/CloakHQ/CloakBrowser) (stealth Chromium binary)

## Development

### Native backend

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt
uvicorn backend.main:app --reload --host 127.0.0.1 --port 8080
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

Linux containers present a Windows browser persona. For coherent emoji, CJK,
and canvas rendering, the image includes CloakBrowser's recommended baseline
font packages. To supply separately licensed Windows fonts, keep them outside
the repository and mount them read-only into Fontconfig's local font directory:

```yaml
services:
  manager:
    volumes:
      - ~/.cloakbrowser-manager:/data
      - /srv/cloakbrowser-manager/fonts/windows:/usr/local/share/fonts/windows:ro
```

The container refreshes the mounted directory's Fontconfig cache at startup.
After recreating it, `/api/status` and the top-bar badge report whether all
required Windows persona font families are available.

## Requirements

- Windows or macOS native: Python 3.10+, Node.js 18+
- Linux server: Docker 20.10+
- ~2 GB disk (application + browser binary)
- ~512 MB RAM per running profile

## Updating

### Windows and macOS

Pull the latest source and run the platform launcher again. It installs changed dependencies and rebuilds the interface automatically.

```bash
git pull
```

```text
Windows: run-windows.bat
macOS:   ./run-macos.sh
```

### Linux server

Pull the latest image and recreate the container:

```bash
docker pull cloakhq/cloakbrowser-manager
docker stop <container-id>
docker rm <container-id>
docker run -p 127.0.0.1:8080:8080 -v cloakprofiles:/data cloakhq/cloakbrowser-manager
```

Profiles and session data remain in the native application-data directory or the `cloakprofiles` Docker volume across updates.

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

The CDP URL is available from the running-profile view. The same browser session is accessible through its native window on Windows/macOS or through VNC on Linux Docker, and programmatically through the API on every platform.

## Remote Access

The container binds to localhost only. To access from a remote server:

```bash
ssh -L 8080:localhost:8080 your-server
```

Then open `http://localhost:8080`.

## Authentication

By default, there is no authentication (ideal for local use). To protect the web UI and API when hosting on a network, set the `AUTH_TOKEN` environment variable:

```bash
docker run -p 127.0.0.1:8080:8080 -v cloakprofiles:/data -e AUTH_TOKEN=your-secret-token cloakhq/cloakbrowser-manager
```

Or in `docker-compose.yml`:

```yaml
environment:
  - AUTH_TOKEN=your-secret-token
```

When `AUTH_TOKEN` is set:

- The web UI shows a login page. Enter the token to unlock.
- API consumers pass the token via `Authorization: Bearer <token>` header.
- VNC WebSocket connections are authenticated via the login cookie.
- The `/api/health` endpoint remains unauthenticated (for Docker healthcheck); it exposes no system details. The `/api/status` endpoint (running counts, version) now requires authentication.

> **Note**: The auth token is transmitted in cleartext over HTTP. If you expose the Manager to the internet, put it behind a reverse proxy with HTTPS (Caddy, nginx, Traefik).

## License

- **This application** (GUI source code) — MIT. See [LICENSE](LICENSE).
- **CloakBrowser binary** (compiled Chromium) — governed by version-specific subscription terms and may not be redistributed. See [BINARY-LICENSE.md](BINARY-LICENSE.md).

The GUI application requires the CloakBrowser Chromium binary to function. The binary is automatically downloaded on first launch and is governed by its own license terms. If you fork or redistribute this application, your users must comply with the [CloakBrowser Binary License](BINARY-LICENSE.md).

## Contributing

Contributions are welcome. Please [open an issue](https://github.com/CloakHQ/CloakBrowser-Manager/issues) first to discuss what you'd like to change.

### Contributors

- [lhq1363511234-arch](https://github.com/lhq1363511234-arch) — native Windows support foundation
- [quorentindupres-dev](https://github.com/quorentindupres-dev) — native macOS workflow and Manager integration concepts
- [shellus](https://github.com/shellus) — auth-gated status endpoint and unauthenticated health probe
- [hayka-pacha](https://github.com/hayka-pacha) — profile reset endpoint

## Links

- **CloakBrowser** — [github.com/CloakHQ/CloakBrowser](https://github.com/CloakHQ/CloakBrowser)
- **Website** — [cloakbrowser.dev](https://cloakbrowser.dev)
- **Bug reports** — [GitHub Issues](https://github.com/CloakHQ/CloakBrowser-Manager/issues)
- **Contact** — cloakhq@pm.me
