"""Launch/stop/track CloakBrowser instances per profile."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cloakbrowser.browser as cloakbrowser_browser
from cloakbrowser import launch_persistent_context_async

from .vnc_manager import VNCManager

logger = logging.getLogger("cloakbrowser.manager.browser")

cloakbrowser_browser._supports_http_proxy_inline_auth = lambda: False


def _normalize_proxy(raw: str) -> str:
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
    default_dir = user_data_dir / "Default"
    default_dir.mkdir(parents=True, exist_ok=True)

    bookmarks_path = default_dir / "Bookmarks"
    if not bookmarks_path.exists():
        ts = str(int(time.time() * 1_000_000))
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


BASE_CDP_PORT = 5100
CDP_PORT_RANGE = 100


@dataclass
class RunningProfile:
    profile_id: str
    context: Any
    display: int
    ws_port: int
    cdp_port: int


class BrowserManager:
    def __init__(self):
        self.running: dict[str, RunningProfile] = {}
        self._launching: set[str] = set()
        self.vnc = VNCManager()
        self._lock = asyncio.Lock()
        self._next_cdp_port = BASE_CDP_PORT
        self._auto_launch_task: asyncio.Task | None = None
        self._rotation_tasks: dict[str, asyncio.Task] = {}

    async def launch(self, profile: dict[str, Any]) -> RunningProfile:
        profile_id = profile["id"]

        async with self._lock:
            if profile_id in self.running or profile_id in self._launching:
                raise RuntimeError(f"Profile {profile_id} is already running")
            self._launching.add(profile_id)

        display, ws_port = await self.vnc.allocate()

        try:
            cdp_port = self._allocate_cdp_port()
        except ValueError:
            async with self._lock:
                self._launching.discard(profile_id)
            await self.vnc.stop_vnc(display)
            raise

        user_data_dir = Path(profile["user_data_dir"])
        for lock_file in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            lock_path = user_data_dir / lock_file
            lock_path.unlink(missing_ok=True)

        _init_profile_defaults(user_data_dir)

        try:
            await self.vnc.start_vnc(
                display,
                ws_port,
                width=profile.get("screen_width", 1920),
                height=profile.get("screen_height", 1080),
            )

            extra_args = self._build_fingerprint_args(profile)
            extra_args += profile.get("launch_args") or []
            extra_args.append(f"--remote-debugging-port={cdp_port}")

            raw_proxy = profile.get("proxy") or None
            proxy = _normalize_proxy(raw_proxy) if raw_proxy else None
            if proxy:
                _validate_proxy(proxy)

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

            _clipboard_init_js = """
                window.__clipboardText = window.__clipboardText || '';

                window.__setClipboardText = (value) => {
                    if (typeof value === 'string' && value) {
                        window.__clipboardText = value;
                    }
                };

                document.addEventListener('copy', (event) => {
                    const eventText = event?.clipboardData?.getData?.('text/plain');
                    if (eventText) {
                        window.__setClipboardText(eventText);
                        return;
                    }

                    const sel = window.getSelection();
                    if (sel) window.__setClipboardText(sel.toString());
                });

                document.addEventListener('keydown', (e) => {
                    if ((e.ctrlKey || e.metaKey) && e.key === 'c' && !e.altKey && !e.shiftKey) {
                        const sel = window.getSelection();
                        if (sel && sel.toString()) window.__setClipboardText(sel.toString());
                    }
                });

                if (navigator.clipboard && !navigator.clipboard.__cloakbrowserPatchedWriteText) {
                    const originalWriteText = navigator.clipboard.writeText?.bind(navigator.clipboard);
                    if (originalWriteText) {
                        navigator.clipboard.writeText = async (text) => {
                            window.__setClipboardText(text);
                            return originalWriteText(text);
                        };
                        Object.defineProperty(navigator.clipboard, '__cloakbrowserPatchedWriteText', {
                            value: true,
                            configurable: true,
                        });
                    }
                }
            """
            await context.add_init_script(_clipboard_init_js)
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

            context.on("close", lambda: asyncio.ensure_future(
                self._on_browser_closed(profile_id)
            ))

            async with self._lock:
                self.running[profile_id] = running
                self._launching.discard(profile_id)

            # Start fingerprint rotation task if interval is set
            rotation_interval = profile.get("fingerprint_rotation_interval", 0) or 0
            if rotation_interval > 0:
                self._start_rotation_task(profile_id, rotation_interval)

            logger.info(
                "Launched profile %s on display :%d (ws_port=%d, cdp_port=%d, rotation=%dmin)",
                profile_id, display, ws_port, cdp_port, rotation_interval,
            )

            return running

        except BaseException:
            async with self._lock:
                self._launching.discard(profile_id)
            await self.vnc.stop_vnc(display)
            raise

    def _start_rotation_task(self, profile_id: str, interval_minutes: int):
        """Start a background task that rotates fingerprint_seed at the given interval."""

        async def _rotate():
            try:
                while True:
                    await asyncio.sleep(interval_minutes * 60)

                    # Generate new random seed
                    new_seed = random.randint(10000, 99999)

                    from . import database as db

                    # Update DB with new seed, but keep rotation task alive
                    db.update_profile(profile_id, fingerprint_seed=new_seed)

                    logger.info(
                        "Rotating fingerprint for profile %s: old_seed=%s new_seed=%s",
                        profile_id, self.running.get(profile_id), new_seed,
                    )

                    # Restart browser with new seed
                    async with self._lock:
                        running = self.running.pop(profile_id, None)

                    if running:
                        logger.info(
                            "Fingerprint rotation: restarting browser for profile %s",
                            profile_id,
                        )

                        # Kill old browser
                        try:
                            await running.context.close()
                        except Exception as exc:
                            logger.warning("Rotation close error for %s: %s", profile_id, exc)

                        await self.vnc.stop_vnc(running.display)

                        # Re-launch with the same profile (fetched fresh from DB with new seed)
                        try:
                            updated_profile = db.get_profile(profile_id)
                            if updated_profile:
                                await self.launch(updated_profile)
                        except Exception as exc:
                            logger.error(
                                "Rotation re-launch failed for %s: %s",
                                profile_id, exc,
                            )
            except asyncio.CancelledError:
                logger.debug("Rotation task cancelled for profile %s", profile_id)
                raise

        # Cancel existing rotation task if any
        old = self._rotation_tasks.pop(profile_id, None)
        if old and not old.done():
            old.cancel()

        task = asyncio.create_task(_rotate(), name=f"rotation-{profile_id}")
        self._rotation_tasks[profile_id] = task

    async def _on_browser_closed(self, profile_id: str):
        async with self._lock:
            running = self.running.pop(profile_id, None)

        if running:
            # Cancel rotation task when browser dies
            rot_task = self._rotation_tasks.pop(profile_id, None)
            if rot_task and not rot_task.done():
                rot_task.cancel()

            logger.info("Browser closed for profile %s, cleaning up", profile_id)
            await self.vnc.stop_vnc(running.display)

    async def stop(self, profile_id: str):
        async with self._lock:
            running = self.running.pop(profile_id, None)

        # Cancel rotation task
        rot_task = self._rotation_tasks.pop(profile_id, None)
        if rot_task and not rot_task.done():
            rot_task.cancel()

        if not running:
            return

        logger.info("Stopping profile %s", profile_id)

        try:
            await running.context.close()
        except Exception as exc:
            logger.warning("Error closing context for %s: %s", profile_id, exc)

        await self.vnc.stop_vnc(running.display)

    def get_status(self, profile_id: str) -> dict[str, Any]:
        running = self.running.get(profile_id)
        if running:
            return {
                "status": "running",
                "vnc_ws_port": running.ws_port,
                "display": f":{running.display}",
                "cdp_url": f"/api/profiles/{profile_id}/cdp",
            }
        return {"status": "stopped", "vnc_ws_port": None, "display": None, "cdp_url": None}

    async def cleanup_all(self):
        # Cancel all rotation tasks
        for tid, task in list(self._rotation_tasks.items()):
            if not task.done():
                task.cancel()
        self._rotation_tasks.clear()

        async with self._lock:
            profile_ids = list(self.running.keys())

        for pid in profile_ids:
            await self.stop(pid)

        await self.vnc.cleanup_all()

    async def cleanup_stale(self):
        await self.vnc.cleanup_stale()

    async def auto_launch_all(self):
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

    def _allocate_cdp_port(self) -> int:
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
        args: list[str] = [
            "--disable-infobars",
            "--test-type",
            "--use-angle=swiftshader",
        ]

        seed = profile.get("fingerprint_seed")
        if seed is not None:
            args.append(f"--fingerprint={seed}")

        p = profile.get("platform")
        if p:
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
