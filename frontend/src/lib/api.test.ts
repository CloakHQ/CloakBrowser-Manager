import { describe, it, expect, vi, beforeEach } from "vitest";
import { api } from "./api";

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

  it("bounds every request so a stalled connection cannot hang forever", async () => {
    // The viewer's reconnect machine serialises on these calls; an unbounded
    // fetch turns "the server stopped answering" into "recovery stops".
    mockFetch.mockResolvedValueOnce(jsonResponse([]));
    await api.listProfiles();
    const init = mockFetch.mock.calls[0][1] as RequestInit;
    expect(init.signal).toBeInstanceOf(AbortSignal);
    expect(init.signal!.aborted).toBe(false);
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
});
