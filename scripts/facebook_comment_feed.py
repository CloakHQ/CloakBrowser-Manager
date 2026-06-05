#!/usr/bin/env python3
"""Comment on visible Facebook News Feed posts through a CloakBrowser CDP profile."""

from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import Locator
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


DEFAULT_PROFILE_ID = "fe71dc04-d999-46bb-8c22-12747b274cf2"
DEFAULT_HOST = "http://localhost:8080"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Connect to a CloakBrowser profile over CDP and comment on Facebook "
            "News Feed posts with one text-file line per post."
        )
    )
    parser.add_argument(
        "comments_file",
        type=Path,
        help="UTF-8 text file. Each non-empty line is posted as one comment.",
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
        default=None,
        help="Maximum number of lines to post. Defaults to all non-empty lines.",
    )
    parser.add_argument(
        "--start-line",
        type=int,
        default=1,
        help=(
            "1-based non-empty comment line to start from. Use 2 to resume after "
            "the first comment line was already posted."
        ),
    )
    parser.add_argument(
        "--feed-url",
        default="https://www.facebook.com/",
        help="Facebook feed URL to open before commenting.",
    )
    parser.add_argument(
        "--max-scrolls",
        type=int,
        default=40,
        help="Maximum scroll attempts while looking for more posts.",
    )
    parser.add_argument(
        "--scroll-px",
        type=int,
        default=900,
        help="Pixels to scroll after no commentable post is visible.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Find targets and print planned comments without posting.",
    )
    return parser


def cdp_url(host: str, profile_id: str) -> str:
    return f"{host.rstrip('/')}/api/profiles/{profile_id}/cdp"


def is_same_page(current_url: str, target_url: str) -> bool:
    current = urlparse(current_url)
    target = urlparse(target_url)
    current_path = current.path.rstrip("/") or "/"
    target_path = target.path.rstrip("/") or "/"
    return current.hostname == target.hostname and current_path == target_path


def load_comments(path: Path, count: int | None, start_line: int) -> list[str]:
    if count is not None and count < 1:
        raise ValueError("--count must be at least 1")
    if start_line < 1:
        raise ValueError("--start-line must be at least 1")
    if not path.exists():
        raise FileNotFoundError(f"Comments file not found: {path}")

    lines = [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines()]
    comments = [line for line in lines if line]
    comments = comments[start_line - 1 :]
    if count is not None:
        comments = comments[:count]
    if not comments:
        raise ValueError("Comments file has no non-empty lines")
    return comments


def mark_next_comment_button(
    page: Any, seen_post_keys: set[str], min_action_page_y: int
) -> dict[str, Any]:
    token = f"codex-fb-comment-{time.time_ns()}"
    result = page.evaluate(
        """
        ({ token, seenPostKeys, minActionPageY }) => {
          const reactionActionNames = new Set(["Like", "Thích", "React", "Bày tỏ cảm xúc"]);

          const normalize = (value) => (value || "").replace(/\\s+/g, " ").trim();
          const labelOf = (el) => normalize(el.getAttribute("aria-label") || el.innerText);
          const isCommentAction = (name) => /^(Comment|Bình luận)$|leave a comment|viết bình luận/i.test(name);

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
              const hasComment = names.some(isCommentAction);
              const hasShare = names.some((name) => /share|chia sẻ|send/i.test(name));
              if (hasReactionAction && hasComment && hasShare) {
                return node;
              }
              node = node.parentElement;
            }
            return null;
          };

          const postContainerFor = (group) => {
            const groupRect = group.getBoundingClientRect();
            let node = group.parentElement;
            for (let depth = 0; node && node !== document.body && depth < 18; depth += 1) {
              const rect = node.getBoundingClientRect();
              const containsGroup =
                rect.top <= groupRect.top &&
                rect.bottom >= groupRect.bottom &&
                rect.left <= groupRect.left + 10 &&
                rect.right >= groupRect.right - 10;
              const hasPostMenu = Array.from(node.querySelectorAll('[role="button"]'))
                .map(labelOf)
                .some((name) => /actions for this post|open menu for|ẩn bài viết|hide post/i.test(name));
              const hasEnoughShape =
                rect.width >= 420 &&
                rect.height >= Math.max(130, groupRect.height + 80) &&
                rect.height <= Math.max(2600, window.innerHeight * 3);
              if (containsGroup && hasPostMenu && hasEnoughShape) {
                return node;
              }
              node = node.parentElement;
            }
            return null;
          };

          const postKey = (post) => {
            const rect = post.getBoundingClientRect();
            const links = Array.from(post.querySelectorAll('a, [role="link"]'))
              .map((link) => {
                const href = link.href || link.getAttribute("href") || "";
                const label = labelOf(link);
                return href || label;
              })
              .filter(Boolean)
              .slice(0, 8)
              .join("|");
            const menu = Array.from(post.querySelectorAll('[role="button"]'))
              .map(labelOf)
              .find((name) => /actions for this post|open menu for|ẩn bài viết|hide post/i.test(name)) || "";
            return [
              menu,
              Math.round(rect.left),
              Math.round(rect.width),
              links,
              Math.round((rect.top + window.scrollY) / 25) * 25,
            ].join("|");
          };

          const actionKey = (group) => {
            const rect = group.getBoundingClientRect();
            const text = normalize(group.innerText).slice(0, 160);
            return `${Math.round(rect.top + window.scrollY)}|${Math.round(rect.left)}|${text}`;
          };

          const buttons = Array.from(document.querySelectorAll('[role="button"]'))
            .filter(isVisible);
          const groups = new Set();
          const posts = new Set();

          for (const button of buttons) {
            const name = labelOf(button);
            if (!isCommentAction(name)) {
              continue;
            }

            const group = actionGroupFor(button);
            if (!group) {
              continue;
            }
            const rect = button.getBoundingClientRect();
            const actionPageY = Math.round(window.scrollY + rect.top);
            if (actionPageY <= minActionPageY) {
              continue;
            }

            const key = actionKey(group);
            groups.add(key);

            const post = postContainerFor(group);
            if (!post) {
              continue;
            }
            const keyForPost = postKey(post);
            posts.add(keyForPost);
            if (
              post.getAttribute("data-codex-fb-comment-processed") === "true" ||
              seenPostKeys.includes(keyForPost)
            ) {
              continue;
            }

            button.setAttribute("data-codex-fb-comment-target", token);
            post.setAttribute("data-codex-fb-comment-post-target", token);
            const postRect = post.getBoundingClientRect();
            return {
              ok: true,
              token,
              actionKey: key,
              postKey: keyForPost,
              actionPageY,
              label: name,
              visibleActionGroups: groups.size,
              visiblePosts: posts.size,
              rect: {
                x: Math.round(rect.left),
                y: Math.round(rect.top),
                width: Math.round(rect.width),
                height: Math.round(rect.height),
              },
              postRect: {
                x: Math.round(postRect.left),
                y: Math.round(postRect.top),
                width: Math.round(postRect.width),
                height: Math.round(postRect.height),
                pageBottom: Math.round(window.scrollY + postRect.bottom),
              },
            };
          }

          return {
            ok: false,
            token,
            visibleButtons: buttons.length,
            visibleActionGroups: groups.size,
            visiblePosts: posts.size,
          };
        }
        """,
        {
            "token": token,
            "seenPostKeys": list(seen_post_keys),
            "minActionPageY": min_action_page_y,
        },
    )
    return result


def mark_nearest_comment_box(page: Any, target_rect: dict[str, int]) -> dict[str, Any]:
    token = f"codex-fb-comment-box-{time.time_ns()}"
    result = page.evaluate(
        """
        ({ token, targetRect }) => {
          const normalize = (value) => (value || "").replace(/\\s+/g, " ").trim();
          const labelOf = (el) => normalize(
            el.getAttribute("aria-label") ||
            el.getAttribute("placeholder") ||
            el.innerText
          );

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

          const isCommentBox = (el) => {
            const label = labelOf(el);
            if (/what's on your mind|bạn đang nghĩ gì/i.test(label)) {
              return false;
            }
            if (/comment|bình luận|viết bình luận/i.test(label)) {
              return true;
            }
            const rect = el.getBoundingClientRect();
            return (
              el.getAttribute("role") === "textbox" &&
              rect.top >= targetRect.y - 80 &&
              rect.top <= targetRect.y + 520
            );
          };

          const candidates = Array.from(
            document.querySelectorAll('[contenteditable="true"], [role="textbox"]')
          )
            .filter(isVisible)
            .filter(isCommentBox)
            .map((el) => {
              const rect = el.getBoundingClientRect();
              const label = labelOf(el);
              const labelScore = /comment|bình luận|viết bình luận/i.test(label) ? 0 : 1000;
              const yDistance = Math.abs(rect.top - targetRect.y);
              const xDistance = Math.abs(rect.left - targetRect.x);
              return { el, label, score: labelScore + yDistance + xDistance / 10, rect };
            })
            .sort((a, b) => a.score - b.score);

          const match = candidates[0];
          if (!match) {
            return { ok: false, token, candidates: candidates.length };
          }

          match.el.setAttribute("data-codex-fb-comment-box", token);
          return {
            ok: true,
            token,
            label: match.label,
            rect: {
              x: Math.round(match.rect.left),
              y: Math.round(match.rect.top),
              width: Math.round(match.rect.width),
              height: Math.round(match.rect.height),
            },
          };
        }
        """,
        {"token": token, "targetRect": target_rect},
    )
    return result


def open_feed_page(page: Any, feed_url: str) -> None:
    page.on("dialog", lambda dialog: dialog.dismiss())
    if page.url == "about:blank" or not is_same_page(page.url, feed_url):
        page.goto(feed_url, wait_until="domcontentloaded", timeout=60_000)
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except PlaywrightTimeoutError:
            pass
    else:
        page.bring_to_front()
        page.wait_for_timeout(1_000)


def click_with_dom_fallback(locator: Locator, timeout: int) -> str:
    try:
        locator.click(timeout=timeout)
        return "playwright"
    except PlaywrightTimeoutError:
        locator.evaluate(
            """
            (el) => {
              el.scrollIntoView({ block: "center", inline: "center" });
              if (typeof el.focus === "function") {
                el.focus();
              }
              el.click();
            }
            """
        )
        return "dom"


def mark_post_processed(page: Any, target: dict[str, Any]) -> None:
    page.evaluate(
        """
        ({ token }) => {
          const post = document.querySelector(
            `[data-codex-fb-comment-post-target="${token}"]`
          );
          if (post) {
            post.setAttribute("data-codex-fb-comment-processed", "true");
          }
        }
        """,
        {"token": target.get("token")},
    )


def scroll_past_post(page: Any, target: dict[str, Any], min_scroll: int) -> None:
    page.evaluate(
        """
        ({ token, postRect, minScroll }) => {
          const post = document.querySelector(
            `[data-codex-fb-comment-post-target="${token}"]`
          );
          let pageBottom = postRect?.pageBottom || window.scrollY + minScroll;
          if (post) {
            const rect = post.getBoundingClientRect();
            pageBottom = window.scrollY + rect.bottom;
          }

          const nextTop = Math.max(
            window.scrollY + Math.max(160, Math.floor(minScroll / 3)),
            (postRect?.actionPageY || 0) + Math.max(220, Math.floor(minScroll / 2)),
            pageBottom - Math.floor(window.innerHeight * 0.18)
          );
          window.scrollTo({ top: nextTop, behavior: "instant" });
        }
        """,
        {
            "token": target.get("token"),
            "postRect": {
                **(target.get("postRect") or {}),
                "actionPageY": target.get("actionPageY", 0),
            },
            "minScroll": min_scroll,
        },
    )


def main() -> int:
    args = build_parser().parse_args()
    try:
        comments = load_comments(args.comments_file, args.count, args.start_line)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    seen_post_keys: set[str] = set()
    min_action_page_y = -1
    posted = 0
    scrolls = 0
    url = args.cdp_url or cdp_url(args.host, args.profile_id)

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(url)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.pages[0] if context.pages else context.new_page()

        print(f"Connected: {url}")
        open_feed_page(page, args.feed_url)
        print(f"Page: {page.title()} ({page.url})")
        print(f"Loaded {len(comments)} comment line(s) from start line {args.start_line}.")

        while posted < len(comments) and scrolls < args.max_scrolls:
            target = mark_next_comment_button(page, seen_post_keys, min_action_page_y)
            if not target.get("ok"):
                scrolls += 1
                print(
                    f"No visible comment target; scrolling {scrolls}/{args.max_scrolls} "
                    f"(visible buttons: {target.get('visibleButtons', 0)}, "
                    f"action groups: {target.get('visibleActionGroups', 0)}, "
                    f"posts: {target.get('visiblePosts', 0)})"
                )
                page.mouse.wheel(0, args.scroll_px)
                page.wait_for_timeout(random.randint(1200, 2200))
                continue

            comment = comments[posted]
            seen_post_keys.add(target["postKey"])
            min_action_page_y = max(
                min_action_page_y,
                int(target.get("actionPageY", 0)) + max(220, args.scroll_px // 2),
            )
            selector = f'[data-codex-fb-comment-target="{target["token"]}"]'
            button = page.locator(selector).first
            button.scroll_into_view_if_needed(timeout=5_000)

            if args.dry_run:
                print(
                    f"Dry run target {posted + 1}/{len(comments)}: "
                    f"{target['label']} at {target.get('rect')} "
                    f"pageY {target.get('actionPageY')} "
                    f"post {target.get('postRect')} -> {comment!r}"
                )
                mark_post_processed(page, target)
                scroll_past_post(page, target, args.scroll_px)
                posted += 1
                page.wait_for_timeout(random.randint(700, 1300))
                continue

            click_method = click_with_dom_fallback(button, timeout=8_000)
            page.wait_for_timeout(random.randint(900, 1500))

            box = mark_nearest_comment_box(page, target.get("rect", {}))
            if not box.get("ok"):
                print(
                    f"Skipped target {posted + 1}: could not find comment box "
                    f"near {target.get('rect')}"
                )
                scroll_past_post(page, target, args.scroll_px)
                continue

            textbox = page.locator(f'[data-codex-fb-comment-box="{box["token"]}"]').first
            textbox_click_method = click_with_dom_fallback(textbox, timeout=5_000)
            textbox.fill(comment, timeout=8_000)
            page.wait_for_timeout(random.randint(300, 700))
            textbox.press("Enter", timeout=8_000)

            posted += 1
            mark_post_processed(page, target)
            print(
                f"Commented {posted}/{len(comments)} at {target.get('rect')}: "
                f"{comment!r} pageY {target.get('actionPageY')} "
                f"(button click: {click_method}, textbox click: {textbox_click_method})"
            )
            scroll_past_post(page, target, args.scroll_px)
            page.wait_for_timeout(random.randint(1800, 3200))

        action = "Planned" if args.dry_run else "Commented"
        print(f"Done. {action} {posted} post(s).")
        if posted < len(comments):
            print(f"Stopped after {scrolls} scroll(s); requested {len(comments)}.")
        return 0 if posted == len(comments) or args.dry_run else 1


if __name__ == "__main__":
    raise SystemExit(main())
