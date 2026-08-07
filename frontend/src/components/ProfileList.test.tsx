import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { ProfileList } from "./ProfileList";
import type { Profile } from "../lib/api";

function profile(overrides: Partial<Profile> = {}): Profile {
  return {
    id: "p1",
    name: "Profile 1",
    fingerprint_seed: 1,
    proxy: null,
    timezone: null,
    locale: null,
    platform: "windows",
    user_agent: null,
    screen_width: 1920,
    screen_height: 1080,
    gpu_vendor: null,
    gpu_renderer: null,
    hardware_concurrency: null,
    humanize: false,
    human_preset: "default",
    headless: false,
    geoip: false,
    clipboard_sync: true,
    auto_launch: false,
    auto_restart: false,
    auto_restart_exhausted: false,
    color_scheme: null,
    license_key: null,
    enabled_extensions: [],
    idle_timeout_seconds: null,
    launch_args: [],
    notes: null,
    user_data_dir: "/data/profiles/p1",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    tags: [],
    status: "stopped",
    vnc_ws_port: null,
    cdp_url: null,
    ...overrides,
  };
}

describe("ProfileList", () => {
  it("shows an empty state with no profiles", () => {
    render(<ProfileList profiles={[]} selectedId={null} onSelect={vi.fn()} onNew={vi.fn()} />);
    expect(screen.getByText("No profiles yet")).toBeInTheDocument();
  });

  it("renders each profile and reports the running count", () => {
    render(
      <ProfileList
        profiles={[profile({ id: "p1", name: "A" }), profile({ id: "p2", name: "B", status: "running" })]}
        selectedId={null}
        onSelect={vi.fn()}
        onNew={vi.fn()}
      />,
    );
    expect(screen.getByText("A")).toBeInTheDocument();
    expect(screen.getByText("B")).toBeInTheDocument();
    expect(screen.getByText("1 running")).toBeInTheDocument();
  });

  it("calls onSelect with the clicked profile's id", () => {
    const onSelect = vi.fn();
    render(
      <ProfileList
        profiles={[profile({ id: "p1", name: "A" })]}
        selectedId={null}
        onSelect={onSelect}
        onNew={vi.fn()}
      />,
    );
    screen.getByText("A").click();
    expect(onSelect).toHaveBeenCalledWith("p1");
  });

  it("filters by the search box", () => {
    render(
      <ProfileList
        profiles={[profile({ id: "p1", name: "Alpha" }), profile({ id: "p2", name: "Beta" })]}
        selectedId={null}
        onSelect={vi.fn()}
        onNew={vi.fn()}
      />,
    );
    const input = screen.getByPlaceholderText("Search profiles...");
    fireEvent.change(input, { target: { value: "alp" } });
    expect(screen.getByText("Alpha")).toBeInTheDocument();
    expect(screen.queryByText("Beta")).not.toBeInTheDocument();
  });

  it("calls onNew when the New Profile button is clicked", () => {
    const onNew = vi.fn();
    render(<ProfileList profiles={[]} selectedId={null} onSelect={vi.fn()} onNew={onNew} />);
    screen.getByRole("button", { name: /new profile/i }).click();
    expect(onNew).toHaveBeenCalledTimes(1);
  });

  it("does not show the auto-restart warning for a healthy profile", () => {
    render(
      <ProfileList
        profiles={[profile({ auto_restart: true, auto_restart_exhausted: false })]}
        selectedId={null}
        onSelect={vi.fn()}
        onNew={vi.fn()}
      />,
    );
    expect(screen.queryByTitle(/on cooldown/i)).not.toBeInTheDocument();
  });

  it("shows the auto-restart cooldown warning when the budget is exhausted", () => {
    render(
      <ProfileList
        profiles={[profile({ auto_restart: true, auto_restart_exhausted: true })]}
        selectedId={null}
        onSelect={vi.fn()}
        onNew={vi.fn()}
      />,
    );
    expect(screen.getByTitle(/on cooldown/i)).toBeInTheDocument();
  });
});
