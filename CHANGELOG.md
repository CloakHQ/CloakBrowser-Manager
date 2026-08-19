# Changelog

All notable changes to CloakBrowser Manager are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-08-19

### Added
- **Native desktop app for macOS and Windows.** Signed, notarized installers bundle the Manager and stealth Chromium binary into a single application, so end users no longer need Python, Node, git, or a build step. Runs the browser directly on the host while the existing Linux Docker/KasmVNC server mode is preserved. Run-from-source (`run.py`) stays available for developers.
- **Standalone application window.** The native app now opens in its own dedicated window (WKWebView on macOS, WebView2 on Windows) carrying the app icon, instead of a tab in your default browser. The window remembers its size and position between launches, and closing it cleanly stops the server and all running browsers. Both the packaged app and `run.py` share the same window code path.
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
