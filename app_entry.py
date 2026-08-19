"""Native/frozen entry point for CloakBrowser Manager.

This is the PyInstaller target. Unlike run.py (the dev-from-source launcher,
which shells out to `uvicorn backend.main:app`), a frozen bundle cannot resolve
the "backend.main:app" import string, so uvicorn is run in-process here.

Serves on 127.0.0.1:8080. The UI is shown in one of two shells, chosen by the
CLOAKBROWSER_MANAGER_UI env var:
  - "webview" — a dedicated native app window (WKWebView on macOS, WebView2 on
    Windows) via pywebview. No browser chrome, its own Dock/taskbar entry.
  - "browser" — the user's default browser at the server URL (the original
    behavior).
  - "auto"    — (default) webview if pywebview is importable, else browser.

There is no visible terminal in the packaged app — logs go to a rotating file
in the data dir (see backend/main.py logging setup).
"""

from __future__ import annotations

import os
import socket
import threading
import time
import urllib.request
import webbrowser

SERVER_URL = "http://127.0.0.1:8080"
HOST = "127.0.0.1"
PORT = 8080
WINDOW_TITLE = "CloakBrowser Manager"


def _port_available() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((HOST, PORT))
            return True
        except OSError:
            return False


def _wait_until_ready(timeout: float = 20.0) -> bool:
    """Poll /api/health until the server answers or the timeout elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{SERVER_URL}/api/health", timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _open_browser_when_ready() -> None:
    if _wait_until_ready():
        webbrowser.open(SERVER_URL)


def _window_state_path():
    # Its own file (not settings.json): save_settings() rewrites the whole
    # settings dict, so persisting geometry there could clobber a license key
    # the backend saved concurrently. Geometry writes stay isolated here.
    from backend.runtime import resolve_runtime

    return resolve_runtime().data_dir / "window.json"


def _load_window_geometry() -> dict:
    import json

    try:
        data = json.loads(_window_state_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for key in ("x", "y", "width", "height"):
        value = data.get(key)
        if isinstance(value, int) and value >= 0:
            out[key] = value
    return out


def _save_window_geometry(state: dict) -> None:
    import json
    import tempfile

    path = _window_state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        os.replace(tmp, path)
    except OSError:
        pass  # geometry persistence is a nicety, never fatal


def _ui_mode() -> str:
    mode = os.environ.get("CLOAKBROWSER_MANAGER_UI", "auto").strip().lower()
    if mode not in {"auto", "webview", "browser"}:
        mode = "auto"
    if mode == "auto":
        try:
            import webview  # noqa: F401
        except ImportError:
            return "browser"
        return "webview"
    return mode


def _set_dock_icon() -> None:
    """Show our mark in the macOS Dock for the from-source dev run.

    The frozen .app already carries the icon via manager.spec's BUNDLE(icon=…),
    so this is dev-only: `python app_entry.py` otherwise borrows the framework
    Python.app's generic rocket. Requires pyobjc (pulled in with pywebview);
    no-op on failure / non-macOS / frozen.
    """
    import sys
    from pathlib import Path

    if sys.platform != "darwin" or getattr(sys, "frozen", False):
        return
    # Use the rounded macOS squircle (icon.icns), NOT icon.png (the square
    # full-bleed master used for the Windows .ico).
    packaging = Path(__file__).resolve().parent / "packaging"
    icon = packaging / "icon.icns"
    if not icon.exists():
        icon = packaging / "icon.png"
    if not icon.exists():
        return
    try:
        from AppKit import NSApplication, NSImage

        image = NSImage.alloc().initWithContentsOfFile_(str(icon))
        if image is not None:
            NSApplication.sharedApplication().setApplicationIconImage_(image)
    except Exception:
        pass


def _run_webview(server) -> int:
    """Run the server in a background thread and the native window on main.

    macOS/Cocoa requires the webview event loop to own the main thread, so the
    uvicorn server is threaded here. uvicorn's install_signal_handlers()
    early-returns off the main thread, so a threaded server.run() is safe.
    """
    import webview

    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    if not _wait_until_ready():
        # Server never came up — fall back to a browser tab rather than an
        # empty window, so the failure is at least visible + logged.
        webbrowser.open(SERVER_URL)
        server.should_exit = True
        server_thread.join(timeout=10)
        return 1

    geometry = _load_window_geometry()
    window_kwargs = {
        "width": geometry.get("width", 1400),
        "height": geometry.get("height", 900),
        "min_size": (1000, 700),
    }
    if "x" in geometry and "y" in geometry:
        window_kwargs["x"] = geometry["x"]
        window_kwargs["y"] = geometry["y"]
    window = webview.create_window(WINDOW_TITLE, SERVER_URL, **window_kwargs)

    # Track live geometry so we can restore the window next launch. Saved once
    # on close (below) rather than on every resize/move tick.
    state = dict(geometry)

    def _on_resized(width, height):
        state["width"], state["height"] = int(width), int(height)

    def _on_moved(x, y):
        state["x"], state["y"] = int(x), int(y)

    window.events.resized += _on_resized
    window.events.moved += _on_moved
    # Set the Dock icon once the GUI (and its NSApplication) exists, on the main
    # thread. Dev-only nicety; the frozen bundle already shows the icon.
    window.events.shown += _set_dock_icon

    webview.start()  # blocks on the main thread until the window is closed

    # Window closed (incl. Cmd+Q via the default macOS menu) → persist geometry,
    # then shut the server down cleanly (the lifespan shutdown closes every
    # profile via cleanup_all()).
    _save_window_geometry(state)
    server.should_exit = True
    server_thread.join(timeout=15)
    return 0


def main() -> int:
    os.environ.setdefault("CLOAKBROWSER_MANAGER_RUNTIME", "native")

    if not _port_available():
        # A Manager is already running on this machine — just surface it.
        webbrowser.open(SERVER_URL)
        return 0

    import uvicorn
    from backend.main import app

    # log_config=None lets uvicorn's own loggers propagate to the root handlers
    # configured in backend/main.py (console + rotating file), instead of
    # uvicorn installing its own console-only handlers.
    #
    # Build the Server explicitly (instead of uvicorn.run) and stash it on
    # app.state so the /api/shutdown endpoint can flip should_exit for a clean
    # cross-platform quit from the UI.
    config = uvicorn.Config(app, host=HOST, port=PORT, log_config=None)
    server = uvicorn.Server(config)
    app.state.uvicorn_server = server

    if _ui_mode() == "webview":
        return _run_webview(server)

    threading.Thread(target=_open_browser_when_ready, daemon=True).start()
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
