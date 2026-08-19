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


def _set_app_icon() -> None:
    """Show our mark in the Dock/taskbar for the from-source dev run.

    The frozen build already carries the icon (manager.spec BUNDLE(icon=…) on
    macOS; the PyInstaller/Inno .ico on Windows), so this is dev-only:
    `python app_entry.py` from source otherwise shows the interpreter's generic
    icon (Python.app rocket on macOS, python.exe on Windows). No-op on failure /
    unsupported platform / frozen.
    """
    import sys
    from pathlib import Path

    if getattr(sys, "frozen", False):
        return
    packaging = Path(__file__).resolve().parent / "packaging"

    if sys.platform == "darwin":
        # The rounded squircle (icon.icns), NOT icon.png (the square master).
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

    elif sys.platform == "win32":
        icon = packaging / "icon.ico"
        if not icon.exists():
            return
        try:
            import ctypes

            user32 = ctypes.windll.user32
            image_icon, lr_loadfromfile, lr_defaultsize = 1, 0x10, 0x40
            wm_seticon, icon_small, icon_big = 0x0080, 0, 1
            hicon = user32.LoadImageW(
                None, str(icon), image_icon, 0, 0,
                lr_loadfromfile | lr_defaultsize,
            )
            if hicon:
                # FindWindow by our exact title — the pywebview window is the
                # only one using it. Set both title-bar and taskbar icons.
                hwnd = user32.FindWindowW(None, WINDOW_TITLE)
                if hwnd:
                    user32.SendMessageW(hwnd, wm_seticon, icon_small, hicon)
                    user32.SendMessageW(hwnd, wm_seticon, icon_big, hicon)
        except Exception:
            pass


def _focus_existing_window() -> bool:
    """Raise an already-running Manager's window — Windows single-instance.

    macOS handles single-instance via the app bundle's
    LSMultipleInstancesProhibited plist, so this is the Windows path: find our
    window by title and bring it to the foreground (restoring it if minimized).
    Returns True if a window was focused. No-op / False on other platforms.
    """
    import sys

    if sys.platform != "win32":
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, WINDOW_TITLE)
        if not hwnd:
            return False
        sw_restore = 9
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, sw_restore)
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def _run_webview(server) -> int:
    """Run the server in a background thread and the native window on main.

    macOS/Cocoa requires the webview event loop to own the main thread, so the
    uvicorn server is threaded here. uvicorn's install_signal_handlers()
    early-returns off the main thread, so a threaded server.run() is safe.
    """
    import sys

    import webview

    # Windows taskbar groups/icons by AppUserModelID; set an explicit one before
    # the window exists so the dev-run taskbar icon can be ours (frozen build is
    # unaffected — it has a real .exe identity). No-op elsewhere / on failure.
    if sys.platform == "win32" and not getattr(sys, "frozen", False):
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "dev.cloakbrowser.manager"
            )
        except Exception:
            pass

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
    # Set the Dock/taskbar icon once the window exists, on the main thread.
    # Dev-only nicety; the frozen build already shows the icon.
    window.events.shown += _set_app_icon

    webview.start()  # blocks on the main thread until the window is closed

    # Window closed (incl. Cmd+Q via the default macOS menu) → persist geometry,
    # then shut the server down cleanly (the lifespan shutdown closes every
    # profile via cleanup_all()).
    _save_window_geometry(state)
    server.should_exit = True
    server_thread.join(timeout=15)
    return 0


def _harden_std_streams() -> None:
    """Make stdout/stderr non-fatal on non-ASCII output.

    A frozen GUI app on Windows has no console, so sys.stdout/stderr carry the
    legacy console codepage (cp1252) with errors="strict". A dependency writing
    a glyph that codepage lacks (e.g. the cloakbrowser welcome banner's → and —,
    printed on the first Pro-binary download) then raises UnicodeEncodeError.
    Because that write sits on the binary-download path, the exception aborts the
    whole profile launch. Switch both streams to errors="replace" so an
    unencodable glyph degrades to '?' instead of crashing the process.
    """
    import sys

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except (ValueError, OSError):
            pass


def main() -> int:
    _harden_std_streams()
    os.environ.setdefault("CLOAKBROWSER_MANAGER_RUNTIME", "native")

    if not _port_available():
        # A Manager is already running on this machine. On Windows, focus its
        # existing window (single-instance); otherwise surface it in a browser.
        if not _focus_existing_window():
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
