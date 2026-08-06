"""CloakBrowser Manager — FastAPI application.

Serves the React dashboard (static files) and provides a REST API
for browser profile management with live VNC viewing.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import shutil
import time
from contextlib import asynccontextmanager
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import starlette.requests
from starlette.types import ASGIApp, Receive, Scope, Send

from . import database as db
from .binary_status import snapshot as binary_status_snapshot
from .browser_manager import (
    DEFAULT_IDLE_TIMEOUT_S,
    KASM_VIEWER_PROBE_TIMEOUT_S,
    LAUNCH_TIMEOUT_S,
    BrowserManager,
    ProfileAlreadyRunning,
    RunningProfile,
    _viewer_attached,
)
from .extensions import list_available_extensions
from .vnc_manager import viewer_stream_mode_preference
from .models import (
    ClipboardRequest,
    ExtensionResponse,
    LaunchResponse,
    LoginRequest,
    ProfileCreate,
    ProfileResponse,
    ProfileStatusResponse,
    ProfileUpdate,
    StatusResponse,
    TagResponse,
    ViewerTokenResponse,
)
from .viewer_tokens import VIEWER_TOKEN_TTL, viewer_tokens

logger = logging.getLogger("cloakbrowser.manager")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)

# Optional authentication via AUTH_TOKEN env var.
# If not set, all routes are open (local dev). If set, all /api/* routes
# (except /api/auth/* and /api/status) require Bearer token or cookie.
AUTH_TOKEN: str | None = os.environ.get("AUTH_TOKEN") or None

# One string for one state. launch, stop and delete used to answer a wedged
# teardown with three different messages (and two different status codes), so
# an operator could not tell they were looking at the same thing.
_SHUTTING_DOWN_DETAIL = "Browser is still shutting down; try again"

# Paths that bypass authentication even when AUTH_TOKEN is set
# (/api/viewer-auth is exempt: the viewer token in X-Original-URI is the credential)
_AUTH_EXEMPT = frozenset({"/api/auth/status", "/api/auth/login", "/api/status", "/api/viewer-auth"})

# Per-profile routes are a CAPABILITY URL: the profile's own id is the
# credential, exactly as it is for a cloud provider's presigned URL. This is
# what keeps the CDP endpoint usable by every CDP client there is — Playwright,
# Puppeteer, chrome-remote-interface, agent-browser — none of which can attach
# an Authorization header to a WebSocket handshake, so requiring a bearer token
# there means requiring a patched client.
#
# The id is uuid4 (database.py), so 122 random bits: not enumerable, and the
# collection routes that WOULD enumerate it — GET/POST /api/profiles — are
# deliberately NOT matched here and still demand the token. That asymmetry is
# the entire security model, so the pattern is anchored at both ends and
# insists on the canonical 8-4-4-4-12 form. A looser match ("starts with
# /api/profiles/") would exempt /api/profiles/ itself on some routers, and with
# it the listing this is supposed to protect.
#
# Consequences worth stating plainly: the id grants FULL control of that one
# profile (CDP is arbitrary code execution in the browser, plus its cookies and
# storage), it does not expire, and it cannot be revoked short of deleting the
# profile. Treat the URL as the secret it is — nginx.conf redacts it from the
# access log for the same reason it redacts viewer tokens.
_PROFILE_CAPABILITY_PATH = re.compile(
    r"^/api/profiles/"
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"(/.*)?$"
)


def _check_auth(scope: Scope) -> bool:
    """Check if the request has a valid auth token (header or cookie).

    A malformed credential can only ever mean "unauthenticated". It used to
    mean HTTP 500 plus a traceback: `Authorization: Bearer <0xe9>` raised
    UnicodeDecodeError from the utf-8 decode, and a well-formed non-ASCII
    token raised TypeError out of hmac.compare_digest, both before any
    authentication decision. Comparing the raw ASGI bytes avoids the decode
    entirely, and the blanket except makes the failure mode a 401 rather than
    an unauthenticated error-rate amplifier.
    """
    try:
        expected = AUTH_TOKEN.encode()
        # Check Authorization: Bearer <token> header
        for key, val in scope.get("headers", []):
            if key == b"authorization":
                if val.startswith(b"Bearer "):
                    token = val[7:]
                    if token and hmac.compare_digest(token, expected):
                        return True
                break

        # Check auth_token cookie
        for key, val in scope.get("headers", []):
            if key == b"cookie":
                cookies = SimpleCookie()
                # latin-1 is the ASGI header encoding and is byte-preserving,
                # so it cannot raise on any header a client can send.
                cookies.load(val.decode("latin-1"))
                if "auth_token" in cookies:
                    cookie_val = cookies["auth_token"].value
                    if cookie_val and hmac.compare_digest(
                        cookie_val.encode("latin-1"), expected,
                    ):
                        return True
                break
    except Exception as exc:
        logger.warning("Rejecting a malformed credential: %s", type(exc).__name__)
        return False

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

        # A per-profile path carries its own credential in the id — see
        # _PROFILE_CAPABILITY_PATH. Enumeration is what the token still guards.
        if _PROFILE_CAPABILITY_PATH.match(path):
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
browser_mgr = BrowserManager()

# Frontend build directory (React production build)
FRONTEND_DIR = Path(__file__).parent.parent / "frontend" / "dist"


def _demote_abandoned_driver_errors(loop, context: dict) -> None:
    """Keep a correctly-handled wedged teardown out of the ERROR log.

    _close_context_bounded deliberately abandons a close that outran its bound;
    when the driver connection later drops, that future's exception is never
    retrieved and asyncio reports it at ERROR with a stack trace, no profile id
    and no context. Operators (and log alerts) read that as a crash in a path
    that in fact worked exactly as designed. Anything else is left alone.
    """
    exc = context.get("exception")
    message = context.get("message", "")
    if "never retrieved" in message and isinstance(exc, Exception) and (
        "Connection closed while reading from the driver" in str(exc)
        or "Target page, context or browser has been closed" in str(exc)
    ):
        logger.warning(
            "Abandoned Playwright close finally failed (expected after a wedged "
            "teardown): %s", exc,
        )
        return
    loop.default_exception_handler(context)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    # Scanned here, eagerly, rather than lazily on first request — see
    # extensions.py's module docstring for why this only ever happens once
    # per process (i.e. per container start/restart).
    list_available_extensions()
    asyncio.get_running_loop().set_exception_handler(_demote_abandoned_driver_errors)
    browser_mgr.add_display_released_hook(_reap_xclip_for_display)
    await browser_mgr.cleanup_stale()
    browser_mgr._auto_launch_task = asyncio.create_task(browser_mgr.auto_launch_all())
    # Nothing else re-examines a teardown claim: the status path is a pure peek
    # by design, so without this loop a profile whose browser exited quietly
    # stays "stopping" with launch/stop/delete all refusing, forever. It also
    # reaps browsers whose Playwright driver died without emitting a close.
    browser_mgr._sweep_task = asyncio.create_task(browser_mgr.run_maintenance())
    logger.info("CloakBrowser Manager started")
    yield
    logger.info("Shutting down — stopping all browsers...")
    background = [browser_mgr._auto_launch_task, browser_mgr._sweep_task]
    for task in background:
        if task and not task.done():
            task.cancel()
    await asyncio.gather(*[t for t in background if t], return_exceptions=True)
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
    """Attach the cheap lifecycle fields to a DB row.

    One helper rather than four inlined copies: the copies drifted apart the
    moment a fourth lifecycle value existed, and three of the four read paths
    would have kept reporting "stopped" for a profile mid-teardown while the
    fourth told the truth. get_status() is a pure peek, so this is safe on the
    3s poll.
    """
    status = browser_mgr.get_status(profile["id"])
    return ProfileResponse(**{
        **profile,
        "status": status["status"],
        "vnc_ws_port": status["vnc_ws_port"],
        "cdp_url": status["cdp_url"],
        "tags": [TagResponse(**t) for t in profile.get("tags", [])],
    })


@app.get("/api/profiles", response_model=list[ProfileResponse])
async def list_profiles():
    return [_profile_response(p) for p in db.list_profiles()]


@app.post("/api/profiles", response_model=ProfileResponse, status_code=201)
async def create_profile(req: ProfileCreate):
    data = req.model_dump()
    tags = data.pop("tags", None)
    if tags:
        data["tags"] = [t.model_dump() if hasattr(t, "model_dump") else t for t in tags]
    else:
        data["tags"] = []
    # Unspecified (None, not an explicit []) defaults to every extension
    # available right now, not none — a new profile starts with everything
    # the operator has staged already enabled rather than opted out.
    if data.get("enabled_extensions") is None:
        data["enabled_extensions"] = [e["id"] for e in list_available_extensions()]
    return _profile_response(db.create_profile(**data))


@app.get("/api/profiles/{profile_id}", response_model=ProfileResponse)
async def get_profile(profile_id: str):
    profile = db.get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    return _profile_response(profile)


# Settings only — never cookies, cache, or any other on-disk profile data,
# and never fingerprint_seed: the whole point of duplicating a profile is
# usually a second, independently-fingerprinted variant, not an identical
# twin that would look like the same machine to a detection service.
_DUPLICATE_FIELDS = (
    "proxy", "timezone", "locale", "platform", "user_agent",
    "screen_width", "screen_height", "gpu_vendor", "gpu_renderer",
    "hardware_concurrency", "humanize", "human_preset", "headless",
    "geoip", "clipboard_sync", "auto_launch", "color_scheme",
    "license_key", "enabled_extensions", "idle_timeout_seconds",
    "launch_args", "notes",
)


def _dedupe_profile_name(base_name: str) -> str:
    """"X" -> "X (copy)" -> "X (copy 2)" -> ... — duplicating a duplicate
    must not collide with (or overwrite the intent of) the one before it.
    name has no uniqueness constraint in the schema, so this is purely for
    a sidebar that would otherwise show several indistinguishable entries.
    """
    existing = {p["name"] for p in db.list_profiles()}
    candidate = f"{base_name} (copy)"
    if candidate not in existing:
        return candidate
    n = 2
    while f"{base_name} (copy {n})" in existing:
        n += 1
    return f"{base_name} (copy {n})"


@app.post("/api/profiles/{profile_id}/duplicate", response_model=ProfileResponse, status_code=201)
async def duplicate_profile(profile_id: str):
    source = db.get_profile(profile_id)
    if not source:
        raise HTTPException(status_code=404, detail="Profile not found")

    fields = {key: source.get(key) for key in _DUPLICATE_FIELDS}
    fields["tags"] = [
        {"tag": t["tag"], "color": t.get("color")} for t in source.get("tags", [])
    ]
    new_profile = db.create_profile(name=_dedupe_profile_name(source["name"]), **fields)
    return _profile_response(new_profile)


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
    # A launch in flight owns the user_data_dir: deleting now would rmtree it
    # under a Chromium that is still starting, and the launch would then
    # register a RunningProfile for a profile that no longer exists.
    if browser_mgr.is_starting(profile_id):
        raise HTTPException(status_code=409, detail="Profile is starting; try again")

    # Hold the id for the whole delete: the re-check after stop() narrows the
    # launch race but cannot close it, because a launch can still claim the
    # profile between that check and the rmtree.
    if not browser_mgr.claim_for_delete(profile_id):
        raise HTTPException(status_code=409, detail="Profile is already being deleted")
    try:
        return await _delete_profile_locked(profile_id)
    finally:
        browser_mgr.release_delete_claim(profile_id)


async def _delete_profile_locked(profile_id: str):
    # A previous /stop may have left Chromium alive (bounded close timed out).
    # It is out of `running`, so the stop below would be skipped and the rmtree
    # would run under a live browser.
    if await browser_mgr.check_wedged(profile_id):
        raise HTTPException(
            status_code=409, detail=_SHUTTING_DOWN_DETAIL,
        )

    # Stop browser if running
    if profile_id in browser_mgr.running:
        closed = await browser_mgr.stop(profile_id)
        # stop() awaits a context close, and a launch can claim the profile
        # during that window. Deleting now would rmtree user_data_dir under a
        # Chromium that is starting on it. Re-check rather than race.
        if browser_mgr.is_starting(profile_id) or profile_id in browser_mgr.running:
            raise HTTPException(status_code=409, detail="Profile restarted; try again")
        if not closed:
            # The teardown outlived its bound, so Chromium may still be alive
            # and writing to user_data_dir. Deleting it now corrupts a live
            # profile; refuse rather than race a wedged browser.
            raise HTTPException(
                status_code=409, detail=_SHUTTING_DOWN_DETAIL,
            )

    # Revoke viewer tokens even if the profile wasn't running
    viewer_tokens.revoke_profile(profile_id)

    profile = db.get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    user_data_dir = Path(profile["user_data_dir"])

    # DB first — if this fails, filesystem is untouched
    db.delete_profile(profile_id)

    # Then clean up disk. Off the event loop: a Chromium profile directory can
    # be hundreds of MB, and rmtree here would block every other request for
    # its duration — including the nginx auth_request subrequests that gate the
    # viewer, stalling live sessions on an unrelated delete.
    if user_data_dir.exists():
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: shutil.rmtree(user_data_dir, ignore_errors=True),
        )

    return {"ok": True}


# ── Launch / Stop ─────────────────────────────────────────────────────────────


@app.post("/api/profiles/{profile_id}/launch", response_model=LaunchResponse)
async def launch_profile(profile_id: str):
    profile = db.get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    if profile_id in browser_mgr.running:
        raise HTTPException(status_code=409, detail="Profile is already running")
    if browser_mgr.is_starting(profile_id):
        raise HTTPException(status_code=409, detail="Profile is already starting")
    # Say what is actually true. A teardown in flight used to surface here as
    # "Profile is already running" — flatly contradicting the status the same
    # API reported a poll earlier, and contradicting DELETE, which reported the
    # shutdown correctly. One state, one message.
    if await browser_mgr.check_wedged(profile_id):
        raise HTTPException(status_code=409, detail=_SHUTTING_DOWN_DETAIL)

    try:
        # Same ceiling auto-launch uses: without it a wedged Playwright call
        # hangs the request and strands the id in `_launching` forever.
        running = await asyncio.wait_for(
            browser_mgr.launch(profile), timeout=LAUNCH_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.error("Launch timed out for profile %s", profile_id)
        raise HTTPException(status_code=504, detail="Launch timed out")
    except ProfileAlreadyRunning:
        # Lost a race with another launch (or a delete) after the checks above.
        raise HTTPException(status_code=409, detail="Profile is already running")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error("Failed to launch profile %s: %s", profile_id, exc)
        raise HTTPException(status_code=500, detail="Failed to launch browser")

    return LaunchResponse(
        profile_id=profile_id,
        status="running",
        vnc_ws_port=running.ws_port,
        display=f":{running.display}" if running.display is not None else None,
        cdp_url=f"/api/profiles/{profile_id}/cdp",
    )


@app.post("/api/profiles/{profile_id}/stop")
async def stop_profile(profile_id: str):
    if profile_id not in browser_mgr.running:
        # Mid-launch is not "not running" — stopping now would race the launch
        # into registering the instance we just tore down.
        if browser_mgr.is_starting(profile_id):
            raise HTTPException(status_code=409, detail="Profile is starting; try again")
        # Neither is mid-teardown. 404 "Profile is not running" was the third
        # of three contradictory answers for one state (list said "stopped",
        # launch said "already running", delete said "shutting down"), and it
        # is the one that tells the operator to stop trying.
        if await browser_mgr.check_wedged(profile_id):
            raise HTTPException(status_code=409, detail=_SHUTTING_DOWN_DETAIL)
        raise HTTPException(status_code=404, detail="Profile is not running")
    closed = await browser_mgr.stop(profile_id)
    # Report honestly: the display is reclaimed either way, but a wedged
    # Chromium means the profile is not cleanly down.
    return {"ok": True, "browser_closed": closed}


@app.get("/api/profiles/{profile_id}/status", response_model=ProfileStatusResponse)
async def get_profile_status(profile_id: str):
    profile = db.get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    status = await browser_mgr.get_liveness_async(profile_id)
    return ProfileStatusResponse(**status)


# ── Viewer Sessions (KasmVNC native client) ──────────────────────────────────
# FastAPI is the control plane only: it issues short-lived viewer tokens and
# authorizes viewer requests for nginx (auth_request). nginx proxies the
# actual page assets and WebSocket straight to KasmVNC's own HTTP server.


def _extract_viewer_token(uri: str) -> str | None:
    """Extract the viewer token from an X-Original-URI like /viewer/<token>/..."""
    path = uri.split("?", 1)[0]
    parts = path.split("/")
    # expect ["", "viewer", "<token>", ...]
    if len(parts) >= 3 and parts[1] == "viewer" and parts[2]:
        return parts[2]
    return None


@app.post("/api/profiles/{profile_id}/viewer-token", response_model=ViewerTokenResponse)
async def create_viewer_token(profile_id: str):
    """Issue a fresh short-lived viewer token for a running profile.

    Each page load calls this once; the token then authorizes both the viewer
    page assets and the WebSocket (via /api/viewer-auth) until expiry.
    """
    running = browser_mgr.running.get(profile_id)
    if not running:
        # A profile mid-launch (container restart, auto-launch queue) is not
        # gone — 404 is terminal to the viewer, 503 tells it to keep retrying.
        if browser_mgr.is_starting(profile_id):
            raise HTTPException(status_code=503, detail="Profile is starting")
        raise HTTPException(status_code=404, detail="Profile not running")
    if running.ws_port is None:
        # Headless: no Xvnc was ever started, so there is nothing to view.
        # 409 rather than 404 — the profile IS running, it just has no
        # display; 404 would send the viewer to a "session ended" overlay
        # implying the browser died.
        raise HTTPException(
            status_code=409, detail="Profile is headless and has no viewer",
        )
    token = viewer_tokens.issue(
        profile_id, running.ws_port, ttl=VIEWER_TOKEN_TTL,
        session_epoch=running.session_epoch,
    )
    logger.info("Issued viewer token for profile %s (ttl=%ds)", profile_id, VIEWER_TOKEN_TTL)
    return ViewerTokenResponse(
        token=token,
        viewer_url=f"/viewer/{token}/",
        expires_in=VIEWER_TOKEN_TTL,
        stream_mode=viewer_stream_mode_preference(),
    )


@app.get("/api/viewer-auth")
async def viewer_auth(request: Request):
    """Authorize a viewer request for nginx auth_request.

    Exempt from app auth — the viewer token itself is the credential.
    On success returns 200 with X-Viewer-Upstream so nginx can proxy to the
    right KasmVNC instance. Never logs the token value.
    """
    token = _extract_viewer_token(request.headers.get("x-original-uri", ""))
    if not token:
        raise HTTPException(status_code=403, detail="Missing viewer token")

    session = viewer_tokens.validate(token)
    if not session:
        raise HTTPException(status_code=403, detail="Invalid or expired viewer token")

    running = browser_mgr.running.get(session.profile_id)
    if not running:
        # 403, not 404: nginx auth_request only understands 2xx (allow) and
        # 401/403 (deny). Anything else is "auth request unexpected status"
        # and the viewer gets a bare 500 instead of a clean denial.
        raise HTTPException(status_code=403, detail="Profile not running")

    # Upstream and credentials must come from the same source of truth. The
    # token names the session it was minted for; the credentials come from the
    # live display. If a token ever outlives a relaunch, mixing the two would
    # point nginx at a stale port while handing it valid-looking auth for a
    # different display.
    #
    # Be clear about what this is: NEITHER comparison can fire today. Every
    # path that clears `running` — stop(), _on_browser_closed() and DELETE —
    # revokes the profile's tokens first, so a token that reaches here always
    # belongs to the session currently in `running` and validate() has already
    # rejected the rest. That ordering is the real defence, and it is pinned by
    # test_teardown_revokes_before_the_profile_can_be_relaunched.
    #
    # It is kept because it is the only thing standing between a future reorder
    # of that ordering and cross-session authorization: a surviving token would
    # otherwise be handed the NEW session's upstream and the NEW display's
    # credentials. This turns that into a clean 403 instead.
    #
    # The epoch is what does the work; the port is a free second assertion that
    # cannot fire on its own, because allocate() gap-fills from BASE_DISPLAY up
    # and a stop+relaunch of the same profile deterministically returns the
    # identical display and ws_port.
    if session.session_epoch != running.session_epoch or session.ws_port != running.ws_port:
        logger.info("Viewer token for profile %s predates a relaunch", session.profile_id)
        raise HTTPException(status_code=403, detail="Viewer token is stale")

    headers = {"X-Viewer-Upstream": f"127.0.0.1:{running.ws_port}"}
    # Kasm's HTTP layer requires Basic auth; nginx injects this on the
    # origin request so the browser never handles the credentials.
    creds = browser_mgr.vnc.get_api_credentials(running.display)
    if creds:
        import base64

        raw = f"{creds[0]}:{creds[1]}".encode()
        headers["X-Viewer-Authorization"] = "Basic " + base64.b64encode(raw).decode()

    return Response(status_code=200, headers=headers)


# An idle profile never produces the encoded frame get_frame_stats waits for,
# so this request is expected to time out; keep it well under the client-wide
# timeout so a stats poll stays cheap.
KASM_FRAME_STATS_TIMEOUT_S = 1.5
# KASM_VIEWER_PROBE_TIMEOUT_S and _viewer_attached live in browser_manager.py
# (imported above) — reap_idle_profiles needs the exact same "is a viewer
# attached" definition this endpoint uses, and a second copy would eventually
# disagree with the first.


def _viewer_client_count(bottleneck) -> int | None:
    """Number of attached viewer endpoints, or None if indeterminate.

    The payload is {username: {peerEndpoint: [...]}} — one inner key per live
    socket, so -AlwaysShared multi-viewer sessions are counted, not collapsed.
    """
    if not isinstance(bottleneck, dict):
        return None
    return sum(len(v) for v in bottleneck.values() if isinstance(v, dict))


@app.get("/api/profiles/{profile_id}/viewer-attached")
async def viewer_attached(profile_id: str):
    """Whether a viewer WebSocket is currently attached to this profile.

    The frontend's connected-heartbeat can only prove the *processes* are alive
    via /status, so a half-open viewer socket leaves a green dot over a frozen
    frame until TCP RTO fires. This is the missing signal, and it is cheap:
    one bounded localhost GET, no frame-clock dependency.

    Always 200 for a running profile — `viewer_attached: null` means the probe
    could not answer, which is deliberately distinct from `false` so a caller
    cannot mistake a stats-endpoint hiccup for a dead viewer.
    """
    running = browser_mgr.running.get(profile_id)
    if not running:
        raise HTTPException(status_code=404, detail="Profile not running")

    bottleneck = None
    try:
        auth = browser_mgr.vnc.get_api_credentials(running.display)
        async with httpx.AsyncClient(
            timeout=KASM_VIEWER_PROBE_TIMEOUT_S, auth=auth,
        ) as client:
            resp = await client.get(
                f"http://127.0.0.1:{running.ws_port}/api/get_bottleneck_stats"
            )
        if resp.status_code >= 400:
            # Not a 502: an unauthenticated or broken stats endpoint says
            # nothing about the viewer socket, and the heartbeat that consumes
            # this must be able to shrug it off rather than error out.
            logger.warning(
                "Viewer probe: bottleneck stats returned HTTP %d for %s",
                resp.status_code, profile_id,
            )
        else:
            bottleneck = _json_or_raw(resp)
    except Exception as exc:
        logger.debug("Viewer probe unavailable for %s: %s", profile_id, exc)

    return {
        "viewer_attached": _viewer_attached(bottleneck),
        "clients": _viewer_client_count(bottleneck),
    }


@app.get("/api/profiles/{profile_id}/kasm-stats")
async def kasm_stats(profile_id: str):
    """Fetch KasmVNC's bottleneck/frame stats for a running profile."""
    running = browser_mgr.running.get(profile_id)
    if not running:
        raise HTTPException(status_code=404, detail="Profile not running")

    try:
        # Kasm's management API always requires owner Basic auth (random
        # per-display credentials generated at Xvnc launch).
        auth = browser_mgr.vnc.get_api_credentials(running.display)
        async with httpx.AsyncClient(timeout=5, auth=auth) as client:
            bottleneck_resp, sessions_resp = await asyncio.gather(
                client.get(f"http://127.0.0.1:{running.ws_port}/api/get_bottleneck_stats"),
                client.get(f"http://127.0.0.1:{running.ws_port}/api/get_sessions"),
            )
            # Kasm answers 401 (bad/missing owner creds) or 5xx with an HTML
            # body. _json_or_raw would happily hand that back as the payload,
            # so the caller would see 200 with an error page where the stats
            # should be — indistinguishable from real data.
            for label, resp in (("bottleneck", bottleneck_resp), ("sessions", sessions_resp)):
                if resp.status_code >= 400:
                    logger.error(
                        "Kasm stats: %s returned HTTP %d for %s",
                        label, resp.status_code, profile_id,
                    )
                    raise HTTPException(
                        status_code=502,
                        detail=f"KasmVNC stats endpoint returned {resp.status_code}",
                    )
            # With no viewer connected these can be empty/non-JSON.
            bottleneck = _json_or_raw(bottleneck_resp)
            sessions = _json_or_raw(sessions_resp)

            # /api/get_frame_stats blocks until Kasm produces an encoded frame
            # with non-zero encoding time (websocket.c:1600-1612 polls
            # netServerFrameStatsReady 500x20ms), then answers 503. It has
            # nothing to do with the client's enable_perf_stats, which drives a
            # separate non-fatal 2s wait — a live client on a static screen
            # still eats the full 10s. So it must be both gated and bounded.
            #
            # Gate on bottleneck, never on sessions: get_sessions is provably
            # stale. VNCServerST::updateSessionUsersList only republishes when
            # the list is NON-empty, and the disconnect path marks the client
            # CLOSING (so it stops counting as authenticated) before calling it
            # — the last viewer's departure computes an empty list and throws it
            # away, leaving sessionsInfo populated forever. Gating on it made
            # this call fire on every /kasm-stats after the first viewer ever
            # attached, costing the client's whole 5s read timeout each time.
            frame = None
            if _viewer_attached(bottleneck):
                try:
                    frame_resp = await client.get(
                        f"http://127.0.0.1:{running.ws_port}/api/get_frame_stats",
                        params={"client": "all"},
                        # Own timeout: an idle profile draws no frames, so this
                        # request is expected to hang. 1.5s bounds the common
                        # case instead of paying the client-wide 5s.
                        timeout=KASM_FRAME_STATS_TIMEOUT_S,
                    )
                    frame = _json_or_raw(frame_resp)
                except Exception as exc:
                    logger.debug("Kasm frame stats unavailable for %s: %s", profile_id, exc)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Kasm stats: failed to reach KasmVNC for %s: %s", profile_id, exc)
        raise HTTPException(status_code=502, detail="KasmVNC stats endpoint unreachable")

    return {"bottleneck": bottleneck, "sessions": sessions, "frame": frame}


def _json_or_raw(resp: httpx.Response):
    """Parse a Kasm API response as JSON, falling back to raw text/None.

    Kasm emits `-nan` for not-yet-measured stats, which is not valid JSON —
    normalize it to null before parsing.
    """
    try:
        return resp.json()
    except ValueError:
        pass
    try:
        return json.loads(re.sub(r"-?nan\b", "null", resp.text))
    except ValueError:
        return resp.text or None


# ── System Status ─────────────────────────────────────────────────────────────


@app.get("/api/status", response_model=StatusResponse)
async def get_system_status():
    from cloakbrowser.config import CHROMIUM_VERSION

    profiles = db.list_profiles()
    binary = binary_status_snapshot()
    return StatusResponse(
        running_count=len(browser_mgr.running),
        binary_version=CHROMIUM_VERSION,
        profiles_total=len(profiles),
        binary_downloading=binary["downloading"],
        binary_download_percent=binary["percent"],
        binary_download_state=binary["state"],
        default_idle_timeout_seconds=DEFAULT_IDLE_TIMEOUT_S,
    )


# ── Extensions ────────────────────────────────────────────────────────────────


@app.get("/api/extensions", response_model=list[ExtensionResponse])
async def get_extensions():
    # list_available_extensions() is cached from the scan lifespan already
    # did at startup — this is a plain in-memory read, not a filesystem scan.
    return [ExtensionResponse(**e) for e in list_available_extensions()]


# ── Clipboard Relay ──────────────────────────────────────────────────────────

_CLIPBOARD_MAX_READ = 1_048_576  # 1MB cap on GET response
# Playwright 1.60 has no timeout parameter on page.evaluate and the call is
# unbounded all the way down (_inner_send waits on FIRST_COMPLETED with no
# deadline), so a page running `while(true){}` made GET /clipboard hang
# forever — permanently, because the loop never reached the next page or the
# xclip fallback, and uvicorn does not cancel a handler when the client goes
# away. Both a per-page and a whole-loop bound are needed: without the second,
# N wedged tabs sum to N * the first.
_CLIPBOARD_PAGE_TIMEOUT_S = 2.0
_CLIPBOARD_READ_TIMEOUT_S = 5.0
_CLIPBOARD_MAX_PAGES = 20

# Track xclip processes per display so we can kill the old one before spawning new
_xclip_procs: dict[int, asyncio.subprocess.Process] = {}
# One lock per display. The pop/spawn/store sequence below contains two awaits,
# so three concurrent pastes on one display each popped the same entry and only
# the last was tracked — the other two owned the X11 CLIPBOARD selection,
# were never killed, and outlived the request.
_xclip_locks: dict[int, asyncio.Lock] = {}


def _xclip_lock(display: int) -> asyncio.Lock:
    lock = _xclip_locks.get(display)
    if lock is None:
        lock = _xclip_locks[display] = asyncio.Lock()
    return lock


def _reap_xclip_for_display(display: int) -> None:
    """Kill the xclip bound to a display once its Xvnc is gone.

    Registered on BrowserManager at startup. Nothing used to remove entries
    from _xclip_procs on stop, delete or crash, so every xclip ever spawned
    survived for the container's lifetime holding a selection on a dead X
    server.
    """
    proc = _xclip_procs.pop(display, None)
    _xclip_locks.pop(display, None)
    if proc is not None and proc.returncode is None:
        try:
            proc.kill()
        except (ProcessLookupError, OSError) as exc:
            logger.debug("xclip for :%d already gone: %s", display, exc)


@app.post("/api/profiles/{profile_id}/clipboard")
async def set_clipboard(profile_id: str, body: ClipboardRequest):
    """Push text into the VNC session's X clipboard via xclip."""
    running = browser_mgr.running.get(profile_id)
    if not running:
        raise HTTPException(status_code=404, detail="Profile not running")

    import os

    display = running.display
    if display is None:
        # Headless: no X server, so there is no X clipboard to push into.
        # Without this the DISPLAY below becomes the literal ":None" and
        # xclip fails with an error the caller cannot act on.
        raise HTTPException(
            status_code=409, detail="Profile is headless and has no X clipboard",
        )
    async with _xclip_lock(display):
        # Kill previous xclip for this display (it stays alive to serve paste)
        old = _xclip_procs.pop(display, None)
        if old and old.returncode is None:
            old.kill()
            await old.wait()

        env = {**os.environ, "DISPLAY": f":{display}"}
        try:
            proc = await asyncio.create_subprocess_exec(
                "xclip", "-selection", "clipboard",
                stdin=asyncio.subprocess.PIPE,
                env=env,
            )
        except (FileNotFoundError, OSError) as exc:
            # No xclip (local dev per the README) used to escape the handler as
            # an unhandled FileNotFoundError and 500 with a traceback.
            logger.warning("Clipboard relay unavailable for :%d: %s", display, exc)
            raise HTTPException(status_code=503, detail="Clipboard relay unavailable")

        try:
            # xclip reads stdin then stays alive to serve paste requests.
            proc.stdin.write(body.text.encode())  # type: ignore[union-attr]
            await proc.stdin.drain()  # type: ignore[union-attr]
            proc.stdin.close()  # type: ignore[union-attr]
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            # xclip exits immediately when DISPLAY is unreachable (a dead
            # Xvnc). Reap it here rather than leaving it untracked.
            logger.warning("Clipboard write failed on :%d: %s", display, exc)
            if proc.returncode is None:
                proc.kill()
            raise HTTPException(status_code=503, detail="Clipboard relay unavailable")

        _xclip_procs[display] = proc

    return {"ok": True}


async def _read_clipboard_from_pages(context) -> str:
    """First non-empty captured selection across a context's pages.

    Each evaluate is individually bounded so ONE wedged tab is skipped rather
    than fatal — previously it blocked the loop before page[1] and before the
    xclip fallback, killing the endpoint for that profile permanently. The page
    count is capped so the caller's own bound is not the only thing standing
    between a tab explosion and a slow request.
    """
    for page in list(context.pages)[:_CLIPBOARD_MAX_PAGES]:
        try:
            text = await asyncio.wait_for(
                page.evaluate("window.__clipboardText || ''"),
                timeout=_CLIPBOARD_PAGE_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            logger.debug("Clipboard read timed out on a wedged page; skipping")
            continue
        except Exception as exc:
            logger.debug("Clipboard read failed on page: %s", exc)
            continue
        if text:
            return text
    return ""


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

    # Read Chrome's current text selection via Playwright.
    # Chrome's native copy (via VNC Ctrl+C) doesn't write to X11 clipboard
    # and doesn't fire DOM events, so we read the visible selection instead.
    # The init script also captures copy events when they do fire.
    # Check all pages — user may have copied in any tab
    try:
        text = await asyncio.wait_for(
            _read_clipboard_from_pages(running.context),
            timeout=_CLIPBOARD_READ_TIMEOUT_S,
        )
        if text:
            return {"text": text[:_CLIPBOARD_MAX_READ]}
    except Exception as exc:
        # Includes the whole-loop TimeoutError: N wedged tabs must not sum to
        # N * _CLIPBOARD_PAGE_TIMEOUT_S before the xclip fallback is tried.
        logger.debug("Playwright clipboard read failed: %s", exc)

    # Fallback: xclip for non-Chrome clipboard owners. The Playwright read
    # above works for a headless profile; this leg cannot, because there is
    # no X server behind it.
    import os

    if running.display is None:
        return {"text": ""}

    env = {**os.environ, "DISPLAY": f":{running.display}"}
    try:
        proc = await asyncio.create_subprocess_exec(
            "xclip", "-selection", "clipboard", "-o",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except (FileNotFoundError, OSError) as exc:
        # Degrade to an empty clipboard rather than a 500 with a traceback.
        logger.warning("Clipboard relay unavailable for :%d: %s", running.display, exc)
        return {"text": ""}
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


# ── CDP WebSocket Proxy ──────────────────────────────────────────────────────
# Simple bidirectional passthrough — CDP is standard JSON over WebSocket.


async def _ensure_running_for_cdp(profile_id: str) -> tuple[RunningProfile | None, str]:
    """Auto-launch a profile on its first CDP hit if it isn't already running.

    A safeguard, not the primary flow: whoever holds a profile id is expected
    to have launched it through the UI/API already, exactly like the idle
    reaper expects them to have stopped watching it before it goes idle. But a
    CDP client (Playwright, Puppeteer, chrome-remote-interface) that dials
    straight into a stopped profile id should get a working session, not a
    404 for a profile that plainly exists and could serve one — the same
    capability-URL model that already lets that id skip the bearer token
    (see _PROFILE_CAPABILITY_PATH above) covers launching it too.

    Returns (running, "") on success, or (None, <reason>) — the reason is
    "Profile not found" only when the id has no profile at all; every other
    failure is a launch problem, not a 404.
    """
    running = browser_mgr.running.get(profile_id)
    if running:
        return running, ""

    profile = db.get_profile(profile_id)
    if not profile:
        return None, "Profile not found"

    if browser_mgr.is_starting(profile_id):
        # Something else already kicked off a launch (e.g. a concurrent CDP
        # hit, or the UI). Wait it out instead of racing a second launch.
        deadline = time.monotonic() + LAUNCH_TIMEOUT_S
        while time.monotonic() < deadline:
            await asyncio.sleep(0.2)
            running = browser_mgr.running.get(profile_id)
            if running:
                return running, ""
            if not browser_mgr.is_starting(profile_id):
                break
        running = browser_mgr.running.get(profile_id)
        return (running, "") if running else (None, "Launch timed out")

    try:
        running = await asyncio.wait_for(
            browser_mgr.launch(profile), timeout=LAUNCH_TIMEOUT_S,
        )
        return running, ""
    except asyncio.TimeoutError:
        logger.error("CDP auto-launch timed out for %s", profile_id)
        return None, "Launch timed out"
    except ProfileAlreadyRunning:
        # Lost a race with a concurrent launch; its result is what we want.
        running = browser_mgr.running.get(profile_id)
        return (running, "") if running else (None, "Launch failed")
    except Exception as exc:
        logger.error("CDP auto-launch failed for %s: %s", profile_id, exc)
        return None, "Failed to launch browser"


@app.get("/api/profiles/{profile_id}/cdp")
async def cdp_info(profile_id: str, request: Request):
    """Return CDP connection info. Prevents SPA catch-all from serving index.html."""
    running, error = await _ensure_running_for_cdp(profile_id)
    if not running:
        raise HTTPException(
            status_code=404 if error == "Profile not found" else 502, detail=error,
        )
    # Scheme and host from the ORIGINAL request, like the neighbouring
    # /json/version endpoint. A hardcoded http://<host> behind a TLS terminator
    # is a snippet that cannot work: an https-only ingress either refuses it or
    # redirects, with nothing on screen explaining why.
    scheme = "https" if _is_https(request) else "http"
    host = request.headers.get("host", "localhost:8080")
    return {
        "cdp_url": f"/api/profiles/{profile_id}/cdp",
        "usage": (
            f"playwright.chromium.connect_over_cdp('{scheme}://{host}"
            f"/api/profiles/{profile_id}/cdp')"
        ),
    }


@app.get("/api/profiles/{profile_id}/cdp/json/version/")
@app.get("/api/profiles/{profile_id}/cdp/json/version")
async def cdp_json_version(profile_id: str, request: Request):
    """Proxy Chrome's /json/version, rewriting WS URLs to go through our proxy."""
    running, error = await _ensure_running_for_cdp(profile_id)
    if not running:
        raise HTTPException(
            status_code=404 if error == "Profile not found" else 502, detail=error,
        )

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
    running, error = await _ensure_running_for_cdp(profile_id)
    if not running:
        raise HTTPException(
            status_code=404 if error == "Profile not found" else 502, detail=error,
        )

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
    websocket: WebSocket, target_url: str, label: str, profile_id: str,
) -> None:
    """Bidirectional WebSocket proxy between a FastAPI client and a CDP target.

    Used by both browser-level and page-level CDP proxy endpoints. Touches the
    profile's idle clock on every forwarded message (either direction) — but
    NOT on the protocol-level ping/pong uvicorn and this proxy exchange to
    keep the socket open, which never reaches `receive()`/`async for` as an
    app-level message. A CDP client attached but issuing no real commands is
    exactly the case reap_idle_profiles is meant to catch.
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
                            browser_mgr.touch_activity(profile_id)
                            await cdp_ws.send(msg["text"])
                        elif "bytes" in msg and msg["bytes"]:
                            browser_mgr.touch_activity(profile_id)
                            await cdp_ws.send(msg["bytes"])
                except WebSocketDisconnect:
                    pass
                except Exception as exc:
                    logger.warning("%s [c->cdp]: %s: %s", label, type(exc).__name__, exc)

            async def cdp_to_client():
                try:
                    async for msg in cdp_ws:
                        browser_mgr.touch_activity(profile_id)
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

    running, error = await _ensure_running_for_cdp(profile_id)
    if not running:
        await websocket.close(
            code=4004 if error == "Profile not found" else 4005, reason=error,
        )
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

    await _proxy_cdp_websocket(websocket, ws_url, f"CDP proxy [{profile_id}]", profile_id)


@app.websocket("/api/profiles/{profile_id}/cdp/devtools/{path:path}")
async def cdp_page_proxy(websocket: WebSocket, profile_id: str, path: str):
    """Proxy page-specific CDP WebSocket connections (e.g. /devtools/page/GUID)."""
    if not await _check_websocket_origin(websocket):
        return

    running, error = await _ensure_running_for_cdp(profile_id)
    if not running:
        await websocket.close(
            code=4004 if error == "Profile not found" else 4005, reason=error,
        )
        return

    await websocket.accept()

    target_url = f"ws://127.0.0.1:{running.cdp_port}/devtools/{path}"
    await _proxy_cdp_websocket(
        websocket, target_url, f"CDP page proxy [{profile_id}]", profile_id,
    )


# ── Static Frontend ───────────────────────────────────────────────────────────


def _resolve_spa_file(base: Path, full_path: str) -> Path | None:
    """Path under `base` for a SPA request, or None to fall through to index.

    Containment is the whole job. `base / full_path` is NOT safe on its own:
    Path.__truediv__ DISCARDS the left side when the right side is absolute, so
    `Path("/app/frontend/dist") / "/etc/passwd"` is `/etc/passwd`. The catch-all
    route below is not behind the auth middleware (it gates only /api/*), and
    nginx forwards a percent-encoded `%2f` verbatim without decoding it, so an
    unauthenticated `GET /%2fdata/profiles.db` reached this function with
    full_path="/data/profiles.db" and served the profile database.

    A pure function so the containment rule is testable directly: the route is
    only registered when the built frontend exists, which it does not in a
    source checkout.
    """
    if not full_path:
        return None
    candidate = Path(full_path)
    # Reject absolute paths outright rather than relying on the resolve()
    # containment check alone — it is the specific case `/` silently swallows.
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    try:
        resolved = (base / candidate).resolve()
        root = base.resolve()
    except OSError:
        return None
    # resolve() follows symlinks, so this also refuses a link inside the build
    # directory that points outside it.
    if resolved != root and root not in resolved.parents:
        return None
    return resolved if resolved.is_file() else None


# Serve React build. Must be AFTER API routes so /api/* isn't caught by the SPA.
if FRONTEND_DIR.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        """Serve React SPA — all non-API routes return index.html."""
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not found")
        file_path = _resolve_spa_file(FRONTEND_DIR, full_path)
        if file_path is not None:
            return FileResponse(file_path)
        return FileResponse(FRONTEND_DIR / "index.html")
