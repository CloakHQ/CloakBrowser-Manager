"""KasmVNC display allocation and lifecycle management.

On platforms without KasmVNC (Windows, macOS without XQuartz), VNC is
unavailable — browsers open natively in their own window instead.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

logger = logging.getLogger("cloakbrowser.manager.vnc")


@dataclass
class VNCInstance:
    display: int
    ws_port: int
    process: subprocess.Popen | None = None


def _xvnc_available() -> bool:
    """Check if KasmVNC (Xvnc) is installed and runnable."""
    return shutil.which("Xvnc") is not None


class VNCManager:
    BASE_DISPLAY = 100
    BASE_WS_PORT = 6100

    def __init__(self):
        self._allocated: dict[int, VNCInstance] = {}
        self._lock = asyncio.Lock()
        self._available: bool | None = None  # cached after first check

    @property
    def available(self) -> bool:
        """Whether KasmVNC is available on this system."""
        if self._available is None:
            self._available = _xvnc_available()
        return self._available

    async def allocate(self) -> tuple[int, int]:
        """Returns (display_number, ws_port) for a new profile.

        When VNC is unavailable, returns (0, 0) — the caller should
        skip VNC setup and let the browser open natively.
        """
        if not self.available:
            return 0, 0

        async with self._lock:
            display = self.BASE_DISPLAY
            while display in self._allocated:
                display += 1
            ws_port = self.BASE_WS_PORT + (display - self.BASE_DISPLAY)
            self._allocated[display] = VNCInstance(display=display, ws_port=ws_port)
            return display, ws_port

    async def start_vnc(
        self,
        display: int,
        ws_port: int,
        width: int = 1920,
        height: int = 1080,
    ) -> subprocess.Popen | None:
        """Start Xvnc (KasmVNC) on the given display.

        Returns None when VNC is unavailable (caller should skip VNC).
        """
        if not self.available or display == 0:
            logger.info("VNC unavailable — browser will open natively on desktop")
            return None

        xvnc_bin = shutil.which("Xvnc") or "Xvnc"

        # KasmVNC requires -httpd to enable the WebSocket handler on the websocket port.
        httpd_dir = "/usr/share/kasmvnc/www"

        cmd = [
            xvnc_bin,
            f":{display}",
            "-websocketPort", str(ws_port),
            "-rfbport", "-1",  # disable raw VNC TCP port — WebSocket only
            "-geometry", f"{width}x{height}",
            "-depth", "24",
            "-SecurityTypes", "None",
            "-DisableBasicAuth",
            "-interface", "127.0.0.1",
            "-AlwaysShared",
            "-httpd", httpd_dir,
        ]

        import tempfile
        log_path = os.path.join(tempfile.gettempdir(), f"xvnc-{display}.log")
        logger.info("Starting Xvnc on :%d (ws_port=%d) log=%s", display, ws_port, log_path)

        log_file = open(log_path, "w")
        proc = subprocess.Popen(
            cmd,
            stdout=log_file,
            stderr=log_file,
        )
        log_file.close()

        # Wait a moment for Xvnc to initialize
        await asyncio.sleep(0.5)

        if proc.poll() is not None:
            try:
                with open(log_path) as f:
                    err = f.read()
            except Exception as exc:
                logger.debug("Failed to read Xvnc log %s: %s", log_path, exc)
                err = ""
            raise RuntimeError(f"Xvnc failed to start on :{display}: {err}")

        async with self._lock:
            if display in self._allocated:
                self._allocated[display].process = proc

        return proc

    async def stop_vnc(self, display: int):
        """Kill Xvnc for given display and release allocation."""
        if display == 0:
            return

        async with self._lock:
            instance = self._allocated.pop(display, None)

        if instance and instance.process:
            logger.info("Stopping Xvnc on :%d", display)
            instance.process.terminate()
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None, instance.process.wait, 5,
                )
            except subprocess.TimeoutExpired:
                instance.process.kill()

    async def cleanup_all(self):
        """Kill all managed Xvnc processes. Called on shutdown."""
        async with self._lock:
            displays = list(self._allocated.keys())

        for display in displays:
            await self.stop_vnc(display)

    async def cleanup_stale(self):
        """Kill orphan Xvnc processes from previous runs."""
        if not self.available:
            return

        if sys.platform == "win32":
            # On Windows, use taskkill to clean up leftover Xvnc if somehow present
            try:
                subprocess.run(
                    ["taskkill", "/F", "/IM", "Xvnc.exe"],
                    capture_output=True,
                )
            except FileNotFoundError:
                pass
            return

        try:
            result = subprocess.run(
                ["pkill", "-f", r"Xvnc :[0-9]"],
                capture_output=True,
            )
            if result.returncode == 0:
                logger.info("Cleaned up stale Xvnc processes")
        except FileNotFoundError:
            logger.debug("pkill not found, skipping stale Xvnc cleanup")

    def get_ws_port(self, display: int) -> int | None:
        """Get WebSocket port for a display."""
        instance = self._allocated.get(display)
        return instance.ws_port if instance else None

    @property
    def active_displays(self) -> list[int]:
        return list(self._allocated.keys())
