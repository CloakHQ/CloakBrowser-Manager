"""Launch/stop/track CloakBrowser instances per profile."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cloakbrowser import launch_persistent_context_async

from .vnc_manager import VNCManager
from .viewer_tokens import viewer_tokens

logger = logging.getLogger("cloakbrowser.manager.browser")


class ProfileAlreadyRunning(RuntimeError):
    """A launch lost the race to another launch (or to a delete in progress)."""


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

    # --- DuckDuckGo as default search engine ---
    prefs_path = default_dir / "Preferences"
    if not prefs_path.exists():
        prefs = {
            "default_search_provider_data": {
                "template_url_data": {
                    "keyword": "duckduckgo.com",
                    "short_name": "DuckDuckGo",
                    "url": "https://duckduckgo.com/?q={searchTerms}",
                    "suggestions_url": "https://duckduckgo.com/ac/?q={searchTerms}&type=list",
                    "favicon_url": "https://duckduckgo.com/favicon.ico",
                }
            },
            "default_search_provider": {
                "enabled": True,
            },
        }
        prefs_path.write_text(json.dumps(prefs, indent=2))
        logger.info("Set DuckDuckGo as default search for %s", user_data_dir.name)


# A wedged Playwright connection makes context.close() hang. Cleanup paths run
# under cancellation (auto_launch_all's wait_for) and under Docker's stop
# deadline, so a best-effort close must never be able to outlive either.
CONTEXT_CLOSE_TIMEOUT_S = 10.0
# Ceiling for a single launch. Auto-launch has always enforced this; the API
# path needs it too, or a wedged Playwright call leaves the request hanging and
# the id stuck in `_launching` — which is_starting() turns into a permanent 409
# on launch, stop and delete alike.
LAUNCH_TIMEOUT_S = 60
# How long a profile stays blocked waiting for a browser that will not close
# before we re-examine the claim. Without a ceiling the guard has no release
# valve: launch 409s, delete 409s and stop 404s forever, so the profile is
# unusable for the life of the container. Expiry does NOT assume the browser is
# gone — a headless profile has no X server to lose, so killing Xvnc says
# nothing about it — the claim is only released once its CDP port stops
# answering.
CLOSING_CLAIM_TTL_S = 60.0
# Absolute ceiling on a claim, however alive its CDP port looks. Ports cycle
# through 5100-5199, so a LATER profile's Chromium can end up bound to the one
# a stale claim remembers — and "something answers" would then extend that
# claim forever, bricking a profile because of an unrelated browser.
CLOSING_CLAIM_MAX_S = 600.0

BASE_CDP_PORT = 5100
CDP_PORT_RANGE = 100  # cycle through 5100-5199 to avoid TIME_WAIT collisions
# Loopback liveness probe: a live Chromium answers instantly, a dead one
# refuses instantly. The timeout only bounds pathological cases.
_BROWSER_PROBE_TIMEOUT_S = 0.25


def _port_is_listening(port: int) -> bool:
    """Whether something still accepts on 127.0.0.1:port."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(_BROWSER_PROBE_TIMEOUT_S)
            return probe.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        return False


async def _close_context_bounded(context: Any, profile_id: str) -> bool:
    """Best-effort context.close() that cannot outlive its caller.

    Shielded so a cancellation of *this* task does not abandon the close
    half-done, and bounded so a wedged Playwright connection cannot hold a
    cancellation (or a container shutdown) open indefinitely.
    """
    if context is None or context.is_closed():
        return True
    closing = asyncio.ensure_future(context.close())
    try:
        # shield: a cancellation of *this* task leaves the close running rather
        # than abandoning the browser half-closed.
        await asyncio.wait_for(asyncio.shield(closing), timeout=CONTEXT_CLOSE_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.warning(
            "Timed out closing browser context for %s after %.0fs; continuing",
            profile_id, CONTEXT_CLOSE_TIMEOUT_S,
        )
        # Let it finish in the background, but consume the eventual result so a
        # later failure is not reported as a never-retrieved exception.
        closing.add_done_callback(
            lambda task: task.cancelled() or task.exception() is not None
        )
        return False
    except Exception as exc:
        logger.warning("Error closing context for %s: %s", profile_id, exc)
        return False
    return True


@dataclass
class RunningProfile:
    profile_id: str
    context: Any  # Playwright BrowserContext
    display: int
    ws_port: int
    cdp_port: int


class BrowserManager:
    def __init__(self):
        self.running: dict[str, RunningProfile] = {}
        self._launching: set[str] = set()  # profile IDs currently being launched
        # Auto-launch profiles queued at startup but not yet reached. Without
        # this, every profile behind the one currently launching would report
        # "stopped" — which an open viewer reads as a terminal session end.
        self._pending_auto_launch: set[str] = set()
        # Profiles being deleted. A launch must not claim one mid-delete: the
        # rmtree would then run under a starting Chromium.
        self._deleting: set[str] = set()
        # Profiles whose browser teardown has not completed, mapped to the
        # context being closed. Held for the WHOLE teardown, not just after it
        # fails: the profile is out of `running` but Chromium is still alive
        # and writing to user_data_dir, so a launch or delete must not treat
        # the directory as free. Keyed by context so the entry is cleared by
        # THAT browser's close and not by some other instance's.
        self._closing: dict[str, Any] = {}
        self.vnc = VNCManager()
        self._lock = asyncio.Lock()
        self._next_cdp_port = BASE_CDP_PORT
        self._auto_launch_task: asyncio.Task | None = None

    async def launch(self, profile: dict[str, Any]) -> RunningProfile:
        """Launch a browser instance for the given profile."""
        profile_id = profile["id"]

        async with self._lock:
            if (
                profile_id in self.running
                or profile_id in self._launching
                or profile_id in self._deleting
                or self.is_wedged(profile_id)
            ):
                raise ProfileAlreadyRunning(f"Profile {profile_id} is already running")
            self._launching.add(profile_id)

        # One handler covers everything after the claim above. The filesystem
        # setup below is the most failure-prone part of the function (a full or
        # read-only /data, EACCES on a lock file) and used to sit outside it —
        # an exception there left the id in `_launching` forever, and
        # is_starting() makes that permanent and total: /launch, /stop and
        # DELETE all answer 409, /viewer-token 503, and the display is never
        # reclaimed. Only a container restart cleared it.
        display: int | None = None
        context = None
        try:
            display, ws_port = await self.vnc.allocate()
            cdp_port = self._allocate_cdp_port()

            # Clean stale Chromium lock files (left by previous container crashes)
            user_data_dir = Path(profile["user_data_dir"])
            for lock_file in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
                lock_path = user_data_dir / lock_file
                lock_path.unlink(missing_ok=True)

            # Set up bookmarks and search engine on first launch
            _init_profile_defaults(user_data_dir)

            # Start KasmVNC on the allocated display
            await self.vnc.start_vnc(
                display,
                ws_port,
                width=profile.get("screen_width", 1920),
                height=profile.get("screen_height", 1080),
            )

            # Build fingerprint args from profile settings
            extra_args = self._build_fingerprint_args(profile)
            extra_args += profile.get("launch_args") or []
            extra_args.append(f"--remote-debugging-port={cdp_port}")

            # Normalize proxy format (host:port:user:pass → http://user:pass@host:port)
            raw_proxy = profile.get("proxy") or None
            proxy = _normalize_proxy(raw_proxy) if raw_proxy else None
            if proxy:
                _validate_proxy(proxy)

            # Launch CloakBrowser on that display
            # DISPLAY is passed via env kwarg to avoid process-wide os.environ mutation
            context = await launch_persistent_context_async(
                user_data_dir=profile["user_data_dir"],
                headless=bool(profile.get("headless", False)),
                proxy=proxy,
                args=extra_args,
                timezone=profile.get("timezone") or None,
                locale=profile.get("locale") or None,
                humanize=bool(profile.get("humanize", False)),
                human_preset=profile.get("human_preset", "default"),
                geoip=bool(profile.get("geoip", False)),
                color_scheme=profile.get("color_scheme") or None,
                user_agent=profile.get("user_agent") or None,
                viewport={
                    "width": profile.get("screen_width", 1920),
                    "height": profile.get("screen_height", 1080) - 133,
                },
                env={**os.environ, "DISPLAY": f":{display}"},
            )

            # Register the close handler NOW, not after the setup below. The
            # setup awaits, so a context that dies (or is cancelled) in that
            # window would otherwise have no handler at all — and nothing would
            # ever clear a wedged entry recorded for it.
            closed_context = context
            context.on("close", lambda: asyncio.ensure_future(
                self._on_browser_closed(profile_id, closed_context)
            ))

            # Inject clipboard listener: captures copied text on every page
            # so the GET /clipboard endpoint can read it via page.evaluate()
            _clipboard_init_js = """
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
            await context.add_init_script(_clipboard_init_js)
            # Also inject into already-open pages (about:blank created before init_script)
            for p in context.pages:
                try:
                    await p.evaluate(_clipboard_init_js)
                except Exception as exc:
                    logger.debug("Clipboard init failed on existing page: %s", exc)

            running = RunningProfile(
                profile_id=profile_id,
                context=context,
                display=display,
                ws_port=ws_port,
                cdp_port=cdp_port,
            )

            async with self._lock:
                self.running[profile_id] = running
                self._launching.discard(profile_id)

            # The close event can fire between registering the handler above
            # and this point (there are awaits in between). _on_browser_closed
            # would then have found nothing in `running`, skipped stop_vnc, and
            # returned — leaving an orphaned Xvnc holding the display/ws_port
            # while we register a dead context as "running" forever. Re-check
            # now that we are visible to the handler.
            if running.context.is_closed():
                logger.warning(
                    "Browser for profile %s exited during launch registration", profile_id,
                )
                # _on_browser_closed() has already reclaimed the display. Clear
                # our handle so the except-path finally does NOT tear it down a
                # second time: by then a concurrent relaunch may have been
                # allocated that same display, and the duplicate stop_vnc would
                # pop its allocation and unlink its password file.
                await self._on_browser_closed(profile_id)
                display = None
                raise RuntimeError("Browser exited during launch")

            logger.info(
                "Launched profile %s on display :%d (ws_port=%d, cdp_port=%d)",
                profile_id, display, ws_port, cdp_port,
            )

            return running

        except BaseException:
            async with self._lock:
                self._launching.discard(profile_id)
            try:
                # Close the browser we opened. The context.on("close") handler
                # only fires if Chromium closes itself, and nothing here asks
                # it to — so aborting after launch_persistent_context_async()
                # returned would leave a live Chromium with its X server killed
                # out from under it, holding cdp_port (permanently skipped by
                # _allocate_cdp_port) and still writing to user_data_dir. A
                # later relaunch deletes the Singleton* locks and opens a
                # SECOND Chromium on the same profile.
                # Claim BEFORE closing, exactly as stop() does. The profile is
                # already out of `_launching` and the close below can take
                # CONTEXT_CLOSE_TIMEOUT_S, so claiming only on failure leaves
                # the whole cleanup window unowned: a concurrent launch would
                # start a second Chromium on the live user_data_dir and a
                # concurrent DELETE would rmtree under it. The claim persists
                # if the close never lands, and the handler registered above
                # clears it when it does.
                if context is not None:
                    self._closing[profile_id] = (
                        context, time.monotonic() + CLOSING_CLAIM_TTL_S, cdp_port,
                        time.monotonic(),
                    )
                if await _close_context_bounded(context, profile_id):
                    self._closing.pop(profile_id, None)
                else:
                    logger.warning(
                        "Aborted launch for %s left a browser that did not close",
                        profile_id,
                    )
            finally:
                # In a finally: if the close hangs past its bound (or we are
                # being cancelled) the display must still be reclaimed, or
                # wait_for() never returns and every queued auto-launch profile
                # stays stuck reporting "starting".
                if display is not None:
                    await self.vnc.stop_vnc(display)
            raise

    async def _on_browser_closed(self, profile_id: str, context: Any = None):
        """Called when a browser exits (crash, closed via VNC, or stop()).

        `context` identifies which instance closed. A late close from a
        superseded context must be ignored: acting on profile_id alone would
        pop and tear down the replacement that now owns this id, killing a
        freshly launched session and orphaning its Chromium.
        """
        # This browser is genuinely gone now. Resolve its wedge FIRST — before
        # the superseded-context check below, which returns early — otherwise a
        # wedged context whose close lands while another instance holds the id
        # would leave the profile blocked forever.
        entry = self._closing.get(profile_id)
        if context is not None and entry is not None and entry[0] is context:
            logger.info("Browser for profile %s finished closing", profile_id)
            del self._closing[profile_id]

        async with self._lock:
            current = self.running.get(profile_id)
            if context is not None and current is not None and current.context is not context:
                logger.info(
                    "Ignoring close from a superseded context for profile %s", profile_id,
                )
                return
            running = self.running.pop(profile_id, None)

        if context is None:
            # Called without an identity (the launch-time is_closed re-check):
            # nothing else can be holding a wedge for this profile.
            self._closing.pop(profile_id, None)
        viewer_tokens.revoke_profile(profile_id)

        if running:
            logger.info("Browser closed for profile %s, cleaning up", profile_id)
            await self.vnc.stop_vnc(running.display)

    async def stop(self, profile_id: str) -> bool:
        """Stop a running browser instance.

        Returns whether the browser actually closed. False means Chromium is
        still alive (a wedged teardown) — callers must not then treat the
        profile directory as free to delete.
        """
        # Pop before close so _on_browser_closed() finds nothing to clean up
        async with self._lock:
            running = self.running.pop(profile_id, None)

        viewer_tokens.revoke_profile(profile_id)

        if not running:
            return True

        logger.info("Stopping profile %s", profile_id)

        # Claim BEFORE closing, not after it fails. The profile is already out
        # of `running` and the close below takes up to CONTEXT_CLOSE_TIMEOUT_S,
        # so recording it only on failure leaves the entire teardown window
        # unguarded — a concurrent DELETE would rmtree user_data_dir, and a
        # concurrent launch would start a second Chromium, while the first is
        # still closing. The claim simply persists if the close never lands.
        self._closing[profile_id] = (
            running.context, time.monotonic() + CLOSING_CLAIM_TTL_S, running.cdp_port,
            time.monotonic(),
        )
        try:
            # Bounded: cleanup_all() runs this for every profile inside Docker's
            # stop grace period, so one wedged context must not hold it open.
            closed = await _close_context_bounded(running.context, profile_id)
            if closed:
                self._closing.pop(profile_id, None)
            else:
                logger.warning(
                    "Browser for profile %s did not close; blocking launch/delete until it does",
                    profile_id,
                )
        except BaseException:
            # The guard is deliberately NOT released here. The close is
            # shielded, so on cancellation it keeps running and Chromium may
            # still be alive — dropping it now would let a relaunch or delete
            # race a live browser. The claim is self-clearing: the close
            # handler resolves it, and failing that the TTL does.
            raise
        finally:
            # Xvnc is ours regardless of how the close went, and reclaiming it
            # is independent of whether the browser exited. Shielded so a
            # cancelled stop() still frees the display, its ws_port and its
            # password file instead of leaking all three for the process
            # lifetime.
            await asyncio.shield(
                asyncio.ensure_future(self.vnc.stop_vnc(running.display))
            )
        # The close may have landed while Xvnc was being torn down; report what
        # is true now rather than what was true a moment ago.
        return not self.is_wedged(profile_id)

    def claim_for_delete(self, profile_id: str) -> bool:
        """Block launches for the duration of a delete.

        Exclusive: a second concurrent delete must not share the claim, or the
        first to finish would release it while the second is still running and
        reopen the launch window this exists to close.
        """
        if profile_id in self._deleting:
            return False
        self._deleting.add(profile_id)
        return True

    def release_delete_claim(self, profile_id: str) -> None:
        self._deleting.discard(profile_id)

    def is_wedged(self, profile_id: str) -> bool:
        """True while a stopped profile's Chromium has not finished exiting.

        Bounded: an expired claim is released so the profile cannot be blocked
        forever by a browser that never reports its close.
        """
        entry = self._closing.get(profile_id)
        if entry is None:
            return False
        _context, deadline, cdp_port, claimed_at = entry
        if time.monotonic() < deadline:
            return True
        # Deadline reached: decide on evidence, not on a timer. A headless
        # profile keeps running after stop_vnc() kills the display, so
        # releasing on time alone would hand a live browser's profile directory
        # to a relaunch or a delete.
        held_for = time.monotonic() - claimed_at
        if (
            cdp_port is not None
            and held_for < CLOSING_CLAIM_MAX_S
            and _port_is_listening(cdp_port)
        ):
            logger.error(
                "Browser for profile %s still alive on cdp port %d after %.0fs; "
                "keeping the guard",
                profile_id, cdp_port, held_for,
            )
            self._closing[profile_id] = (
                _context, time.monotonic() + CLOSING_CLAIM_TTL_S, cdp_port, claimed_at,
            )
            return True
        if held_for >= CLOSING_CLAIM_MAX_S:
            logger.error(
                "Releasing the guard on profile %s after %.0fs: the cdp port may "
                "now belong to an unrelated browser, and a profile must not stay "
                "blocked indefinitely",
                profile_id, held_for,
            )
        logger.warning(
            "Browser for profile %s never reported closing but is gone; releasing the guard",
            profile_id,
        )
        del self._closing[profile_id]
        return False

    def is_starting(self, profile_id: str) -> bool:
        """True while a launch is in flight or queued behind auto-launch."""
        return profile_id in self._launching or profile_id in self._pending_auto_launch

    def get_status(self, profile_id: str) -> dict[str, Any]:
        """Cheap lifecycle status: running | starting | stopped.

        Deliberately does no process probing — the profile list polls this for
        every profile every 3 seconds and discards liveness. Use
        get_liveness() where the answer actually matters.
        """
        running = self.running.get(profile_id)
        if running:
            return {
                "status": "running",
                "vnc_ws_port": running.ws_port,
                "display": f":{running.display}",
                "cdp_url": f"/api/profiles/{profile_id}/cdp",
            }
        # "starting" is distinct from "stopped" on purpose: an open viewer
        # treats "stopped" as terminal, so reporting it during a container
        # restart would kill a session that is about to come back.
        return {
            "status": "starting" if self.is_starting(profile_id) else "stopped",
            "vnc_ws_port": None,
            "display": None,
            "cdp_url": None,
        }

    async def get_liveness_async(self, profile_id: str) -> dict[str, Any]:
        """get_liveness() with the blocking probes off the event loop.

        _browser_alive() opens a TCP connection; on loopback that is normally
        instantaneous, but "normally" is not "always" and this runs in a request
        handler on the single event loop that also serves nginx's viewer
        auth_request subrequests.
        """
        return await asyncio.get_running_loop().run_in_executor(
            None, self.get_liveness, profile_id,
        )

    def get_liveness(self, profile_id: str) -> dict[str, Any]:
        """Lifecycle status plus real Xvnc/browser liveness.

        Drives the viewer's reconnect classification, so both flags have to
        observe the actual processes rather than local bookkeeping.
        """
        status = self.get_status(profile_id)
        running = self.running.get(profile_id)
        return {
            **status,
            "xvnc_alive": self.vnc.is_alive(running.display) if running else None,
            "browser_alive": self._browser_alive(running) if running else None,
        }

    @staticmethod
    def _browser_alive(running: RunningProfile) -> bool:
        """Whether Chromium is still up, via its DevTools listener.

        `context.pages` cannot answer this: Playwright implements it as
        `self._pages.copy()`, a purely local list copy that never touches the
        connection and never raises — so it returns True for a browser that is
        already dead or wedged, making the viewer's "browser-dead"
        classification unreachable. The CDP port is bound by the Chromium
        process and dies with it, so a loopback connect is the real signal.
        Cheap either way: sub-millisecond when up, immediate ECONNREFUSED
        when not.
        """
        return _port_is_listening(running.cdp_port)

    async def cleanup_all(self):
        """Stop all running profiles. Called on shutdown.

        Concurrently: profiles are independent, and the entrypoint waits for
        this before tearing down nginx, inside Docker's stop grace period.
        Sequentially, shutdown cost is the SUM of every context close plus Xvnc
        teardown, so a handful of profiles is enough to get the container
        SIGKILLed mid-cleanup — killing uncleanly the very browsers the ordered
        shutdown exists to protect.
        """
        async with self._lock:
            profile_ids = list(self.running.keys())

        results = await asyncio.gather(
            *(self.stop(pid) for pid in profile_ids), return_exceptions=True,
        )
        for pid, result in zip(profile_ids, results):
            if isinstance(result, BaseException):
                logger.warning("Error stopping profile %s during shutdown: %s", pid, result)

        await self.vnc.cleanup_all()

    async def cleanup_stale(self):
        """Kill orphan processes from previous container runs."""
        await self.vnc.cleanup_stale()

    async def auto_launch_all(self):
        """Launch all profiles with auto_launch=True. Called on startup."""
        from . import database as db

        profiles = db.list_profiles()
        auto_profiles = [p for p in profiles if p.get("auto_launch")]
        if not auto_profiles:
            logger.info("No profiles configured for auto-launch")
            return

        # Claim the whole queue up front so profiles waiting their turn report
        # "starting" rather than "stopped" — launches are sequential and the
        # last one can be minutes away.
        self._pending_auto_launch = {p["id"] for p in auto_profiles}

        logger.info("Auto-launching %d profile(s)...", len(auto_profiles))
        try:
            for profile in auto_profiles:
                try:
                    await asyncio.wait_for(self.launch(profile), timeout=LAUNCH_TIMEOUT_S)
                    logger.info("Auto-launched profile %s (%s)", profile["name"], profile["id"])
                except Exception as exc:
                    logger.error(
                        "Auto-launch failed for profile %s (%s): %s",
                        profile["name"], profile["id"], exc,
                    )
                finally:
                    self._pending_auto_launch.discard(profile["id"])
        finally:
            # Cancellation (shutdown) must not leave profiles stuck "starting".
            self._pending_auto_launch.clear()
        logger.info("Auto-launch complete: %d running", len(self.running))

    def _allocate_cdp_port(self) -> int:
        """Find a free CDP port using a rotating counter to avoid TIME_WAIT collisions."""
        for _ in range(CDP_PORT_RANGE):
            port = self._next_cdp_port
            self._next_cdp_port = BASE_CDP_PORT + (
                (self._next_cdp_port + 1 - BASE_CDP_PORT) % CDP_PORT_RANGE
            )
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind(("127.0.0.1", port))
                    return port
                except OSError:
                    continue
        raise ValueError("No free CDP ports available in range %d-%d" % (BASE_CDP_PORT, BASE_CDP_PORT + CDP_PORT_RANGE - 1))

    def _build_fingerprint_args(self, profile: dict[str, Any]) -> list[str]:
        """Build extra Chromium args from profile fingerprint settings."""
        args: list[str] = [
            "--disable-infobars",
            "--test-type",  # suppress "unsupported flag: --no-sandbox" bad flags warning
            "--use-angle=swiftshader",  # software GL for VNC (no GPU in container)
        ]

        seed = profile.get("fingerprint_seed")
        if seed is not None:
            args.append(f"--fingerprint={seed}")

        p = profile.get("platform")
        if p:
            # Map our "macos" to binary's "macos"
            args.append(f"--fingerprint-platform={p}")

        vendor = profile.get("gpu_vendor")
        if vendor:
            args.append(f"--fingerprint-gpu-vendor={vendor}")

        renderer = profile.get("gpu_renderer")
        if renderer:
            args.append(f"--fingerprint-gpu-renderer={renderer}")

        hw = profile.get("hardware_concurrency")
        if hw is not None:
            args.append(f"--fingerprint-hardware-concurrency={hw}")

        sw = profile.get("screen_width")
        sh = profile.get("screen_height")
        if sw:
            args.append(f"--fingerprint-screen-width={sw}")
        if sh:
            args.append(f"--fingerprint-screen-height={sh}")

        return args
