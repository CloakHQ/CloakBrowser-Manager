"""Launch/stop/track CloakBrowser instances per profile."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import logging
import os
import socket
import sqlite3
import time
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cloakbrowser import launch_persistent_context_async
from cloakbrowser.license import (
    CloakBrowserLicenseError,
    license_error_for_code,
    read_denial_file,
)

from . import downloads
from .runtime import RuntimeConfig, resolve_runtime
from .vnc_manager import VNCManager

logger = logging.getLogger("cloakbrowser.manager.browser")


UPGRADE_URL = "https://cloakbrowser.dev/#pricing"


def is_seat_limit_error(exc: BaseException) -> bool:
    """True if a license error is specifically the concurrency-seat denial (76).

    The wrapper turns exit code 76 into a message containing "session limit
    reached"; the other license codes (invalid/expired key, server unreachable,
    local config) carry different text. Used to attach the upgrade CTA only to
    the out-of-seats case.
    """
    return "session limit reached" in str(exc).lower()


def is_cooldown_error(exc: BaseException) -> bool:
    """True when the refusal is a local cool-down wait, not a spent seat pool."""
    return "cooling down" in str(exc).lower()


def license_error_detail(exc: BaseException) -> dict[str, str]:
    """Build the structured launch-error payload the frontend renders.

    ``{message, reason, upgrade_url?}`` — reason is "seat_limit" (out of seats,
    with an upgrade CTA) or "license" (bad/expired/revoked key, server
    unreachable, local config). Shared by the synchronous launch-raises path
    (main.py) and the post-handshake close path (_on_browser_closed).
    """
    seat = is_seat_limit_error(exc)
    detail: dict[str, str] = {
        "message": str(exc),
        "reason": "seat_limit" if seat else "license",
    }
    if seat:
        detail["upgrade_url"] = UPGRADE_URL
    return detail


# The Pro binary identifies the machine by a UUID kept at ~/.cloakbrowser/install_id
# and sends it on every session/start. Left alone, every key shares one value and
# the server reads multiple keys on one box as farming. Give each key its own
# stable UUID and point the file at it right before spawning so each key looks
# like it runs on a separate machine.
INSTALL_ID_NAMESPACE = uuid.UUID("5a7d2b8e-9c41-4f0a-8b3d-2c1e6f9a0d47")
INSTALL_ID_FILENAME = "install_id"
# Sidecar holding this machine's own id. The live file is swapped per key, so
# after a crash it may still contain a derived key id; the sidecar lets us tell
# the two apart and always come back to the machine's real value.
MACHINE_INSTALL_ID_FILENAME = "install_id.machine"


def install_id_path() -> Path:
    return Path.home() / ".cloakbrowser" / INSTALL_ID_FILENAME


def machine_install_id_path() -> Path:
    return Path.home() / ".cloakbrowser" / MACHINE_INSTALL_ID_FILENAME


def install_id_for(key: str | None) -> str:
    """Stable per-key install id (the device UUID the binary reports)."""
    return str(uuid.uuid5(INSTALL_ID_NAMESPACE, f"cloakbrowser-install:{key}"))


def write_install_id(value: str) -> None:
    """Atomically put ``value`` in the file the binary reads on next spawn."""
    path = install_id_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(value, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        # Best-effort: a read-only home just falls back to the on-disk value.
        logger.warning("could not write install id at %s", path)


def read_install_id() -> str | None:
    try:
        value = install_id_path().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _write_machine_install_id(value: str) -> None:
    path = machine_install_id_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(value, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        logger.warning("could not write machine install id at %s", path)


def _derive_machine_install_id() -> str:
    """This machine's own id, captured once and kept out of the live file.

    A keyless launch (or anything running the binary outside the Manager) must
    present one stable identity. Seeding it from whatever is on disk the first
    time keeps an existing install recognisable; the result is then pinned so a
    crash that leaves a key's derived id behind cannot rewrite it.
    """
    pinned = _read_machine_install_id()
    if pinned:
        return pinned
    existing = read_install_id()
    if existing and not _is_derived(existing):
        # First run after the upgrade: the file still holds the real value.
        _write_machine_install_id(existing)
        return existing
    return _pin_new_machine_id()


def _read_machine_install_id() -> str | None:
    try:
        value = machine_install_id_path().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _pin_new_machine_id() -> str:
    value = str(uuid.uuid4())
    _write_machine_install_id(value)
    return value


def _is_derived(value: str) -> bool:
    """Whether ``value`` is one of the uuid5 ids this module mints."""
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return False
    return parsed.version == 5 and str(parsed) == value




# The license/session calls normally go straight to cloakbrowser.dev, so the
# server sees the host's real IP for every key no matter what proxy the page
# uses — one more shared identity across an otherwise separate key. The Pro
# binary can route them through --proxy-server instead; first build that has it.
LICENSE_ROUTE_MIN_VERSION = "155.0"


def _version_tuple(value: str) -> tuple[int, ...] | None:
    try:
        return tuple(int(part) for part in value.split("."))
    except (AttributeError, ValueError):
        return None


def supports_license_route(binary_version: str | None) -> bool:
    """Whether the resolved binary knows --license-route.

    Older builds ignore the flag (the calls stay direct), so it is only added
    where it actually takes effect.
    """
    current = _version_tuple(binary_version or "")
    floor = _version_tuple(LICENSE_ROUTE_MIN_VERSION)
    return current is not None and floor is not None and current >= floor


def _normalize_proxy(raw: str) -> str:
    """Convert common proxy formats to http://user:pass@host:port.

    Accepts:
      - http://user:pass@host:port  (already valid)
      - user:pass@host:port
      - host:port:user:pass
      - host:port
    """
    if raw.startswith(("http://", "https://", "socks5://")):
        return raw
    if "@" in raw and "://" not in raw:
        return f"http://{raw}"
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


def _validate_transparent_proxy(url: str | None) -> None:
    """Reject proxies the binary's transparent mode can't consume.

    Transparent mode speaks to exactly one HTTP proxy named on the command line.
    Credentials are optional: the binary accepts `http://host:port` and an IP
    allow-list proxy just as readily as `http://user:pass@host:port`. What it
    cannot take is SOCKS, HTTPS, or a missing proxy — it then fails closed (dead
    network) or rejects the configuration. Refuse to launch instead.

    A username containing a ':' is rejected: Chromium splits the inline
    credential at the first colon, and percent-encoding it (`%3A`) does not help
    once the URL is decoded.
    """
    from urllib.parse import unquote, urlparse

    parsed = urlparse(url or "")
    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or (parsed.password and ":" in unquote(parsed.username or ""))
    ):
        raise ValueError(
            "Transparent proxy needs a single http://host:port proxy "
            "(credentials optional)"
        )


def _transparent_proxy_arg(url: str | None) -> str | None:
    """The `--proxy-server` value transparent mode requires, or None.

    Transparent mode only sees a proxy that reaches it as a command-line flag.
    Without credentials the wrapper leaves a plain `http://host:port` to
    Playwright's proxy dict, which transparent mode never reads — so the Manager
    has to supply the flag itself. With credentials the wrapper already adds it
    (and re-encodes them), so leaving it alone avoids a duplicate flag.
    """
    from urllib.parse import urlparse

    parsed = urlparse(url or "")
    if parsed.scheme != "http" or not parsed.hostname:
        return None
    if parsed.username:
        return None
    return f"http://{parsed.hostname}:{parsed.port}" if parsed.port else None


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


# License-seat probing. Read-only and free, so the pool can ask the server
# whether a cooling key's seat is really free before re-issuing it.
SEAT_PROBE_TIMEOUT = 12      # per seat lookup; this is the network, not CPU
SEAT_POLL_INTERVAL = 1.0     # poll cadence while waiting for a seat release
SEARCH_ENGINE_SEAT_WAIT_S = 20

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


class ProfileBusyError(RuntimeError):
    """The profile's browser or on-disk state is in use by another operation."""


@dataclass
class RunningProfile:
    profile_id: str
    context: Any  # Playwright BrowserContext
    cdp_port: int
    display: int | None = None
    ws_port: int | None = None
    user_data_dir: Path | None = None
    screenshot_task: Any = None  # asyncio.Task for the periodic screenshot loop
    download_task: Any = None  # asyncio.Task capturing this browser's downloads
    download_rearm: Any = None  # asyncio.Event: a CDP client left, reassert download capture
    capture_preview: bool = True
    # Path to the wrapper's per-launch denial file (set by launch_persistent_
    # context_async on the returned context). Read on close to tell a seat/
    # license denial apart from a real crash or a user-initiated close.
    denial_path: str | None = None
    # Pooled key id this browser was launched with (None for a keyless launch).
    # A denial for this launch is recorded against that key in the pool.
    license_key_id: str | None = None
    # Proxy provider lease for this launch (KiotProxy/TMProxy), if any. The raw
    # provider key is kept only so KiotProxy can be detached when the browser
    # stops; it is never returned by the status API.
    proxy_provider: str | None = None
    proxy_provider_key: str | None = None
    proxy_provider_fingerprint: str | None = None
    proxy_info: dict[str, Any] | None = None


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
        self.license_tier = "none"
        self.license_plan: str | None = None
        self.binary_version: str | None = None
        self.running: dict[str, RunningProfile] = {}
        # Last launch failure per profile, surfaced on the status poll and cleared
        # on the next launch. Holds post-handshake denials (out of seats, bad key)
        # that land AFTER launch() already returned 200 — the browser boots, gets
        # denied ~1s later, and self-closes, so there's no HTTP response to carry
        # the reason. get_status() returns this so the frontend can show it.
        self._last_errors: dict[str, dict[str, str]] = {}
        self._launching: set[str] = set()  # profile IDs currently being launched
        # Reservations held while a context is closing (stop(), a self-exit, a
        # failed launch's cleanup). Counted per reservation so one operation's
        # cleanup can never release another's; see _reserve_stopping().
        self._stopping: dict[str, int] = {}
        # Profiles whose user_data_dir is reserved by a filesystem operation
        # (duplicate / reset / delete). launch() refuses these; see hold_stopped().
        self._held: set[str] = set()
        self._initializing: set[str] = set()  # profile IDs in one-time first-launch setup
        # profile_id -> pooled key id leased to the browser that profile is
        # running. One key per running browser: two free-tier keys can't share a
        # seat, so a key already leased is never handed to a second launch.
        self._key_leases: dict[str, str] = {}
        # profile_id -> fingerprint of the proxy-provider key that profile's
        # browser is using. A KiotProxy/TMProxy key hands out one proxy at a
        # time, so it must not be shared by two running profiles.
        self._proxy_leases: dict[str, str] = {}
        self.vnc = VNCManager(self.runtime.viewer_mode == "vnc")
        self._lock = asyncio.Lock()
        # Serializes spawns: the install-id file is process-global, so a spawn must
        # hold this from writing its key's id until the browser has sent
        # session/start — otherwise a racing launch overwrites it first.
        self._spawn_lock = asyncio.Lock()
        # Keyless launches have no key to derive an id from; keep the machine's
        # existing value (or mint one) so they don't churn the file per launch.
        # The value lives in a sidecar so a crash mid-launch (leaving some key's
        # derived id in the live file) cannot make it drift.
        self._default_install_id = _derive_machine_install_id()
        self._cdp_ports: set[int] = set()
        self._auto_launch_task: asyncio.Task | None = None

    def _install_id_for_key(self, key: str | None) -> str:
        return install_id_for(key) if key else self._default_install_id

    def _restore_machine_install_id(self, used: str) -> None:
        """Put the machine's own id back once a key has checked in.

        The binary only reads the file on spawn, so after that the value is free
        to change. Restoring the machine id means the steady state of the file is
        this machine's real identity: a launch by the user directly (or the next
        keyless run) does not inherit the last key's derived id.

        Only rewrite if the file still holds the id we just used - a concurrent
        launch that already wrote its own value wins.
        """
        machine = _read_machine_install_id()
        if machine and read_install_id() == used:
            write_install_id(machine)

    def resolve_binary_status(self) -> None:
        """Resolve the license tier + binary version and pre-download the binary.

        Blocking — called once at startup (via a thread) so the multi-hundred-MB
        Pro download stays out of the launch path and auto-launch's 60s timeout.
        Never raises. Without a key there is nothing to download (wrapper 0.7+
        needs one for every build); launches then fail with the free-key link.
        """
        from cloakbrowser.download import ensure_binary
        from cloakbrowser.license import (
            get_pro_latest_version,
            resolve_license_key,
            validate_license,
        )

        tier = "none"
        version = None

        # The badge describes the key a launch would actually get, so a pool-only
        # setup reports its tier instead of claiming "no key".
        from . import key_pool

        pooled = key_pool.usable_keys()
        key = resolve_license_key(self.license_key) or (pooled[0] if pooled else None)
        if not key:
            self.license_tier = tier
            self.license_plan = None
            self.binary_version = None
            logger.info("No license key set: add one in Settings to launch profiles")
            return

        try:
            info = validate_license(key)
        except Exception as exc:
            info = None
            logger.warning("License validation failed: %s", exc)
        if info and info.valid:
            tier = "free" if info.plan == "free" else "pro"
            self.license_plan = info.plan
            try:
                version = get_pro_latest_version(self.release_channel)
            except Exception as exc:
                logger.warning("Could not resolve Pro version: %s", exc)

        self.license_tier = tier
        self.binary_version = version

        try:
            ensure_binary(
                license_key=key,
                release_channel=self.release_channel,
            )
            logger.info("Binary ready: tier=%s version=%s", tier, version)
        except Exception as exc:
            logger.error(
                "Binary pre-download failed: %s",
                exc,
                exc_info=True,
            )

    # ── License key leasing ───────────────────────────────────────────────
    def _leased_key_ids(self, exclude: str | None = None) -> set[str]:
        """Key ids currently held by a running profile, optionally ignoring one."""
        return {
            key_id for owner, key_id in self._key_leases.items() if owner != exclude
        }

    def _reserved_key_ids(self, exclude: str | None = None) -> set[str]:
        """Key ids pinned by a profile, optionally ignoring one.

        A pin is a reservation even while its profile is stopped: otherwise an
        unpinned launch would happily lease the key a profile is pinned to, and
        that profile would then be refused as "already in use" when it starts.
        The pinned profile itself is excluded because the caller handles its own
        pin before falling back to the shared pool.

        Only pins to a key *still in the pool* reserve anything. A pin to a key
        that was removed is stale: its owner is already refused with "no longer
        in the key pool", so letting it also block everyone else would deadlock
        the app — no profile can launch until the pin is cleared by hand, which
        is exactly the "all profiles closed but still out of seats" report.
        """
        from . import database as db, key_pool

        try:
            profiles = db.list_profiles()
        except sqlite3.OperationalError:
            # No schema yet (a launch in a fresh data dir): nothing can be pinned.
            return set()
        pooled = {entry["id"] for entry in key_pool.entries()}
        return {
            profile["license_key_id"]
            for profile in profiles
            if profile.get("license_key_id")
            and profile["id"] != exclude
            and profile["license_key_id"] in pooled
        }

    @staticmethod
    def _pin_owner_ids(key_id: str, exclude: str | None = None) -> list[str]:
        """Profile ids pinning ``key_id``, optionally ignoring one."""
        from . import database as db

        try:
            profiles = db.list_profiles()
        except sqlite3.OperationalError:
            return []
        return [
            profile["id"]
            for profile in profiles
            if profile.get("license_key_id") == key_id and profile["id"] != exclude
        ]

    @staticmethod
    def _profile_names(profile_ids: list[str]) -> list[str]:
        """Names for ``profile_ids``; the id stands in for a profile that is gone.

        Only used to explain a refusal: "reserved for another profile" is
        useless when the user has four profiles, so the message names them.
        """
        from . import database as db

        names: list[str] = []
        for profile_id in profile_ids:
            try:
                profile = db.get_profile(profile_id)
            except sqlite3.OperationalError:
                profile = None
            names.append((profile or {}).get("name") or profile_id)
        return names

    async def _claim_license_key(
        self, profile: dict[str, Any]
    ) -> tuple[str | None, str | None]:
        """Pick the key this launch gets, and reserve it.

        Order: the profile's pinned key, then the first claimable key in the
        shared pool that no other running profile holds, then the app-wide key.
        Reserving under the manager lock is what makes N free-tier keys usable by
        N concurrent profiles: a key is never handed to a second browser while
        the first still holds its seat.

        A key whose browser died is not immediately claimable — the license
        server keeps counting that abandoned session for a plan-specific grace
        window, and re-issuing it inside that window is exactly the exit-76
        denial. Rather than always waiting the full window out, the pool asks the
        server (a read-only seat lookup) and only then re-checks. One probe per
        claim, never more.

        Raises CloakBrowserLicenseError ("session limit reached", so the UI offers
        the upgrade CTA) when every key is already leased — that is exactly the
        condition the binary would deny a second later, caught before spending a
        launch.
        """
        from . import key_pool

        profile_id = profile["id"]
        leased = self._leased_key_ids(exclude=profile_id)
        reserved = self._reserved_key_ids(exclude=profile_id)
        pinned = profile.get("license_key_id")
        if pinned:
            entry = key_pool.find_by_id(pinned)
            if entry is None:
                raise CloakBrowserLicenseError(
                    f"CloakBrowser Pro: this profile is pinned to license key {pinned}, "
                    "which is no longer in the key pool. Re-add the key or clear the pin."
                )
            if entry["state"] == "invalid":
                raise CloakBrowserLicenseError(
                    f"CloakBrowser Pro: this profile's pinned license key "
                    f"({key_pool.mask_key(entry['key'])}) was rejected by the license "
                    "server. Replace it in the key pool or clear the pin."
                )
            if pinned in leased:
                raise CloakBrowserLicenseError(
                    f"CloakBrowser Pro: session limit reached — the key this profile is "
                    f"pinned to ({key_pool.mask_key(entry['key'])}) is already in use by "
                    "another running profile. Stop it, or unpin the key so this profile "
                    "can lease a free one."
                )
            if not key_pool.claimable(entry):
                await self._probe_cooled_keys([entry])
                # The probe writes the file only, so re-read before judging: a
                # freed seat must be believed, and the message needs live numbers.
                entry = key_pool.find_by_id(pinned) or entry
                if not key_pool.claimable(entry):
                    raise CloakBrowserLicenseError(
                        "CloakBrowser Pro: session limit reached — pinned key "
                        f"({key_pool.mask_key(entry['key'])}) is cooling down, the "
                        "license server still counts its last session; retry in "
                        f"{key_pool.cooldown_remaining(entry)}s, or unpin the key."
                    )
            self._key_leases[profile_id] = pinned
            key_pool.set_lease(profile_id, pinned, plan=entry.get("plan"))
            return entry["key"], pinned

        entry = self._first_claimable(leased, reserved)
        if entry is None:
            # Nothing free right now. If the only thing standing in the way is a
            # cool-down, ask the server whether those seats are actually free
            # yet, then look once more.
            cooling = [
                candidate
                for candidate in key_pool.entries()
                if candidate["id"] not in leased
                and candidate["id"] not in reserved
                and key_pool.cooldown_remaining(candidate) > 0
            ]
            if cooling:
                await self._probe_cooled_keys(cooling)
                entry = self._first_claimable(leased, reserved)
        if entry is not None:
            self._key_leases[profile_id] = entry["id"]
            key_pool.set_lease(profile_id, entry["id"], plan=entry.get("plan"))
            return entry["key"], entry["id"]

        # Pool exhausted (or empty) — fall back to the single app-wide key, which
        # is also leased so a pool key can never double-book the same seat.
        app_key = self.license_key
        if app_key:
            app_id = key_pool.key_id(app_key)
            if app_id in leased or app_id in reserved:
                holders = self._profile_names(
                    [owner for owner, key_id in self._key_leases.items()
                     if key_id == app_id and owner != profile_id]
                )
                pins = self._profile_names(self._pin_owner_ids(app_id, exclude=profile_id))
                who = ", ".join(
                    [f"{name} (đang chạy)" for name in holders]
                    + [f"{name} (ghim key)" for name in pins]
                )
                raise CloakBrowserLicenseError(
                    "CloakBrowser Pro: session limit reached — the app-wide license "
                    f"key is already held by {who or 'another profile'}. Stop that "
                    "profile, clear its pin, or add a key to the pool so profiles can "
                    "run side by side."
                )
            self._key_leases[profile_id] = app_id
            key_pool.set_lease(profile_id, app_id, plan=self.license_plan)
            return app_key, app_id
        pooled = key_pool.entries()
        if pooled and not key_pool.usable_keys():
            # Every pooled key was rejected. That is a dead pool, not a busy one,
            # so neither "session limit" (which sends the user to the pricing
            # page) nor a refusal fits: the keyless build still works, and
            # refusing here would be a regression from the pre-pool behaviour.
            # The pool list already shows the keys as invalid.
            logger.warning(
                "All %d pooled license keys were rejected; launching keyless",
                len(pooled),
            )
            return None, None
        if pooled:
            waiting = [
                key_pool.cooldown_remaining(candidate)
                for candidate in pooled
                if candidate["id"] not in leased
                and candidate["id"] not in reserved
                and key_pool.cooldown_remaining(candidate) > 0
            ]
            suffix = (
                f" (cooling down: {min(waiting)}s until the server stops counting "
                "an abandoned session)"
                if waiting
                else ""
            )
            reserved_note = (
                " Some keys are reserved by another profile's pin; unpin one."
                if any(entry["id"] in reserved for entry in pooled)
                else ""
            )
            raise CloakBrowserLicenseError(
                "CloakBrowser Pro: session limit reached — every license key in the "
                "pool is already in use or reserved. Stop one, or add "
                f"another key.{reserved_note}{suffix}"
            )
        return None, None

    def _first_claimable(
        self, leased: set[str], reserved: set[str] | None = None
    ) -> dict | None:
        """First pooled key nothing else holds, pins included, and may hand out."""
        from . import key_pool

        reserved = reserved or set()
        for entry in key_pool.entries():
            if (
                entry["id"] in leased
                or entry["id"] in reserved
                or not key_pool.claimable(entry)
            ):
                continue
            return entry
        return None

    async def _probe_cooled_keys(self, entries: list[dict]) -> None:
        """Ask the server whether cooling keys' seats are free yet.

        The local grace window is a guess; the seat list is the truth. A key the
        server reports below its cap is released early. Everything else — still
        busy, probe failed, server flaky — keeps cooling, so the grace window
        stays the fallback. Probes run concurrently: a claim holds the manager
        lock and N keys must not add up to N timeouts.

        Never raises.
        """
        from . import key_pool

        targets = [entry for entry in entries if key_pool.cooldown_remaining(entry) > 0]
        if not targets:
            return
        await asyncio.gather(*(self._probe_one(entry) for entry in targets))

    async def _probe_one(self, entry: dict) -> None:
        """Probe one cooling key; clear its cool-down when the seat is free."""
        from cloakbrowser.license import get_session_seats

        from . import key_pool

        try:
            seats = await asyncio.wait_for(
                asyncio.to_thread(get_session_seats, entry["key"]), SEAT_PROBE_TIMEOUT
            )
        except Exception as exc:  # noqa: BLE001 — a probe never blocks a launch
            logger.debug(
                "Seat probe failed for %s: %s", key_pool.mask_key(entry["key"]), exc
            )
            return
        if seats.state == "ok" and (seats.active or 0) < (seats.limit or 1):
            key_pool.clear_cooldown(entry["id"])
            logger.debug(
                "Seat probe: %s is free again (active=%s limit=%s)",
                key_pool.mask_key(entry["key"]),
                seats.active,
                seats.limit,
            )
        else:
            logger.debug(
                "Seat probe: %s still busy (state=%s active=%s limit=%s)",
                key_pool.mask_key(entry["key"]),
                seats.state,
                seats.active,
                seats.limit,
            )

    def _release_license_key(self, profile_id: str, *, cooldown: int = 0) -> str | None:
        """Free the key a profile held, cooling it when the seat is not really free.

        ``cooldown`` is the plan's reclaim window: a browser that died leaves the
        server still counting its session, so the key must sit out that window
        before the pool hands it to anyone. ``stop()`` passes 0 — a clean stop
        says the seat was given back. Returns the key id that was held.
        """
        from . import key_pool

        self._key_leases.pop(profile_id, None)
        return key_pool.clear_lease(profile_id, cooldown=cooldown)

    def leased_keys(self) -> dict[str, str]:
        """profile_id → key id for every running profile that holds a key."""
        return dict(self._key_leases)

    @staticmethod
    def _proxy_fingerprint(provider: str, key: str) -> str:
        """Stable, non-secret handle for a provider key (in-memory lease only)."""
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        return f"{provider}:{digest}"

    async def _claim_proxy(
        self, profile: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str | None, str | None, str | None]:
        """Fetch a fresh proxy from the profile's provider, or return no proxy.

        The provider key is reserved before the network call so two launches
        racing on the same key cannot both receive a proxy. A failed fetch
        releases the reservation again.
        """
        from . import proxy_providers

        provider = (profile.get("proxy_provider") or "none").strip().lower()
        if provider not in ("kiotproxy", "tmproxy"):
            return None, None, None, None
        key = (profile.get("proxy_provider_key") or "").strip()
        if not key:
            raise ValueError(f"{provider} is selected but no API key is configured")
        fingerprint = self._proxy_fingerprint(provider, key)
        profile_id = profile["id"]

        async with self._lock:
            for owner, held in self._proxy_leases.items():
                if held == fingerprint and owner != profile_id:
                    raise ValueError(
                        f"The {provider} key is already in use by running profile {owner}. "
                        "Each provider key can hand out one proxy at a time; use another key."
                    )
            self._proxy_leases[profile_id] = fingerprint

        try:
            info = await proxy_providers.new_for(
                provider,
                key,
                region=profile.get("kiotproxy_region") or "random",
                location_id=int(profile.get("tmproxy_location_id") or 0),
                isp_id=int(profile.get("tmproxy_isp_id") or 0),
            )
        except proxy_providers.ProxyProviderError as exc:
            self._proxy_leases.pop(profile_id, None)
            raise ValueError(str(exc)) from exc
        except BaseException:
            self._proxy_leases.pop(profile_id, None)
            raise
        if not info.get("http"):
            self._proxy_leases.pop(profile_id, None)
            raise ValueError(f"{provider} did not return an HTTP proxy")
        return info, provider, key, fingerprint

    async def _release_proxy(
        self, profile_id: str, provider: str | None, key: str | None
    ) -> None:
        """Detach the provider proxy (KiotProxy) and free its in-memory lease."""
        from . import proxy_providers

        try:
            if provider and key:
                await proxy_providers.release_for(provider, key)
        except Exception as exc:  # noqa: BLE001 - teardown must never fail on this
            logger.warning("Could not release %s proxy for %s: %s", provider, profile_id, exc)
        finally:
            self._proxy_leases.pop(profile_id, None)

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
            if profile_id in self._held:
                raise ProfileBusyError(
                    f"Profile {profile_id} is busy: its browser state is being copied or reset"
                )
            if profile_id in self._stopping:
                # The previous browser is still closing. Starting another Chrome
                # on the same directory now would fight it for the profile — and
                # in Docker mode the launch would even delete its Singleton lock.
                raise ProfileBusyError(f"Profile {profile_id} is still closing; retry shortly")
            if profile_id in self.running or profile_id in self._launching:
                raise RuntimeError(f"Profile {profile_id} is already running")
            self._launching.add(profile_id)
            # Fresh attempt — drop any stale denial from a previous launch so the
            # status poll doesn't keep showing an old "out of seats" message.
            self._last_errors.pop(profile_id, None)
            # Reserve a license key while still holding the lock: two launches
            # racing here must never lease the same key.
            try:
                launch_key, launch_key_id = await self._claim_license_key(profile)
            except BaseException:
                self._launching.discard(profile_id)
                raise

        display: int | None = None
        ws_port: int | None = None
        cdp_port: int | None = None
        context: Any | None = None
        # Whether every context this launch opened really closed. The CDP retry
        # loop drops its half-started context per attempt, so a failed close
        # there has to survive as a flag: the seat may still be held.
        context_closed_cleanly = True
        # Provider proxy lease, kept here so a failed launch can release it too.
        proxy_info: dict[str, Any] | None = None
        proxy_provider: str | None = None
        proxy_provider_key: str | None = None
        proxy_provider_fingerprint: str | None = None
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

            # A provider (KiotProxy/TMProxy) takes precedence over the manual
            # proxy field. Claimed before anything that spawns a browser so the
            # one-time search-engine launch rides the same exit IP as the real
            # launch — and keeps the lease in this launch's failure path.
            proxy_info, proxy_provider, proxy_provider_key, proxy_provider_fingerprint = (
                await self._claim_proxy(profile)
            )
            raw_proxy = (proxy_info or {}).get("http") or profile.get("proxy") or None
            proxy = _normalize_proxy(raw_proxy) if raw_proxy else None
            if proxy:
                _validate_proxy(proxy)
            if profile.get("transparent_proxy"):
                _validate_transparent_proxy(proxy)

            # One-time per profile (opt-out via set_google_default): make Google
            # the default search engine. Runs before the user-facing launch;
            # reports "initializing" via get_status while it works (one short
            # headless launch). Never fatal.
            if profile.get("set_google_default", True):
                await self._ensure_search_engine(
                    profile_id, user_data_dir, launch_key, proxy
                )

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
            # Transparent mode and --license-route only recognise a proxy that
            # arrived as --proxy-server; add it for the credential-free HTTP case
            # the wrapper would otherwise keep inside Playwright.
            extra_args += self._proxy_server_args(
                proxy, transparent=bool(profile.get("transparent_proxy"))
            )
            # Send the license/session calls out through the profile's proxy when
            # the binary understands the flag, so the license server does not see
            # every key on this machine's one real IP.
            extra_args += self._license_route_args(proxy)
            # WebRTC must not leak the host's real IP through STUN either. The
            # wrapper only spoofs it for free when GeoIP is on; with GeoIP off a
            # proxied profile would leak. `auto` makes the wrapper resolve the
            # proxy's exit IP itself (cheap, and it drops the flag if it can't).
            extra_args += self._webrtc_args(proxy, user_launch_args)
            # Reopen the tabs the user had open when the profile was last stopped.
            # Chrome's persistent session is saved on disk but only restored when told to.
            if profile.get("restore_session", True):
                extra_args.append("--restore-last-session")

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
                "license_key": launch_key,
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
                    # Hold the spawn lock from writing this key's install id until
                    # the browser has sent session/start (the wrapper returns only
                    # after spawn), so a concurrent launch can't swap the file.
                    async with self._spawn_lock:
                        used_install_id = self._install_id_for_key(launch_key)
                        write_install_id(used_install_id)
                        context = await launch_persistent_context_async(**launch_options)
                        self._restore_machine_install_id(used_install_id)
                    # An over-cap/denied seat leaves the browser booting but never
                    # serving a usable CDP endpoint — so waiting on CDP would just
                    # time out (or worse). The wrapper wrote the reason to a denial
                    # file; check it immediately and each CDP poll so a denial bails
                    # in ~1s instead of waiting out CDP that will never come.
                    denial_path = getattr(context, "_cloak_denial_path", None)
                    lic = self._denial_error(denial_path)
                    if lic is not None:
                        raise lic
                    await self._wait_for_cdp(cdp_port, denial_path=denial_path)
                    break
                except asyncio.CancelledError:
                    if context is not None:
                        context_closed_cleanly = (
                            await self._close_context(context, profile_id)
                        ) and context_closed_cleanly
                    self._release_cdp_port(cdp_port)
                    context = None
                    cdp_port = None
                    raise
                except Exception as exc:
                    last_cdp_error = exc
                    # Grab the denial path before dropping the context: a denial
                    # that lands during _wait_for_cdp surfaces as a TimeoutError,
                    # not the license exception, so check the file explicitly.
                    dp = getattr(context, "_cloak_denial_path", None) if context is not None else None
                    if context is not None:
                        context_closed_cleanly = (
                            await self._close_context(context, profile_id)
                        ) and context_closed_cleanly
                    self._release_cdp_port(cdp_port)
                    context = None
                    cdp_port = None
                    # A license denial (out of seats, bad/expired key, server
                    # unreachable, local config) is deterministic — retrying just
                    # wastes ~10s and re-denies. Fail fast with the real reason,
                    # whether it raised as the license exception or as a timeout.
                    if isinstance(exc, CloakBrowserLicenseError):
                        raise
                    lic = self._denial_error(dp)
                    if lic is not None:
                        raise lic from exc
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
                denial_path=getattr(context, "_cloak_denial_path", None),
                license_key_id=launch_key_id,
                proxy_provider=proxy_provider,
                proxy_provider_key=proxy_provider_key,
                proxy_provider_fingerprint=proxy_provider_fingerprint,
                proxy_info=proxy_info,
            )
            context.on(
                "close",
                lambda *_: asyncio.ensure_future(self._on_browser_closed(running)),
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

            # Files this browser downloads become artifacts of the profile — but only under
            # Docker. On a native install the browser writes to the user's own Downloads
            # folder, and diverting that into the Manager's store would take their files away.
            if self.runtime.runtime_mode == "docker":
                running.download_rearm = asyncio.Event()
                running.download_task = asyncio.ensure_future(
                    downloads.watch(
                        profile_id,
                        cdp_port,
                        still_running=lambda: profile_id in self.running,
                        rearm=running.download_rearm,
                    )
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

        except BaseException as _failure:
            # Stay "active" until the half-launched browser is really gone: swap
            # _launching for _stopping under the lock so is_active() never sees a
            # gap while a context may still be open, then clear it after cleanup.
            from . import key_pool

            async with self._lock:
                self._launching.discard(profile_id)
                self._reserve_stopping(profile_id)
                # A denial that says the key itself is dead retires it from the
                # pool first — it must read launch_key_id while it is still valid.
                if isinstance(_failure, CloakBrowserLicenseError):
                    self._retire_rejected_key(launch_key_id, _failure)
                # Settle the cool-down BEFORE closing. A seat refusal (76) means
                # the seat belongs to someone else right now, so the key must sit
                # out its plan's reclaim window even if the close is clean; a
                # retired key (77) never needs one. Any other failure keeps the
                # key free only if the context really closed.
                plan = (key_pool.find_by_id(launch_key_id) or {}).get("plan") or self.license_plan
                grace = key_pool.grace_for(plan) if is_seat_limit_error(_failure) else 0
            # Close outside the lock: it is a long await and nothing else needs it.
            closed = context_closed_cleanly
            try:
                if context is not None:
                    closed = await self._close_context(context, profile_id)
                if not closed:
                    # The browser may still be alive holding the seat.
                    grace = key_pool.grace_for(plan)
                if cdp_port is not None:
                    self._release_cdp_port(cdp_port)
                if display is not None:
                    await self.vnc.stop_vnc(display)
            finally:
                async with self._lock:
                    self._release_license_key(profile_id, cooldown=grace)
                await self._release_proxy(profile_id, proxy_provider, proxy_provider_key)
                self._release_stopping(profile_id)
            raise

    async def _ensure_search_engine(
        self,
        profile_id: str,
        user_data_dir: Path,
        license_key: str | None = None,
        proxy: str | None = None,
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
            await self._setup_google_default(user_data_dir, license_key, proxy)
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("google\n")
            logger.info(
                "Set Google as default search engine for %s", user_data_dir.name
            )
        except CloakBrowserLicenseError:
            # A license denial (out of seats, bad key, …) isn't a search-engine
            # failure — don't burn a retry on the marker; abort the launch so the
            # API surfaces the real reason.
            raise
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

    async def _setup_google_default(
        self, user_data_dir: Path, license_key: str | None = None, proxy: str | None = None
    ) -> None:
        """Add Google via the settings UI, then commit it as the default.

        Single headless launch, no file seeding. A plain Preferences write can't
        set the default (the authoritative value is MAC-protected in Secure
        Preferences, rebuilt from the prepopulated set every startup), and a
        hand-written Web Data keyword row is deleted on load for its invalid,
        un-forgeable url_hash (strictly so on Windows). So we drive the real Add
        dialog — Chrome creates the row with a valid hash — then "Make default".
        Every later launch then carries Google via the profile's own files.

        ``license_key`` is the key this profile already leased: the headless
        launch is a real browser and must ride it, then the seat is waited on
        before the user-facing launch takes it back. ``proxy`` keeps that headless
        launch on the same exit IP as the real one.
        """
        ctx = await self._headless_launch(user_data_dir, license_key, proxy)
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
        if license_key and not await self._wait_for_seat_release(license_key):
            from . import key_pool

            logger.warning(
                "Search-engine setup left its seat held for %s; launching anyway",
                key_pool.mask_key(license_key),
            )
        await asyncio.sleep(0.5)

    async def _wait_for_seat_release(self, key: str) -> bool:
        """Wait until the server stops counting ``key``'s last session. Best-effort.

        The per-profile setup launch runs on the profile's own key; the
        user-facing launch immediately follows on the same key, so the server
        must have noticed the close first. A failed lookup only skips that poll —
        the local grace window is not needed here because there is nothing else
        to hand the key to.
        """
        from cloakbrowser.license import get_session_seats  # lazy

        deadline = time.monotonic() + SEARCH_ENGINE_SEAT_WAIT_S
        while time.monotonic() < deadline:
            try:
                seats = await asyncio.wait_for(
                    asyncio.to_thread(get_session_seats, key), SEAT_PROBE_TIMEOUT
                )
            except Exception:
                seats = None
            if seats is not None and seats.state == "ok" and seats.active == 0:
                return True
            await asyncio.sleep(SEAT_POLL_INTERVAL)
        return False

    async def _headless_launch(
        self, user_data_dir: Path, license_key: str | None = None, proxy: str | None = None
    ) -> Any:
        """Short headless launch used only by the one-time search-engine setup.

        Takes the key the caller already leased for this profile: this is a real
        browser launch and would otherwise try to take a second seat with the
        app-wide key, which a one-seat free plan denies. It also takes the proxy,
        so this launch checks in from the same exit IP as the real one.
        """
        for lock_file in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            (user_data_dir / lock_file).unlink(missing_ok=True)
        async with self._spawn_lock:
            used_install_id = self._install_id_for_key(license_key)
            write_install_id(used_install_id)
            context = await launch_persistent_context_async(
                user_data_dir=str(user_data_dir),
                headless=True,
                proxy=proxy,
                args=self._proxy_server_args(proxy, transparent=True)
                + self._license_route_args(proxy)
                + self._webrtc_args(proxy, []),
                license_key=license_key,
                release_channel=self.release_channel,
            )
            self._restore_machine_install_id(used_install_id)
            return context

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

    async def _close_context(self, context: Any, profile_id: str) -> bool:
        """Close a context. False when the close failed — the seat may still be held."""
        try:
            await context.close()
        except Exception as exc:
            logger.warning("Error closing context for %s: %s", profile_id, exc)
            return False
        return True

    async def _dispose_running(
        self,
        running: RunningProfile,
        *,
        close_context: bool,
        grace_seconds: int = 0,
    ) -> bool:
        """Tear down a browser and release its key.

        ``grace_seconds`` is how long the key must stay out of rotation after the
        release — the caller passes the plan's reclaim window when the browser
        died rather than being stopped. A failed close raises it to the full
        window: the browser may still be alive, so the seat is certainly not
        free. Returns whether the context actually closed.
        """
        from . import key_pool

        plan = (
            key_pool.find_by_id(running.license_key_id) or {}
        ).get("plan") or self.license_plan
        closed = True
        try:
            if running.screenshot_task is not None:
                running.screenshot_task.cancel()
            if running.download_task is not None:
                running.download_task.cancel()
                # Awaited, not just cancelled: the watcher owns staged download files, and
                # disposal must not race its cleanup.
                await asyncio.gather(running.download_task, return_exceptions=True)
                # The browser is gone, so a transfer still marked pending can never finish.
                # Reconciling only on the next launch would strand it while the profile is stopped.
                try:
                    downloads.reconcile_interrupted(running.profile_id)
                except Exception as exc:  # noqa: BLE001 — teardown must not fail on bookkeeping
                    logger.warning(
                        "Profile %s: could not reconcile downloads on stop: %s",
                        running.profile_id, exc,
                    )
            if close_context:
                closed = await self._close_context(running.context, running.profile_id)
                if not closed:
                    grace_seconds = max(grace_seconds, key_pool.grace_for(plan))
            if running.display is not None:
                await self.vnc.stop_vnc(running.display)
            self._release_cdp_port(running.cdp_port)
            return closed
        finally:
            # In `finally` so a stop_vnc raise or a cancelled dispose can never
            # strand the lease. Both stop() and a self-close land here: stop()
            # releases the seat outright, a death cools the key down first.
            self._release_license_key(running.profile_id, cooldown=grace_seconds)
            await self._release_proxy(
                running.profile_id, running.proxy_provider, running.proxy_provider_key
            )

    async def _on_browser_closed(self, running: RunningProfile):
        """Release resources after a browser crash or user-initiated close.

        A post-handshake license denial (out of seats, bad/expired key) lands
        HERE, not at launch: the browser boots, launch() returns 200, then the
        binary self-exits ~1s later. It leaves the reason in a denial file the
        wrapper minted; read it so the status poll can tell the user why the
        profile just vanished instead of silently flipping back to stopped.

        Takes the specific RunningProfile (not just its id): a late close event
        from a superseded launch must not pop/dispose a healthy relaunch that now
        owns the same profile_id, nor stamp a stale denial onto it.
        """
        profile_id = running.profile_id
        from . import key_pool

        async with self._lock:
            if self.running.get(profile_id) is not running:
                return  # superseded — a newer launch owns this profile now
            # Record the denial and pop under the SAME lock so it can't race with
            # a concurrent launch() clearing _last_errors at its start.
            denial = self._record_denial_if_any(profile_id, running.denial_path)
            if denial is not None:
                self._retire_rejected_key(running.license_key_id, denial)
            # A browser that died (crash, window closed, killed) leaves the server
            # still counting its session, so the key cools down for the plan's
            # reclaim window before the pool may hand it out again. A dead key is
            # retired by _retire_rejected_key and needs no cool-down.
            plan = (
                key_pool.find_by_id(running.license_key_id) or {}
            ).get("plan") or self.license_plan
            grace = key_pool.grace_for(plan)
            if denial is not None and not is_seat_limit_error(denial):
                grace = 0  # a dead key is retired by _retire_rejected_key anyway
            self.running.pop(profile_id, None)
            # Same contract as stop(): active until the dispose is really done.
            self._reserve_stopping(profile_id)

        logger.info("Browser closed for profile %s, cleaning up", profile_id)
        try:
            await self._dispose_running(
                running, close_context=True, grace_seconds=grace
            )
        finally:
            self._release_stopping(profile_id)

    def _denial_error(self, denial_path: str | None) -> CloakBrowserLicenseError | None:
        """Read the wrapper's denial file → a license error, or None.

        Destructive read (consumes the file) but cached in-process, so it's safe
        even if the wrapper's guard already consumed it. Non-license closes (real
        crash, user closed the tab) leave no file → None.
        """
        if not denial_path:
            return None
        try:
            code = read_denial_file(denial_path)
        except Exception as exc:  # never let cleanup fail on a read error
            logger.debug("Denial-file read failed: %s", exc)
            return None
        return license_error_for_code(code) if code is not None else None

    def _record_denial_if_any(
        self, profile_id: str, denial_path: str | None
    ) -> CloakBrowserLicenseError | None:
        """If the close was a license denial, stash it for the status poll."""
        lic = self._denial_error(denial_path)
        if lic is None:
            return None
        self._last_errors[profile_id] = license_error_detail(lic)
        logger.warning("Profile %s closed on a license denial: %s", profile_id, lic)
        return lic

    @staticmethod
    def _is_dead_key_error(exc: BaseException) -> bool:
        """True for the "key itself is bad" denial (exit 77), not a seat refusal.

        Only this one means the key is worthless everywhere: a seat refusal (76)
        or an unreachable server (78) says nothing about the key's validity, so
        marking it dead would retire a perfectly good key.
        """
        return "invalid, expired" in str(exc).lower()

    def _retire_rejected_key(self, license_key_id: str | None, exc: BaseException) -> None:
        """Mark a pooled key invalid after the binary rejected it.

        The pool is only a hint — the binary is the authority — but a key the
        binary has just refused should not be handed to the next launch.
        """
        if not license_key_id or not self._is_dead_key_error(exc):
            return
        from . import key_pool

        if key_pool.set_state_by_id(license_key_id, "invalid"):
            logger.warning(
                "Key %s rejected by the binary — removed from rotation", license_key_id
            )

    async def stop(self, profile_id: str):
        """Stop a running browser instance and release all owned resources."""
        # Pop before close so the close event observes an already-clean state.
        # _stopping keeps the profile "active" for is_active() until the context
        # is really closed: Chrome is still flushing its profile dir in between.
        async with self._lock:
            running = self.running.pop(profile_id, None)
            if running:
                self._reserve_stopping(profile_id)

        if not running:
            return

        logger.info("Stopping profile %s", profile_id)
        try:
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
        finally:
            self._release_stopping(profile_id)

    def _reserve_stopping(self, profile_id: str) -> None:
        """Mark a context-close in progress for the profile (counted)."""
        self._stopping[profile_id] = self._stopping.get(profile_id, 0) + 1

    def _release_stopping(self, profile_id: str) -> None:
        """Release ONE close reservation; the profile stays active while others remain."""
        left = self._stopping.get(profile_id, 0) - 1
        if left > 0:
            self._stopping[profile_id] = left
        else:
            self._stopping.pop(profile_id, None)

    def is_active(self, profile_id: str) -> bool:
        """True while a browser owns the profile dir: launching, running or still
        closing. ``running`` alone is not enough — launch() registers there only
        at the very end, and stop() pops it before the context is closed."""
        return (
            profile_id in self.running
            or profile_id in self._launching
            or profile_id in self._stopping
        )

    def cdp_client_detached(self, running: RunningProfile) -> None:
        """A proxied CDP client that changed download behaviour has disconnected. Chromium
        reverts to its default, so capture is re-armed. ``running`` is the browser the
        client was attached to: a late notification must not touch a relaunched one."""
        if self.running.get(running.profile_id) is running and running.download_rearm is not None:
            running.download_rearm.set()

    @asynccontextmanager
    async def hold_stopped(self, profile_id: str):
        """Exclusive access to a stopped profile's on-disk state for the block.

        Refuses with ProfileBusyError if a browser is launching, running or
        closing for the profile, or another hold is active. While held, launch()
        refuses the profile. Every operation that reads or rewrites a
        user_data_dir (duplicate, reset, delete) takes this, so a launch and a
        filesystem operation can never interleave on the same directory.
        """
        async with self._lock:
            if self.is_active(profile_id):
                raise ProfileBusyError(f"Profile {profile_id} is running or changing state")
            if profile_id in self._held:
                raise ProfileBusyError(
                    f"Profile {profile_id} is busy: its browser state is being copied or reset"
                )
            self._held.add(profile_id)
        try:
            yield
        finally:
            self._held.discard(profile_id)

    def get_status(self, profile_id: str) -> dict[str, Any]:
        """Get running status and viewer capabilities for a profile."""
        from . import proxy_providers

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
            "active_proxy": proxy_providers.public_info(running.proxy_info) if running else None,
            # Set when the last launch closed on a license denial (post-handshake
            # out-of-seats / bad key). Only meaningful while stopped; cleared on
            # the next launch. {message, reason, upgrade_url?} or None.
            "last_error": None if running else self._last_errors.get(profile_id),
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

    async def reconcile_leases(self) -> None:
        """Rebuild the lease book from disk after a restart. Never raises.

        No browser survives a Manager restart, so every lease stored in the pool
        is an orphan — including one whose pid happens to be reused. Each held key
        is therefore cooled down for its plan's reclaim window before the pool may
        hand it out, and then the license server is asked whether those seats are
        already free again so a short-lived restart doesn't cost four minutes.
        """
        from . import key_pool

        try:
            freed = key_pool.drop_all_leases()
            for entry_id in freed:
                plan = (key_pool.find_by_id(entry_id) or {}).get("plan") or self.license_plan
                key_pool.stamp_cooldown(entry_id, key_pool.grace_for(plan))
            if freed:
                logger.info(
                    "Restart: %d leased license key(s) held out of rotation", len(freed)
                )
                await self._probe_cooled_keys(key_pool.entries())
        except Exception as exc:  # noqa: BLE001 — startup must survive bookkeeping
            logger.warning("License lease reconcile failed: %s", exc)

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
        denial_path: str | None = None,
    ) -> None:
        """Wait for and verify Chromium's debugger endpoint on the reserved port.

        If ``denial_path`` is given, the wrapper's denial file is checked each
        poll: a seat/license denial makes the browser never serve CDP, so bail
        immediately with the real reason instead of waiting out ``timeout``.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        last_error: Exception | None = None
        while asyncio.get_running_loop().time() < deadline:
            lic = self._denial_error(denial_path)
            if lic is not None:
                raise lic
            try:
                version = await self._fetch_cdp_version(port)
                websocket_url = str(version.get("webSocketDebuggerUrl") or "")
                if f":{port}/" not in websocket_url:
                    raise RuntimeError(
                        f"CDP endpoint returned an unexpected debugger URL: {websocket_url!r}"
                    )
                return
            except CloakBrowserLicenseError:
                raise
            except Exception as exc:
                last_error = exc
                await asyncio.sleep(0.1)
        # One last check — the denial may have landed on the final poll.
        lic = self._denial_error(denial_path)
        if lic is not None:
            raise lic
        raise TimeoutError(f"CDP endpoint on 127.0.0.1:{port} was not ready") from last_error

    def _build_fingerprint_args(self, profile: dict[str, Any]) -> list[str]:
        """Build extra Chromium args from profile fingerprint settings."""
        args: list[str] = []
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
        if profile.get("transparent_proxy", False):
            args.append("--fingerprint-transparent-proxy")
        if profile.get("portable_cookies", False):
            args.append("--fingerprint-portable-cookies")

        sw = profile.get("screen_width")
        sh = profile.get("screen_height")
        if sw:
            args.append(f"--fingerprint-screen-width={sw}")
        if sh:
            args.append(f"--fingerprint-screen-height={sh}")

        return args

    @staticmethod
    def _webrtc_args(proxy: str | None, launch_args: list[str]) -> list[str]:
        """Spoof WebRTC to the proxy exit IP when GeoIP would not already do it.

        STUN reveals the host's real address regardless of the HTTP proxy, which
        is one more signal tying two keys to one box. With GeoIP on (the default)
        the wrapper resolves the exit IP and sets the flag itself; with GeoIP off
        there is no lookup, so ask for it explicitly with `auto` (the wrapper
        resolves it and removes the flag again if the lookup fails).

        Skipped when the user already set the flag, or when there is no proxy —
        without one the real IP is correct anyway.
        """
        if not proxy:
            return []
        if any(arg.startswith("--fingerprint-webrtc-ip") for arg in launch_args):
            return []
        return ["--fingerprint-webrtc-ip=auto"]

    def _proxy_server_args(self, proxy: str | None, *, transparent: bool) -> list[str]:
        """Supply `--proxy-server` for paths the wrapper leaves to Playwright.

        Two features need the proxy to arrive as a command-line flag rather than
        through Playwright's proxy dict: transparent mode (which reads nothing
        else) and `--license-route=proxy` (which only accepts a flag proxy). Only
        a credential-free `http://host:port` needs help — the wrapper adds the
        flag itself for SOCKS5 and credentialed proxies.
        """
        if not transparent and not supports_license_route(self.binary_version):
            return []
        arg = _transparent_proxy_arg(proxy)
        return [f"--proxy-server={arg}"] if arg else []

    def _license_route_args(self, proxy: str | None) -> list[str]:
        """Route the license/session calls through the profile's own proxy.

        The exit IP is an identity the server can see as readily as the install
        id, so a proxied profile must not still check in from the host's real
        IP. The binary only accepts the flag when `--proxy-server` is actually
        set — otherwise it exits 79 ("--license-route value is invalid...").

        The wrapper puts proxies in `--proxy-server` itself for SOCKS5 and for
        credentialed HTTP(S), but a plain `http://host:port` is handed to
        Playwright's proxy dict instead. That path is covered here by adding the
        flag too, which is safe: Chromium keeps using the one proxy it was given.

        All of it is gated on the binary understanding the flag; older builds
        ignore it and would just start up normally.
        """
        if not proxy or not supports_license_route(self.binary_version):
            return []
        if self._license_route_supported_proxy(proxy):
            return ["--license-route=proxy"]
        return []

    @staticmethod
    def _license_route_supported_proxy(proxy: str) -> bool:
        """True if `--proxy-server` will be set for this proxy at spawn."""
        return proxy.startswith("socks5://") or "@" in proxy or bool(
            _transparent_proxy_arg(proxy)
        )
