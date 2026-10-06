"""Menu bar front for CloakBrowser-Manager.

launchd starts this file. It starts the Manager server as a child and shows
an icon in the menu bar with every CloakBrowser Chromium that runs on this
Mac, headless ones included, so nothing runs out of sight.
Quit stops the server and exits 0, and launchd leaves it stopped.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import urllib.request
import webbrowser
from pathlib import Path

import AppKit
import rumps

ROOT = Path(__file__).resolve().parent.parent
HOME = Path.home()
PORT = int(os.environ.get("MANAGER_PORT", "8081"))
BASE_URL = f"http://127.0.0.1:{PORT}"
PROFILES_ROOT = os.environ.get("CLOAK_NATIVE_PROFILES_ROOT", str(HOME / ".cloakbrowser/profiles"))
LOG_DIR = Path(os.environ.get("DATA_DIR", HOME / ".cloakbrowser/manager")) / "logs"
POLL_SECONDS = 5

USER_DATA_DIR = re.compile(r"--user-data-dir=(\S+)")
CDP_PORT = re.compile(r"--remote-debugging-port=(\d+)")


def cloak_browsers() -> list[dict]:
    """Main Chromium processes that use a CloakBrowser profile (child processes skipped)."""
    out = subprocess.run(["/bin/ps", "ax", "-o", "pid=,command="], capture_output=True, text=True).stdout
    browsers = []
    for line in out.splitlines():
        pid, _, command = line.strip().partition(" ")
        if "Chromium" not in command or " --type=" in command:
            continue
        data_dir = USER_DATA_DIR.search(command)
        if not data_dir:
            continue
        path = data_dir.group(1)
        port = CDP_PORT.search(command)
        browsers.append({
            "pid": int(pid),
            "profile": Path(path).name,
            "managed": path.startswith(PROFILES_ROOT),
            "port": port.group(1) if port else None,
            "headless": "--headless" in command,
        })
    return sorted(browsers, key=lambda b: b["profile"])


def server_up() -> bool:
    try:
        with urllib.request.urlopen(f"{BASE_URL}/api/status", timeout=2) as response:
            json.load(response)
        return True
    except Exception:
        return False


class CloakMenu(rumps.App):
    def __init__(self) -> None:
        super().__init__("Cloak", title="🛡 …", quit_button=None)
        self.server: subprocess.Popen | None = None
        self.start_server()
        self.timer = rumps.Timer(self.refresh, POLL_SECONDS)
        self.timer.start()
        self.refresh(None)

    def start_server(self) -> None:
        if server_up():
            return  # another copy already serves the port; just watch it
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.server = subprocess.Popen(
            [str(ROOT / ".venv/bin/uvicorn"), "backend.main:app", "--host", "127.0.0.1", "--port", str(PORT)],
            cwd=ROOT,
            stdout=open(LOG_DIR / "manager.log", "a"),
            stderr=open(LOG_DIR / "manager-error.log", "a"),
        )

    def stop_server(self) -> None:
        if self.server and self.server.poll() is None:
            self.server.terminate()
            try:
                self.server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.server.kill()
        self.server = None

    def refresh(self, _sender) -> None:
        if self.server and self.server.poll() is not None:
            self.start_server()  # the server died; bring it back
        up = server_up()
        browsers = cloak_browsers()
        hidden = sum(1 for b in browsers if b["headless"])
        self.title = f"🛡 {len(browsers)}" + (f" · {hidden} hidden" if hidden else "") if up else "🛡 ⚠"

        items: list = [
            rumps.MenuItem(f"{'🟢' if up else '🔴'} Manager {'running' if up else 'down'} · :{PORT}"
                           + ("" if self.server else " · started outside this app")),
            rumps.MenuItem("Open Manager", callback=lambda _: webbrowser.open(BASE_URL)),
            None,
            rumps.MenuItem(f"Browsers running: {len(browsers)}"),
        ]
        for browser in browsers:
            label = f"{'👻' if browser['headless'] else '🪟'} {browser['profile']}"
            if browser["port"]:
                label += f"  · cdp {browser['port']}"
            if not browser["managed"]:
                label += "  · outside ~/.cloakbrowser"
            item = rumps.MenuItem(label)
            item.add(rumps.MenuItem(f"Stop {browser['profile']} (pid {browser['pid']})", callback=self.stopper(browser)))
            items.append(item)
        items += [
            None,
            rumps.MenuItem("Restart Manager", callback=self.restart),
            rumps.MenuItem("Open logs", callback=lambda _: subprocess.run(["/usr/bin/open", str(LOG_DIR)])),
            rumps.MenuItem("Quit (stops Manager, leaves browsers open)", callback=self.quit),
        ]
        self.menu.clear()
        self.menu = items

    def stopper(self, browser: dict):
        def stop(_sender) -> None:
            try:
                os.kill(browser["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass
            self.refresh(None)
        return stop

    def restart(self, _sender) -> None:
        self.stop_server()
        self.start_server()
        self.refresh(None)

    def quit(self, _sender) -> None:
        self.stop_server()
        rumps.quit_application()


if __name__ == "__main__":
    # Menu bar only: no Dock icon.
    AppKit.NSApplication.sharedApplication().setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    # New status items land far left and hide behind the notch on a full bar.
    # Start near the clock; a Cmd-drag later overwrites this and is kept.
    defaults = AppKit.NSUserDefaults.standardUserDefaults()
    if defaults.objectForKey_("NSStatusItem Preferred Position Item-0") is None:
        defaults.setFloat_forKey_(260.0, "NSStatusItem Preferred Position Item-0")
    CloakMenu().run()
