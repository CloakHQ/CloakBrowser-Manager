import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import { useProfiles } from "./useProfiles";

// Mock the api module
vi.mock("../lib/api", () => ({
  api: {
    listProfiles: vi.fn(),
    createProfile: vi.fn(),
    updateProfile: vi.fn(),
    deleteProfile: vi.fn(),
    reorderProfiles: vi.fn(),
    launchProfile: vi.fn(),
    stopProfile: vi.fn(),
  },
}));

import { api } from "../lib/api";

const mockApi = api as {
  listProfiles: ReturnType<typeof vi.fn>;
  createProfile: ReturnType<typeof vi.fn>;
  updateProfile: ReturnType<typeof vi.fn>;
  deleteProfile: ReturnType<typeof vi.fn>;
  reorderProfiles: ReturnType<typeof vi.fn>;
  launchProfile: ReturnType<typeof vi.fn>;
  stopProfile: ReturnType<typeof vi.fn>;
};

const fakeProfile = {
  id: "abc-123",
  name: "Test",
  fingerprint_seed: 12345,
  proxy: null,
  timezone: null,
  locale: null,
  screen_width: 1920,
  screen_height: 1080,
  gpu_family: "auto" as const,
  humanize: false,
  human_preset: "default",
  geoip: true,
  clipboard_sync: true,
  auto_launch: false,
  color_scheme: null,
  launch_args: [],
  extension_paths: [],
  allow_3p_cookies: false,
  notes: null,
  user_data_dir: "/data/profiles/abc-123",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  sort_order: 0,
  tags: [],
  status: "stopped" as const,
  runtime_mode: "docker" as const,
  viewer_mode: "vnc" as const,
  vnc_ws_port: null,
  cdp_url: null,
};

beforeEach(() => {
  mockApi.listProfiles.mockResolvedValue([fakeProfile]);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("useProfiles", () => {
  it("starts with loading state", () => {
    const { result } = renderHook(() => useProfiles());
    expect(result.current.loading).toBe(true);
    expect(result.current.profiles).toEqual([]);
  });

  it("fetches profiles on mount", async () => {
    const { result } = renderHook(() => useProfiles());
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.profiles).toEqual([fakeProfile]);
    expect(mockApi.listProfiles).toHaveBeenCalled();
  });

  it("create prepends to list", async () => {
    const newProfile = { ...fakeProfile, id: "new-1", name: "New" };
    mockApi.createProfile.mockResolvedValue(newProfile);

    const { result } = renderHook(() => useProfiles());
    await waitFor(() => expect(result.current.loading).toBe(false));

    await act(async () => {
      await result.current.create({ name: "New" });
    });

    expect(result.current.profiles[0].id).toBe("new-1");
  });

  it("update replaces in list", async () => {
    const updated = { ...fakeProfile, name: "Renamed" };
    mockApi.updateProfile.mockResolvedValue(updated);

    const { result } = renderHook(() => useProfiles());
    await waitFor(() => expect(result.current.loading).toBe(false));

    await act(async () => {
      await result.current.update("abc-123", { name: "Renamed" });
    });

    expect(result.current.profiles[0].name).toBe("Renamed");
  });

  it("remove filters from list", async () => {
    mockApi.deleteProfile.mockResolvedValue({ ok: true });

    const { result } = renderHook(() => useProfiles());
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.profiles).toHaveLength(1);

    await act(async () => {
      await result.current.remove("abc-123");
    });

    expect(result.current.profiles).toHaveLength(0);
  });

  it("reorder optimistically updates order then persists", async () => {
    const p2 = { ...fakeProfile, id: "xyz-789", name: "Second" };
    mockApi.listProfiles.mockResolvedValue([fakeProfile, p2]);
    mockApi.reorderProfiles.mockResolvedValue({ ok: true });

    const { result } = renderHook(() => useProfiles());
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.profiles.map((p) => p.id)).toEqual(["abc-123", "xyz-789"]);

    await act(async () => {
      await result.current.reorder(["xyz-789", "abc-123"]);
    });

    expect(result.current.profiles.map((p) => p.id)).toEqual(["xyz-789", "abc-123"]);
    expect(mockApi.reorderProfiles).toHaveBeenCalledWith(["xyz-789", "abc-123"]);
  });

  it("reorder reverts (refreshes) on API failure", async () => {
    const p2 = { ...fakeProfile, id: "xyz-789", name: "Second" };
    // initial fetch + the refresh after failure both return the server order
    mockApi.listProfiles.mockResolvedValue([fakeProfile, p2]);
    mockApi.reorderProfiles.mockRejectedValue(new Error("boom"));

    const { result } = renderHook(() => useProfiles());
    await waitFor(() => expect(result.current.loading).toBe(false));

    await act(async () => {
      await result.current.reorder(["xyz-789", "abc-123"]);
    });

    // The failed persist triggers refresh(), which re-applies the server order.
    // (refresh clears the transient error, matching launch/stop behavior.)
    expect(mockApi.reorderProfiles).toHaveBeenCalled();
    expect(result.current.profiles.map((p) => p.id)).toEqual(["abc-123", "xyz-789"]);
  });

  it("sets error on fetch failure", async () => {
    mockApi.listProfiles.mockRejectedValue(new Error("Network error"));

    const { result } = renderHook(() => useProfiles());
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.error).toBe("Network error");
  });
});
