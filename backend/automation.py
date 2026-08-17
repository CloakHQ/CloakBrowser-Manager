"""Native automation on the Playwright context owned by CloakBrowser Manager."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .browser_manager import RunningProfile
from .models import AutomationBootstrapRequest

logger = logging.getLogger("cloakbrowser.manager.automation")

_ALLOWED_UPLOAD_SUFFIXES = {".jpg", ".jpeg", ".png", ".mp4", ".mov"}
_INSTAGRAM_COOKIE_URL = "https://www.instagram.com/"


class AutomationPolicyError(ValueError):
    """An automation request violates the configured browser policy."""


def normalize_origin(raw: str) -> str:
    parsed = urlparse(raw.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise AutomationPolicyError("Invalid allowed origin")
    return f"{parsed.scheme}://{parsed.netloc.lower()}"


def origin_for_url(raw: str) -> str:
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise AutomationPolicyError("Invalid browser URL")
    if parsed.username or parsed.password:
        raise AutomationPolicyError("Browser URL credentials are not allowed")
    return f"{parsed.scheme}://{parsed.netloc.lower()}"


def is_allowed_url(raw: str, allowed_origins: frozenset[str]) -> bool:
    try:
        return origin_for_url(raw) in allowed_origins
    except AutomationPolicyError:
        return False


def cookie_payloads(
    request: AutomationBootstrapRequest,
    allowed_origins: frozenset[str],
) -> list[dict[str, Any]]:
    allowed_hosts = {
        str(urlparse(origin).hostname or "").lower() for origin in allowed_origins
    }
    result: list[dict[str, Any]] = []
    for cookie in request.cookies:
        payload = cookie.model_dump(exclude_none=True)
        cookie_url = payload.get("url")
        domain = str(payload.get("domain") or "").lstrip(".").lower()
        if cookie_url:
            if not is_allowed_url(str(cookie_url), allowed_origins):
                continue
            payload.pop("domain", None)
            payload.pop("path", None)
        elif domain:
            if not any(host == domain or host.endswith(f".{domain}") for host in allowed_hosts):
                continue
            payload["domain"] = str(payload["domain"])
        else:
            continue
        if float(payload.get("expires") or 0) <= 0:
            payload.pop("expires", None)
        result.append(payload)
    return result


async def _cancel_download(download: Any) -> None:
    try:
        await download.cancel()
    except Exception:
        logger.warning("Could not cancel a browser download", exc_info=True)


async def _guard_page(running: RunningProfile, page: Any) -> None:
    page_identity = id(page)
    if page_identity in running.guarded_page_ids:
        return
    running.guarded_page_ids.add(page_identity)
    page.on("download", lambda download: asyncio.create_task(_cancel_download(download)))


async def _install_policy(
    running: RunningProfile,
    allowed_origins: frozenset[str],
) -> None:
    context = running.context
    if running.automation_route is not None and running.automation_origins != allowed_origins:
        await context.unroute("**/*", running.automation_route)
        running.automation_route = None

    if running.automation_route is None:
        async def route_handler(route: Any, request: Any) -> None:
            is_top_level_navigation = False
            try:
                is_top_level_navigation = (
                    request.is_navigation_request()
                    and request.frame == request.frame.page.main_frame
                )
            except Exception:
                logger.warning("Could not inspect browser navigation", exc_info=True)
            if is_top_level_navigation and not is_allowed_url(
                str(request.url), running.automation_origins or frozenset()
            ):
                await route.abort("blockedbyclient")
                return
            await route.continue_()

        await context.route("**/*", route_handler)
        running.automation_route = route_handler

    running.automation_origins = allowed_origins
    for page in context.pages:
        await _guard_page(running, page)
    context.on("page", lambda page: asyncio.create_task(_guard_page(running, page)))


async def _primary_page(running: RunningProfile) -> Any:
    pages = running.context.pages
    return pages[0] if pages else await running.context.new_page()


async def _login_if_needed(page: Any, request: AutomationBootstrapRequest) -> None:
    if not request.username or not request.password:
        return
    username_input = page.locator('input[name="username"]').first
    password_input = page.locator('input[name="password"]').first
    if not await username_input.count() or not await password_input.count():
        return
    await username_input.fill(request.username)
    await password_input.fill(request.password)
    await password_input.press("Enter")
    if not request.totp_code:
        return
    try:
        verification = page.locator(
            'input[name="verificationCode"], input[name="code"]'
        ).first
        await verification.wait_for(state="visible", timeout=10_000)
        await verification.fill(request.totp_code)
        await verification.press("Enter")
    except Exception:
        logger.info("Instagram verification input was not shown")


async def profile_state(running: RunningProfile) -> dict[str, Any]:
    page = await _primary_page(running)
    cookies = await running.context.cookies([_INSTAGRAM_COOKIE_URL])
    user_agent: str | None = None
    try:
        value = await page.evaluate("navigator.userAgent")
        user_agent = str(value) if value else None
    except Exception:
        logger.warning("Could not read browser user agent", exc_info=True)
    return {
        "cookies": [
            cookie
            for cookie in cookies
            if "instagram.com" in str(cookie.get("domain") or "")
        ],
        "user_agent": user_agent,
        "url": str(page.url),
    }


async def bootstrap_profile(
    running: RunningProfile,
    request: AutomationBootstrapRequest,
) -> dict[str, Any]:
    allowed_origins = frozenset(normalize_origin(item) for item in request.allowed_origins)
    if not is_allowed_url(request.start_url, allowed_origins):
        raise AutomationPolicyError("Start URL is outside the allowed origins")

    async with running.automation_lock:
        await _install_policy(running, allowed_origins)
        cookies = cookie_payloads(request, allowed_origins)
        if cookies:
            await running.context.add_cookies(cookies)
        page = await _primary_page(running)
        try:
            await page.goto(request.start_url, wait_until="domcontentloaded", timeout=45_000)
        except Exception:
            if page.is_closed():
                raise
            logger.info("Initial browser navigation is still loading")
        await _login_if_needed(page, request)
        state = await profile_state(running)
        return {
            "ready": True,
            "cookies_imported": len(cookies),
            "user_agent": state["user_agent"],
            "url": state["url"],
        }


def resolve_upload_path(raw: str) -> Path:
    upload_root = Path(
        os.environ.get("AUTOMATION_UPLOAD_DIR", "/tmp/instagram-browser-uploads")
    ).resolve()
    try:
        path = Path(raw).resolve(strict=True)
        path.relative_to(upload_root)
    except (OSError, ValueError) as exc:
        raise AutomationPolicyError("Upload path is outside the shared directory") from exc
    if not path.is_file() or path.suffix.lower() not in _ALLOWED_UPLOAD_SUFFIXES:
        raise AutomationPolicyError("Unsupported upload file")
    return path


async def set_file_input(running: RunningProfile, raw_path: str) -> bool:
    path = resolve_upload_path(raw_path)
    async with running.automation_lock:
        page = await _primary_page(running)
        locator = page.locator('input[type="file"]')
        count = await locator.count()
        if not count:
            return False
        await locator.nth(count - 1).set_input_files(str(path))
        return True
