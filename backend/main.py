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
from .browser_manager import LAUNCH_TIMEOUT_S, BrowserManager, ProfileAlreadyRunning
from .models import (
    ClipboardRequest,
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

# Paths that bypass authentication even when AUTH_TOKEN is set
# (/api/viewer-auth is exempt: the viewer token in X-Original-URI is the credential)
_AUTH_EXEMPT = frozenset({"/api/auth/status", "/api/auth/login", "/api/status", "/api/viewer-auth"})


def _check_auth(scope: Scope) -> bool:
    """Check if the request has a valid auth token (header or cookie)."""
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
browser_mgr = BrowserManager()

# Frontend build directory (React production build)
FRONTEND_DIR = Path(__file__).parent.parent / "frontend" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    await browser_mgr.cleanup_stale()
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


@app.get("/api/profiles", response_model=list[ProfileResponse])
async def list_profiles():
    profiles = db.list_profiles()
    result = []
    for p in profiles:
        status = browser_mgr.get_status(p["id"])
        p["status"] = status["status"]
        p["vnc_ws_port"] = status["vnc_ws_port"]
        p["cdp_url"] = status["cdp_url"]
        p["tags"] = [TagResponse(**t) for t in p.get("tags", [])]
        result.append(ProfileResponse(**p))
    return result


@app.post("/api/profiles", response_model=ProfileResponse, status_code=201)
async def create_profile(req: ProfileCreate):
    data = req.model_dump()
    tags = data.pop("tags", None)
    if tags:
        data["tags"] = [t.model_dump() if hasattr(t, "model_dump") else t for t in tags]
    else:
        data["tags"] = []
    profile = db.create_profile(**data)
    status = browser_mgr.get_status(profile["id"])
    profile["status"] = status["status"]
    profile["vnc_ws_port"] = status["vnc_ws_port"]
    profile["cdp_url"] = status["cdp_url"]
    profile["tags"] = [TagResponse(**t) for t in profile.get("tags", [])]
    return ProfileResponse(**profile)


@app.get("/api/profiles/{profile_id}", response_model=ProfileResponse)
async def get_profile(profile_id: str):
    profile = db.get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    status = browser_mgr.get_status(profile_id)
    profile["status"] = status["status"]
    profile["vnc_ws_port"] = status["vnc_ws_port"]
    profile["cdp_url"] = status["cdp_url"]
    profile["tags"] = [TagResponse(**t) for t in profile.get("tags", [])]
    return ProfileResponse(**profile)


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
    status = browser_mgr.get_status(profile_id)
    profile["status"] = status["status"]
    profile["vnc_ws_port"] = status["vnc_ws_port"]
    profile["cdp_url"] = status["cdp_url"]
    profile["tags"] = [TagResponse(**t) for t in profile.get("tags", [])]
    return ProfileResponse(**profile)


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
    if browser_mgr.is_wedged(profile_id):
        raise HTTPException(
            status_code=409, detail="Browser is still shutting down; try again",
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
                status_code=409, detail="Browser is still shutting down; try again",
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
        display=f":{running.display}",
        cdp_url=f"/api/profiles/{profile_id}/cdp",
    )


@app.post("/api/profiles/{profile_id}/stop")
async def stop_profile(profile_id: str):
    if profile_id not in browser_mgr.running:
        # Mid-launch is not "not running" — stopping now would race the launch
        # into registering the instance we just tore down.
        if browser_mgr.is_starting(profile_id):
            raise HTTPException(status_code=409, detail="Profile is starting; try again")
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
    token = viewer_tokens.issue(profile_id, running.ws_port, ttl=VIEWER_TOKEN_TTL)
    logger.info("Issued viewer token for profile %s (ttl=%ds)", profile_id, VIEWER_TOKEN_TTL)
    return ViewerTokenResponse(
        token=token,
        viewer_url=f"/viewer/{token}/",
        expires_in=VIEWER_TOKEN_TTL,
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
    # token carries the ws_port it was minted for; the credentials come from
    # the live display. If a token ever outlives a relaunch, mixing the two
    # would point nginx at a stale port while handing it valid-looking auth
    # for a different display.
    if session.ws_port != running.ws_port:
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

            # /api/get_frame_stats only works while the client collects perf
            # stats (client-side enable_perf_stats); otherwise it 503s after
            # a 10s wait. Isolate it so bottleneck/sessions still return.
            frame = None
            if isinstance(sessions, dict) and sessions.get("users"):
                try:
                    frame_resp = await client.get(
                        f"http://127.0.0.1:{running.ws_port}/api/get_frame_stats",
                        params={"client": "all"},
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
    return StatusResponse(
        running_count=len(browser_mgr.running),
        binary_version=CHROMIUM_VERSION,
        profiles_total=len(profiles),
    )


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


# ── CDP WebSocket Proxy ──────────────────────────────────────────────────────
# Simple bidirectional passthrough — CDP is standard JSON over WebSocket.


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
