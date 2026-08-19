"""CloakBrowser Manager — FastAPI application.

Serves the React dashboard (static files) and provides a REST API
for browser profile management with live VNC viewing.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import signal
import struct
import shutil
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import starlette.requests
from starlette.types import ASGIApp, Receive, Scope, Send

from .env_file import load_env_file

# Populate os.environ from the manager-root .env before anything reads it
# (database.resolve_runtime, AUTH_TOKEN, the license config below).
load_env_file()

from . import database as db
from .browser_manager import BrowserManager
from .models import (
    ClipboardRequest,
    LaunchResponse,
    LoginRequest,
    ProfileCreate,
    ProfileResponse,
    ProfileStatusResponse,
    ProfileUpdate,
    ReorderRequest,
    SettingsResponse,
    SettingsUpdate,
    StatusResponse,
    TagResponse,
)
from .runtime import bundle_dir
from .settings_store import load_settings, save_settings

logger = logging.getLogger("cloakbrowser.manager")

# Log to the console AND to a rotating file in the data dir. The native frozen
# app has no visible terminal, so the file is the only way a user (or we, for
# support) can see what happened. db.DATA_DIR is resolved at the import above.
_LOG_HANDLERS: list[logging.Handler] = [logging.StreamHandler()]
try:
    _log_dir = db.DATA_DIR / "logs"
    _log_dir.mkdir(parents=True, exist_ok=True)
    _LOG_HANDLERS.append(
        RotatingFileHandler(
            _log_dir / "manager.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
    )
except OSError as exc:  # read-only data dir etc. — keep console logging
    logger.warning("Could not open log file, console only: %s", exc)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=_LOG_HANDLERS,
)
logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)

# Optional authentication via AUTH_TOKEN env var.
# If not set, all routes are open (local dev). If set, all /api/* routes
# (except /api/auth/status, /api/auth/login and /api/health) require Bearer
# token or cookie.
AUTH_TOKEN: str | None = os.environ.get("AUTH_TOKEN") or None

# App-wide CloakBrowser Pro license. One key per Manager instance — the
# concurrency-seat pool is per-license, so every launched profile shares it.
# Release channel picks Stable vs Preview builds.
#
# Precedence: environment (incl. values load_env_file pulled from a .env) wins,
# then the in-app Settings (settings.json). Native users have no .env and set
# the key in the Settings UI; Docker/CI keep overriding via env vars.
_STORED_SETTINGS = load_settings()


def _resolve_setting(env_key: str, settings_key: str) -> str | None:
    return os.environ.get(env_key) or _STORED_SETTINGS.get(settings_key) or None


LICENSE_KEY: str | None = _resolve_setting("CLOAKBROWSER_LICENSE_KEY", "license_key")
RELEASE_CHANNEL: str | None = _resolve_setting(
    "CLOAKBROWSER_RELEASE_CHANNEL", "release_channel"
)

# Paths that bypass authentication even when AUTH_TOKEN is set
_AUTH_EXEMPT = frozenset({"/api/auth/status", "/api/auth/login", "/api/health"})


def _check_auth(scope: Scope) -> bool:
    """Check if the request has a valid auth token (header or cookie)."""
    if AUTH_TOKEN is None:
        return True

    # Check Authorization: Bearer <token> header
    for key, val in scope.get("headers", []):
        if key == b"authorization":
            auth_value = val.decode()
            if auth_value.startswith("Bearer "):
                token = auth_value[7:]
                if token and hmac.compare_digest(token, AUTH_TOKEN):
                    return True
            break

    # Check auth_token cookie
    for key, val in scope.get("headers", []):
        if key == b"cookie":
            cookies = SimpleCookie()
            cookies.load(val.decode())
            if "auth_token" in cookies:
                cookie_val = cookies["auth_token"].value
                if cookie_val and hmac.compare_digest(cookie_val, AUTH_TOKEN):
                    return True
            break

    return False


def _is_https(request: Request) -> bool:
    """Check if the original client connection was HTTPS (via reverse proxy header)."""
    proto = request.headers.get("x-forwarded-proto", "")
    return "https" in proto


async def _check_websocket_origin(websocket: WebSocket) -> bool:
    """Reject cross-origin WebSocket connections (CSWSH protection).

    Browsers always send an Origin header on WebSocket upgrades.
    Non-browser clients (Playwright, curl) typically don't — those are allowed.
    If Origin is present, its host must match the request Host header.
    """
    origin = None
    host = None
    for key, val in websocket.scope.get("headers", []):
        if key == b"origin":
            origin = val.decode("latin-1")
        elif key == b"host":
            host = val.decode("latin-1")

    # No Origin header → non-browser client (Playwright, Puppeteer) → allow
    if not origin:
        return True

    # Parse origin to extract host:port
    try:
        parsed = urlparse(origin)
        origin_host = parsed.hostname or ""
        origin_port = parsed.port
    except ValueError:
        logger.warning("WebSocket origin malformed: %s", origin)
        await websocket.close(code=4403, reason="Origin not allowed")
        return False
    # Build origin netloc (host:port or just host if default port)
    if origin_port and origin_port not in (80, 443):
        origin_netloc = f"{origin_host}:{origin_port}"
    else:
        origin_netloc = origin_host

    if not host:
        return True  # no Host header to compare against

    # Strip default port from Host too (some proxies send "example.com:443")
    host_normalized = host
    if host.endswith(":80") or host.endswith(":443"):
        host_normalized = host.rsplit(":", 1)[0]

    if origin_netloc == host_normalized:
        return True

    logger.warning("WebSocket origin mismatch: origin=%s host=%s", origin, host)
    await websocket.close(code=4403, reason="Origin not allowed")
    return False


def _same_origin_request(request: Request) -> bool:
    """CSRF guard for state-changing simple POSTs (HTTP mirror of
    _check_websocket_origin). A POST with no body/custom headers is a CORS
    "simple request" (no preflight), so any website the user visits could hit a
    localhost endpoint. If a browser Origin header is present it must match Host;
    non-browser clients (curl, no Origin) are allowed.
    """
    origin = request.headers.get("origin")
    if not origin:
        return True
    host = request.headers.get("host")
    if not host:
        return True
    try:
        parsed = urlparse(origin)
        origin_host = parsed.hostname or ""
        origin_port = parsed.port
    except ValueError:
        return False
    if origin_port and origin_port not in (80, 443):
        origin_netloc = f"{origin_host}:{origin_port}"
    else:
        origin_netloc = origin_host
    host_normalized = host
    if host.endswith(":80") or host.endswith(":443"):
        host_normalized = host.rsplit(":", 1)[0]
    return origin_netloc == host_normalized


class AuthMiddleware:
    """Raw ASGI middleware for optional token auth.

    Uses raw ASGI instead of BaseHTTPMiddleware because the latter
    breaks WebSocket routes (wraps request body, preventing WS upgrade).
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        # Pass through if auth disabled, or non-HTTP/WS scope (e.g. lifespan)
        if not AUTH_TOKEN or scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path = scope["path"]

        # Skip auth for exempt endpoints and non-API paths (static frontend)
        if path in _AUTH_EXEMPT or not path.startswith("/api/"):
            await self.app(scope, receive, send)
            return

        if _check_auth(scope):
            await self.app(scope, receive, send)
            return

        # Reject — unauthenticated
        if scope["type"] == "websocket":
            # ASGI requires receiving websocket.connect before sending close
            await receive()
            await send({"type": "websocket.close", "code": 4401, "reason": "Unauthorized"})
        else:
            response = JSONResponse({"detail": "Unauthorized"}, status_code=401)
            await response(scope, receive, send)


# Singleton browser manager
browser_mgr = BrowserManager(license_key=LICENSE_KEY, release_channel=RELEASE_CHANNEL)

# Frontend build directory (React production build). bundle_dir() resolves to
# the PyInstaller extraction root when frozen, else the manager repo root.
FRONTEND_DIR = bundle_dir() / "frontend" / "dist"


# ---------------------------------------------------------------------------
# RFB server message translator — KasmVNC BinaryClipboard → standard RFB
# ---------------------------------------------------------------------------


def _parse_kasmvnc_clipboard(data: bytes) -> str | None:
    """Extract text/plain from KasmVNC BinaryClipboard (type 180).

    Format: type(1) + action(1) + flags(4) + entries...
    Each entry: mime_len(u8) + mime(N) + data_len(u32 BE) + data(M)
    """
    if len(data) < 7:
        return None
    offset = 6  # skip type(1) + action(1) + flags(4)
    while offset < len(data):
        if offset + 1 > len(data):
            break
        mime_len = data[offset]
        offset += 1
        if offset + mime_len > len(data):
            break
        mime_type = data[offset:offset + mime_len]
        offset += mime_len
        if offset + 4 > len(data):
            break
        data_len = struct.unpack_from(">I", data, offset)[0]
        offset += 4
        if mime_type == b"text/plain":
            end = min(offset + data_len, len(data))
            return data[offset:end].decode("utf-8", errors="replace")
        offset += data_len
    return None


def _build_server_cut_text(text: str) -> bytes:
    """Build standard RFB ServerCutText (type 3) message.

    RFB spec mandates Latin-1 encoding for ServerCutText.
    Characters outside Latin-1 (CJK, emoji, etc.) are replaced with '?'.
    """
    text_bytes = text.encode("latin-1", errors="replace")
    return struct.pack(">BxxxI", 3, len(text_bytes)) + text_bytes


# ---------------------------------------------------------------------------
# RFB client message filter — strip extension types KasmVNC doesn't support
# ---------------------------------------------------------------------------
# noVNC v1.4 batches multiple RFB messages into one WebSocket frame.
# KasmVNC 1.3.3 crashes on unsupported types (150, 248, etc.).
# We parse message boundaries using known sizes and keep only standard types.

# Client→server message sizes (fixed, except 2 and 6 which encode length)
_RFB_MSG_SIZE: dict[int, int | None] = {
    0: 20,    # SetPixelFormat
    2: None,  # SetEncodings — 4 + numEncodings*4 (rewritten to strip bad pseudo-encodings)
    3: 10,    # FramebufferUpdateRequest
    4: 8,     # KeyEvent
    5: 6,     # PointerEvent
    6: None,  # ClientCutText — 8 + length
}

# Extension types that noVNC sends — known sizes so we can skip past them
# instead of breaking and dropping all trailing data in the frame.
_RFB_EXTENSION_SIZE: dict[int, int] = {
    150: 10,  # EnableContinuousUpdates (1+1+2+2+2+2)
    248: 10,  # QEMU-like key event (observed from noVNC 1.4.0)
    252: 4,   # xvp (1+1+1+1)
    255: 4,   # QEMU audio control (1+1+2) — noVNC QEMUExtendedKeyEvent is actually 12
}

# Whitelist of encodings safe to send to KasmVNC.
# Instead of trying to blocklist problematic pseudo-encodings (error-prone —
# we had wrong numbers), we ONLY keep known-good encodings.
# Anything not on this list is stripped from SetEncodings.
_ALLOWED_ENCODINGS: set[int] = {
    # Framebuffer encodings (standard RFB)
    0,    # Raw
    1,    # CopyRect
    2,    # RRE
    5,    # Hextile
    7,    # Tight
    16,   # ZRLE
    # Safe pseudo-encodings
    -239,  # Cursor (0xFFFFFF11) — cursor shape
    -224,  # LastRect (0xFFFFFF20) — performance optimization
    # Tight quality/compress levels (these are just hints)
    *range(-32, -22),   # quality levels 0-9
    *range(-256, -246),  # compress levels 0-9
}


def _rfb_msg_length(data: bytes, offset: int) -> int | None:
    """Return total length of the RFB message at offset, or None if unrecognized."""
    if offset >= len(data):
        return None
    msg_type = data[offset]
    fixed = _RFB_MSG_SIZE.get(msg_type)
    if fixed is not None:
        return fixed
    remaining = len(data) - offset
    if msg_type == 2 and remaining >= 4:  # SetEncodings
        num_enc = struct.unpack_from(">H", data, offset + 2)[0]
        return 4 + num_enc * 4
    if msg_type == 6 and remaining >= 8:  # ClientCutText
        length = struct.unpack_from(">I", data, offset + 4)[0]
        return 8 + length
    # Known extension types — skip past them instead of giving up
    ext_size = _RFB_EXTENSION_SIZE.get(msg_type)
    if ext_size is not None:
        return ext_size
    return None  # truly unknown type


def _rewrite_set_encodings(data: bytes, offset: int, msg_len: int) -> bytes:
    """Keep only whitelisted encodings in a SetEncodings message."""
    _log = logging.getLogger("cloakbrowser.manager")
    num_enc = struct.unpack_from(">H", data, offset + 2)[0]
    kept = []
    stripped = []
    for i in range(num_enc):
        enc = struct.unpack_from(">i", data, offset + 4 + i * 4)[0]  # signed
        if enc in _ALLOWED_ENCODINGS:
            kept.append(enc)
        else:
            stripped.append(enc)
    if not stripped:
        return data[offset:offset + msg_len]
    _log.info("RFB filter: SetEncodings keeping %d: %s, stripped %d: %s", len(kept), kept, len(stripped), stripped)
    result = struct.pack(">BxH", 2, len(kept))
    for enc in kept:
        result += struct.pack(">i", enc)
    return result


def _rewrite_pointer_event(data: bytes, offset: int) -> bytes:
    """Convert standard 6-byte PointerEvent to KasmVNC's 11-byte format.

    Standard RFB:  [5:u8][mask:u8][x:u16][y:u16]          = 6 bytes
    KasmVNC:       [5:u8][mask:u16][x:u16][y:u16][sx:s16][sy:s16] = 11 bytes
    """
    mask = data[offset + 1]
    x = struct.unpack_from(">H", data, offset + 2)[0]
    y = struct.unpack_from(">H", data, offset + 4)[0]
    # Expand mask from u8 to u16.  Scroll deltas (sx, sy) are zero because
    # noVNC encodes scroll as button-mask bits (3=up, 4=down, 5=left, 6=right)
    # which pass through in the mask.  KasmVNC accepts mask-bit scroll on its
    # extended 11-byte format, so explicit deltas are unnecessary.
    return struct.pack(">BHHHhh", 5, mask, x, y, 0, 0)


def _filter_rfb_client_messages(data: bytes) -> bytes:
    """Parse concatenated RFB messages, keep only standard types (0-6).

    Rewrites PointerEvents from 6-byte standard to 11-byte KasmVNC format
    and strips unsupported pseudo-encodings from SetEncodings.
    """
    _log = logging.getLogger("cloakbrowser.manager")
    result = bytearray()
    offset = 0
    msg_idx = 0
    while offset < len(data):
        msg_type = data[offset]
        msg_len = _rfb_msg_length(data, offset)
        if msg_len is None:
            _log.info("RFB filter: DROPPING unknown type=%d at offset=%d/%d, skipping %d trailing bytes, hex=%s",
                       msg_type, offset, len(data), len(data) - offset, data[offset:offset+20].hex())
            break
        if offset + msg_len > len(data):
            # Incomplete message — DO NOT forward partial data, it desynchronizes
            # the RFB stream (KasmVNC buffers partial reads across frames).
            _log.warning("RFB filter: DROPPING incomplete type=%d need=%d have=%d — would desync stream",
                         msg_type, msg_len, len(data) - offset)
            break
        msg_idx += 1
        if msg_type in _RFB_MSG_SIZE:
            # Standard RFB type — keep (with rewrites for KasmVNC compatibility)
            _log.debug("RFB filter: KEEP type=%d len=%d at offset=%d (msg #%d in frame)", msg_type, msg_len, offset, msg_idx)
            if msg_type == 2:  # SetEncodings — whitelist safe encodings
                result.extend(_rewrite_set_encodings(data, offset, msg_len))
            elif msg_type == 5:  # PointerEvent — expand to KasmVNC's 11-byte format
                result.extend(_rewrite_pointer_event(data, offset))
            else:
                result.extend(data[offset:offset + msg_len])
        else:
            # Extension type (150, 248, etc.) — skip but continue parsing
            _log.debug("RFB filter: SKIP extension type=%d len=%d at offset=%d (msg #%d in frame)", msg_type, msg_len, offset, msg_idx)
        offset += msg_len
    if len(result) != len(data):
        _log.info("RFB filter: input=%d output=%d (delta %+d bytes)", len(data), len(result), len(result) - len(data))
    return bytes(result)


@asynccontextmanager
async def lifespan(app: FastAPI):
    browser_mgr.vnc.validate_available()
    db.init_db()
    await browser_mgr.cleanup_stale()
    # Resolve tier + pre-download the (Pro) binary before serving launches, so the
    # download never blocks a launch or auto-launch's 60s timeout.
    await asyncio.to_thread(browser_mgr.resolve_binary_status)
    browser_mgr._auto_launch_task = asyncio.create_task(browser_mgr.auto_launch_all())
    logger.info("CloakBrowser Manager started")
    yield
    logger.info("Shutting down — stopping all browsers...")
    if browser_mgr._auto_launch_task and not browser_mgr._auto_launch_task.done():
        browser_mgr._auto_launch_task.cancel()
        await asyncio.gather(browser_mgr._auto_launch_task, return_exceptions=True)
    await browser_mgr.cleanup_all()


app = FastAPI(title="CloakBrowser Manager", lifespan=lifespan)
app.add_middleware(AuthMiddleware)


# ── Authentication ────────────────────────────────────────────────────────────


@app.get("/api/auth/status")
async def auth_status(request: starlette.requests.Request):
    """Check if auth is enabled and if the current request is authenticated.

    Exempt from auth middleware so the frontend can always call it.
    """
    authenticated = False
    if AUTH_TOKEN:
        authenticated = _check_auth(request.scope)
    return {"auth_required": AUTH_TOKEN is not None, "authenticated": authenticated}


@app.post("/api/auth/login")
async def auth_login(body: LoginRequest, request: Request, response: Response):
    if not AUTH_TOKEN:
        return {"ok": True}
    if not body.token or not hmac.compare_digest(body.token, AUTH_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid token")
    is_https = _is_https(request)
    response.set_cookie(
        key="auth_token",
        value=AUTH_TOKEN,
        httponly=True,
        samesite="strict",
        secure=is_https,
        path="/",
    )
    return {"ok": True}


@app.post("/api/auth/logout")
async def auth_logout(request: Request, response: Response):
    is_https = _is_https(request)
    response.delete_cookie(
        key="auth_token", path="/", secure=is_https, samesite="strict",
    )
    return {"ok": True}


# ── Profile CRUD ──────────────────────────────────────────────────────────────


def _profile_response(profile: dict) -> ProfileResponse:
    payload = {**profile, **browser_mgr.get_status(profile["id"])}
    payload["tags"] = [TagResponse(**tag) for tag in profile.get("tags", [])]
    return ProfileResponse(**payload)


@app.get("/api/profiles", response_model=list[ProfileResponse])
async def list_profiles():
    return [_profile_response(profile) for profile in db.list_profiles()]


@app.post("/api/profiles", response_model=ProfileResponse, status_code=201)
async def create_profile(req: ProfileCreate):
    data = req.model_dump()
    tags = data.pop("tags", None)
    if tags:
        data["tags"] = [t.model_dump() if hasattr(t, "model_dump") else t for t in tags]
    else:
        data["tags"] = []
    return _profile_response(db.create_profile(**data))


@app.post("/api/profiles/reorder")
async def reorder_profiles(req: ReorderRequest):
    db.reorder_profiles(req.ordered_ids)
    return {"ok": True}


@app.get("/api/profiles/{profile_id}", response_model=ProfileResponse)
async def get_profile(profile_id: str):
    profile = db.get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    return _profile_response(profile)


@app.put("/api/profiles/{profile_id}", response_model=ProfileResponse)
async def update_profile(profile_id: str, req: ProfileUpdate):
    # Only pass fields that were explicitly set
    data = req.model_dump(exclude_unset=True)
    tags = data.pop("tags", None)
    if tags is not None:
        data["tags"] = [t.model_dump() if hasattr(t, "model_dump") else t for t in tags]
    profile = db.update_profile(profile_id, **data)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    return _profile_response(profile)


@app.delete("/api/profiles/{profile_id}")
async def delete_profile(profile_id: str):
    # Stop browser if running
    if profile_id in browser_mgr.running:
        await browser_mgr.stop(profile_id)

    profile = db.get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    user_data_dir = Path(profile["user_data_dir"])

    # DB first — if this fails, filesystem is untouched
    db.delete_profile(profile_id)

    # Then clean up disk
    if user_data_dir.exists():
        shutil.rmtree(user_data_dir, ignore_errors=True)

    return {"ok": True}


# Browser state wiped on reset (inside the profile's Default/ dir). Bookmarks,
# Preferences, Secure Preferences and Web Data are deliberately preserved so the
# profile keeps its default search engine (the Google keyword row lives in Web
# Data, the default pointer in Secure Preferences) and bookmarks across a reset —
# no fragile search-engine rebuild is needed.
_RESET_STATE_FILES = [
    "Cookies", "Cookies-journal",
    "History", "History-journal", "History Provider Cache",
    "Login Data", "Login Data-journal",
    "Favicons", "Favicons-journal",
    "Shortcuts", "Shortcuts-journal",
    "Top Sites", "Top Sites-journal",
    "Visited Links",
    "Network Action Predictor", "Network Action Predictor-journal",
    "TransportSecurity",
    "Current Session", "Current Tabs",
    "Last Session", "Last Tabs",
    "affiliation_db", "coupon_db",
    "DownloadMetadata",
]
_RESET_STATE_DIRS = [
    "Cache", "Code Cache", "GPUCache",
    "Service Worker", "Service Worker/CacheStorage",
    "Local Storage", "Session Storage",
    "IndexedDB", "databases",
    "blob_storage",
    "File System",
    "GCM Store",
    "Extension Rules", "Extension Scripts", "Extension State",
    "Platform Notifications",
]


@app.post("/api/profiles/{profile_id}/reset", response_model=ProfileResponse)
async def reset_profile(profile_id: str):
    """Reset a profile: stop browser, wipe state files, re-roll fingerprint seed.

    Preserves bookmarks, preferences and all profile settings (name, proxy, tags,
    etc.). Returns the updated (stopped) profile — the frontend re-launches it.
    """
    profile = db.get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    if profile_id in browser_mgr.running:
        await browser_mgr.stop(profile_id)

    user_data_dir = Path(profile["user_data_dir"])
    default_dir = user_data_dir / "Default"
    if default_dir.exists():
        for fname in _RESET_STATE_FILES:
            (default_dir / fname).unlink(missing_ok=True)
        for dname in _RESET_STATE_DIRS:
            shutil.rmtree(default_dir / dname, ignore_errors=True)

    updated = db.reset_profile(profile_id)
    if not updated:
        raise HTTPException(status_code=500, detail="Failed to reset profile")
    return _profile_response(updated)


# ── Launch / Stop ─────────────────────────────────────────────────────────────


@app.post("/api/profiles/{profile_id}/launch", response_model=LaunchResponse)
async def launch_profile(profile_id: str):
    profile = db.get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    if profile_id in browser_mgr.running:
        raise HTTPException(status_code=409, detail="Profile is already running")

    try:
        running = await browser_mgr.launch(profile)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error("Failed to launch profile %s: %s", profile_id, exc)
        raise HTTPException(status_code=500, detail="Failed to launch browser")

    return LaunchResponse(
        profile_id=profile_id,
        status="running",
        runtime_mode=browser_mgr.runtime.runtime_mode,
        viewer_mode=browser_mgr.runtime.viewer_mode,
        vnc_ws_port=running.ws_port,
        display=f":{running.display}" if running.display is not None else None,
        cdp_url=f"/api/profiles/{profile_id}/cdp",
    )


@app.post("/api/profiles/{profile_id}/stop")
async def stop_profile(profile_id: str):
    if profile_id not in browser_mgr.running:
        raise HTTPException(status_code=404, detail="Profile is not running")
    await browser_mgr.stop(profile_id)
    return {"ok": True}


@app.get("/api/profiles/{profile_id}/status", response_model=ProfileStatusResponse)
async def get_profile_status(profile_id: str):
    profile = db.get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    status = browser_mgr.get_status(profile_id)
    return ProfileStatusResponse(**status)


# ── System Status ─────────────────────────────────────────────────────────────


@app.get("/api/health")
async def health_check():
    """Unauthenticated liveness probe for the Docker healthcheck.

    Intentionally returns no system details; see /api/status (auth-gated)
    for running counts and versions.
    """
    return {"status": "ok"}


def _windows_font_health() -> tuple[int | None, int | None, bool | None]:
    """Report Windows persona font coverage only where that persona is emulated."""
    if not (
        browser_mgr.runtime.runtime_mode == "docker"
        and browser_mgr.runtime.host_os == "linux"
    ):
        return None, None, None
    try:
        # Delayed import keeps API tests usable when cloakbrowser is mocked.
        from cloakbrowser.browser import _WINDOWS_FONT_TELLS, _count_fonts_present

        present = _count_fonts_present(_WINDOWS_FONT_TELLS)
        required = len(_WINDOWS_FONT_TELLS)
        return present, required, None if present is None else present == required
    except Exception as exc:
        logger.warning("Could not inspect Windows font health: %s", exc)
        return None, None, None


@app.get("/api/status", response_model=StatusResponse)
async def get_system_status():
    # Prefer the version/tier resolved at startup (reflects the actual Pro build
    # in use). Fall back to the keyless constant before startup resolution runs.
    binary_version = browser_mgr.binary_version
    if not binary_version:
        try:
            from cloakbrowser.config import get_chromium_version

            binary_version = get_chromium_version()
        except ImportError:
            from cloakbrowser.config import CHROMIUM_VERSION

            binary_version = CHROMIUM_VERSION

    profiles = db.list_profiles()
    fonts_present, fonts_required, fonts_complete = await asyncio.to_thread(
        _windows_font_health
    )
    return StatusResponse(
        running_count=len(browser_mgr.running),
        binary_version=binary_version,
        license_tier=browser_mgr.license_tier,
        profiles_total=len(profiles),
        host_os=browser_mgr.runtime.host_os,
        runtime_mode=browser_mgr.runtime.runtime_mode,
        viewer_mode=browser_mgr.runtime.viewer_mode,
        windows_fonts_present=fonts_present,
        windows_fonts_required=fonts_required,
        windows_fonts_complete=fonts_complete,
    )


# ── Settings ─────────────────────────────────────────────────────────────────


def _mask_key(key: str | None) -> str | None:
    """Show enough of a license key to recognise it, never the whole thing."""
    if not key:
        return None
    if len(key) <= 9:
        return "…"
    return f"{key[:5]}…{key[-4:]}"


def _settings_response() -> SettingsResponse:
    return SettingsResponse(
        license_key_set=bool(browser_mgr.license_key),
        license_key_masked=_mask_key(browser_mgr.license_key),
        release_channel=(browser_mgr.release_channel or "stable"),
    )


@app.post("/api/shutdown")
async def shutdown_manager(request: Request):
    """Stop the Manager: graceful uvicorn shutdown → lifespan closes every
    running browser (cleanup_all) → the process exits. Lets the user quit from
    the UI so no orphaned server is left behind.

    CSRF-guarded: a bodyless POST is a CORS simple request (no preflight), so
    without this any site the user visits could kill the Manager via a no-cors
    fetch. Reject when a browser Origin is present and doesn't match Host.
    """
    if not _same_origin_request(request):
        logger.warning(
            "Rejected cross-origin shutdown (origin=%s)", request.headers.get("origin")
        )
        raise HTTPException(status_code=403, detail="Cross-origin request rejected")
    logger.info("Shutdown requested via UI — stopping server")
    server = getattr(app.state, "uvicorn_server", None)
    if server is not None:
        # Frozen/native path (app_entry holds the Server): flip the flag, uvicorn
        # exits its serve loop gracefully. Cross-platform, no signals.
        server.should_exit = True
    else:
        # Dev path (run.py spawns `uvicorn backend.main:app`): signal ourselves.
        async def _signal_self():
            await asyncio.sleep(0.3)
            os.kill(os.getpid(), signal.SIGINT)

        asyncio.create_task(_signal_self())
    return {"ok": True, "message": "CloakBrowser Manager is shutting down"}


@app.get("/api/settings", response_model=SettingsResponse)
async def get_settings():
    return _settings_response()


@app.put("/api/settings", response_model=StatusResponse)
async def update_settings(payload: SettingsUpdate):
    """Persist license key / release channel and hot-apply without a restart.

    Returns the refreshed system status (tier + resolved binary version) so the
    top-bar badge updates immediately. Re-resolving may download the Pro build,
    so it runs off the event loop; the request completes when it's ready.
    """
    stored = load_settings()

    if payload.license_key is not None:
        key = payload.license_key.strip()
        if key:
            stored["license_key"] = key
            browser_mgr.license_key = key
        else:  # empty string = clear the key (back to keyless)
            stored.pop("license_key", None)
            browser_mgr.license_key = None

    if payload.release_channel is not None:
        channel = payload.release_channel.strip().lower()
        if channel not in {"stable", "preview"}:
            raise HTTPException(
                status_code=400,
                detail="release_channel must be 'stable' or 'preview'",
            )
        stored["release_channel"] = channel
        browser_mgr.release_channel = channel

    save_settings(stored)
    await asyncio.to_thread(browser_mgr.resolve_binary_status)
    return await get_system_status()


# ── Clipboard Relay ──────────────────────────────────────────────────────────

_CLIPBOARD_MAX_READ = 1_048_576  # 1MB cap on GET response

# Track xclip processes per display so we can kill the old one before spawning new
_xclip_procs: dict[int, asyncio.subprocess.Process] = {}


@app.post("/api/profiles/{profile_id}/clipboard")
async def set_clipboard(profile_id: str, body: ClipboardRequest):
    """Push text into the VNC session's X clipboard via xclip."""
    running = browser_mgr.running.get(profile_id)
    if not running:
        raise HTTPException(status_code=404, detail="Profile not running")
    if browser_mgr.runtime.viewer_mode != "vnc" or running.display is None:
        raise HTTPException(
            status_code=409,
            detail="Clipboard relay is available only in Docker/VNC mode",
        )

    import os

    # Kill previous xclip for this display (it stays alive to serve paste)
    old = _xclip_procs.pop(running.display, None)
    if old and old.returncode is None:
        old.kill()
        await old.wait()

    env = {**os.environ, "DISPLAY": f":{running.display}"}
    proc = await asyncio.create_subprocess_exec(
        "xclip", "-selection", "clipboard",
        stdin=asyncio.subprocess.PIPE,
        env=env,
    )
    # xclip reads stdin then stays alive to serve paste requests.
    proc.stdin.write(body.text.encode())  # type: ignore[union-attr]
    await proc.stdin.drain()  # type: ignore[union-attr]
    proc.stdin.close()  # type: ignore[union-attr]

    _xclip_procs[running.display] = proc

    return {"ok": True}


@app.get("/api/profiles/{profile_id}/clipboard")
async def get_clipboard(profile_id: str):
    """Read the VNC session's clipboard.

    Chrome doesn't write to X11 clipboard under KasmVNC, so xclip can't read it.
    Instead, read via Playwright's CDP connection to Chrome (navigator.clipboard.readText).
    Falls back to xclip for non-Chrome clipboard owners.
    """
    running = browser_mgr.running.get(profile_id)
    if not running:
        raise HTTPException(status_code=404, detail="Profile not running")
    if browser_mgr.runtime.viewer_mode != "vnc" or running.display is None:
        raise HTTPException(
            status_code=409,
            detail="Clipboard relay is available only in Docker/VNC mode",
        )

    # Read Chrome's current text selection via Playwright.
    # Chrome's native copy (via VNC Ctrl+C) doesn't write to X11 clipboard
    # and doesn't fire DOM events, so we read the visible selection instead.
    # The init script also captures copy events when they do fire.
    # Check all pages — user may have copied in any tab
    try:
        for page in running.context.pages:
            try:
                text = await page.evaluate("window.__clipboardText || ''")
                if text:
                    return {"text": text[:_CLIPBOARD_MAX_READ]}
            except Exception as exc:
                logger.debug("Clipboard read failed on page: %s", exc)
                continue
    except Exception as exc:
        logger.debug("Playwright clipboard read failed: %s", exc)

    # Fallback: xclip for non-Chrome clipboard owners
    import os

    env = {**os.environ, "DISPLAY": f":{running.display}"}
    proc = await asyncio.create_subprocess_exec(
        "xclip", "-selection", "clipboard", "-o",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {"text": ""}

    if proc.returncode != 0:
        return {"text": ""}

    text = stdout[:_CLIPBOARD_MAX_READ].decode("utf-8", errors="replace")
    return {"text": text}


# ── VNC WebSocket Proxy ──────────────────────────────────────────────────────


@app.websocket("/api/profiles/{profile_id}/vnc")
async def vnc_proxy(websocket: WebSocket, profile_id: str):
    """Proxy WebSocket frames between the frontend and a profile's KasmVNC."""
    if not await _check_websocket_origin(websocket):
        return

    running = browser_mgr.running.get(profile_id)
    if not running:
        await websocket.close(code=4004, reason="Profile not running")
        return
    if (
        browser_mgr.runtime.viewer_mode != "vnc"
        or running.display is None
        or running.ws_port is None
    ):
        await websocket.close(code=4005, reason="VNC unavailable in native-window mode")
        return

    # Accept with client's requested subprotocol (if any) — RFC 6455 requires
    # the server must not respond with a subprotocol the client didn't request.
    requested = websocket.scope.get("subprotocols", [])
    subprotocol = "binary" if "binary" in requested else None
    await websocket.accept(subprotocol=subprotocol)

    import websockets

    vnc_url = f"ws://127.0.0.1:{running.ws_port}/websockify"

    try:
        async with websockets.connect(
            vnc_url,
            subprotocols=["binary"],
            origin=f"http://127.0.0.1:{running.ws_port}",
            max_size=None,  # VNC frames can be large (1920x1080 framebuffer)
            ping_interval=None,  # KasmVNC doesn't respond to WS pings
            ping_timeout=None,
            compression=None,  # KasmVNC can't handle permessage-deflate
        ) as vnc_ws:
            logger.info(
                "VNC proxy: connected to KasmVNC for %s (subprotocol=%s)",
                profile_id, vnc_ws.subprotocol,
            )

            # noVNC v1.4 sends extension message types (150=ContinuousUpdates,
            # 248=QEMUKey, etc.) that KasmVNC 1.3.3 doesn't support, causing
            # "unknown message type" → disconnect.
            #
            # noVNC batches multiple RFB messages into a single WebSocket frame,
            # so we must parse the RFB stream to find message boundaries and strip
            # unsupported types before forwarding. Standard client→server types
            # have known fixed sizes (except SetEncodings and ClientCutText which
            # encode their length).

            async def client_to_vnc():
                count = 0
                handshake = 0  # first 3 messages are RFB handshake
                dropped = 0
                try:
                    while True:
                        msg = await websocket.receive()
                        msg_type = msg.get("type", "")
                        if msg_type == "websocket.disconnect":
                            logger.info("VNC proxy [c->v]: client disconnect (code=%s) after %d msgs (%d dropped)", msg.get("code"), count, dropped)
                            break
                        if "bytes" in msg and msg["bytes"]:
                            count += 1
                            data = msg["bytes"]
                            handshake += 1

                            # First 3 messages are RFB handshake — forward as-is
                            if handshake <= 3:
                                logger.debug("VNC handshake #%d: %d bytes hex=%s", handshake, len(data), data[:20].hex())
                                await vnc_ws.send(data)
                                continue

                            # Parse RFB messages and strip unsupported types
                            filtered = _filter_rfb_client_messages(data)
                            if filtered:
                                # Safety: verify first byte is a valid RFB client type
                                if filtered[0] not in _RFB_MSG_SIZE:
                                    logger.error("RFB SAFETY: refusing to send data with invalid first byte=%d hex=%s",
                                                 filtered[0], filtered[:20].hex())
                                    dropped += 1
                                    continue
                                logger.debug("VNC send: %d bytes first_type=%d hex=%s", len(filtered), filtered[0], filtered[:100].hex())
                                await vnc_ws.send(filtered)
                            else:
                                dropped += 1

                        elif "text" in msg and msg["text"]:
                            # noVNC only sends binary frames — text frames are unexpected
                            # and would bypass the RFB filter, so drop them.
                            count += 1
                            logger.warning("VNC proxy [c->v]: DROPPING text frame len=%d (noVNC should only send binary)", len(msg["text"]))
                            dropped += 1
                        else:
                            logger.warning("VNC proxy [c->v]: unhandled msg keys=%s type=%s", list(msg.keys()), msg_type)
                except WebSocketDisconnect as exc:
                    logger.info("VNC proxy [c->v]: WebSocketDisconnect code=%s after %d msgs (%d dropped)", exc.code, count, dropped)
                except Exception as exc:
                    logger.warning("VNC proxy [c->v]: %s: %s (after %d msgs)", type(exc).__name__, exc, count)

            async def vnc_to_client():
                count = 0
                try:
                    async for msg in vnc_ws:
                        count += 1
                        if isinstance(msg, bytes) and len(msg) > 0:
                            msg_type = msg[0]
                            if msg_type == 180:
                                # KasmVNC BinaryClipboard → convert to standard
                                # ServerCutText (type 3) so noVNC can handle it
                                text = _parse_kasmvnc_clipboard(msg)
                                if text:
                                    logger.info("VNC proxy [v->c]: clipboard %d chars", len(text))
                                    await websocket.send_bytes(_build_server_cut_text(text))
                                else:
                                    logger.info("VNC proxy [v->c]: dropped type 180 (no text/plain)")
                                continue
                            await websocket.send_bytes(msg)
                        elif isinstance(msg, bytes):
                            await websocket.send_bytes(msg)
                        else:
                            await websocket.send_text(msg)
                    logger.info("VNC proxy [v->c]: KasmVNC stream ended after %d msgs (close_code=%s)", count, vnc_ws.close_code)
                except WebSocketDisconnect as exc:
                    logger.info("VNC proxy [v->c]: client disconnect code=%s after %d msgs", exc.code, count)
                except Exception as exc:
                    logger.warning("VNC proxy [v->c]: %s: %s (after %d msgs)", type(exc).__name__, exc, count)

            c2v = asyncio.create_task(client_to_vnc(), name="c2v")
            v2c = asyncio.create_task(vnc_to_client(), name="v2c")

            done, pending = await asyncio.wait(
                [c2v, v2c],
                return_when=asyncio.FIRST_COMPLETED,
            )
            finished = [t.get_name() for t in done]
            still_running = [t.get_name() for t in pending]

            # Check if Xvnc is still alive
            vnc_instance = browser_mgr.vnc._allocated.get(running.display)
            xvnc_alive = vnc_instance and vnc_instance.process and vnc_instance.process.poll() is None
            logger.info(
                "VNC proxy: finished=%s pending=%s xvnc_alive=%s display=:%d for %s",
                finished, still_running, xvnc_alive, running.display, profile_id,
            )

            # Dump Xvnc log on disconnect
            import os
            xvnc_log = f"/tmp/xvnc-{running.display}.log"
            if os.path.exists(xvnc_log):
                with open(xvnc_log) as f:
                    log_content = f.read()
                if log_content.strip():
                    for line in log_content.strip().split("\n")[-20:]:
                        logger.info("Xvnc[:%d] %s", running.display, line)

            for task in pending:
                task.cancel()

    except Exception as exc:
        logger.error("VNC proxy connect error for %s: %s: %s", profile_id, type(exc).__name__, exc)
    finally:
        try:
            await websocket.close()
        except Exception as exc:
            logger.debug("VNC proxy: websocket.close() failed: %s", exc)


# ── CDP WebSocket Proxy ──────────────────────────────────────────────────────
# Simple bidirectional passthrough — CDP is standard JSON over WebSocket,
# no protocol translation needed (unlike VNC which requires RFB filtering).


@app.get("/api/profiles/{profile_id}/cdp")
async def cdp_info(profile_id: str):
    """Return CDP connection info. Prevents SPA catch-all from serving index.html."""
    running = browser_mgr.running.get(profile_id)
    if not running:
        raise HTTPException(status_code=404, detail="Profile not running")
    return {
        "cdp_url": f"/api/profiles/{profile_id}/cdp",
        "usage": "playwright.chromium.connect_over_cdp('http://<host>/api/profiles/"
        + profile_id + "/cdp')",
    }


@app.get("/api/profiles/{profile_id}/cdp/json/version/")
@app.get("/api/profiles/{profile_id}/cdp/json/version")
async def cdp_json_version(profile_id: str, request: Request):
    """Proxy Chrome's /json/version, rewriting WS URLs to go through our proxy."""
    running = browser_mgr.running.get(profile_id)
    if not running:
        raise HTTPException(status_code=404, detail="Profile not running")

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"http://127.0.0.1:{running.cdp_port}/json/version", timeout=5
            )
            data = resp.json()
    except Exception as exc:
        logger.error("CDP proxy: failed to reach Chrome CDP for %s: %s", profile_id, exc)
        raise HTTPException(status_code=502, detail="CDP endpoint unreachable")

    # Rewrite webSocketDebuggerUrl to point through our proxy
    host = request.headers.get("host", "localhost:8080")
    ws_scheme = "wss" if _is_https(request) else "ws"
    data["webSocketDebuggerUrl"] = f"{ws_scheme}://{host}/api/profiles/{profile_id}/cdp"
    return data


@app.get("/api/profiles/{profile_id}/cdp/json/list/")
@app.get("/api/profiles/{profile_id}/cdp/json/list")
@app.get("/api/profiles/{profile_id}/cdp/json/")
@app.get("/api/profiles/{profile_id}/cdp/json")
async def cdp_json_list(profile_id: str, request: Request):
    """Proxy Chrome's /json/list, rewriting WS URLs."""
    running = browser_mgr.running.get(profile_id)
    if not running:
        raise HTTPException(status_code=404, detail="Profile not running")

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"http://127.0.0.1:{running.cdp_port}/json/list", timeout=5
            )
            data = resp.json()
    except Exception as exc:
        logger.error("CDP proxy: failed to reach Chrome CDP for %s: %s", profile_id, exc)
        raise HTTPException(status_code=502, detail="CDP endpoint unreachable")

    host = request.headers.get("host", "localhost:8080")
    ws_scheme = "wss" if _is_https(request) else "ws"
    for entry in data:
        if "webSocketDebuggerUrl" in entry:
            ws_path = entry["webSocketDebuggerUrl"].split("/devtools/")[-1]
            entry["webSocketDebuggerUrl"] = (
                f"{ws_scheme}://{host}/api/profiles/{profile_id}/cdp/devtools/{ws_path}"
            )
    return data


async def _proxy_cdp_websocket(
    websocket: WebSocket, target_url: str, label: str,
) -> None:
    """Bidirectional WebSocket proxy between a FastAPI client and a CDP target.

    Used by both browser-level and page-level CDP proxy endpoints.
    """
    import websockets

    try:
        async with websockets.connect(
            target_url, max_size=None, ping_interval=None, ping_timeout=None
        ) as cdp_ws:
            logger.info("%s: connected to %s", label, target_url)

            async def client_to_cdp():
                try:
                    while True:
                        msg = await websocket.receive()
                        if msg.get("type") == "websocket.disconnect":
                            break
                        if "text" in msg and msg["text"]:
                            await cdp_ws.send(msg["text"])
                        elif "bytes" in msg and msg["bytes"]:
                            await cdp_ws.send(msg["bytes"])
                except WebSocketDisconnect:
                    pass
                except Exception as exc:
                    logger.warning("%s [c->cdp]: %s: %s", label, type(exc).__name__, exc)

            async def cdp_to_client():
                try:
                    async for msg in cdp_ws:
                        if isinstance(msg, str):
                            await websocket.send_text(msg)
                        else:
                            await websocket.send_bytes(msg)
                except WebSocketDisconnect:
                    pass
                except Exception as exc:
                    logger.warning("%s [cdp->c]: %s: %s", label, type(exc).__name__, exc)

            c2d = asyncio.create_task(client_to_cdp(), name="c2d")
            d2c = asyncio.create_task(cdp_to_client(), name="d2c")
            done, pending = await asyncio.wait(
                [c2d, d2c], return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            logger.info("%s: disconnected", label)

    except Exception as exc:
        logger.error("%s error: %s", label, exc)
    finally:
        try:
            await websocket.close()
        except Exception as exc:
            logger.debug("%s: websocket.close() failed: %s", label, exc)


@app.websocket("/api/profiles/{profile_id}/cdp")
async def cdp_proxy(websocket: WebSocket, profile_id: str):
    """Proxy WebSocket frames between external tools and Chrome's CDP."""
    if not await _check_websocket_origin(websocket):
        return

    running = browser_mgr.running.get(profile_id)
    if not running:
        await websocket.close(code=4004, reason="Profile not running")
        return

    await websocket.accept()

    # Get browser-level CDP WebSocket URL from Chrome
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"http://127.0.0.1:{running.cdp_port}/json/version", timeout=5
            )
            ws_url = resp.json()["webSocketDebuggerUrl"]
    except Exception as exc:
        logger.error("CDP proxy: failed to get WS URL for %s: %s", profile_id, exc)
        await websocket.close(code=4005, reason="CDP not available")
        return

    await _proxy_cdp_websocket(websocket, ws_url, f"CDP proxy [{profile_id}]")


@app.websocket("/api/profiles/{profile_id}/cdp/devtools/{path:path}")
async def cdp_page_proxy(websocket: WebSocket, profile_id: str, path: str):
    """Proxy page-specific CDP WebSocket connections (e.g. /devtools/page/GUID)."""
    if not await _check_websocket_origin(websocket):
        return

    running = browser_mgr.running.get(profile_id)
    if not running:
        await websocket.close(code=4004, reason="Profile not running")
        return

    await websocket.accept()

    target_url = f"ws://127.0.0.1:{running.cdp_port}/devtools/{path}"
    await _proxy_cdp_websocket(websocket, target_url, f"CDP page proxy [{profile_id}]")


# ── Static Frontend ───────────────────────────────────────────────────────────

# Serve React build. Must be AFTER API routes so /api/* isn't caught by the SPA.
if FRONTEND_DIR.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        """Serve React SPA — all non-API routes return index.html."""
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not found")
        file_path = FRONTEND_DIR / full_path
        if full_path and file_path.exists() and file_path.is_file():
            return FileResponse(file_path)
        return FileResponse(FRONTEND_DIR / "index.html")
