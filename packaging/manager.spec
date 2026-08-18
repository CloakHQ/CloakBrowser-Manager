# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for CloakBrowser Manager (native Mac/Windows).

Onedir build wrapped by the platform installer (.dmg / Inno Setup). Run from
the manager repo root:  pyinstaller packaging/manager.spec

The frontend must be built first (frontend/dist). build_macos.sh /
build_windows.ps1 do that before invoking PyInstaller.
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

# spec files don't get __file__; SPECPATH is injected by PyInstaller.
ROOT = Path(SPECPATH).resolve().parent  # noqa: F821  (SPECPATH is a spec global)

APP_NAME = "CloakBrowser Manager"
IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"

ICON_ICNS = str(ROOT / "packaging" / "icon.icns")
ICON_ICO = str(ROOT / "packaging" / "icon.ico")
APP_ICON = ICON_ICO if IS_WIN else ICON_ICNS

datas = []
binaries = []
hiddenimports = []

# The built React SPA — served by FastAPI from bundle_dir()/frontend/dist.
frontend_dist = ROOT / "frontend" / "dist"
if not (frontend_dist / "index.html").exists():
    raise SystemExit(
        "frontend/dist/index.html missing — run `npm run build` in frontend/ first"
    )
datas.append((str(frontend_dist), "frontend/dist"))

# Our own backend package (main.py lazy-imports several cloakbrowser modules).
hiddenimports += collect_submodules("backend")

# Playwright ships a Node driver as data + its own PyInstaller hooks; collect it
# fully so cloakbrowser can drive the browser over CDP. (We do NOT ship
# Playwright's downloaded browsers — cloakbrowser fetches its own Chromium.)
for pkg in ("playwright", "cloakbrowser", "uvicorn"):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(pkg)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

# uvicorn[standard] + friends: lazily-loaded native protocol/loop backends.
hiddenimports += [
    "httptools",
    "websockets",
    "websockets.legacy",
    "watchfiles",
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    # cloakbrowser geoip extra
    "geoip2",
    "maxminddb",
    "socksio",
]

# uvloop is POSIX-only; keep it on macOS, drop it on Windows.
excludes = ["uvloop"] if IS_WIN else []

a = Analysis(
    [str(ROOT / "app_entry.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    console=False,  # no terminal window; logs go to the data-dir file
    disable_windowed_traceback=False,
    icon=APP_ICON,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name=APP_NAME,
)

if IS_MAC:
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=ICON_ICNS,
        bundle_identifier="dev.cloakbrowser.manager",
        info_plist={
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": APP_NAME,
            "NSHighResolutionCapable": True,
            # Normal app: a browser tab is the UI, but keep a Dock icon.
            "LSApplicationCategoryType": "public.app-category.utilities",
        },
    )
