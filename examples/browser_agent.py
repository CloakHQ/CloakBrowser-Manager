"""Small reference client for controlling a Manager profile with Playwright."""

from __future__ import annotations

import asyncio
import os

import httpx
from playwright.async_api import async_playwright

MANAGER_URL = os.getenv("CLOAK_MANAGER_URL", "http://127.0.0.1:8080")


async def connect(native_profile: str):
    async with httpx.AsyncClient(base_url=MANAGER_URL) as client:
        profiles = (await client.get("/api/profiles")).raise_for_status().json()
        marker = f"--native-profile={native_profile}"
        profile = next(p for p in profiles if marker in p["launch_args"])
        if profile["status"] != "running":
            (await client.post(f"/api/profiles/{profile['id']}/launch")).raise_for_status()

    playwright = await async_playwright().start()
    browser = await playwright.chromium.connect_over_cdp(
        f"{MANAGER_URL}/api/profiles/{profile['id']}/cdp"
    )
    return playwright, browser, browser.contexts[0]


async def main():
    playwright, browser, context = await connect("google-002")
    try:
        page = context.pages[-1] if context.pages else await context.new_page()
        await page.goto("https://accounts.google.com/", wait_until="domcontentloaded")
        print(await page.title())
    finally:
        # Disconnect agent only. Manager owns browser lifecycle.
        await playwright.stop()


if __name__ == "__main__":
    asyncio.run(main())
