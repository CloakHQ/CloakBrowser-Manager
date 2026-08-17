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

Each profile is an isolated CloakBrowser instance with its own fingerprint, proxy, cookies, and session data. Profiles persist across restarts. Windows and macOS launch browsers directly in native desktop windows; Linux keeps the Docker/KasmVNC server experience.

### Windows and macOS

Clone the repository:

```bash
git clone https://github.com/CloakHQ/CloakBrowser-Manager.git
cd CloakBrowser-Manager
```

On Windows, run:

```bat
run-windows.bat
```

On macOS, run:

```bash
chmod +x run-macos.sh
./run-macos.sh
```

The first run creates a local Python environment, installs dependencies, builds the React UI, starts Manager on `127.0.0.1:8080`, and opens it in your default browser. Profiles are stored in `%LOCALAPPDATA%\CloakBrowser Manager` on Windows and `~/Library/Application Support/CloakBrowser Manager` on macOS.

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

> **Early alpha** — this project is under active development. Expect bugs. If you find one, please [open an issue](https://github.com/CloakHQ/CloakBrowser-Manager/issues).

## CloakBrowser license key

[Get a free key with GitHub](https://cloakbrowser.dev/free) to use the current CloakBrowser build with one concurrent browser session. Paid plans at [cloakbrowser.dev](https://cloakbrowser.dev) let you run more profiles at once.

Without a key, the Manager uses the older keyless build. Add your key once and every profile will use it. The key is set per Manager instance, not per profile.

After cloning, copy the example env file and fill it in:

```bash
cp .env.example .env
```

```bash
# .env
CLOAKBROWSER_LICENSE_KEY=cb_your_key_here
CLOAKBROWSER_RELEASE_CHANNEL=stable   # or: preview
```

The file is loaded automatically at startup on both native and Docker. Restart the Manager after changing it. With Docker, you can also pass the key with `-e CLOAKBROWSER_LICENSE_KEY=...`. The badge in the top bar shows which tier and binary version are active.

## Why Not Just Use a VPN?

A VPN changes your IP, while Incognito and standard Chrome profiles mainly separate local browsing data. They still expose the same underlying browser and hardware signals.

CloakBrowser Manager gives every profile a persistent, seeded browser identity alongside its own proxy, cookies, storage, and history.

| Solution | What it isolates | Browser fingerprint isolation |
|---|---|---|
| VPN | IP address | No |
| Incognito | Temporary cookies and storage | No |
| Chrome profiles | Cookies, history, and bookmarks | No |
| **CloakBrowser Manager** | **Profile data, network settings, and browser identity** | **Yes — per-profile identity** |

## Features

- **Profile organization** — create, search, tag, edit, auto-launch, and delete profiles
- **Persistent identities** — each profile keeps its fingerprint seed, cookies, localStorage, cache, and browsing history
- **Per-profile network and locale** — proxy, GeoIP, timezone, locale, and screen controls
- **Platform-aware hardware profiles** — automatic Apple Silicon selection and configurable Windows GPU families
- **Compatibility controls** — unpacked extensions, third-party-cookie support, and advanced Chromium arguments
- **Humanized interaction** — optional human-like mouse, keyboard, and scrolling behavior
- **Clipboard sync** — copy and paste between the Manager and Linux VNC browser profiles
- **Platform-native browsing** — Windows and macOS profiles open in normal desktop windows
- **Linux server viewing** — interact with Docker-launched browsers through KasmVNC in the web GUI
- **Playwright/Puppeteer API** — connect to any running profile through CDP while watching the same session live
- **License and system status** — see the active tier, binary version, and Windows font health in the top bar
- **Optional authentication** — protect the web UI and API with a single token, or run locally without authentication
- **Powered by CloakBrowser** — 71 source-level C++ patches, tested against Cloudflare Turnstile, reCAPTCHA v3, FingerprintJS, and BrowserScan

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

## Links

- **CloakBrowser** — [github.com/CloakHQ/CloakBrowser](https://github.com/CloakHQ/CloakBrowser)
- **Website** — [cloakbrowser.dev](https://cloakbrowser.dev)
- **Bug reports** — [GitHub Issues](https://github.com/CloakHQ/CloakBrowser-Manager/issues)
- **Contact** — cloakhq@pm.me
