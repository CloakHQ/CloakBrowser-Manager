# Changelog

All notable changes to CloakBrowser Manager are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]
### Added
- **Profiles can use a saved provider key without copying the secret.** The profile form now has one key dropdown fed by the **Nhà cung cấp** accounts: pick **Ngẫu nhiên key còn trống** to lease a free key on every launch, pin one specific key, or choose **Nhập key thủ công** to keep the old raw-key behaviour. Saved selections store only the account and key ids; a random profile picks a key that is neither expired nor already held by another running profile, so two keys serve two concurrent browsers and a third launch gets a clear "no free key" message. The form's current/new proxy buttons work with saved selections too.
- **Provider accounts are managed in their own section.** A new **Nhà cung cấp** page stores a KiotProxy account API token or a TMProxy API key once and shows every key with its status, expiry and plan. KiotProxy keys can be extended, bought, rotated or deleted from the page (the token never appears in a response), and TMProxy keys show the provider's expiry, daily IP use and note, with an **Đổi IP** action. The profile form can now load a key from these saved accounts instead of retyping it: KiotProxy lists the account's keys and TMProxy reuses the account key. Loading a key is an explicit action that asks the local backend for that one secret (marked `no-store`); the profile still stores the key exactly as before, so launch behaviour is unchanged.
- **Each pooled key now reports its own device identity.** The license server flags several keys running on one machine as farming because they all sent the same `install_id`. The Manager derives a stable UUID per key and writes it to `~/.cloakbrowser/install_id` immediately before that key's browser starts (serialized so two launches cannot race on the file), so the server treats every key as a separate machine. The machine's own id is pinned in `~/.cloakbrowser/install_id.machine` and put back once the launch has checked in, so a keyless run — or the binary started by hand — no longer inherits the last key's id, even after a crash mid-launch. Ids never change across restarts.
- **License/session calls now leave through the profile's proxy.** The session check-in used to go direct to cloakbrowser.dev, so every key showed the host's one real IP even when the browser itself was proxied. A proxied profile now passes `--license-route=proxy` (binaries 155+), so each key checks in from its own rotating exit IP too. Where the wrapper would leave the proxy inside Playwright, the Manager supplies `--proxy-server` itself; credential-free `http://host:port` proxies are covered too, not just SOCKS5 and credentialed HTTP. The one-time search-engine setup launch rides the same proxy. Direct profiles are unchanged. `user:pass@host:port` is also accepted as a manual proxy now.
- **WebRTC can no longer leak the real IP of a proxied profile.** An HTTP proxy does not touch STUN, so a page could read the host's actual address and tie two keys to one box. Proxied profiles now pass `--fingerprint-webrtc-ip=auto`: the wrapper resolves the proxy's exit IP and reports that, and drops the flag again if the lookup fails. A `--fingerprint-webrtc-ip` the user set in the profile's launch arguments wins.
- **KiotProxy and TMProxy as proxy sources, per profile.** Instead of pasting a proxy by hand, a profile can name a provider and an API key; every launch then fetches a fresh rotating proxy from that provider (KiotProxy region, TMProxy location/ISP selectable) and uses it for the browser. KiotProxy's assignment is detached again when the profile stops. One provider key hands out one proxy at a time, so it cannot be shared by two running profiles. The provider key is a secret and is never echoed by the API — the profile only reports whether one is set and a masked hint. The form has test/fetch-now controls, and the running profile shows the exit IP, location and TTL of the proxy it got. A manual proxy still works for profiles with no provider.
- **Keys survive a crash, a restart and the server's own grace window.** The license server keeps counting a free key's session for four minutes after the browser dies (15 on team, 30 on business), so the pool no longer hands that key straight back: a crashed, force-closed or failed launch puts the key on a cool-down for its plan's window, and the pool asks the license server — a read-only lookup that costs no seat — to release it early the moment the seat is really free. Leases are now stored in `keys.json` beside the pool, so a Manager restart no longer re-books every key the last run was using; startup cools those keys down and reconciles them with the server. **Settings → Key pool** shows `cooling down` for such a key and refuses to delete it until the window passes; a launch refused because every key is cooling says how long it still has to wait.
- **License key pool: one key per concurrent profile.** The Manager used to hold a single app-wide key, so a free plan allowed one running profile and every second launch was denied by the license server. **Settings → Key pool** now holds as many keys as you have accounts: each launch leases one key, and a stopped profile hands its key straight back, so a small pool can rotate through many profiles as long as they are not all running at once. Keys are validated on add and can be re-validated with **Validate all**; a key the binary rejects (exit 77) is marked invalid and drops out of rotation, while an out-of-seats denial (76) leaves the key healthy. The launch is refused *before* the browser starts when every key is leased, with the same "session limit reached" message the server would have produced. A profile can pin one pooled key, which reserves it so a first-come launch cannot consume it. The raw key never leaves the server: `GET /api/keys` returns a masked form and a short id, and `POST /api/keys`, `DELETE /api/keys/{id}` and `POST /api/keys/check` manage the pool. The one-key-one-concurrent-browser seat limit is unchanged — pooling your own keys is not a seat bypass.
- **Assign one proxy to a whole selection.** The Proxy page gained a search box (name or exit) and bulk selection, and a dialog applies a manual proxy — or clears the profiles back to a direct connection — for every selected profile at once, checking the address before it writes. The page also shows how many profiles share each exit.
- **Light and dark themes.** A theme switch in **Settings → Giao diện** offers system, light and dark. The palette resolves before the bundle loads, so there is no flash on start, and follows the OS setting live while "system" is selected.

### Changed
- **The key pool is now the only place a key lives; the app-wide "shared key" setting is gone.** Having a pooled key and a separate single key was two sources of truth: the Settings field described a fallback that the pool quietly shadowed, and the launch path had a second branch for it. Keys now enter through the pool (Settings → Key pool) or through `CLOAKBROWSER_LICENSE_KEY`/`~/.cloakbrowser/license.key`, which are seeded into the pool at startup — so Docker/CI keep working and a key from an old `settings.json` is carried over automatically on first start. The license page drops the shared-key box and its stat card; pool size and plan remain.
- **Transparent proxy no longer demands a username and password.** Providers that authenticate by IP (KiotProxy's `authMode: ip`, and any IP-allowlisted proxy) hand out a bare `http://host:port`, which the old check refused with "needs an http:// proxy with a username and password". Credentials are now optional; a SOCKS5 or `https://` proxy is still rejected, since transparent mode only speaks HTTP.
- **Renaming a profile renames its folder with it.** Browsers are stored in a folder named after the profile, so a rename now moves the folder too and the row follows, keeping the disk readable. A UUID-era path or one moved by hand keeps its location. Renaming while the browser runs is refused (409) so the open profile cannot be torn out from under it, and a second profile takes a ` (2)` suffix instead of colliding.
- **A provider hold can be inspected without retyping the key.** The provider section of the profile form gained **Kiểm tra proxy hiện tại**, which reads the proxy the stored key is holding (exit IP, location, TTL) server-side; the key itself still never leaves the process.
- **The UI is now a sectioned app shell instead of one settings modal.** A persistent left rail holds four sections — Tổng quan, Proxy, Bản quyền, Cài đặt — plus a plan card that always shows the current tier and running/total profiles. Overview keeps the profile table (per-row launch/stop, and a menu for config, duplicate, move up/down and delete), status and text filters, and a recent-activity column; Proxy lists where each profile's traffic exits; Bản quyền owns the shared key and the key pool; Cài đặt holds the release channel, environment details and session controls. The top bar carries a breadcrumb, a viewer/config switch for the selected running profile, and the system-status badge. The plan card and license page surface the one-key-per-running-profile rule; nothing here can raise the seat limit.

## [0.2.0] - 2026-11-01

### Changed
- **Bundles the CloakBrowser 0.7.0 engine: a license key is now required to launch profiles.** A free key takes one email and runs the latest build. Without a key, the Manager opens on a "No license key set" screen with links to enter a key in Settings or get a free one, the top bar shows "No key", and launching a profile shows the free-key link. Saving a key in Settings takes effect right away, no restart needed.
- **The Docker image no longer contains a browser build.** The latest build downloads on the first start with a key.

## [0.1.8] - 2026-10-14

### Added
- **Transparent proxy and portable cookies, per profile.** Two new checkboxes under Compatibility, both off by default. **Transparent proxy connection** turns on the browser's stronger proxy connection mode. It needs one `http://` proxy with a username and password, so any other proxy is refused at launch with a clear message (HTTP 400) instead of starting a browser with no network. **Portable cookies** lets a profile move to another machine with its logins. Only cookies saved while it is on are portable, so turn it on before logging in. Existing profiles get both settings switched off, and duplicating a profile copies them.

### Changed
- **Bundles the CloakBrowser 0.6.3 engine.** Humanized typing, mouse moves and scrolling follow measured human timing, humanized clicks reach popups inside scrolling panels, and humanized actions work on Playwright 1.64. A proxy exit in a country with no mapped language no longer fails the launch.

## [0.1.7] - 2026-10-11

### Fixed
- **Profiles stayed stopped after booting without internet.** On a host with no internet uplink (e.g. after a power cut), the display server exited at startup, so auto-launched profiles never came up. It now starts offline too.

### Changed
- **Bundles the CloakBrowser 0.6.1 engine.** Profiles with humanize on now humanize the page they open with; on 0.6.0 that first page could run without it.

## [0.1.6] - 2026-10-08

### Added
- **Duplicate a profile together with its browser state.** `POST /api/profiles/{id}/duplicate` now accepts `{"include_browser_state": true}`, which copies the source profile's cookies, logged-in sessions, history and local storage into the clone alongside its settings and fingerprint — so the copy launches as the same identity *and* the same session. The source is held stopped for the whole copy — a launch, reset or delete of it is refused with 409 until the copy finishes, and a source that is still launching or closing is refused too — and the clone only appears in the list once its directory is complete, so a half-built clone can never be launched or deleted. Chromium's single-instance lock files and the source's preview frame are left behind; a failed copy leaves nothing behind. Reset and Delete now also refuse (409) a profile whose browser is still launching or closing, instead of touching a directory Chrome is using, and Launch refuses (409) a profile whose previous browser is still closing instead of starting a second Chrome on the same directory. In the profile editor, **Duplicate** becomes a split button: the button itself still makes a config-only copy, and its menu offers **With browser state** (disabled until the profile is stopped). Without the flag the endpoint behaves exactly as before.
- **Send files to a profile, and keep what it downloads.** A profile now has files of its own: `POST /api/profiles/{id}/files` uploads one, `GET` lists them or fetches the bytes, `DELETE` removes one. Under Docker the browser cannot see your filesystem, so an upload also appears in the guest's own file dialog under **Uploads — <profile>**, letting a website's "Choose file" button reach it, and files the browser downloads are captured automatically instead of being trapped in the container — published only once the transfer completes, so a half-written download is never served. The live viewer gains a files button: upload, drop a file onto the screen, fetch one back, or remove one. Files are stored beside the profile rather than inside it, so duplicating a profile's browser state never copies its documents, and deleting a profile deletes them with it. Per-profile limits are configurable (`ARTIFACT_MAX_BYTES`, `ARTIFACT_MAX_COUNT`, `ARTIFACT_MAX_TOTAL_BYTES`). On the native macOS and Windows builds the browser already shares your filesystem and its own Downloads folder, so download capture and the sidebar shortcut do not apply there.

### Changed
- **Bundles the CloakBrowser 0.6.0 engine.**

## [0.1.5] - 2026-08-30

### Fixed
- **Profiles failed to launch on some Windows systems with a "Failed to launch browser" error.** On a Windows console using a non-Unicode (legacy) codepage, a status message printed while fetching the stealth binary could abort every launch. This build bundles the updated CloakBrowser engine (0.5.10), which makes that output safe so the launch always proceeds.

## [0.1.4] - 2026-08-21

### Fixed
- **Memory leak when a browser closed on its own.** If a browser exited outside the Stop button (a crash, or closing Chrome from inside the profile window), a helper process was left running and holding around 130 MB. These built up over time on a long-running manager. The manager now releases it on that path as well.

### Added
- **License and seat limits now surface in a banner.** When a profile fails to launch because the plan's concurrent-session limit is reached or the license key is invalid, the Manager shows a dismissible top banner with an Upgrade link instead of failing silently.
- **Automated multi-architecture Docker releases.** Version tags now build Linux AMD64 and ARM64 images on native GitHub-hosted runners, combine them under one Docker tag, and publish both the release version and `latest` alongside the Windows and macOS installers.

### Changed
- **Safer release validation.** Docker architecture digests, provenance, and the combined manifest are validated before publication, and `latest` is promoted only from a complete verified release. Release tags are validated as canonical semantic versions, and workflow dependencies are pinned to immutable revisions.

## [0.1.3] - 2026-08-19

### Fixed
- **macOS app failed to launch profiles on a fresh machine.** On a Mac that had never run CloakBrowser before, launching a profile with a license key set could fail with an internal library-loading error while the app fetched the stealth binary. The macOS build now bundles a self-contained TLS library, so the download works on a clean machine and profiles launch as expected. (Intel and Apple Silicon.)

### Changed
- **Dropped a redundant browser launch flag.** The Manager no longer passes an extra Chromium flag at launch; the CloakBrowser engine already handles that behavior internally, so the flag added nothing.

## [0.1.2] - 2026-08-19

### Fixed
- **First launch on a fresh Windows machine no longer fails.** The packaged app has no console, so a status message printed during the first stealth-binary download could abort the launch on some systems. Startup output is now handled safely and the launch proceeds.

### Added
- **Update notification.** When a newer Manager release is available, a dismissible banner appears at the top of the app; clicking Download opens the release page in your default browser. The check is fail-soft and cached, so it never delays or interferes with normal use.
- **Richer diagnostics in `manager.log`.** Startup now records a one-line environment fingerprint (app version, OS, architecture, packaged-vs-source, license tier/plan, binary version, data directory). Uncaught errors from any background thread or task, and each profile launch's context (with proxy credentials redacted), are now logged with full detail, and output from the underlying CloakBrowser engine is mirrored into the log. This makes crash reports diagnosable from the log alone.

## [0.1.1] - 2026-08-19

### Added
- **Restore previous tabs on launch.** Each profile can reopen the tabs that were open when it was last stopped. Enabled by default; toggle it off per profile in the Behavior section.
- **Clone / duplicate a profile.** Copy an existing profile's full configuration and fingerprint into a fresh profile with its own `user_data_dir`, ready to launch independently.
- **Proxy test button.** Test a profile's proxy from the form and see the exit IP, geolocation, and latency before launching.
- **Last-browser-screenshot preview.** The profile shows a preview image captured from its last browser session.
- **Drag-and-drop reordering** of the profile list.
- **Profile reset.** Wipe a profile's browser state and re-roll its fingerprint in one action.
- **Transient confirmation on form buttons.** Save and Reset now show an inline "Saved" / "Reset done" confirmation.

### Changed
- README: restored the "Browser Profile Manager" headline and tightened the tagline.

## [0.1.0] - 2026-08-19

### Added
- **Native desktop app for macOS and Windows.** Installers bundle the Manager and stealth Chromium binary into a single application, so end users no longer need Python, Node, git, or a build step. Runs the browser directly on the host while the existing Linux Docker/KasmVNC server mode is preserved. Run-from-source (`run.py`) stays available for developers. The builds are unsigned for now — on first launch macOS needs a one-time Gatekeeper approval and Windows a SmartScreen click-through (see the README).
- **Standalone application window.** The native app now opens in its own dedicated window (WKWebView on macOS, WebView2 on Windows) carrying the app icon, instead of a tab in your default browser. The window remembers its size and position between launches, and closing it cleanly stops the server and all running browsers. Relaunching the app focuses the existing window instead of starting a second copy. Both the packaged app and `run.py` share the same window code path.
- **In-app Settings panel.** A gear-icon panel lets you set the CloakBrowser Pro license key and release channel from inside the app; changes are hot-applied with no restart.
- **CloakBrowser Pro licensing wired app-wide.** A license key and release channel configured once (native Settings, or a `.env` for server mode) are passed to every profile launch so the Pro stealth binary is used. The binary is resolved and pre-downloaded at startup, keeping it off the launch path.
- **License tier and binary-version status badge** in the top bar, reporting the active tier and the real Chromium binary version.
- **Keyless empty-state prompt.** When no license key is set, the empty view shows a "No license key set" call-to-action with links to enter a key, get a free key, or view Pro plans, instead of the generic "Select a profile" text.
- **Quit / Power control.** A Power button cleanly stops the server and all running browsers and exits; the shutdown endpoint is same-origin (CSRF) guarded so no website can trigger it.
- **Unauthenticated `/api/health` probe** returning only `{"status": "ok"}` with no system details, for health checks.
- **Third-party cookie compatibility control** per profile (defaults on for new profiles).
- **Google set as the default search engine** for new profiles on first launch, with an opt-out toggle.
- GeoIP enabled by default for new profiles.

### Changed
- **`/api/status` now requires authentication.** It previously leaked running-session count, binary version, and profile totals to unauthenticated scanners; health checks now use the new `/api/health` probe instead.
- **Simplified profile configuration.** Removed obsolete override fields and moved unrestricted Chromium arguments under an Advanced section. Existing profiles are migrated automatically to the new schema.
- **Clipboard sync is now limited to the Linux VNC mode.** Clipboard controls are hidden and injection is skipped in the native macOS and Windows apps.
- Per-profile clipboard preferences are now persisted.

### Fixed
- Native launcher readiness poll now targets `/api/health`, fixing a startup hang where the app never opened the browser when an auth token was set.
