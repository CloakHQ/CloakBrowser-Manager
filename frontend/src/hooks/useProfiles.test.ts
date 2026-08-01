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
  clipboard_sync: true,
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

  it("update touches only the profile it names", async () => {
    // The map runs over every row; without the id guard an update would
    // overwrite every other profile in the sidebar with this one.
    const other = { ...fakeProfile, id: "other-1", name: "Other" };
    mockApi.listProfiles.mockResolvedValue([fakeProfile, other]);
    mockApi.updateProfile.mockResolvedValue({ ...fakeProfile, name: "Renamed" });

    const { result } = renderHook(() => useProfiles());
    await waitFor(() => expect(result.current.loading).toBe(false));

    await act(async () => {
      await result.current.update("abc-123", { name: "Renamed" });
    });

    expect(result.current.profiles.map((p) => p.name)).toEqual(["Renamed", "Other"]);
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

  it("clears a stale error once a poll succeeds", async () => {
    // The banner is driven straight off this value, so an error that outlives
    // the condition sits over a working app until the page is reloaded.
    mockApi.listProfiles.mockRejectedValueOnce(new Error("Network error"));

    const { result } = renderHook(() => useProfiles());
    await waitFor(() => expect(result.current.error).toBe("Network error"));

    await act(async () => {
      await result.current.refresh();
    });
    expect(result.current.error).toBeNull();
    expect(result.current.profiles).toEqual([fakeProfile]);
  });
});

// ── failure paths ───────────────────────────────────────────────────────────
//
// Every mutation here is a write the user asked for that silently did not
// happen: the hook swallows the rejection, so the ONLY thing that tells them
// is `error`. These also pin that a failed write leaves the cached list alone
// rather than optimistically showing a change the server rejected.

describe("useProfiles failure paths", () => {
  async function mounted() {
    const view = renderHook(() => useProfiles());
    await waitFor(() => expect(view.result.current.loading).toBe(false));
    return view;
  }

  it("reports a failed create and does not add anything to the list", async () => {
    mockApi.createProfile.mockRejectedValue(new Error("name taken"));
    const { result } = await mounted();

    let returned: unknown = "unset";
    await act(async () => {
      returned = await result.current.create({ name: "New" });
    });

    expect(returned).toBeUndefined();
    expect(result.current.error).toBe("name taken");
    expect(result.current.profiles).toEqual([fakeProfile]);
  });

  it("reports a failed update and leaves the cached profile untouched", async () => {
    mockApi.updateProfile.mockRejectedValue(new Error("conflict"));
    const { result } = await mounted();

    await act(async () => {
      await result.current.update("abc-123", { name: "Renamed" });
    });

    expect(result.current.error).toBe("conflict");
    expect(result.current.profiles[0].name).toBe("Test");
  });

  it("reports a failed delete and keeps the profile in the list", async () => {
    // The 409 the manager raises for a wedged teardown lands here. Dropping
    // the row anyway would tell the user a profile is gone while its Chromium
    // is still writing to user_data_dir.
    mockApi.deleteProfile.mockRejectedValue(new Error("Profile is shutting down"));
    const { result } = await mounted();

    await act(async () => {
      await result.current.remove("abc-123");
    });

    expect(result.current.error).toBe("Profile is shutting down");
    expect(result.current.profiles).toHaveLength(1);
  });

  it("returns the launch result and refreshes so the status catches up", async () => {
    // The result is what App.handleLaunch routes on, and the refresh is what
    // flips the row out of "stopped" without waiting up to 3s for the poll.
    const launched = { vnc_ws_port: 6100, display: ":100", cdp_url: "/cdp/abc" };
    mockApi.launchProfile.mockResolvedValue(launched);
    const { result } = await mounted();
    const before = mockApi.listProfiles.mock.calls.length;

    let returned: unknown;
    await act(async () => {
      returned = await result.current.launch("abc-123");
    });

    expect(returned).toEqual(launched);
    expect(mockApi.listProfiles.mock.calls.length).toBeGreaterThan(before);
    expect(result.current.error).toBeNull();
  });

  it("reports a failed launch and returns nothing to route on", async () => {
    // App.handleLaunch switches to the viewer on a truthy result, so this
    // must be undefined or a failed launch opens a viewer for a dead session.
    mockApi.launchProfile.mockRejectedValue(new Error("Profile is already running"));
    const { result } = await mounted();

    let returned: unknown = "unset";
    await act(async () => {
      returned = await result.current.launch("abc-123");
    });

    expect(returned).toBeUndefined();
    expect(result.current.error).toBe("Profile is already running");
  });

  it("refreshes after a successful stop", async () => {
    mockApi.stopProfile.mockResolvedValue({ ok: true });
    const { result } = await mounted();
    const before = mockApi.listProfiles.mock.calls.length;

    await act(async () => {
      await result.current.stop("abc-123");
    });

    expect(mockApi.stopProfile).toHaveBeenCalledWith("abc-123");
    expect(mockApi.listProfiles.mock.calls.length).toBeGreaterThan(before);
    expect(result.current.error).toBeNull();
  });

  it("reports a failed stop", async () => {
    mockApi.stopProfile.mockRejectedValue(new Error("teardown wedged"));
    const { result } = await mounted();

    await act(async () => {
      await result.current.stop("abc-123");
    });

    expect(result.current.error).toBe("teardown wedged");
  });

  it("falls back to a readable message when the rejection is not an Error", async () => {
    // fetch rejects with an Error, but a non-Error can reach here from a
    // thrown string or a rejected promise with no reason — and `String(err)`
    // on those renders as "undefined"/"[object Object]" in the banner.
    const { result } = await mounted();

    mockApi.createProfile.mockRejectedValue("boom");
    await act(async () => {
      await result.current.create({ name: "New" });
    });
    expect(result.current.error).toBe("Failed to create profile");

    mockApi.updateProfile.mockRejectedValue({ code: 500 });
    await act(async () => {
      await result.current.update("abc-123", { name: "x" });
    });
    expect(result.current.error).toBe("Failed to update profile");

    mockApi.deleteProfile.mockRejectedValue(undefined);
    await act(async () => {
      await result.current.remove("abc-123");
    });
    expect(result.current.error).toBe("Failed to delete profile");

    mockApi.launchProfile.mockRejectedValue(null);
    await act(async () => {
      await result.current.launch("abc-123");
    });
    expect(result.current.error).toBe("Failed to launch profile");

    mockApi.stopProfile.mockRejectedValue("nope");
    await act(async () => {
      await result.current.stop("abc-123");
    });
    expect(result.current.error).toBe("Failed to stop profile");

    mockApi.listProfiles.mockRejectedValue("gone");
    await act(async () => {
      await result.current.refresh();
    });
    expect(result.current.error).toBe("Failed to fetch profiles");
  });
});
