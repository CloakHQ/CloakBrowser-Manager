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


CDP_START_ATTEMPTS = 3
CDP_READY_TIMEOUT = 10.0


@dataclass
class RunningProfile:
    profile_id: str
    context: Any  # Playwright BrowserContext
    cdp_port: int
    display: int | None = None
    ws_port: int | None = None


class BrowserManager:
    def __init__(self, runtime_config: RuntimeConfig | None = None):
        self.runtime = runtime_config or resolve_runtime()
        self.running: dict[str, RunningProfile] = {}
        self._launching: set[str] = set()  # profile IDs currently being launched
        self.vnc = VNCManager(self.runtime.viewer_mode == "vnc")
        self._lock = asyncio.Lock()
        self._cdp_ports: set[int] = set()
        self._auto_launch_task: asyncio.Task | None = None

    async def launch(self, profile: dict[str, Any]) -> RunningProfile:
        """Launch a browser instance using the configured host runtime."""
        profile_id = profile["id"]

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

            raw_proxy = profile.get("proxy") or None
            proxy = _normalize_proxy(raw_proxy) if raw_proxy else None
            if proxy:
                _validate_proxy(proxy)

            launch_options: dict[str, Any] = {
                "user_data_dir": profile["user_data_dir"],
                "headless": bool(profile.get("headless", False)),
                "proxy": proxy,
                "args": extra_args,
                "timezone": profile.get("timezone") or None,
                "locale": profile.get("locale") or None,
                "humanize": bool(profile.get("humanize", False)),
                "human_preset": profile.get("human_preset", "default"),
                "geoip": bool(profile.get("geoip", False)),
                "color_scheme": profile.get("color_scheme") or None,
                "user_agent": profile.get("user_agent") or None,
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
            )
            context.on(
                "close",
                lambda *_: asyncio.ensure_future(self._on_browser_closed(profile_id)),
            )

            async with self._lock:
                self.running[profile_id] = running
                self._launching.discard(profile_id)

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
        await self._dispose_running(running, close_context=True)

    def get_status(self, profile_id: str) -> dict[str, Any]:
        """Get running status and viewer capabilities for a profile."""
        running = self.running.get(profile_id)
        status = {
            "status": "running" if running else "stopped",
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
        args: list[str] = [
            "--disable-infobars",
            "--test-type",  # suppress "unsupported flag: --no-sandbox" bad flags warning
        ]
        if self.runtime.viewer_mode == "vnc":
            args.append("--use-angle=swiftshader")

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
