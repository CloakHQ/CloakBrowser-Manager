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
  color_scheme: null,
  notes: null,
  user_data_dir: "/data/profiles/abc-123",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  tags: [],
  status: "stopped" as const,
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

  it("sets error on fetch failure", async () => {
    mockApi.listProfiles.mockRejectedValue(new Error("Network error"));

    const { result } = renderHook(() => useProfiles());
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.error).toBe("Network error");
  });
});
