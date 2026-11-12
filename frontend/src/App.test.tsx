import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { api } from "./lib/api";
import { useProfiles } from "./hooks/useProfiles";
import type { Profile } from "./lib/api";

const { rfbCreated, rfbDisconnect } = vi.hoisted(() => ({
  rfbCreated: vi.fn(),
  rfbDisconnect: vi.fn(),
}));

vi.mock("@novnc/novnc", () => ({
  default: class extends EventTarget {
    disconnect = rfbDisconnect;
    constructor() {
      super();
      rfbCreated();
    }
  },
}));

vi.mock("./components/ProfileFiles", () => ({
  ProfileFilesButton: () => null,
  useProfileFiles: () => ({ files: [], transfers: [], uploading: false, error: null }),
}));

vi.mock("./lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      authStatus: vi.fn(),
      getConfig: vi.fn(),
      getStatus: vi.fn(),
      checkUpdate: vi.fn(),
      logout: vi.fn(),
    },
    setOnUnauthorized: vi.fn(),
  };
});

vi.mock("./hooks/useProfiles", () => ({
  useProfiles: vi.fn(),
}));

const mockApi = api as typeof api & {
  authStatus: ReturnType<typeof vi.fn>;
  getConfig: ReturnType<typeof vi.fn>;
  getStatus: ReturnType<typeof vi.fn>;
  checkUpdate: ReturnType<typeof vi.fn>;
  logout: ReturnType<typeof vi.fn>;
};
const mockUseProfiles = useProfiles as ReturnType<typeof vi.fn>;

beforeEach(() => {
  mockApi.authStatus.mockResolvedValue({ auth_required: false, authenticated: false });
  mockApi.getConfig.mockResolvedValue({ sidebar_width: "20rem" });
  mockApi.getStatus.mockRejectedValue(new Error("not available in unit test"));
  mockApi.checkUpdate.mockRejectedValue(new Error("not available in unit test"));
  mockApi.logout.mockResolvedValue({ ok: true });
  mockUseProfiles.mockReturnValue({
    profiles: [],
    loading: false,
    error: null,
    refresh: vi.fn(),
    create: vi.fn(),
    update: vi.fn(),
    remove: vi.fn(),
    reorder: vi.fn(),
    launch: vi.fn(),
    stop: vi.fn(),
    reset: vi.fn(),
    duplicate: vi.fn(),
  });
});

describe("App", () => {
  it("applies the configured sidebar width", async () => {
    render(<App />);

    const sidebar = await screen.findByLabelText("Profile sidebar");

    await waitFor(() => expect(mockApi.getConfig).toHaveBeenCalled());
    await waitFor(() => expect(sidebar.getAttribute("style")).toContain("width: 20rem"));
  });
});

describe("running VNC profile details", () => {
  it("opens read-only details and returns without dropping the VNC session", async () => {
    const running = {
      id: "p1",
      name: "Work",
      status: "running",
      viewer_mode: "vnc",
      cdp_url: null,
      clipboard_sync: false,
      fingerprint_seed: 42,
      license_key_set: false,
      license_key_masked: null,
      release_channel: "stable",
      browser_version: null,
      screen_width: 1920,
      screen_height: 1080,
      launch_args: [],
      extension_paths: [],
      tags: [],
    } as unknown as Profile;
    mockUseProfiles.mockReturnValue({ ...mockUseProfiles(), profiles: [running] });

    render(<App />);
    fireEvent.click(await screen.findByText("Work"));
    await waitFor(() => expect(rfbCreated).toHaveBeenCalledOnce());

    fireEvent.click(screen.getByLabelText("Profile details"));
    const back = await screen.findByRole("button", { name: "Back to browser" });
    expect(screen.getByDisplayValue("42").matches(":disabled")).toBe(true);
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();

    fireEvent.click(back);
    expect(screen.queryByText("Profile Details")).toBeNull();
    expect(rfbDisconnect).not.toHaveBeenCalled();
    expect(rfbCreated).toHaveBeenCalledOnce();
  });
});
