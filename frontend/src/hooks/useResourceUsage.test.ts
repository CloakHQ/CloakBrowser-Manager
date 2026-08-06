import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { useResourceUsage } from "./useResourceUsage";

vi.mock("../lib/api", () => ({
  api: {
    profileResources: vi.fn(),
  },
}));

import { api } from "../lib/api";

const mockApi = api as unknown as { profileResources: ReturnType<typeof vi.fn> };

beforeEach(() => {
  mockApi.profileResources.mockResolvedValue({
    cpu_percent: 12.3, memory_mb: 256, process_count: 5,
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("useResourceUsage", () => {
  it("does not poll when the profile is not running", () => {
    renderHook(() => useResourceUsage("p1", false));
    expect(mockApi.profileResources).not.toHaveBeenCalled();
  });

  it("does not poll without a profile id", () => {
    renderHook(() => useResourceUsage(null, true));
    expect(mockApi.profileResources).not.toHaveBeenCalled();
  });

  it("polls and returns usage for a running profile", async () => {
    const { result } = renderHook(() => useResourceUsage("p1", true));
    await waitFor(() => expect(result.current).not.toBeNull());
    expect(result.current).toEqual({ cpu_percent: 12.3, memory_mb: 256, process_count: 5 });
    expect(mockApi.profileResources).toHaveBeenCalledWith("p1");
  });

  it("resets to null once the profile stops running", async () => {
    const { result, rerender } = renderHook(
      ({ id, running }: { id: string | null; running: boolean }) => useResourceUsage(id, running),
      { initialProps: { id: "p1", running: true } },
    );
    await waitFor(() => expect(result.current).not.toBeNull());

    rerender({ id: "p1", running: false });
    expect(result.current).toBeNull();
  });
});
