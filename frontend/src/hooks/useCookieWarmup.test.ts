import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { useCookieWarmup } from "./useCookieWarmup";

vi.mock("../lib/api", () => ({
  api: {
    cookieWarmupStatus: vi.fn(),
    startCookieWarmup: vi.fn(),
    stopCookieWarmup: vi.fn(),
  },
}));

import { api } from "../lib/api";

const mockApi = api as unknown as {
  cookieWarmupStatus: ReturnType<typeof vi.fn>;
  startCookieWarmup: ReturnType<typeof vi.fn>;
  stopCookieWarmup: ReturnType<typeof vi.fn>;
};

function status(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    state: "idle",
    sites_total: 20,
    sites_visited: 0,
    current_site: null,
    elapsed_seconds: null,
    remaining_seconds: null,
    error: null,
    ...overrides,
  };
}

// Real-timer-based `waitFor` from testing-library never fires under fake
// timers (its own retry loop is scheduled on the very timers we've faked),
// so every wait here is an explicit fake-timer flush instead.
async function flush() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  // resetAllMocks, not clearAllMocks: a mockResolvedValueOnce queue left over
  // from a previous test (this file's mocks are module-level, not per-test)
  // would otherwise silently answer this test's first call too.
  vi.resetAllMocks();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("useCookieWarmup", () => {
  it("does not fetch without a profile id", () => {
    renderHook(() => useCookieWarmup(null));
    expect(mockApi.cookieWarmupStatus).not.toHaveBeenCalled();
  });

  it("fetches once on mount and does not reschedule an idle status", async () => {
    mockApi.cookieWarmupStatus.mockResolvedValue(status({ state: "idle" }));
    const { result } = renderHook(() => useCookieWarmup("p1"));
    await flush();
    expect(result.current.status?.state).toBe("idle");
    expect(mockApi.cookieWarmupStatus).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(mockApi.cookieWarmupStatus).toHaveBeenCalledTimes(1);
  });

  it("keeps polling while the state is running, and stops once it finishes", async () => {
    mockApi.cookieWarmupStatus
      .mockResolvedValueOnce(status({ state: "running", sites_visited: 1 }))
      .mockResolvedValueOnce(status({ state: "running", sites_visited: 2 }))
      .mockResolvedValueOnce(status({ state: "done", sites_visited: 20 }));

    const { result } = renderHook(() => useCookieWarmup("p1"));
    await flush();
    expect(result.current.status?.sites_visited).toBe(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    expect(result.current.status?.sites_visited).toBe(2);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    expect(result.current.status?.state).toBe("done");
    expect(mockApi.cookieWarmupStatus).toHaveBeenCalledTimes(3);

    // No further polling once terminal.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(mockApi.cookieWarmupStatus).toHaveBeenCalledTimes(3);
  });

  it("resets status when the profile id changes", async () => {
    mockApi.cookieWarmupStatus.mockResolvedValue(status({ state: "done" }));
    const { result, rerender } = renderHook(
      ({ id }: { id: string | null }) => useCookieWarmup(id),
      { initialProps: { id: "p1" } },
    );
    await flush();
    expect(result.current.status?.state).toBe("done");

    rerender({ id: "p2" });
    expect(result.current.status).toBeNull();
  });

  it("start() calls the API, applies the response, and schedules a follow-up poll", async () => {
    mockApi.cookieWarmupStatus.mockResolvedValue(status({ state: "idle" }));
    mockApi.startCookieWarmup.mockResolvedValue(status({ state: "idle" }));
    const { result } = renderHook(() => useCookieWarmup("p1"));
    await flush();
    mockApi.cookieWarmupStatus.mockClear();

    mockApi.cookieWarmupStatus.mockResolvedValue(status({ state: "running", sites_visited: 1 }));
    await act(async () => {
      await result.current.start();
    });
    expect(mockApi.startCookieWarmup).toHaveBeenCalledWith("p1");
    expect(result.current.busy).toBe(false);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    expect(mockApi.cookieWarmupStatus).toHaveBeenCalledTimes(1);
    expect(result.current.status?.state).toBe("running");
  });

  it("stop() calls the API, applies the response, and cancels further polling", async () => {
    mockApi.cookieWarmupStatus.mockResolvedValue(status({ state: "running", sites_visited: 5 }));
    const { result } = renderHook(() => useCookieWarmup("p1"));
    await flush();
    expect(result.current.status?.state).toBe("running");

    mockApi.stopCookieWarmup.mockResolvedValue(status({ state: "cancelled", sites_visited: 5 }));
    await act(async () => {
      await result.current.stop();
    });
    expect(mockApi.stopCookieWarmup).toHaveBeenCalledWith("p1");
    expect(result.current.status?.state).toBe("cancelled");

    mockApi.cookieWarmupStatus.mockClear();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(mockApi.cookieWarmupStatus).not.toHaveBeenCalled();
  });
});
