import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { Profile } from "../lib/api";
import { ProfileList } from "./ProfileList";

function profile(overrides: Partial<Profile>): Profile {
  return {
    id: "profile-1",
    name: "Base Profile",
    fingerprint_seed: 12345,
    proxy: null,
    timezone: null,
    locale: null,
    screen_width: 1920,
    screen_height: 1080,
    gpu_family: "auto",
    humanize: false,
    human_preset: "default",
    geoip: false,
    clipboard_sync: true,
    auto_launch: false,
    color_scheme: null,
    launch_args: [],
    extension_paths: [],
    allow_3p_cookies: true,
    set_google_default: true,
    capture_preview: true,
    restore_session: true,
    notes: null,
    user_data_dir: "/data/profiles/profile-1",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    sort_order: 0,
    tags: [],
    status: "stopped",
    runtime_mode: "docker",
    viewer_mode: "vnc",
    vnc_ws_port: null,
    cdp_url: null,
    last_error: null,
    ...overrides,
  };
}

function renderList(profiles: Profile[]) {
  render(
    <ProfileList
      profiles={profiles}
      selectedId={null}
      onSelect={vi.fn()}
      onNew={vi.fn()}
      onReorder={vi.fn()}
    />,
  );
}

function pressed(name: RegExp): string | null {
  return screen.getByRole("button", { name }).getAttribute("aria-pressed");
}

describe("ProfileList", () => {
  it("keeps tag names out of text search", () => {
    renderList([
      profile({
        id: "tagged",
        name: "Tagged Account",
        tags: [{ tag: "claude", color: null }],
      }),
      profile({ id: "plain", name: "Plain Account" }),
    ]);

    fireEvent.change(screen.getByPlaceholderText("Search profiles..."), {
      target: { value: "claude" },
    });

    expect(screen.queryByText("Tagged Account")).toBeNull();
    expect(screen.queryByText("Plain Account")).toBeNull();
    expect(screen.queryByText("No matches")).not.toBeNull();
  });

  it("defaults to All and toggles All and Ungrouped as mutually exclusive filters", () => {
    renderList([
      profile({ id: "plain", name: "Plain Account" }),
      profile({
        id: "claude",
        name: "Claude Account",
        tags: [{ tag: "claude", color: null }],
      }),
      profile({
        id: "team",
        name: "Team Account",
        tags: [{ tag: "team", color: null }],
      }),
    ]);

    expect(pressed(/^All\s+3$/)).toBe("true");
    expect(pressed(/^Ungrouped\s+1$/)).toBe("false");
    expect(screen.queryByText("Plain Account")).not.toBeNull();
    expect(screen.queryByText("Claude Account")).not.toBeNull();
    expect(screen.queryByText("Team Account")).not.toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /^Ungrouped\s+1$/ }));

    expect(pressed(/^All\s+3$/)).toBe("false");
    expect(pressed(/^Ungrouped\s+1$/)).toBe("true");
    expect(screen.queryByText("Plain Account")).not.toBeNull();
    expect(screen.queryByText("Claude Account")).toBeNull();
    expect(screen.queryByText("Team Account")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /^All\s+3$/ }));

    expect(pressed(/^All\s+3$/)).toBe("true");
    expect(pressed(/^Ungrouped\s+1$/)).toBe("false");
    expect(screen.queryByText("Claude Account")).not.toBeNull();
    expect(screen.queryByText("Team Account")).not.toBeNull();
  });

  it("filters ordinary tags with multi-select AND semantics and returns to All when cleared", () => {
    renderList([
      profile({
        id: "claude",
        name: "Claude Account",
        tags: [{ tag: "claude", color: null }],
      }),
      profile({
        id: "team",
        name: "Team Account",
        tags: [{ tag: "team", color: null }],
      }),
      profile({
        id: "both",
        name: "Claude Team Account",
        tags: [
          { tag: "claude", color: null },
          { tag: "team", color: null },
        ],
      }),
    ]);

    fireEvent.click(screen.getByRole("button", { name: /^claude\s+2$/ }));

    expect(pressed(/^claude\s+2$/)).toBe("true");
    expect(screen.queryByText("Claude Account")).not.toBeNull();
    expect(screen.queryByText("Claude Team Account")).not.toBeNull();
    expect(screen.queryByText("Team Account")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /^team\s+2$/ }));

    expect(pressed(/^claude\s+2$/)).toBe("true");
    expect(pressed(/^team\s+2$/)).toBe("true");
    expect(screen.queryByText("Claude Team Account")).not.toBeNull();
    expect(screen.queryByText("Claude Account")).toBeNull();
    expect(screen.queryByText("Team Account")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /^team\s+2$/ }));
    fireEvent.click(screen.getByRole("button", { name: /^claude\s+2$/ }));

    expect(pressed(/^All\s+3$/)).toBe("true");
    expect(screen.queryByText("Claude Account")).not.toBeNull();
    expect(screen.queryByText("Team Account")).not.toBeNull();
  });

  it("wraps tag filters instead of using horizontal scrolling", () => {
    renderList([
      profile({
        id: "claude",
        name: "Claude Account",
        tags: [{ tag: "claude", color: null }],
      }),
    ]);

    const filterGroup = screen.getByRole("group", { name: "Profile tag filters" });
    expect(filterGroup.className).toContain("flex-wrap");
    expect(filterGroup.className).not.toContain("overflow-x-auto");
  });

  it("filters profiles by notes and renders notes in each list item", () => {
    renderList([
      profile({ id: "noted", name: "Noted Account", notes: "billing appeal sent" }),
      profile({ id: "unrelated", name: "Unrelated Account", notes: "clean" }),
    ]);

    expect(screen.queryByText("billing appeal sent")).not.toBeNull();

    fireEvent.change(screen.getByPlaceholderText("Search profiles..."), {
      target: { value: "appeal" },
    });

    expect(screen.queryByText("Noted Account")).not.toBeNull();
    expect(screen.queryByText("Unrelated Account")).toBeNull();
  });

  it("renders tags and notes on one compact metadata line", () => {
    renderList([
      profile({
        id: "compact",
        name: "Compact Account",
        proxy: "http://127.0.0.1:7890",
        notes: "team note",
        tags: [{ tag: "team", color: null }],
      }),
    ]);

    const item = screen.getByText("Compact Account").closest("button");
    expect(item).not.toBeNull();

    const itemQueries = within(item as HTMLElement);
    expect(itemQueries.queryByText("Proxy")).toBeNull();

    const tag = itemQueries.getByText("team");
    const notes = itemQueries.getByText("team note");
    expect(tag.parentElement).toBe(notes.parentElement);
  });

  it("disables drag ordering while a tag filter is active", () => {
    renderList([
      profile({
        id: "team",
        name: "Team Account",
        tags: [{ tag: "team", color: null }],
      }),
    ]);

    const row = screen.getByText("Team Account").closest("button");
    expect(row?.className).toContain("cursor-grab");

    fireEvent.click(screen.getByRole("button", { name: /^team\s+1$/ }));

    expect(row?.className).not.toContain("cursor-grab");
  });
});
