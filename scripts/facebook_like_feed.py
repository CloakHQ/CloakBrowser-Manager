#!/usr/bin/env python3
"""Like visible Facebook News Feed posts through a CloakBrowser CDP profile."""

from __future__ import annotations

import argparse
import random
import sys
import time
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


DEFAULT_PROFILE_ID = "fe71dc04-d999-46bb-8c22-12747b274cf2"
DEFAULT_HOST = "http://localhost:8080"
DEFAULT_COUNT = 10


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Connect to a CloakBrowser profile over CDP and like Facebook feed posts."
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="CloakBrowser Manager host.")
    parser.add_argument(
        "--profile-id",
        default=DEFAULT_PROFILE_ID,
        help="Running CloakBrowser profile id.",
    )
    parser.add_argument(
        "--cdp-url",
        default=None,
        help="Direct CDP URL. Overrides --host and --profile-id when set.",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=DEFAULT_COUNT,
        help="Maximum number of unliked feed posts to like.",
    )
    parser.add_argument(
        "--feed-url",
        default="https://www.facebook.com/",
        help="Facebook feed URL to open before liking.",
    )
    parser.add_argument(
        "--max-scrolls",
        type=int,
        default=30,
        help="Maximum scroll attempts while looking for more posts.",
    )
    parser.add_argument(
        "--scroll-px",
        type=int,
        default=900,
        help="Pixels to scroll after no likeable post is visible.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Find targets without clicking Like.",
    )
    return parser


def cdp_url(host: str, profile_id: str) -> str:
    return f"{host.rstrip('/')}/api/profiles/{profile_id}/cdp"


def is_same_host(current_url: str, target_url: str) -> bool:
    current_host = urlparse(current_url).hostname or ""
    target_host = urlparse(target_url).hostname or ""
    return current_host == target_host


def mark_next_like_button(page: Any, seen_action_keys: set[str]) -> dict[str, Any]:
    token = f"codex-fb-like-{time.time_ns()}"
    result = page.evaluate(
        """
        ({ token, seenActionKeys }) => {
          const reactionActionNames = new Set(["Like", "Thích", "React", "Bày tỏ cảm xúc"]);

          const normalize = (value) => (value || "").replace(/\\s+/g, " ").trim();
          const labelOf = (el) => normalize(el.getAttribute("aria-label") || el.innerText);

          const isVisible = (el) => {
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return (
              rect.width > 0 &&
              rect.height > 0 &&
              rect.bottom > 0 &&
              rect.top < window.innerHeight &&
              style.visibility !== "hidden" &&
              style.display !== "none" &&
              style.pointerEvents !== "none"
            );
          };

          const actionGroupFor = (button) => {
            let node = button.parentElement;
            for (let depth = 0; node && node !== document.body && depth < 10; depth += 1) {
              const names = Array.from(node.querySelectorAll('[role="button"]'))
                .filter(isVisible)
                .map(labelOf);
              const hasReactionAction = names.some((name) => reactionActionNames.has(name));
              const hasComment = names.some((name) => /comment|bình luận/i.test(name));
              const hasShare = names.some((name) => /share|chia sẻ|send/i.test(name));
              if (hasReactionAction && (hasComment || hasShare)) {
                return node;
              }
              node = node.parentElement;
            }
            return null;
          };

          const actionKey = (group) => {
            const rect = group.getBoundingClientRect();
            const text = normalize(group.innerText).slice(0, 160);
            return `${Math.round(rect.top + window.scrollY)}|${Math.round(rect.left)}|${text}`;
          };

          const buttons = Array.from(document.querySelectorAll('[role="button"]'))
            .filter(isVisible);
          const groups = new Set();

          for (const button of buttons) {
            const name = labelOf(button);
            if (!reactionActionNames.has(name)) {
              continue;
            }
            if (button.getAttribute("aria-pressed") === "true") {
              continue;
            }

            const group = actionGroupFor(button);
            if (!group) {
              continue;
            }
            const key = actionKey(group);
            groups.add(key);
            if (seenActionKeys.includes(key)) {
              continue;
            }

            button.setAttribute("data-codex-fb-like-target", token);
            const rect = button.getBoundingClientRect();
            return {
              ok: true,
              token,
              actionKey: key,
              label: name,
              visibleActionGroups: groups.size,
              rect: {
                x: Math.round(rect.left),
                y: Math.round(rect.top),
                width: Math.round(rect.width),
                height: Math.round(rect.height),
              },
            };
          }

          return {
            ok: false,
            token,
            visibleButtons: buttons.length,
            visibleActionGroups: groups.size,
          };
        }
        """,
        {"token": token, "seenActionKeys": list(seen_action_keys)},
    )
    return result


def main() -> int:
    args = build_parser().parse_args()
    if args.count < 1:
        print("--count must be at least 1", file=sys.stderr)
        return 2

    seen_action_keys: set[str] = set()
    liked = 0
    scrolls = 0
    url = args.cdp_url or cdp_url(args.host, args.profile_id)

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(url)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.pages[0] if context.pages else context.new_page()

        print(f"Connected: {url}")
        page.on("dialog", lambda dialog: dialog.dismiss())
        if page.url == "about:blank" or not is_same_host(page.url, args.feed_url):
            page.goto(args.feed_url, wait_until="domcontentloaded", timeout=60_000)
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except PlaywrightTimeoutError:
                pass
        else:
            page.bring_to_front()
            page.wait_for_timeout(1_000)
        print(f"Page: {page.title()} ({page.url})")

        while liked < args.count and scrolls < args.max_scrolls:
            target = mark_next_like_button(page, seen_action_keys)
            if target.get("ok"):
                selector = f'[data-codex-fb-like-target="{target["token"]}"]'
                button = page.locator(selector).first
                button.scroll_into_view_if_needed(timeout=5_000)

                if args.dry_run:
                    print(
                        f"Dry run target {liked + 1}/{args.count}: "
                        f"{target['label']} at {target.get('rect')}"
                    )
                else:
                    button.click(timeout=8_000)
                    print(
                        f"Liked {liked + 1}/{args.count}: "
                        f"{target['label']} at {target.get('rect')}"
                    )

                liked += 1
                seen_action_keys.add(target["actionKey"])
                page.wait_for_timeout(random.randint(900, 1700))
                continue

            scrolls += 1
            print(
                f"No visible like target; scrolling {scrolls}/{args.max_scrolls} "
                f"(visible buttons: {target.get('visibleButtons', 0)}, "
                f"action groups: {target.get('visibleActionGroups', 0)})"
            )
            page.mouse.wheel(0, args.scroll_px)
            page.wait_for_timeout(random.randint(1200, 2200))

        print(f"Done. Liked {liked} post(s).")
        if liked < args.count:
            print(f"Stopped after {scrolls} scroll(s); requested {args.count}.")
        return 0 if liked == args.count or args.dry_run else 1


if __name__ == "__main__":
    raise SystemExit(main())
