import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, act } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { TabManagerPanel } from "./TabManagerPanel";
import type { Profile } from "../lib/api";

vi.mock("../lib/api", () => ({
  api: {
    listTabs: vi.fn(),
    closeTab: vi.fn(),
  },
}));

import { api } from "../lib/api";

const mockApi = api as unknown as {
  listTabs: ReturnType<typeof vi.fn>;
  closeTab: ReturnType<typeof vi.fn>;
};

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
    status: "running",
    vnc_ws_port: 6100,
    cdp_url: "/api/profiles/p1/cdp",
    ...overrides,
  };
}

beforeEach(() => {
  vi.resetAllMocks();
});

function openPanel() {
  return act(async () => {
    screen.getByRole("button", { name: /tab manager/i }).click();
  });
}

describe("TabManagerPanel", () => {
  it("does not fetch until opened", () => {
    render(<TabManagerPanel profiles={[profile()]} />);
    expect(mockApi.listTabs).not.toHaveBeenCalled();
  });

  it("reports no running profiles when none are running", async () => {
    render(<TabManagerPanel profiles={[profile({ status: "stopped" })]} />);
    await openPanel();
    expect(screen.getByText("No profiles are running.")).toBeInTheDocument();
    expect(mockApi.listTabs).not.toHaveBeenCalled();
  });

  it("fetches and renders tabs for each running profile", async () => {
    mockApi.listTabs.mockResolvedValue({
      tabs: [
        { index: 0, title: "Example", url: "https://example.com/", favicon: null },
      ],
    });
    render(<TabManagerPanel profiles={[profile()]} />);
    await openPanel();

    expect(mockApi.listTabs).toHaveBeenCalledWith("p1");
    expect(screen.getByText("Profile 1")).toBeInTheDocument();
    expect(screen.getByText("Example")).toBeInTheDocument();
    expect(screen.getByText("https://example.com/")).toBeInTheDocument();
  });

  it("ignores stopped profiles when fetching", async () => {
    mockApi.listTabs.mockResolvedValue({ tabs: [] });
    render(
      <TabManagerPanel
        profiles={[profile({ id: "p1", status: "running" }), profile({ id: "p2", status: "stopped" })]}
      />,
    );
    await openPanel();
    expect(mockApi.listTabs).toHaveBeenCalledTimes(1);
    expect(mockApi.listTabs).toHaveBeenCalledWith("p1");
  });

  it("shows an empty state for a running profile with no open tabs", async () => {
    mockApi.listTabs.mockResolvedValue({ tabs: [] });
    render(<TabManagerPanel profiles={[profile()]} />);
    await openPanel();
    expect(screen.getByText("No open tabs.")).toBeInTheDocument();
  });

  it("shows an error state when the fetch fails", async () => {
    mockApi.listTabs.mockRejectedValue(new Error("boom"));
    render(<TabManagerPanel profiles={[profile()]} />);
    await openPanel();
    expect(screen.getByText("Failed to load tabs.")).toBeInTheDocument();
  });

  it("closes a tab and refreshes the list", async () => {
    mockApi.listTabs
      .mockResolvedValueOnce({
        tabs: [{ index: 0, title: "Example", url: "https://example.com/", favicon: null }],
      })
      .mockResolvedValueOnce({ tabs: [] });
    mockApi.closeTab.mockResolvedValue({ ok: true });

    render(<TabManagerPanel profiles={[profile()]} />);
    await openPanel();
    expect(screen.getByText("Example")).toBeInTheDocument();

    await act(async () => {
      screen.getByRole("button", { name: /close tab/i }).click();
    });

    expect(mockApi.closeTab).toHaveBeenCalledWith("p1", 0);
    expect(mockApi.listTabs).toHaveBeenCalledTimes(2);
    expect(screen.getByText("No open tabs.")).toBeInTheDocument();
  });

  it("still refreshes if closing the tab fails", async () => {
    mockApi.listTabs
      .mockResolvedValueOnce({
        tabs: [{ index: 0, title: "Example", url: "https://example.com/", favicon: null }],
      })
      .mockResolvedValueOnce({ tabs: [] });
    mockApi.closeTab.mockRejectedValue(new Error("gone"));

    render(<TabManagerPanel profiles={[profile()]} />);
    await openPanel();

    await act(async () => {
      screen.getByRole("button", { name: /close tab/i }).click();
    });

    expect(mockApi.listTabs).toHaveBeenCalledTimes(2);
  });

  it("closes the panel when the close button is clicked", async () => {
    mockApi.listTabs.mockResolvedValue({ tabs: [] });
    render(<TabManagerPanel profiles={[profile()]} />);
    await openPanel();
    expect(screen.getByText("Open Tabs")).toBeInTheDocument();

    await act(async () => {
      screen.getByRole("button", { name: /close$/i }).click();
    });
    expect(screen.queryByText("Open Tabs")).not.toBeInTheDocument();
  });
});
