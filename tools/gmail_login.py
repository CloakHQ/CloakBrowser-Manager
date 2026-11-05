r"""Sign a CSV of Gmail accounts in, one CloakManager profile at a time.

The Manager holds a single license seat, so exactly one profile may run at a
time: this tool creates (or reuses) a profile per email, launches it, drives the
Google sign-in form over the Manager's CDP proxy with Playwright, records the
outcome, and stops the profile again before the next account.

Nothing here touches the Manager's database or the account file: profiles are
created through the HTTP API, results land in a checkpoint CSV of their own.

Usage (from the repository root, with the Manager venv):

    .venv\Scripts\python.exe tools\gmail_login.py --dry-run --limit 2
    .venv\Scripts\python.exe tools\gmail_login.py --limit 1
    .venv\Scripts\python.exe tools\gmail_login.py --batch 30 --batch-sleep 600

Resume is automatic: an email already recorded as ``OK`` in the checkpoint is
skipped. ``BLOCKED`` (2FA / "verify it's you") is skipped too unless
``--retry-blocked`` is given, because those need a human at the keyboard.

The password is typed with real key events into the page; it is never put in a
profile note (the Manager stores notes in plaintext).
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

DEFAULT_ACCOUNTS = Path(r"C:\Users\DANKIET\Documents\Gmail\check.csv")
DEFAULT_OUT = Path(r"C:\Users\DANKIET\Documents\Gmail\login-run.csv")
DEFAULT_SHOTS = Path(r"C:\Users\DANKIET\Documents\Gmail\login-run-screenshots")
DEFAULT_BASE_URL = "http://127.0.0.1:8090"

CHECKPOINT_FIELDS = (
    "email", "status", "note", "profile_id", "proxy_key_id",
    "at", "seconds", "screenshot",
)

# Statuses that end a run: the browser itself is being refused, so every further
# attempt burns an account without teaching us anything new.
FATAL_STATUSES = {"INSECURE_BROWSER"}

SIGNIN_URL = (
    "https://accounts.google.com/signin/v2/identifier"
    "?hl=vi&flowName=GlifWebSignIn&flowEntry=ServiceLogin"
)
GMAIL_URL = "https://mail.google.com/mail/u/0/"

EMAIL_SEL = 'input[name="identifier"], #identifierId'
PASSWORD_SEL = 'input[type="password"], input[name="Passwd"]'
SESSION_COOKIES = {"SID", "SAPISID", "SSID", "HSID"}

# Page text -> outcome. Matched casefolded against the sign-in page's body text;
# the first hit wins, so hard failures come before the softer challenge screens.
# A "challenge" status means Google accepted the password and wants a second
# factor, which for this workload is where a human has to take over.
SIGNATURES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("INSECURE_BROWSER", (
        "this browser or app may not be secure",
        "trình duyệt hoặc ứng dụng này có thể không bảo mật",
    )),
    ("DISABLED", (
        "account has been disabled",
        "tài khoản của bạn đã bị vô hiệu hóa",
        "tài khoản của bạn đã bị vô hiệu hoá",
        "tài khoản này đã bị vô hiệu hóa",
    )),
    ("BAD_ACCOUNT", (
        "couldn't find your google account",
        "không thể tìm thấy tài khoản google của bạn",
        "không tìm thấy tài khoản google của bạn",
        "enter a valid email",
        "nhập một email hợp lệ",
    )),
    ("WRONG_PASSWORD", (
        "wrong password",
        "mật khẩu sai",
        "sai mật khẩu",
        "mật khẩu không chính xác",
    )),
    ("CHALLENGE_QR", (
        "quét mã qr",
        "scan the qr code",
    )),
    ("CHALLENGE_TOTP", (
        "enter a code from your authenticator",
        "nhập mã từ ứng dụng xác thực",
        "authenticator app",
        "ứng dụng xác thực",
        "get a verification code",
        "nhận mã xác minh từ",
    )),
    ("CHALLENGE_PHONE", (
        "enter a phone number",
        "nhập số điện thoại",
        "google will only use this phone number for security",
        "google sẽ lưu trữ và chỉ sử dụng số điện thoại này",
        "text message",
        "tin nhắn văn bản",
    )),
    ("CHALLENGE_PROMPT", (
        "tap yes on your phone",
        "nhấn có trên điện thoại",
        "check your phone",
        "kiểm tra điện thoại của bạn",
    )),
    ("CHALLENGE_RECOVERY", (
        "confirm your recovery email",
        "xác nhận email khôi phục",
    )),
    ("CAPTCHA", (
        "unusual traffic",
        "lưu lượng truy cập bất thường",
        "type the text you hear or see",
        "nhập văn bản bạn nghe hoặc nhìn thấy",
    )),
    ("CHALLENGE", (
        "2-step verification",
        "xác minh 2 bước",
        "verify it's you",
        "xác minh là bạn",
        "verify your identity",
        "xác minh danh tính",
        "try another way",
        "thử cách khác",
        "choose how you want to sign in",
        "chọn cách bạn muốn đăng nhập",
    )),
)


HARD_FAILURES = {"BAD_ACCOUNT", "WRONG_PASSWORD", "DISABLED", "INSECURE_BROWSER"}
# Google wants a second factor. Every one of these implies the password was right.
CHALLENGE_STATUSES = {
    "CHALLENGE", "CHALLENGE_QR", "CHALLENGE_TOTP",
    "CHALLENGE_PHONE", "CHALLENGE_PROMPT", "CHALLENGE_RECOVERY",
}

def log(message: str) -> None:
    stamp = time.strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def mask(email: str) -> str:
    name, _, domain = email.partition("@")
    return f"{name[:3]}***@{domain}" if domain else email


@dataclass
class Account:
    email: str
    password: str
    source_status: str


class Manager:
    """Thin HTTP client for the Manager API."""

    def __init__(self, base_url: str, token: str | None = None) -> None:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(
            base_url=self.base_url, headers=headers, timeout=60.0
        )

    def close(self) -> None:
        self.client.close()

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        return self.client.request(method, path, **kwargs)

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", path, **kwargs)

    def json(self, path: str) -> Any:
        response = self.get(path)
        response.raise_for_status()
        return response.json()


def detail(response: httpx.Response) -> str:
    """Readable API error: pydantic's list form flattened to one line."""
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        return response.text.strip()[:300]
    if isinstance(payload, dict):
        value = payload.get("detail", payload)
    else:
        value = payload
    if isinstance(value, list):
        return "; ".join(
            f"{item.get('loc', ['?'])[-1]}: {item.get('msg', '')}"
            if isinstance(item, dict)
            else str(item)
            for item in value
        )
    return str(value)[:300]


def ensure_single_browser(mgr: Manager) -> None:
    """Stop every running profile so the single seat is free for us."""
    try:
        status = mgr.json("/api/status")
    except httpx.HTTPError as exc:
        raise SystemExit(f"Manager not reachable at {mgr.base_url}: {exc}") from exc
    if not status.get("running_count"):
        return
    log(f"Stopping {status['running_count']} running profile(s) to free the seat")
    for profile in mgr.json("/api/profiles"):
        try:
            state = mgr.json(f"/api/profiles/{profile['id']}/status").get("status")
        except httpx.HTTPError:
            continue
        if state == "stopped":
            continue
        log(f"  stop {profile['name']} ({state})")
        response = mgr.post(f"/api/profiles/{profile['id']}/stop")
        if response.status_code not in (200, 404):
            log(f"  stop failed: {detail(response)}")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if not mgr.json("/api/status").get("running_count"):
            return
        time.sleep(1.0)
    log("  still reporting a running profile; continuing anyway")


RANDOM_KEY = "random"
COOLDOWN_RE = re.compile(r"(?:sau|in)\s+(\d+)\s*(?:giây|second)", re.IGNORECASE)


def proxy_choices(mgr: Manager, provider: str) -> str:
    """Record the provider's keys once; launches then take whichever is free.

    ``proxy_key_id="random"`` makes the Manager pick a key that is not in use and
    not inside the provider's rotation cooldown — with ``per_launch`` that is a
    fresh exit IP from a different key on every launch, which is exactly what
    three keys are for.
    """
    if provider == "none":
        return ""
    try:
        rows = mgr.json(f"/api/proxy-providers/{provider}/available-keys")
    except httpx.HTTPError as exc:
        log(f"Could not list {provider} keys ({exc}); falling back to direct")
        return ""
    usable = [
        row for row in rows
        if row.get("key_id")
        and str(row.get("status") or "ACTIVE").upper() in {"ACTIVE", "VALID"}
        and not row.get("error")
    ]
    if not usable:
        return ""
    log(f"{provider}: {len(usable)} usable key(s); each launch takes a free one")
    return RANDOM_KEY


def cooldown_seconds(message: str) -> float:
    """The wait a provider refusal asks for, if it names one."""
    match = COOLDOWN_RE.search(message or "")
    return float(match.group(1)) if match else 0.0


def profile_by_email(mgr: Manager, email: str) -> dict | None:
    want = email.strip().casefold()
    for profile in mgr.json("/api/profiles"):
        if profile["name"].strip().casefold() == want:
            return profile
    return None


def create_profile(
    mgr: Manager,
    account: Account,
    *,
    args: argparse.Namespace,
) -> dict:
    payload: dict[str, Any] = {
        "name": account.email,
        "timezone": args.timezone,
        "locale": args.locale,
        "set_google_default": False,
        "capture_preview": True,
        "auto_launch": False,
        "notes": "gmail-batch",
        "proxy_provider": "none",
        # Real mouse/keyboard/scroll motion: a plain CDP-typed password is the
        # single most bot-looking thing about this workflow.
        "humanize": bool(args.humanize),
        "human_preset": args.human_preset,
        "launch_args": ["--headless"] if args.headless else [],
    }
    if args.provider != "none":
        payload.update(_proxy_fields(args.provider, RANDOM_KEY, args.rotation))
    if args.pin_key:
        payload["license_key_id"] = args.pin_key

    response = mgr.post("/api/profiles", json=payload)
    if response.status_code == 422 and "proxy_rotation_mode" in detail(response):
        # Manager older than the sticky-rotation change: the field is unknown.
        payload.pop("proxy_rotation_mode", None)
        log("  this Manager build has no proxy_rotation_mode; creating as per_launch")
        response = mgr.post("/api/profiles", json=payload)
    if response.status_code != 201:
        raise RuntimeError(f"create failed ({response.status_code}): {detail(response)}")
    return response.json()


def _proxy_fields(provider: str, key_id: str, rotation: str | None) -> dict[str, Any]:
    fields: dict[str, Any] = {"proxy_provider": provider, "proxy_key_id": key_id}
    if rotation:
        fields["proxy_rotation_mode"] = rotation
    return fields


def align_profile(
    mgr: Manager,
    profile: dict,
    args: argparse.Namespace,
) -> dict | None:
    """Point a reused profile at this run's rotation mode and random key choice.

    Re-runs have to fix profiles from an earlier run: a sticky mode keeps an old
    exit IP, and a pinned key keeps hitting that one key's rotation cooldown
    instead of moving to a free one.
    """
    updates: dict[str, Any] = {}
    if args.rotation and profile.get("proxy_rotation_mode") != args.rotation:
        updates["proxy_rotation_mode"] = args.rotation
    wanted = _proxy_fields(args.provider, RANDOM_KEY, None) if args.provider != "none" else {}
    for field, value in wanted.items():
        if profile.get(field) != value:
            updates[field] = value
    if bool(profile.get("humanize")) != bool(args.humanize):
        updates["humanize"] = bool(args.humanize)
    if args.humanize and profile.get("human_preset") != args.human_preset:
        updates["human_preset"] = args.human_preset
    wanted_args = [arg for arg in (profile.get("launch_args") or []) if arg != "--headless"]
    if args.headless:
        wanted_args.append("--headless")
    if (profile.get("launch_args") or []) != wanted_args:
        updates["launch_args"] = wanted_args
    if not updates:
        return None
    response = mgr.request("PUT", f"/api/profiles/{profile['id']}", json=updates)
    if response.status_code != 200:
        log(f"  could not update profile ({response.status_code}): {detail(response)}")
        return None
    changes = ", ".join(
        f"{name}={updates[name]}"
        for name in ("proxy_rotation_mode", "proxy_key_id", "humanize",
                     "human_preset", "launch_args")
        if name in updates
    )
    if changes:
        log(f"  profile updated: {changes}")
    return response.json()


def launch_profile(mgr: Manager, profile_id: str, args: argparse.Namespace) -> dict:
    """Launch, waiting out the seat and provider windows that block it."""
    delay = 30.0
    last = ""
    attempts = max(args.seat_retries, 1)
    for attempt in range(1, attempts + 1):
        response = mgr.post(f"/api/profiles/{profile_id}/launch", timeout=180.0)
        if response.status_code == 200:
            return response.json()
        last = detail(response)
        if response.status_code == 409 and "already running" in last:
            return {"cdp_url": f"/api/profiles/{profile_id}/cdp"}
        if response.status_code == 402:
            log(f"  seat busy ({last}); waiting {int(delay)}s (try {attempt}/{attempts})")
            time.sleep(delay)
            delay = min(delay * 2, 300)
            continue
        if response.status_code == 400:
            # per_launch asks the provider for a new IP. When every key of the
            # provider is inside its rotation window the Manager says how long
            # that lasts; waiting is the only way to get a fresh IP.
            wait = cooldown_seconds(last)
            if wait and wait <= args.max_proxy_wait and attempt < attempts:
                log(f"  every proxy key is cooling down: waiting {int(wait + 2)}s")
                time.sleep(wait + 2)
                continue
        if response.status_code == 403:
            raise SystemExit(f"License refused the launch: {last}")
        raise RuntimeError(f"launch failed ({response.status_code}): {last}")
    raise RuntimeError(f"launch kept failing: {last}")


def stop_profile(mgr: Manager, profile_id: str) -> None:
    try:
        mgr.post(f"/api/profiles/{profile_id}/stop")
    except httpx.HTTPError as exc:  # best effort: the next launch stops it anyway
        log(f"  stop failed: {exc}")


def _active_proxy(mgr: Manager, profile_id: str) -> dict:
    """Proxy metadata the launch recorded for a running profile."""
    try:
        return mgr.json(f"/api/profiles/{profile_id}").get("active_proxy") or {}
    except httpx.HTTPError:
        return {}


def _exit_ip(mgr: Manager, profile_id: str) -> str:
    """The proxy's exit IP, as recorded by the launch (empty when direct)."""
    active = _active_proxy(mgr, profile_id)
    return str(active.get("real_ip") or active.get("host") or "")


def _page_shot(page: Any, email: str, folder: Path) -> str:
    """Screenshot the page itself — the Manager's preview may be a stale frame."""
    safe = re.sub(r"[^A-Za-z0-9._@-]+", "_", email)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{safe}.jpg"
        page.screenshot(path=str(path), type="jpeg", quality=80, timeout=15000)
        return str(path)
    except Exception:
        return ""


def save_screenshot(mgr: Manager, profile_id: str, email: str, folder: Path) -> str:
    try:
        response = mgr.get(f"/api/profiles/{profile_id}/screenshot")
    except httpx.HTTPError:
        return ""
    if response.status_code != 200 or not response.content:
        return ""
    folder.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._@-]+", "_", email)
    path = folder / f"{safe}.jpg"
    path.write_bytes(response.content)
    return str(path)


# ── Browser driving ─────────────────────────────────────────────────────────

def _visible(page: Any, selector: str) -> bool:
    try:
        locator = page.locator(selector).first
        return locator.count() > 0 and locator.is_visible(timeout=300)
    except Exception:
        return False


def _page_closed(page: Any) -> bool:
    try:
        return page.is_closed()
    except Exception:
        return True


def _body_text(page: Any) -> str:
    try:
        return (page.inner_text("body", timeout=2000) or "").casefold()
    except Exception:
        return ""


def classify(page: Any) -> str | None:
    """Outcome implied by the visible page text, if any."""
    text = _body_text(page)
    if not text:
        return None
    for status, needles in SIGNATURES:
        if any(needle in text for needle in needles):
            return status
    return None


def signed_in(context: Any) -> bool:
    """True once Google has handed the browser a session cookie."""
    for url in ("https://accounts.google.com", "https://mail.google.com",
                "https://www.google.com"):
        try:
            cookies = context.cookies(url)
        except Exception:
            continue
        for cookie in cookies:
            if cookie.get("name") in SESSION_COOKIES and cookie.get("value"):
                return True
    return False


def _recaptcha_anchor(page: Any) -> Any | None:
    """The reCAPTCHA checkbox frame, when one is on screen."""
    for frame in page.frames:
        if "recaptcha" not in (frame.url or ""):
            continue
        try:
            box = frame.locator("#recaptcha-anchor")
            if box.count() > 0:
                return box.first
        except Exception:
            continue
    return None


def solve_recaptcha(page: Any, wait_seconds: float = 12.0) -> str:
    """Tick Google's "I'm not a robot" box. Returns solved/blocked/absent.

    A checkbox that clears itself is what Google shows first; it settles without
    any puzzle often enough to be worth one click. An image grid (the bframe
    challenge) is not automatable and is reported as blocked.
    """
    box = _recaptcha_anchor(page)
    if box is None:
        return "absent"
    try:
        if box.get_attribute("aria-checked") == "true":
            return "solved"
        box.click(timeout=5000)
    except Exception:
        return "blocked"
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        try:
            if box.get_attribute("aria-checked") == "true":
                return "solved"
        except Exception:
            return "solved"  # frame went away: the challenge progressed
        if _recaptcha_puzzle(page):
            return "blocked"
        page.wait_for_timeout(500)
    return "blocked"


def _recaptcha_puzzle(page: Any) -> bool:
    for frame in page.frames:
        if "/recaptcha/" in (frame.url or "") and "bframe" in frame.url:
            try:
                if frame.locator(".rc-imageselect, #rc-imageselect").count() > 0:
                    return True
            except Exception:
                continue
    return False


MOUSE_STEPS = 26


def _human_move(page: Any, x: float, y: float) -> None:
    """Glide the pointer to (x, y) along a slight curve, with overshoot."""
    try:
        start = page.evaluate(
            "() => [window.__lastX ?? null, window.__lastY ?? null]"
        )
    except Exception:
        start = [None, None]
    sx, sy = start if isinstance(start, list) and len(start) == 2 else (None, None)
    if sx is None or sy is None:
        sx, sy = x + random.uniform(-180, 180), y + random.uniform(-140, 140)
        try:
            page.mouse.move(sx, sy)
        except Exception:
            return
        page.wait_for_timeout(random.randint(60, 160))

    # A quadratic curve through a jittered midpoint reads as a hand, not a line.
    cx = (sx + x) / 2 + random.uniform(-40, 40)
    cy = (sy + y) / 2 + random.uniform(-40, 40)
    for step in range(1, MOUSE_STEPS + 1):
        t = step / MOUSE_STEPS
        px = (1 - t) ** 2 * sx + 2 * (1 - t) * t * cx + t ** 2 * x
        py = (1 - t) ** 2 * sy + 2 * (1 - t) * t * cy + t ** 2 * y
        try:
            page.mouse.move(px, py)
        except Exception:
            return
        page.wait_for_timeout(random.randint(4, 16))
    try:
        page.evaluate("([x, y]) => { window.__lastX = x; window.__lastY = y }", [x, y])
    except Exception:
        pass


def _human_click(page: Any, target: Any, timeout: int = 8000) -> bool:
    """Click an element the way a hand would: move first, then press."""
    try:
        box = target.bounding_box(timeout=timeout)
    except Exception:
        box = None
    if not box:
        try:
            target.click(timeout=timeout)
            return True
        except Exception:
            return False
    x = box["x"] + box["width"] * random.uniform(0.35, 0.65)
    y = box["y"] + box["height"] * random.uniform(0.35, 0.65)
    _human_move(page, x, y)
    page.wait_for_timeout(random.randint(80, 220))
    try:
        page.mouse.down()
        page.wait_for_timeout(random.randint(45, 120))
        page.mouse.up()
        return True
    except Exception:
        return False


def _human_fill(page: Any, selector: str, text: str, min_ms: int = 45, max_ms: int = 130) -> None:
    """Focus a field like a person would, then type into it.

    A coordinate click is what actually moves the pointer, but Google's inputs
    swallow it (labels/containers take the hit), so focus is established
    explicitly first and the mouse motion happens on top of that.
    """
    locator = page.locator(selector).first
    locator.focus(timeout=8000)
    page.wait_for_timeout(random.randint(120, 320))
    try:
        box = locator.bounding_box(timeout=4000)
    except Exception:
        box = None
    if box:
        _human_move(
            page,
            box["x"] + box["width"] * random.uniform(0.3, 0.7),
            box["y"] + box["height"] * random.uniform(0.35, 0.65),
        )
    page.keyboard.press("Control+A")
    page.keyboard.press("Delete")
    _human_type(page, text, min_ms, max_ms)


def _human_type(page: Any, text: str, min_ms: int = 45, max_ms: int = 130) -> None:
    """Type with per-key jitter and the odd pause, like a person at the keyboard."""
    for index, char in enumerate(text):
        page.keyboard.type(char)
        delay = random.randint(min_ms, max_ms)
        if index and index % random.randint(5, 9) == 0:
            delay += random.randint(90, 260)
        page.wait_for_timeout(delay)


def _human_scroll(page: Any, clicks: int) -> None:
    try:
        page.mouse.wheel(0, clicks * random.randint(80, 140))
    except Exception:
        pass
    page.wait_for_timeout(random.randint(200, 500))


def _click_next(page: Any, selector: str) -> None:
    try:
        locator = page.locator(selector).first
        if locator.count() > 0 and locator.is_visible(timeout=300):
            if _human_click(page, locator):
                return
    except Exception:
        pass
    page.keyboard.press("Enter")


def sign_in(page: Any, context: Any, account: Account, args: argparse.Namespace) -> tuple[str, str]:
    """Drive the Google form. Returns ``(status, note)``."""
    page.goto(SIGNIN_URL, wait_until="domcontentloaded", timeout=60000)

    sent_email = False
    sent_password = False
    puzzle_until: float | None = None
    challenge_since: float | None = None
    deadline = time.monotonic() + args.login_timeout
    status = "TIMEOUT"

    while time.monotonic() < deadline:
        if signed_in(context):
            return _verify_gmail(page, args)
        if _page_closed(page):
            return "ERROR", "browser page closed"

        verdict = solve_recaptcha(page) if _recaptcha_anchor(page) else "absent"
        if verdict == "solved":
            continue
        if verdict == "blocked":
            puzzle_until = puzzle_until or time.monotonic() + args.challenge_timeout
            if args.challenge_timeout <= 0:
                return "BLOCKED", "reCAPTCHA image challenge"
            if time.monotonic() > puzzle_until:
                return "BLOCKED", "reCAPTCHA image challenge (timed out)"
            page.wait_for_timeout(1000)
            continue

        if not sent_email and _visible(page, EMAIL_SEL):
            _human_fill(page, EMAIL_SEL, account.email)
            _click_next(page, "#identifierNext")
            sent_email = True
            page.wait_for_timeout(random.randint(900, 1800))
            continue

        if sent_email and not sent_password and _visible(page, PASSWORD_SEL):
            _human_fill(page, PASSWORD_SEL, account.password, 60, 150)
            _click_next(page, "#passwordNext")
            sent_password = True
            page.wait_for_timeout(random.randint(900, 1800))
            continue

        status = classify(page) or "PENDING"
        if status in HARD_FAILURES:
            return status, "rejected by Google"
        if status in CHALLENGE_STATUSES:
            # A challenge is only reached after Google accepted the password,
            # which is the fact worth recording when this cannot be automated.
            if challenge_since is None:
                challenge_since = time.monotonic()
                if args.challenge_timeout > 0:
                    log(f"  {status} on screen — leaving it to a human for "
                        f"{int(args.challenge_timeout)}s")
                    deadline = max(deadline, challenge_since + args.challenge_timeout)
        page.wait_for_timeout(800)

    if challenge_since is not None and status in CHALLENGE_STATUSES:
        return status, f"{status.lower()} (timed out)"
    if puzzle_until is not None:
        return "BLOCKED_RECAPTCHA", "reCAPTCHA image challenge (timed out)"
    if sent_email and not sent_password:
        return "ERROR", "no password field"
    return status if status != "PENDING" else "TIMEOUT", "no session cookie"


def _verify_gmail(page: Any, args: argparse.Namespace) -> tuple[str, str]:
    """Prove the session reaches Gmail, not just that a cookie exists."""
    if not args.verify_gmail:
        return "OK", "session cookie"
    try:
        page.goto(GMAIL_URL, wait_until="domcontentloaded", timeout=60000)
    except Exception as exc:
        return "OK", f"session cookie (gmail check failed: {type(exc).__name__})"
    page.wait_for_timeout(2500)
    url = page.url
    if "accounts.google.com" in url:
        return "OK", "session cookie, but Gmail bounced to sign-in"
    return "OK", "gmail loaded"


# ── Checkpoint ──────────────────────────────────────────────────────────────

def read_accounts(path: Path, statuses: set[str], types: set[str] | None) -> list[Account]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    accounts: list[Account] = []
    seen: set[str] = set()
    for row in rows:
        email = (row.get("Email") or "").strip()
        password = (row.get("Pass") or "").strip()
        status = (row.get("Status") or "").strip()
        kind = (row.get("Type") or "").strip()
        if not email or "@" not in email or not password:
            continue
        if statuses and status not in statuses:
            continue
        if types and kind not in types:
            continue
        key = email.casefold()
        if key in seen:
            continue
        seen.add(key)
        accounts.append(Account(email=email, password=password, source_status=status))
    return accounts


def read_checkpoint(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return {
        (row.get("email") or "").strip().casefold(): row
        for row in rows
        if (row.get("email") or "").strip()
    }


def write_checkpoint(path: Path, rows: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    ordered = sorted(rows.items(), key=lambda item: item[1].get("at") or "")
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CHECKPOINT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for _, row in ordered:
            writer.writerow({key: row.get(key, "") for key in CHECKPOINT_FIELDS})
    tmp.replace(path)


# ── One account ─────────────────────────────────────────────────────────────

def run_account(
    pw: Any,
    mgr: Manager,
    account: Account,
    args: argparse.Namespace,
) -> dict[str, Any]:
    started = time.monotonic()
    row: dict[str, Any] = {
        "email": account.email, "status": "ERROR", "note": "",
        "profile_id": "", "proxy_key_id": "", "screenshot": "",
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    profile_id = ""
    try:
        profile = profile_by_email(mgr, account.email)
        if profile is None:
            profile = create_profile(mgr, account, args=args)
            row["note"] = "profile created"
        else:
            row["note"] = "profile reused"
            profile = align_profile(mgr, profile, args) or profile
        profile_id = profile["id"]
        row["profile_id"] = profile_id
        row["proxy_key_id"] = profile.get("proxy_key_id") or ""

        if args.dry_run:
            launch_profile(mgr, profile_id, args)
            time.sleep(2.0)
            row["status"] = "DRY_OK"
            row["note"] = f"{row['note']}; exit={_exit_ip(mgr, profile_id) or '-'}"
            return row

        launch_profile(mgr, profile_id, args)
        cdp_url = f"{mgr.base_url}/api/profiles/{profile_id}/cdp"
        browser = pw.chromium.connect_over_cdp(cdp_url, timeout=30000)
        try:
            context = browser.contexts[0]
            # A page of our own: the launch leaves chrome:// tabs and blank
            # about:blank pages open, and navigating those goes nowhere.
            page = context.new_page()
            try:
                row["status"], row["note"] = sign_in(page, context, account, args)
                row["screenshot"] = _page_shot(page, account.email, args.screenshots) or \
                    save_screenshot(mgr, profile_id, account.email, args.screenshots)
            finally:
                try:
                    page.close()
                except Exception:
                    pass
            active_ip = _exit_ip(mgr, profile_id)
            if active_ip:
                row["note"] = f"{row['note']}; exit={active_ip}"
        finally:
            try:
                browser.close()
            except Exception:
                pass
        return row
    except RuntimeError as exc:
        row["status"] = "ERROR"
        row["note"] = str(exc)[:300]
        return row
    except Exception as exc:  # noqa: BLE001 — one account must not end the run
        row["status"] = "ERROR"
        row["note"] = f"{type(exc).__name__}: {exc}"[:300]
        return row
    finally:
        row["seconds"] = round(time.monotonic() - started, 1)
        if profile_id:
            stop_profile(mgr, profile_id)


# ── Main ────────────────────────────────────────────────────────────────────

def single_instance(port: int) -> socket.socket | None:
    """Hold a loopback port for the life of the run — a cheap cross-platform lock."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
        sock.listen(1)
    except OSError:
        sock.close()
        return None
    return sock


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sign Gmail accounts in through CloakManager profiles."
    )
    parser.add_argument("--accounts", type=Path, default=DEFAULT_ACCOUNTS,
                        help=f"source CSV (default {DEFAULT_ACCOUNTS})")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help=f"checkpoint CSV (default {DEFAULT_OUT})")
    parser.add_argument("--screenshots", type=Path, default=DEFAULT_SHOTS,
                        help=f"screenshot folder (default {DEFAULT_SHOTS})")
    parser.add_argument("--status", action="append",
                        help="only rows with this Status (repeatable; default Ok)")
    parser.add_argument("--type", action="append", dest="types",
                        help="only rows with this Type (repeatable; default any)")
    parser.add_argument("--limit", type=int, default=0, help="stop after N accounts")
    parser.add_argument("--start-at", default=None,
                        help="skip every account before this email in the source CSV")
    parser.add_argument("--delay", type=float, default=5.0,
                        help="seconds between accounts (default 5; provider cooldowns "
                             "are waited out separately, per key)")
    parser.add_argument("--batch", type=int, default=30,
                        help="accounts per run block (default 30, 0 = no blocks)")
    parser.add_argument("--batch-sleep", type=float, default=600.0,
                        help="seconds to rest between blocks (default 600)")
    parser.add_argument("--seat-retries", type=int, default=6,
                        help="launch retries while the seat is still taken (default 6)")
    parser.add_argument("--max-proxy-wait", type=float, default=600.0,
                        help="longest provider rotation cooldown to sit out (default 600s)")
    parser.add_argument("--login-timeout", type=float, default=75.0,
                        help="seconds to wait for a sign-in outcome (default 75)")
    parser.add_argument("--challenge-timeout", type=float, default=300.0,
                        help="seconds to leave a 2FA screen to a human (0 = never wait)")
    parser.add_argument("--no-verify-gmail", dest="verify_gmail", action="store_false",
                        help="accept the session cookie without loading Gmail")
    parser.add_argument("--retry-blocked", action="store_true",
                        help="also retry accounts recorded as BLOCKED")
    parser.add_argument("--dry-run", action="store_true",
                        help="create + launch + stop, without signing in")
    parser.add_argument("--provider", default="kiotproxy",
                        choices=("kiotproxy", "tmproxy", "none"),
                        help="proxy provider for new profiles (default kiotproxy)")
    parser.add_argument("--rotation", default="per_launch",
                        help="proxy rotation mode for profiles; per_launch = a new "
                             "exit IP on every launch (default), sticky = keep one")
    parser.add_argument("--pin-key", default=None,
                        help="pin new profiles to this pooled license key id")
    parser.add_argument("--headless", dest="headless", action="store_true",
                        default=True,
                        help="run the browser without a window (default on; the "
                             "window is not needed and headless is faster)")
    parser.add_argument("--show", dest="headless", action="store_false",
                        help="show the browser window (needed to solve 2FA by hand)")
    parser.add_argument("--no-humanize", dest="humanize", action="store_false",
                        help="disable simulated mouse/keyboard/scroll motion "
                             "(on by default; it is what makes a CDP-typed "
                             "password look human)")
    parser.add_argument("--human-preset", default="default",
                        choices=("default", "careful"),
                        help="humanize preset (default default, careful = slower)")
    parser.add_argument("--timezone", default="Asia/Ho_Chi_Minh")
    parser.add_argument("--locale", default="vi-VN")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--token", default=None, help="AUTH_TOKEN, if the Manager needs one")
    parser.add_argument("--lock-port", type=int, default=8091,
                        help="loopback port held while running (default 8091)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass

    args = parse_args(argv)
    lock = single_instance(args.lock_port)
    if lock is None:
        print("Another gmail_login run is already going (lock port busy).", file=sys.stderr)
        return 2

    if not args.accounts.exists():
        print(f"Accounts file not found: {args.accounts}", file=sys.stderr)
        return 2

    statuses = {s.strip() for s in (args.status or ["Ok"])}
    types = {t.strip() for t in args.types} if args.types else None
    accounts = read_accounts(args.accounts, statuses, types)
    checkpoint = read_checkpoint(args.out)

    def skip(account: Account) -> bool:
        row = checkpoint.get(account.email.casefold())
        if row is None:
            return False
        status = (row.get("status") or "").upper()
        if status == "OK":
            return True
        if status in FATAL_STATUSES:
            return True
        # A second factor needs a human; retrying it just re-runs the same wall.
        stuck = status.startswith("CHALLENGE") or status == "BLOCKED_RECAPTCHA"
        return stuck and not args.retry_blocked

    queue = [account for account in accounts if not skip(account)]
    if args.start_at:
        wanted = args.start_at.strip().casefold()
        positions = [i for i, a in enumerate(queue) if a.email.casefold() == wanted]
        if not positions:
            print(f"--start-at {args.start_at}: not in the remaining queue "
                  "(already done, filtered out, or not in the CSV).", file=sys.stderr)
            return 2
        queue = queue[positions[0]:]
    if args.limit:
        queue = queue[: args.limit]

    log(f"source: {len(accounts)} account(s) from {args.accounts.name}; "
        f"{len(checkpoint)} recorded; queue {len(queue)}")
    if not queue:
        return 0

    mgr = Manager(args.base_url, args.token)
    try:
        mgr.json("/api/health")
        ensure_single_browser(mgr)
        key_choice = proxy_choices(mgr, args.provider)
        if not key_choice and args.provider != "none":
            print("No usable proxy keys; pass --provider none to run direct.",
                  file=sys.stderr)
            return 2
        counts: dict[str, int] = {}
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            for index, account in enumerate(queue):
                row = run_account(pw, mgr, account, args)
                checkpoint[account.email.casefold()] = row
                write_checkpoint(args.out, checkpoint)
                counts[row["status"]] = counts.get(row["status"], 0) + 1
                log(f"{index + 1}/{len(queue)} {mask(account.email)} -> "
                    f"{row['status']} ({row.get('seconds')}s) {row.get('note', '')}")
                if row["status"] in FATAL_STATUSES:
                    log("stopping: the browser itself is being refused by Google")
                    break
                last = index == len(queue) - 1
                if not last:
                    if args.batch and (index + 1) % args.batch == 0:
                        log(f"block done — resting {int(args.batch_sleep)}s")
                        time.sleep(args.batch_sleep)
                    elif args.delay:
                        time.sleep(args.delay)
    finally:
        mgr.close()
        lock.close()

    log("summary: " + ", ".join(f"{name}={count}" for name, count in sorted(counts.items())))
    log(f"checkpoint: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
