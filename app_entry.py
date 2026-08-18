"""Native/frozen entry point for CloakBrowser Manager.

This is the PyInstaller target. Unlike run.py (the dev-from-source launcher,
which shells out to `uvicorn backend.main:app`), a frozen bundle cannot resolve
the "backend.main:app" import string, so uvicorn is run in-process here.

Serves on 127.0.0.1:8080 and opens the default browser when the server is up.
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


def _port_available() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((HOST, PORT))
            return True
        except OSError:
            return False


def _open_when_ready() -> None:
    for _ in range(200):
        try:
            with urllib.request.urlopen(f"{SERVER_URL}/api/health", timeout=0.5):
                webbrowser.open(SERVER_URL)
                return
        except OSError:
            time.sleep(0.1)


def main() -> int:
    os.environ.setdefault("CLOAKBROWSER_MANAGER_RUNTIME", "native")

    if not _port_available():
        # A Manager is already running on this machine — just surface it.
        webbrowser.open(SERVER_URL)
        return 0

    import uvicorn
    from backend.main import app

    threading.Thread(target=_open_when_ready, daemon=True).start()
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
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
