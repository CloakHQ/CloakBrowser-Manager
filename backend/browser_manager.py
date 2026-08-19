"""Launch/stop/track CloakBrowser instances per profile."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cloakbrowser import launch_persistent_context_async

from .runtime import RuntimeConfig, resolve_runtime
from .vnc_manager import VNCManager

logger = logging.getLogger("cloakbrowser.manager.browser")


def _normalize_proxy(raw: str) -> str:
    """Convert common proxy formats to http://user:pass@host:port.

    Accepts:
      - http://user:pass@host:port  (already valid)
      - host:port:user:pass
      - host:port
    """
    if raw.startswith(("http://", "https://", "socks5://")):
        return raw
    parts = raw.split(":")
    if len(parts) == 4:
        host, port, user, passwd = parts
        return f"http://{user}:{passwd}@{host}:{port}"
    if len(parts) == 2:
        return f"http://{raw}"
    return raw


def _validate_proxy(url: str) -> None:
    """Validate that a normalized proxy URL has scheme, host, and port."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https", "socks5"):
        raise ValueError(
            f"Invalid proxy scheme '{parsed.scheme}'. Must be http, https, or socks5."
        )
    if not parsed.hostname:
        raise ValueError(f"Proxy URL missing hostname: {url}")
    if not parsed.port:
        raise ValueError(f"Proxy URL missing port: {url}")


async def test_proxy(raw_proxy: str) -> dict[str, Any]:
    """Connect through a proxy, return exit IP + geo + latency (or an error).

    Reuses the same normalize/validate as launch, then resolves the exit IP
    via cloakbrowser's geoip echo services. Blocking work runs off-thread.
    """
    proxy = _normalize_proxy(raw_proxy)
    _validate_proxy(proxy)  # raises ValueError on bad format -> 400 in the route
    return await asyncio.to_thread(_test_proxy_sync, proxy)


def _test_proxy_sync(proxy: str) -> dict[str, Any]:
    from cloakbrowser.geoip import _ensure_geoip_db, resolve_proxy_exit_ip

    t0 = time.monotonic()
    try:
        ip = resolve_proxy_exit_ip(proxy)
    except Exception as exc:  # SOCKS w/o socksio, connection refused, etc.
        logger.warning("Proxy test failed: %s", exc)
        return {"ok": False, "error": "Could not connect through proxy"}
    latency_ms = round((time.monotonic() - t0) * 1000)
    if not ip:
        return {"ok": False, "error": "Proxy did not return an exit IP (timeout or blocked)"}
    country = city = timezone = None
    try:  # geo is best-effort; never fails the test
        import geoip2.database

        with geoip2.database.Reader(_ensure_geoip_db()) as reader:
            resp = reader.city(ip)
            country, city = resp.country.iso_code, resp.city.name
            timezone = resp.location.time_zone
    except Exception as exc:
        logger.debug("Proxy test geo lookup failed for %s: %s", ip, exc)
    return {
        "ok": True,
        "ip": ip,
        "country": country,
        "city": city,
        "timezone": timezone,
        "latency_ms": latency_ms,
    }


def _init_profile_defaults(user_data_dir: Path) -> None:
    """Set up bookmarks and DuckDuckGo search on first launch."""
    default_dir = user_data_dir / "Default"
    default_dir.mkdir(parents=True, exist_ok=True)

    # --- Bookmarks (only on first launch) ---
    bookmarks_path = default_dir / "Bookmarks"
    if not bookmarks_path.exists():
        ts = str(int(time.time() * 1_000_000))  # Chrome timestamp format
        _id = 1

        def bm(name: str, url: str) -> dict:
            nonlocal _id
            _id += 1
            return {"type": "url", "id": str(_id), "name": name, "url": url, "date_added": ts}

        def folder(name: str, children: list) -> dict:
            nonlocal _id
            _id += 1
            return {"type": "folder", "id": str(_id), "name": name, "children": children, "date_added": ts, "date_modified": ts}

        bookmarks = {
            "checksum": "",
            "roots": {
                "bookmark_bar": {
                    "type": "folder", "id": "1", "name": "Bookmarks bar",
                    "date_added": ts, "date_modified": ts,
                    "children": [
                        folder("Detection Tests", [
                            bm("Rebrowser Bot Detector", "https://bot-detector.rebrowser.net/"),
                            bm("Incolumitas", "https://bot.incolumitas.com/"),
                            bm("SannySort", "https://bot.sannysoft.com/"),
                            bm("BrowserScan Bot", "https://www.browserscan.net/bot-detection"),
                            bm("FingerprintJS Demo", "https://demo.fingerprint.com/web-scraping"),
                            bm("Pixelscan", "https://pixelscan.net/fingerprint-check"),
                            bm("CreepJS", "https://abrahamjuliot.github.io/creepjs/"),
                            bm("fingerprint-scan", "https://fingerprint-scan.com/"),
                            bm("DeviceInfo Bot", "https://deviceandbrowserinfo.com/are_you_a_bot"),
                        ]),
                        folder("Fingerprint", [
                            bm("BrowserLeaks Canvas", "https://browserleaks.com/canvas"),
                            bm("BrowserLeaks WebGL", "https://browserleaks.com/webgl"),
                            bm("BrowserLeaks Fonts", "https://browserleaks.com/fonts"),
                            bm("BrowserLeaks JS", "https://browserleaks.com/javascript"),
                            bm("FingerprintJS OSS", "https://fingerprintjs.github.io/fingerprintjs/"),
                            bm("Audio FP", "https://audiofingerprint.openwpm.com/"),
                            bm("DeviceInfo", "https://deviceandbrowserinfo.com/info_device"),
                        ]),
                        folder("Headers & TLS", [
                            bm("httpbin headers", "https://httpbin.org/headers"),
                            bm("httpbin IP", "https://httpbin.org/ip"),
                            bm("TLS Fingerprint", "https://tls.browserleaks.com/"),
                        ]),
                        folder("reCAPTCHA", [
                            bm("Google v3 Demo", "https://recaptcha-demo.appspot.com/recaptcha-v3-request-scores.php"),
                            bm("2captcha v3", "https://2captcha.com/demo/recaptcha-v3"),
                            bm("Turnstile", "https://peet.ws/turnstile-test/non-interactive.html"),
                        ]),
                    ],
                },
                "other": {"type": "folder", "id": "2", "name": "Other bookmarks", "children": []},
                "synced": {"type": "folder", "id": "3", "name": "Mobile bookmarks", "children": []},
            },
            "version": 1,
        }
        bookmarks_path.write_text(json.dumps(bookmarks, indent=2))
        logger.info("Created default bookmarks for %s", user_data_dir.name)

    # NOTE: the default search engine is NOT set here. The binary is built
    # de-Googled (prepopulated default is "No Search"), and writing
    # default_search_provider_data into Default/Preferences does NOT take — the
    # authoritative value lives in the MAC-protected Default/Secure Preferences,
    # rebuilt from the prepopulated set on every startup. Setting Google as the
    # default requires a live TemplateURLService commit, done once per profile in
    # BrowserManager._ensure_search_engine() (see that method).


CDP_START_ATTEMPTS = 3
CDP_READY_TIMEOUT = 10.0

# Periodic browser preview capture (shown in the edit view of a stopped profile).
# Captured every interval while running + once on stop; written to the profile's
# own user_data_dir so it's removed with the profile.
SCREENSHOT_FILENAME = "last_screenshot.jpg"
SCREENSHOT_INTERVAL = 30.0
SCREENSHOT_JPEG_QUALITY = 55

# One-time default-search-engine setup (see _ensure_search_engine).
SEARCH_ENGINE_MARKER = ".cloak_search_engine"
SEARCH_ENGINE_MAX_ATTEMPTS = 3
SEARCH_ENGINE_NAME = "Google"
SEARCH_ENGINE_KEYWORD = "google.com"
SEARCH_ENGINE_URL = "https://www.google.com/search?q=%s"

# Read the live default search engine straight from the settings WebUI backend.
_ACTIVE_DEFAULT_JS = """async () => {
  const cr = await import('chrome://resources/js/cr.js');
  const l = await cr.sendWithPromise('getSearchEnginesList');
  const a = (l.defaults || []).find(e => e.default);
  return a ? {name: a.name, keyword: a.keyword} : null;
}"""
# Click the "Add" button that opens the add-search-engine dialog. We can't seed a
# search engine by writing files: a hand-written Web Data row carries an invalid
# url_hash (an HMAC we can't forge) and Chrome deletes it on load (strictly so on
# Windows). Only Chrome may create the row, so we drive the real Add dialog.
_CLICK_ADD_JS = """() => {
  let clicked = false;
  const walk = (root) => root.querySelectorAll('*').forEach(el => {
    if (el.shadowRoot) walk(el.shadowRoot);
    if (el.tagName === 'CR-BUTTON'
        && /^Add$/i.test((el.textContent || '').trim())
        && /Add Site Search/i.test(el.getAttribute('aria-label') || '')) {
      el.click(); clicked = true;
    }
  });
  walk(document);
  return clicked;
}"""
# Whether the dialog's Add action button is enabled (all fields validated).
_ADD_ENABLED_JS = """() => {
  let enabled = false;
  const walk = (root) => root.querySelectorAll('*').forEach(el => {
    if (el.shadowRoot) walk(el.shadowRoot);
    if (el.tagName === 'CR-BUTTON'
        && /^Add$/i.test((el.textContent || '').trim())
        && el.closest('cr-dialog')) enabled = !el.disabled;
  });
  walk(document);
  return enabled;
}"""
# Click the (enabled) dialog Add button to commit the new engine.
_SUBMIT_ADD_JS = """() => {
  let clicked = false;
  const walk = (root) => root.querySelectorAll('*').forEach(el => {
    if (el.shadowRoot) walk(el.shadowRoot);
    if (el.tagName === 'CR-BUTTON'
        && /^Add$/i.test((el.textContent || '').trim())
        && el.closest('cr-dialog') && !el.disabled) { el.click(); clicked = true; }
  });
  walk(document);
  return clicked;
}"""
# Open Google's "More actions" menu, then click "Make default". Exact aria-label
# match so we don't hit the "Google AI Mode" starter-pack entry. Playwright
# locators don't pierce this page's nested shadow DOM reliably, so we walk it.
_MAKE_GOOGLE_DEFAULT_JS = """() => {
  const walk = (root, fn) => root.querySelectorAll('*').forEach(el => {
    if (el.shadowRoot) walk(el.shadowRoot, fn);
    fn(el);
  });
  walk(document, el => {
    const label = ((el.getAttribute && el.getAttribute('aria-label')) || '').trim();
    if (el.tagName === 'CR-ICON-BUTTON' && label === 'More actions for Google') el.click();
  });
  return new Promise(resolve => setTimeout(() => {
    let clicked = false;
    walk(document, el => {
      if (el.tagName === 'BUTTON'
          && /^Make default$/i.test((el.textContent || '').trim())
          && !el.disabled) { el.click(); clicked = true; }
    });
    resolve(clicked);
  }, 400));
}"""


@dataclass
class RunningProfile:
    profile_id: str
    context: Any  # Playwright BrowserContext
    cdp_port: int
    display: int | None = None
    ws_port: int | None = None
    user_data_dir: Path | None = None
    screenshot_task: Any = None  # asyncio.Task for the periodic screenshot loop
    capture_preview: bool = True


class BrowserManager:
    def __init__(
        self,
        runtime_config: RuntimeConfig | None = None,
        license_key: str | None = None,
        release_channel: str | None = None,
    ):
        self.runtime = runtime_config or resolve_runtime()
        # App-wide license: passed to every launch so the wrapper downloads the
        # Pro build and injects the key the Pro binary needs to boot + take a seat.
        self.license_key = license_key
        self.release_channel = release_channel
        # Resolved at startup by resolve_binary_status(); read by GET /api/status.
        self.license_tier = "keyless"
        self.license_plan: str | None = None
        self.binary_version: str | None = None
        self.running: dict[str, RunningProfile] = {}
        self._launching: set[str] = set()  # profile IDs currently being launched
        self._initializing: set[str] = set()  # profile IDs in one-time first-launch setup
        self.vnc = VNCManager(self.runtime.viewer_mode == "vnc")
        self._lock = asyncio.Lock()
        self._cdp_ports: set[int] = set()
        self._auto_launch_task: asyncio.Task | None = None

    def resolve_binary_status(self) -> None:
        """Resolve the license tier + binary version and pre-download the binary.

        Blocking — called once at startup (via a thread) so the multi-hundred-MB
        Pro download stays out of the launch path and auto-launch's 60s timeout.
        Never raises: on any failure the keyless baked-in binary remains usable.
        """
        from cloakbrowser.config import CHROMIUM_VERSION, get_chromium_version
        from cloakbrowser.download import ensure_binary
        from cloakbrowser.license import (
            get_pro_latest_version,
            resolve_license_key,
            validate_license,
        )

        try:
            keyless_version = get_chromium_version()
        except Exception:
            keyless_version = CHROMIUM_VERSION

        tier = "keyless"
        version = keyless_version

        key = resolve_license_key(self.license_key)
        if key:
            try:
                info = validate_license(key)
            except Exception as exc:
                info = None
                logger.warning("License validation failed: %s", exc)
            if info and info.valid:
                tier = "free" if info.plan == "free" else "pro"
                self.license_plan = info.plan
                try:
                    version = get_pro_latest_version(self.release_channel) or keyless_version
                except Exception as exc:
                    logger.warning("Could not resolve Pro version: %s", exc)

        self.license_tier = tier
        self.binary_version = version

        try:
            ensure_binary(
                license_key=self.license_key,
                release_channel=self.release_channel,
            )
            logger.info("Binary ready: tier=%s version=%s", tier, version)
        except Exception as exc:
            logger.error(
                "Binary pre-download failed (keyless fallback remains): %s",
                exc,
                exc_info=True,
            )

    async def launch(self, profile: dict[str, Any]) -> RunningProfile:
        """Launch a browser instance using the configured host runtime."""
        profile_id = profile["id"]

        # Per-launch context so any launch failure carries the inputs that
        # produced it. Proxy credentials are redacted; never log the key.
        from . import diagnostics

        logger.info(
            "Launching profile %s: seed=%s proxy=%s tier=%s plan=%s runtime=%s",
            profile_id,
            profile.get("fingerprint_seed"),
            diagnostics.redact_proxy(profile.get("proxy")),
            self.license_tier,
            self.license_plan or "-",
            self.runtime.runtime_mode,
        )

        async with self._lock:
            if profile_id in self.running or profile_id in self._launching:
                raise RuntimeError(f"Profile {profile_id} is already running")
            self._launching.add(profile_id)

        display: int | None = None
        ws_port: int | None = None
        cdp_port: int | None = None
        context: Any | None = None
        try:
            if self.runtime.viewer_mode == "vnc":
                display, ws_port = await self.vnc.allocate()

            user_data_dir = Path(profile["user_data_dir"])

            # Docker can leave stale locks after an unclean container exit. Native
            # mode must let Chromium arbitrate profile ownership itself.
            if self.runtime.runtime_mode == "docker":
                for lock_file in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
                    (user_data_dir / lock_file).unlink(missing_ok=True)

            _init_profile_defaults(user_data_dir)

            # One-time per profile (opt-out via set_google_default): make Google
            # the default search engine. Runs before the user-facing launch;
            # reports "initializing" via get_status while it works (one short
            # headless launch). Never fatal.
            if profile.get("set_google_default", True):
                await self._ensure_search_engine(profile_id, user_data_dir)

            if display is not None and ws_port is not None:
                await self.vnc.start_vnc(
                    display,
                    ws_port,
                    width=profile.get("screen_width", 1920),
                    height=profile.get("screen_height", 1080),
                )

            user_launch_args = profile.get("launch_args") or []
            conflicting_debug_args = [
                arg for arg in user_launch_args
                if arg.startswith(("--remote-debugging-port", "--remote-debugging-address"))
            ]
            if conflicting_debug_args:
                raise ValueError(
                    "Manager owns remote debugging configuration; remove: "
                    + ", ".join(conflicting_debug_args)
                )

            extra_args = self._build_fingerprint_args(profile)
            extra_args += user_launch_args
            extra_args.append("--remote-debugging-address=127.0.0.1")
            # Reopen the tabs the user had open when the profile was last stopped.
            # Chrome's persistent session is saved on disk but only restored when told to.
            if profile.get("restore_session", True):
                extra_args.append("--restore-last-session")

            raw_proxy = profile.get("proxy") or None
            proxy = _normalize_proxy(raw_proxy) if raw_proxy else None
            if proxy:
                _validate_proxy(proxy)

            launch_options: dict[str, Any] = {
                "user_data_dir": profile["user_data_dir"],
                "headless": False,
                "proxy": proxy,
                "args": extra_args,
                "timezone": profile.get("timezone") or None,
                "locale": profile.get("locale") or None,
                "humanize": bool(profile.get("humanize", False)),
                "human_preset": profile.get("human_preset", "default"),
                "geoip": bool(profile.get("geoip", False)),
                "color_scheme": profile.get("color_scheme") or None,
                "extension_paths": profile.get("extension_paths") or [],
                "license_key": self.license_key,
                "release_channel": self.release_channel,
            }
            if display is not None:
                launch_options["viewport"] = {
                    "width": profile.get("screen_width", 1920),
                    "height": profile.get("screen_height", 1080) - 133,
                }
                launch_options["env"] = {**os.environ, "DISPLAY": f":{display}"}

            last_cdp_error: Exception | None = None
            for attempt in range(1, CDP_START_ATTEMPTS + 1):
                cdp_port = self._reserve_cdp_port()
                launch_options["args"] = [
                    *extra_args,
                    f"--remote-debugging-port={cdp_port}",
                ]
                try:
                    context = await launch_persistent_context_async(**launch_options)
                    await self._wait_for_cdp(cdp_port)
                    break
                except asyncio.CancelledError:
                    if context is not None:
                        await self._close_context(context, profile_id)
                    self._release_cdp_port(cdp_port)
                    context = None
                    cdp_port = None
                    raise
                except Exception as exc:
                    last_cdp_error = exc
                    if context is not None:
                        await self._close_context(context, profile_id)
                    self._release_cdp_port(cdp_port)
                    context = None
                    cdp_port = None
                    logger.warning(
                        "Browser/CDP startup attempt %d/%d failed for %s: %s",
                        attempt,
                        CDP_START_ATTEMPTS,
                        profile_id,
                        exc,
                    )
            else:
                raise RuntimeError(
                    f"Unable to start verified CDP for profile {profile_id}"
                ) from last_cdp_error

            if context is None or cdp_port is None:
                raise RuntimeError(f"Browser startup did not complete for profile {profile_id}")

            if self.runtime.viewer_mode == "vnc":
                # Capture copied text so the Manager clipboard endpoint can read it.
                clipboard_init_js = """
                    window.__clipboardText = '';
                    document.addEventListener('copy', () => {
                        const sel = window.getSelection();
                        if (sel) window.__clipboardText = sel.toString();
                    });
                    document.addEventListener('keydown', (e) => {
                        if ((e.ctrlKey || e.metaKey) && e.key === 'c' && !e.altKey && !e.shiftKey) {
                            const sel = window.getSelection();
                            if (sel && sel.toString()) window.__clipboardText = sel.toString();
                        }
                    });
                """
                await context.add_init_script(clipboard_init_js)
                for page in context.pages:
                    try:
                        await page.evaluate(clipboard_init_js)
                    except Exception as exc:
                        logger.debug("Clipboard init failed on existing page: %s", exc)

            running = RunningProfile(
                profile_id=profile_id,
                context=context,
                cdp_port=cdp_port,
                display=display,
                ws_port=ws_port,
                user_data_dir=user_data_dir,
                capture_preview=bool(profile.get("capture_preview", True)),
            )
            context.on(
                "close",
                lambda *_: asyncio.ensure_future(self._on_browser_closed(profile_id)),
            )

            async with self._lock:
                self.running[profile_id] = running
                self._launching.discard(profile_id)

            # Periodically snapshot the page so the edit view (and the native
            # running view) can show the last frame. Runs even when the browser
            # is closed from inside VNC, where an on-close capture is impossible.
            # Per-profile opt-out via capture_preview.
            if running.capture_preview:
                running.screenshot_task = asyncio.ensure_future(
                    self._screenshot_loop(profile_id)
                )

            logger.info(
                "Launched profile %s (runtime=%s, display=%s, ws_port=%s, cdp_port=%d)",
                profile_id,
                self.runtime.runtime_mode,
                f":{display}" if display is not None else "native",
                ws_port,
                cdp_port,
            )
            return running

        except BaseException:
            async with self._lock:
                self._launching.discard(profile_id)
            if context is not None:
                await self._close_context(context, profile_id)
            if cdp_port is not None:
                self._release_cdp_port(cdp_port)
            if display is not None:
                await self.vnc.stop_vnc(display)
            raise

    async def _ensure_search_engine(
        self, profile_id: str, user_data_dir: Path
    ) -> None:
        """Make Google the default search engine, once per profile.

        Marker-gated so it runs only on a profile's first launch. Reports
        "initializing" via get_status while it works. Never fatal: on failure the
        profile still launches (with the binary's de-Googled "No Search" default).

        The marker records outcome: "google" = done (skip forever); "failed:N" =
        N attempts spent. Retries up to SEARCH_ENGINE_MAX_ATTEMPTS, then gives up
        so a persistently-failing profile doesn't pay an extra headless launch
        (~5s) on every single launch, silently, forever.
        """
        marker = user_data_dir / SEARCH_ENGINE_MARKER
        state = marker.read_text().strip() if marker.exists() else ""
        if state == "google":
            return
        attempts = 0
        if state.startswith("failed:"):
            try:
                attempts = int(state.split(":", 1)[1])
            except ValueError:
                attempts = 0
            if attempts >= SEARCH_ENGINE_MAX_ATTEMPTS:
                return  # gave up earlier — stop burning launches on every start

        self._initializing.add(profile_id)
        try:
            await self._setup_google_default(user_data_dir)
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("google\n")
            logger.info(
                "Set Google as default search engine for %s", user_data_dir.name
            )
        except Exception as exc:
            attempts += 1
            try:
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text(f"failed:{attempts}\n")
            except OSError:
                pass
            log = logger.error if attempts >= SEARCH_ENGINE_MAX_ATTEMPTS else logger.warning
            log(
                "Default-search-engine setup failed for %s (attempt %d/%d, launching anyway): %s",
                user_data_dir.name,
                attempts,
                SEARCH_ENGINE_MAX_ATTEMPTS,
                exc,
            )
        finally:
            self._initializing.discard(profile_id)

    async def _setup_google_default(self, user_data_dir: Path) -> None:
        """Add Google via the settings UI, then commit it as the default.

        Single headless launch, no file seeding. A plain Preferences write can't
        set the default (the authoritative value is MAC-protected in Secure
        Preferences, rebuilt from the prepopulated set every startup), and a
        hand-written Web Data keyword row is deleted on load for its invalid,
        un-forgeable url_hash (strictly so on Windows). So we drive the real Add
        dialog — Chrome creates the row with a valid hash — then "Make default".
        Every later launch then carries Google via the profile's own files.
        """
        ctx = await self._headless_launch(user_data_dir)
        try:
            page = await ctx.new_page()
            await page.goto("chrome://settings/searchEngines")
            await asyncio.sleep(2)

            if not await page.evaluate(_CLICK_ADD_JS):
                raise RuntimeError("could not open the Add search engine dialog")
            await asyncio.sleep(1.2)

            # Real .fill() emits trusted events so the dialog's async field
            # validation runs and enables the Add button; a synthetic value-set
            # does not. Playwright pierces the cr-input's open shadow root.
            await page.locator('cr-input[label="Name"] input').first.fill(SEARCH_ENGINE_NAME)
            await page.locator('cr-input[label="Shortcut"] input').first.fill(SEARCH_ENGINE_KEYWORD)
            await page.locator('cr-input[label^="URL"] input').first.fill(SEARCH_ENGINE_URL)

            enabled = False
            for _ in range(12):
                await asyncio.sleep(0.25)
                if await page.evaluate(_ADD_ENABLED_JS):
                    enabled = True
                    break
            if not enabled:
                raise RuntimeError("Add dialog stayed disabled after fill")

            if not await page.evaluate(_SUBMIT_ADD_JS):
                raise RuntimeError("could not submit the Add dialog")
            await asyncio.sleep(1.2)

            clicked = await page.evaluate(_MAKE_GOOGLE_DEFAULT_JS)
            await asyncio.sleep(1)
            active = await page.evaluate(_ACTIVE_DEFAULT_JS)
            if not (active and active.get("keyword") == SEARCH_ENGINE_KEYWORD):
                raise RuntimeError(
                    f"make-default did not take (clicked={clicked}, active={active})"
                )
        finally:
            await self._close_context(ctx, "search-init")
        await asyncio.sleep(0.5)

    async def _headless_launch(self, user_data_dir: Path) -> Any:
        """Short headless launch used only by the one-time search-engine setup."""
        for lock_file in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            (user_data_dir / lock_file).unlink(missing_ok=True)
        return await launch_persistent_context_async(
            user_data_dir=str(user_data_dir),
            headless=True,
            license_key=self.license_key,
            release_channel=self.release_channel,
        )

    async def _capture_screenshot(self, running: RunningProfile) -> None:
        """Write a compressed JPEG of the profile's current page to disk.

        Best-effort: a live page and user_data_dir are required. Written to a
        temp file then atomically renamed so the endpoint never serves a
        half-written image. Callers wrap this; it may raise on a dead/navigating
        page.
        """
        if running.user_data_dir is None:
            return
        pages = list(running.context.pages)
        if not pages:
            return
        page = pages[-1]  # most recently opened tab ≈ what the user is viewing
        dest = running.user_data_dir / SCREENSHOT_FILENAME
        tmp = running.user_data_dir / (SCREENSHOT_FILENAME + ".tmp")
        await page.screenshot(
            path=str(tmp), type="jpeg", quality=SCREENSHOT_JPEG_QUALITY
        )
        os.replace(tmp, dest)

    async def _screenshot_loop(self, profile_id: str) -> None:
        """Capture a preview every SCREENSHOT_INTERVAL while the profile runs."""
        try:
            while self.running.get(profile_id) is not None:
                await asyncio.sleep(SCREENSHOT_INTERVAL)
                running = self.running.get(profile_id)
                if running is None:
                    break
                try:
                    await self._capture_screenshot(running)
                except Exception as exc:
                    logger.debug(
                        "Preview screenshot failed for %s: %s", profile_id, exc
                    )
        except asyncio.CancelledError:
            pass

    async def _close_context(self, context: Any, profile_id: str) -> None:
        try:
            await context.close()
        except Exception as exc:
            logger.warning("Error closing context for %s: %s", profile_id, exc)

    async def _dispose_running(
        self,
        running: RunningProfile,
        *,
        close_context: bool,
    ) -> None:
        if running.screenshot_task is not None:
            running.screenshot_task.cancel()
        if close_context:
            await self._close_context(running.context, running.profile_id)
        if running.display is not None:
            await self.vnc.stop_vnc(running.display)
        self._release_cdp_port(running.cdp_port)

    async def _on_browser_closed(self, profile_id: str):
        """Release resources after a browser crash or user-initiated close."""
        async with self._lock:
            running = self.running.pop(profile_id, None)

        if running:
            logger.info("Browser closed for profile %s, cleaning up", profile_id)
            await self._dispose_running(running, close_context=False)

    async def stop(self, profile_id: str):
        """Stop a running browser instance and release all owned resources."""
        # Pop before close so the close event observes an already-clean state.
        async with self._lock:
            running = self.running.pop(profile_id, None)

        if not running:
            return

        logger.info("Stopping profile %s", profile_id)
        # Final preview capture while the context is still alive (the on-close
        # path can't screenshot — the browser is already gone by then).
        if running.capture_preview:
            try:
                await self._capture_screenshot(running)
            except Exception as exc:
                logger.debug(
                    "Final preview screenshot failed for %s: %s", profile_id, exc
                )
        await self._dispose_running(running, close_context=True)

    def get_status(self, profile_id: str) -> dict[str, Any]:
        """Get running status and viewer capabilities for a profile."""
        running = self.running.get(profile_id)
        if running:
            state = "running"
        elif profile_id in self._initializing:
            state = "initializing"
        else:
            state = "stopped"
        status = {
            "status": state,
            "runtime_mode": self.runtime.runtime_mode,
            "viewer_mode": self.runtime.viewer_mode,
            "vnc_ws_port": running.ws_port if running else None,
            "display": (
                f":{running.display}"
                if running and running.display is not None
                else None
            ),
            "cdp_url": f"/api/profiles/{profile_id}/cdp" if running else None,
        }
        return status

    async def cleanup_all(self):
        """Stop all running profiles. Called on shutdown."""
        async with self._lock:
            profile_ids = list(self.running.keys())

        for pid in profile_ids:
            await self.stop(pid)

        if self.runtime.viewer_mode == "vnc":
            await self.vnc.cleanup_all()

    async def cleanup_stale(self):
        """Kill orphan display processes in the Docker runtime only."""
        if self.runtime.viewer_mode == "vnc":
            await self.vnc.cleanup_stale()

    async def auto_launch_all(self):
        """Launch all profiles with auto_launch=True. Called on startup."""
        from . import database as db

        profiles = db.list_profiles()
        auto_profiles = [p for p in profiles if p.get("auto_launch")]
        if not auto_profiles:
            logger.info("No profiles configured for auto-launch")
            return

        logger.info("Auto-launching %d profile(s)...", len(auto_profiles))
        for profile in auto_profiles:
            try:
                await asyncio.wait_for(self.launch(profile), timeout=60)
                logger.info("Auto-launched profile %s (%s)", profile["name"], profile["id"])
            except Exception as exc:
                logger.error(
                    "Auto-launch failed for profile %s (%s): %s",
                    profile["name"], profile["id"], exc,
                )
        logger.info("Auto-launch complete: %d running", len(self.running))

    def _reserve_cdp_port(self) -> int:
        """Reserve an OS-selected loopback port for one managed browser."""
        for _ in range(20):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind(("127.0.0.1", 0))
                port = int(sock.getsockname()[1])
            if port not in self._cdp_ports:
                self._cdp_ports.add(port)
                return port
        raise RuntimeError("Unable to reserve a unique CDP port")

    def _release_cdp_port(self, port: int) -> None:
        self._cdp_ports.discard(port)

    @staticmethod
    async def _fetch_cdp_version(port: int) -> dict[str, Any]:
        def fetch() -> dict[str, Any]:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/version",
                timeout=1,
            ) as response:
                payload = json.load(response)
            if not isinstance(payload, dict):
                raise RuntimeError("CDP version response is not an object")
            return payload

        return await asyncio.to_thread(fetch)

    async def _wait_for_cdp(
        self,
        port: int,
        timeout: float = CDP_READY_TIMEOUT,
    ) -> None:
        """Wait for and verify Chromium's debugger endpoint on the reserved port."""
        deadline = asyncio.get_running_loop().time() + timeout
        last_error: Exception | None = None
        while asyncio.get_running_loop().time() < deadline:
            try:
                version = await self._fetch_cdp_version(port)
                websocket_url = str(version.get("webSocketDebuggerUrl") or "")
                if f":{port}/" not in websocket_url:
                    raise RuntimeError(
                        f"CDP endpoint returned an unexpected debugger URL: {websocket_url!r}"
                    )
                return
            except Exception as exc:
                last_error = exc
                await asyncio.sleep(0.1)
        raise TimeoutError(f"CDP endpoint on 127.0.0.1:{port} was not ready") from last_error

    def _build_fingerprint_args(self, profile: dict[str, Any]) -> list[str]:
        """Build extra Chromium args from profile fingerprint settings."""
        # Keep --test-type until the Linux Docker/VNC no-sandbox warning path
        # is verified without it. Playwright already supplies --disable-infobars.
        args: list[str] = ["--test-type"]
        if self.runtime.viewer_mode == "vnc":
            args.append("--use-angle=swiftshader")

        seed = profile.get("fingerprint_seed")
        if seed is not None:
            args.append(f"--fingerprint={seed}")

        # Persona is always determined by the runtime, not editable profile data.
        platform = "macos" if self.runtime.host_os == "macos" else "windows"
        args.append(f"--fingerprint-platform={platform}")

        # Apple GPU models are selected automatically by the seeded macOS
        # persona. Windows vendor-family overrides are incoherent on macOS.
        gpu_family = profile.get("gpu_family", "auto")
        if self.runtime.host_os != "macos":
            if gpu_family == "nvidia":
                args.append("--fingerprint-gpu-vendor=NVIDIA")
            elif gpu_family == "intel":
                args.append("--fingerprint-gpu-vendor=Intel")

        if profile.get("allow_3p_cookies", False):
            args.append("--fingerprint-allow-3p-cookies")

        sw = profile.get("screen_width")
        sh = profile.get("screen_height")
        if sw:
            args.append(f"--fingerprint-screen-width={sw}")
        if sh:
            args.append(f"--fingerprint-screen-height={sh}")

        return args
