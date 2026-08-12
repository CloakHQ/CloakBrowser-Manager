import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  api, ApiError, setOnUnauthorized, PROFILE_LIFECYCLES,
  downloadFileUrl, downloadsZipUrl,
} from "./api";

// Mock fetch globally
const mockFetch = vi.fn();
vi.stubGlobal("fetch", mockFetch);

function jsonResponse(data: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? "OK" : "Error",
    json: () => Promise.resolve(data),
  };
}

beforeEach(() => {
  mockFetch.mockReset();
});

// ── listProfiles ────────────────────────────────────────────────────────────

describe("api.listProfiles", () => {
  it("returns profile array on success", async () => {
    const profiles = [{ id: "1", name: "Test" }];
    mockFetch.mockResolvedValueOnce(jsonResponse(profiles));
    const result = await api.listProfiles();
    expect(result).toEqual(profiles);
    expect(mockFetch).toHaveBeenCalledWith(
      "/api/profiles",
      expect.objectContaining({ headers: { "Content-Type": "application/json" } }),
    );
  });

  it("gives lifecycle mutations a budget larger than the backend's own", async () => {
    // The backend allows a launch 60s (wait_for in auto_launch_all) and Xvnc
    // readiness alone is 15s. Sharing the short poll budget aborts a launch
    // the server goes on to complete, so the UI reports a failure for a
    // success and the next click 409s.
    const deadlines: number[] = [];
    const realTimeout = AbortSignal.timeout.bind(AbortSignal);
    const spy = vi.spyOn(AbortSignal, "timeout").mockImplementation((ms: number) => {
      deadlines.push(ms);
      return realTimeout(600_000);
    });
    try {
      mockFetch.mockResolvedValueOnce(jsonResponse({ profile_id: "p1" }));
      await api.launchProfile("p1");
      mockFetch.mockResolvedValueOnce(jsonResponse({ ok: true }));
      await api.stopProfile("p1");
      mockFetch.mockResolvedValueOnce(jsonResponse({ ok: true }));
      await api.deleteProfile("p1");
      mockFetch.mockResolvedValueOnce(jsonResponse([]));
      await api.listProfiles();
    } finally {
      spy.mockRestore();
    }
    const [launch, stop, del, list] = deadlines;
    for (const ms of [launch, stop, del]) expect(ms).toBeGreaterThanOrEqual(60_000);
    expect(list).toBeLessThanOrEqual(15_000); // recovery-path reads stay tight
  });

});

// ── request bounding ────────────────────────────────────────────────────────

/**
 * Every exported api method with arguments good enough to reach fetch(). Typed
 * as a Record over `keyof typeof api` so a new endpoint is a compile error here
 * — the previous version of the bounding test below exercised `listProfiles`
 * alone, which meant `createViewerToken` and `profileStatus` (the two calls the
 * reconnect machine actually serialises on) could be made unbounded with the
 * whole suite green.
 */
const CALL_ARGS: Record<keyof typeof api, unknown[]> = {
  authStatus: [],
  login: ["tok"],
  logout: [],
  listProfiles: [],
  getProfile: ["p1"],
  createProfile: [{ name: "New" }],
  updateProfile: ["p1", { name: "New" }],
  duplicateProfile: ["p1"],
  deleteProfile: ["p1"],
  launchProfile: ["p1"],
  stopProfile: ["p1"],
  getStatus: [],
  systemCheck: [],
  listExtensions: [],
  rescanExtensions: [],
  uploadExtension: [new File(["dummy"], "test.zip")],
  installExtensionFromUrl: ["https://chromewebstore.google.com/detail/x/edibdbjcniadpccecjdfdjjppcpchdlm"],
  setClipboard: ["p1", "hello"],
  getClipboard: ["p1"],
  createViewerToken: ["p1"],
  profileStatus: ["p1"],
  profileResources: ["p1"],
  extendIdleTimeout: ["p1", 900],
  listDownloads: ["p1"],
  deleteDownload: ["p1", "/file.txt"],
  viewerAttached: ["p1"],
  listTabs: ["p1"],
  closeTab: ["p1", 0],
  startCookieWarmup: ["p1"],
  cookieWarmupStatus: ["p1"],
  stopCookieWarmup: ["p1"],
};

describe("request bounding", () => {
  it("covers every exported api method", () => {
    // The Record type catches an endpoint added to api.ts; this catches one
    // deleted from api.ts but left in the table (which would silently stop
    // testing nothing at all).
    expect(Object.keys(CALL_ARGS).sort()).toEqual(Object.keys(api).sort());
  });

  it.each(Object.keys(CALL_ARGS) as (keyof typeof api)[])(
    "api.%s bounds its request so a stalled connection cannot hang forever",
    async (name) => {
      // No browser applies a default fetch timeout, and `request()` builds its
      // init as `{ signal: timeoutSignal(...), ...options }` — so any method
      // whose own options carry a `signal` key silently wins the spread and
      // becomes unbounded. The viewer's reconnect machine awaits
      // createViewerToken and profileStatus inside its in-flight guard, so one
      // unbounded call turns "the server stopped answering" into "recovery
      // stops" for the OS retransmit window (minutes).
      mockFetch.mockResolvedValueOnce(jsonResponse({}));
      await (api[name] as (...args: unknown[]) => Promise<unknown>)(...CALL_ARGS[name]);
      const init = mockFetch.mock.calls[0][1] as RequestInit;
      expect(init.signal).toBeInstanceOf(AbortSignal);
      expect(init.signal!.aborted).toBe(false);
    },
  );
});

// ── standalone URL builders ──────────────────────────────────────────────────
// downloadFileUrl/downloadsZipUrl are plain functions, not `api` members (see
// their own comments in api.ts for why), so they're outside the exhaustiveness
// loop above and need their own direct coverage — components that use them
// mock the whole module, which never executes the real bodies here.

describe("downloadFileUrl", () => {
  it("builds a path under the profile's downloads route", () => {
    expect(downloadFileUrl("p1", "/report.pdf")).toBe("/api/profiles/p1/downloads/report.pdf");
  });
});

describe("downloadsZipUrl", () => {
  it("builds the bulk-zip route for the profile", () => {
    expect(downloadsZipUrl("p1")).toBe("/api/profiles/p1/downloads-zip");
  });
});

// ── lifecycle union ─────────────────────────────────────────────────────────

describe("ProfileLifecycle", () => {
  it("pins the lifecycle set to exactly the four backend states", () => {
    // Changing this list is a tsc error in all three Record<ProfileLifecycle,…>
    // consumers (App's VIEW_ON_SELECT, LaunchButton's BUSY_LABEL,
    // StatusIndicator's DOT_CLASS) AND a failure here, so a new backend state
    // cannot land as a silent fall-through the way "stopping" would have:
    // an enabled "Launch" that 409s, a grey "stopped" dot, and the viewer
    // opening on a profile whose /viewer-token 404s.
    expect([...PROFILE_LIFECYCLES]).toEqual([
      "running", "starting", "stopping", "stopped",
    ]);
  });
});

// ── createProfile ───────────────────────────────────────────────────────────

describe("api.createProfile", () => {
  it("sends POST with JSON body", async () => {
    const profile = { id: "2", name: "New" };
    mockFetch.mockResolvedValueOnce(jsonResponse(profile));
    await api.createProfile({ name: "New" });
    const [url, options] = mockFetch.mock.calls[0];
    expect(url).toBe("/api/profiles");
    expect(options.method).toBe("POST");
    expect(JSON.parse(options.body)).toEqual({ name: "New" });
  });
});

// ── updateProfile ───────────────────────────────────────────────────────────

describe("api.updateProfile", () => {
  it("sends PUT with JSON body", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse({ id: "1", name: "Updated" }));
    await api.updateProfile("1", { name: "Updated" });
    const [url, options] = mockFetch.mock.calls[0];
    expect(url).toBe("/api/profiles/1");
    expect(options.method).toBe("PUT");
  });
});

// ── deleteProfile ───────────────────────────────────────────────────────────

describe("api.deleteProfile", () => {
  it("sends DELETE request", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse({ ok: true }));
    const result = await api.deleteProfile("1");
    expect(result).toEqual({ ok: true });
    const [url, options] = mockFetch.mock.calls[0];
    expect(url).toBe("/api/profiles/1");
    expect(options.method).toBe("DELETE");
  });
});

// ── launchProfile ───────────────────────────────────────────────────────────

describe("api.launchProfile", () => {
  it("sends POST to launch endpoint", async () => {
    const result = { profile_id: "1", status: "running", vnc_ws_port: 6100, display: ":100" };
    mockFetch.mockResolvedValueOnce(jsonResponse(result));
    const data = await api.launchProfile("1");
    expect(data.vnc_ws_port).toBe(6100);
    expect(mockFetch.mock.calls[0][0]).toBe("/api/profiles/1/launch");
  });
});

// ── stopProfile ─────────────────────────────────────────────────────────────

describe("api.stopProfile", () => {
  it("sends POST to stop endpoint", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse({ ok: true }));
    await api.stopProfile("1");
    expect(mockFetch.mock.calls[0][0]).toBe("/api/profiles/1/stop");
  });
});

// ── setClipboard ────────────────────────────────────────────────────────────

describe("api.setClipboard", () => {
  it("sends POST with text body", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse({ ok: true }));
    await api.setClipboard("1", "hello");
    const [url, options] = mockFetch.mock.calls[0];
    expect(url).toBe("/api/profiles/1/clipboard");
    expect(options.method).toBe("POST");
    expect(JSON.parse(options.body)).toEqual({ text: "hello" });
  });
});

// ── getClipboard ────────────────────────────────────────────────────────────

describe("api.getClipboard", () => {
  it("returns clipboard text", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse({ text: "copied" }));
    const result = await api.getClipboard("1");
    expect(result.text).toBe("copied");
  });
});

// ── listTabs / closeTab ─────────────────────────────────────────────────────

describe("api.listTabs", () => {
  it("returns the tab list for a profile", async () => {
    const tabs = [{ index: 0, title: "Example", url: "https://example.com/", favicon: null }];
    mockFetch.mockResolvedValueOnce(jsonResponse({ tabs }));
    const result = await api.listTabs("1");
    expect(result.tabs).toEqual(tabs);
    expect(mockFetch.mock.calls[0][0]).toBe("/api/profiles/1/tabs");
  });
});

describe("api.closeTab", () => {
  it("sends DELETE to the indexed tab route", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse({ ok: true }));
    await api.closeTab("1", 2);
    const [url, options] = mockFetch.mock.calls[0];
    expect(url).toBe("/api/profiles/1/tabs/2");
    expect(options.method).toBe("DELETE");
  });
});

// ── Error handling ──────────────────────────────────────────────────────────

describe("error handling", () => {
  it("throws ApiError with detail on non-ok response", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 404,
      statusText: "Not Found",
      json: () => Promise.resolve({ detail: "Profile not found" }),
    });
    await expect(api.getProfile("bad")).rejects.toThrow("Profile not found");
  });

  it("falls back to statusText when response is not JSON", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 500,
      statusText: "Internal Server Error",
      json: () => Promise.reject(new Error("not json")),
    });
    await expect(api.getStatus()).rejects.toThrow("Internal Server Error");
  });

  it("routes a 401 to the global unauthorized handler as well as the caller", async () => {
    // Without this wiring an expired AUTH_TOKEN cookie leaves the user on a
    // dead dashboard: the ApiError still reaches the caller (isAuthError in
    // useViewerSession matches on status alone), so nothing throws — App just
    // never learns to swap in the LoginPage and every poll 401s forever.
    const onUnauthorized = vi.fn();
    setOnUnauthorized(onUnauthorized);
    try {
      mockFetch.mockResolvedValueOnce({
        ok: false,
        status: 401,
        statusText: "Unauthorized",
        json: () => Promise.resolve({ detail: "Unauthorized" }),
      });
      const err = await api.profileStatus("p1").catch((e) => e);
      expect(err).toBeInstanceOf(ApiError);
      expect(err.status).toBe(401);
      expect(onUnauthorized).toHaveBeenCalledTimes(1);
    } finally {
      setOnUnauthorized(null);
    }
  });

  it("still rejects with ApiError(401) when no unauthorized handler is registered", async () => {
    // The handler is null between App mounts (and in every other consumer of
    // api.ts); the 401 must not become a different error class there.
    setOnUnauthorized(null);
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 401,
      statusText: "Unauthorized",
      json: () => Promise.resolve({ detail: "Unauthorized" }),
    });
    const err = await api.profileStatus("p1").catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(401);
  });
});
