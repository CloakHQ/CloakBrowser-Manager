import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ManagerSettings, Profile, SystemStatus } from "../lib/api";
import { renderWithProviders as render } from "../test/render";
import { LicensePage } from "./LicensePage";
import { ProfileTable } from "./ProfileTable";
import { ProxyPage } from "./ProxyPage";
import { Sidebar } from "./Sidebar";

const profile = (over: Partial<Profile> = {}): Profile => ({
  id: "p1",
  name: "Hồ sơ A",
  fingerprint_seed: 1234,
  proxy: null,
  proxy_provider: "none",
  proxy_provider_key_set: false,
  proxy_provider_key_masked: null,
  kiotproxy_region: "random",
  tmproxy_location_id: 0,
  tmproxy_isp_id: 0,
  active_proxy: null,
  timezone: null,
  locale: null,
  screen_width: 1920,
  screen_height: 1080,
  gpu_family: "auto",
  humanize: true,
  human_preset: "default",
  geoip: true,
  clipboard_sync: false,
  auto_launch: false,
  color_scheme: null,
  launch_args: [],
  extension_paths: [],
  allow_3p_cookies: false,
  set_google_default: false,
  capture_preview: false,
  restore_session: true,
  transparent_proxy: false,
  portable_cookies: false,
  license_key_id: null,
  notes: null,
  tags: [],
  user_data_dir: "/tmp/p1",
  created_at: "2026-10-01T00:00:00Z",
  updated_at: "2026-10-01T00:00:00Z",
  status: "stopped",
  cdp_url: null,
  last_error: null,
  ...over,
});

const status: SystemStatus = {
  running_count: 0,
  binary_version: "0.7.0",
  license_tier: "free",
  license_plan: "free",
  profiles_total: 1,
  host_os: "windows",
  runtime_mode: "native",
  viewer_mode: "native-window",
  windows_fonts_present: 1,
  windows_fonts_required: 1,
  windows_fonts_complete: true,
};

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    getSettings: vi.fn(),
    updateSettings: vi.fn(),
    listKeys: vi.fn(),
    addKeys: vi.fn(),
    deleteKey: vi.fn(),
    checkKeys: vi.fn(),
  },
}));

vi.mock("../lib/api", () => ({ api: apiMock }));

beforeEach(() => {
  vi.clearAllMocks();
  const settings: ManagerSettings = {
    license_key_set: true,
    license_key_masked: "cb_abc…wxyz",
    release_channel: "stable",
    key_pool_size: 3,
  };
  apiMock.getSettings.mockResolvedValue(settings);
  apiMock.listKeys.mockResolvedValue([]);
});

describe("Sidebar", () => {
  it("navigates and marks the active section", () => {
    const onNavigate = vi.fn();
    render(
      <Sidebar
        page="overview"
        onNavigate={onNavigate}
        status={status}
        runningCount={1}
        profileCount={2}
        version="0.2.0"
        authRequired={false}
        onLogout={() => {}}
        onQuit={() => {}}
      />,
    );

    const nav = screen.getByRole("navigation", { name: "Điều hướng" });
    expect(within(nav).getByRole("button", { name: /Tổng quan/ }).getAttribute("aria-current")).toBe("page");

    fireEvent.click(within(nav).getByRole("button", { name: /Proxy/ }));
    expect(onNavigate).toHaveBeenCalledWith("proxy");
  });

  it("flags a missing key on the license item", () => {
    render(
      <Sidebar
        page="overview"
        onNavigate={() => {}}
        status={{ ...status, license_tier: "none" }}
        runningCount={0}
        profileCount={0}
        version={null}
        authRequired={false}
        onLogout={() => {}}
        onQuit={() => {}}
      />,
    );
    expect(screen.getByText("cần key")).toBeTruthy();
  });
});

describe("ProxyPage", () => {
  it("shows the provider and the address each profile exits from", () => {
    render(
      <ProxyPage
        profiles={[
          profile({
            id: "p1",
            name: "Rotating",
            proxy_provider: "kiotproxy",
            proxy_provider_key_set: true,
            active_proxy: {
              provider: "kiotproxy",
              host: "103.1.2.3:8000",
              real_ip: "103.1.2.3",
              location: "Hà Nội",
              isp: null,
              expires_at: null,
              ttl: null,
              ttc: null,
              next_request_at: null,
            },
          }),
          profile({ id: "p2", name: "Direct", proxy: null }),
        ]}
        onEdit={() => {}}
      />,
    );

    expect(screen.getByText("103.1.2.3:8000")).toBeTruthy();
    expect(screen.getByText("trực tiếp")).toBeTruthy();
  });
});

describe("ProfileTable", () => {
  it("moves a profile down through the row menu", () => {
    const onReorder = vi.fn();
    render(
      <ProfileTable
        profiles={[profile({ id: "a", name: "A" }), profile({ id: "b", name: "B" })]}
        selectedId={null}
        systemStatus={status}
        onSelect={() => {}}
        onEdit={() => {}}
        onNew={() => {}}
        onOpenSettings={() => {}}
        onLaunch={async () => {}}
        onStop={async () => {}}
        onDuplicate={() => {}}
        onDelete={() => {}}
        onReorder={onReorder}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Thao tác khác cho A" }));
    fireEvent.click(screen.getByRole("menuitem", { name: /Di chuyển xuống/ }));
    expect(onReorder).toHaveBeenCalledWith(["b", "a"]);
  });

  it("refuses to delete a running profile from the menu", () => {
    render(
      <ProfileTable
        profiles={[profile({ id: "a", name: "A", status: "running" })]}
        selectedId={null}
        systemStatus={status}
        onSelect={() => {}}
        onEdit={() => {}}
        onNew={() => {}}
        onOpenSettings={() => {}}
        onLaunch={async () => {}}
        onStop={async () => {}}
        onDuplicate={() => {}}
        onDelete={() => {}}
        onReorder={() => {}}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Thao tác khác cho A" }));
    expect((screen.getByRole("menuitem", { name: /Xóa hồ sơ/ }) as HTMLButtonElement).disabled).toBe(true);
  });
});

describe("LicensePage", () => {
  it("shows the tier, pool size and the masked shared key", async () => {
    render(<LicensePage status={status} onStatusChange={() => {}} />);

    expect(screen.getByText("Key miễn phí")).toBeTruthy();
    expect(await screen.findByText("cb_abc…wxyz")).toBeTruthy();
    await waitFor(() => expect(apiMock.getSettings).toHaveBeenCalled());
  });
});
