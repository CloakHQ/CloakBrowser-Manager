import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { useIdleCountdown, formatCountdown } from "./useIdleCountdown";

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("useIdleCountdown", () => {
  it("returns null when no idle timeout is reported", () => {
    const { result } = renderHook(() => useIdleCountdown(null));
    expect(result.current).toBeNull();
  });

  it("starts at the server value and ticks down once a second", () => {
    const { result } = renderHook(() => useIdleCountdown(120));
    expect(result.current).toBe(120);

    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(result.current).toBe(119);

    act(() => {
      vi.advanceTimersByTime(3000);
    });
    expect(result.current).toBe(116);
  });

  it("never ticks below zero", () => {
    const { result } = renderHook(() => useIdleCountdown(2));
    act(() => {
      vi.advanceTimersByTime(5000);
    });
    expect(result.current).toBe(0);
  });

  it("resyncs to a fresh server value instead of drifting from local ticking", () => {
    const { result, rerender } = renderHook(
      ({ remaining }: { remaining: number | null }) => useIdleCountdown(remaining),
      { initialProps: { remaining: 300 } },
    );
    act(() => {
      vi.advanceTimersByTime(4000);
    });
    expect(result.current).toBe(296);

    // A fresh 5s poll reports real activity reset the reaper's clock —
    // the local countdown must jump to match, not keep counting from 296.
    rerender({ remaining: 3600 });
    expect(result.current).toBe(3600);
  });

  it("resets to null once the profile stops reporting a timeout", () => {
    const { result, rerender } = renderHook(
      ({ remaining }: { remaining: number | null }) => useIdleCountdown(remaining),
      { initialProps: { remaining: 60 } as { remaining: number | null } },
    );
    expect(result.current).toBe(60);

    rerender({ remaining: null });
    expect(result.current).toBeNull();
  });

  it("stops ticking once display reaches null and does not error", () => {
    const { result, rerender, unmount } = renderHook(
      ({ remaining }: { remaining: number | null }) => useIdleCountdown(remaining),
      { initialProps: { remaining: 1 } as { remaining: number | null } },
    );
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(result.current).toBe(0);

    rerender({ remaining: null });
    expect(result.current).toBeNull();

    // No interval left running to throw on unmount or advance past this.
    act(() => {
      vi.advanceTimersByTime(5000);
    });
    unmount();
  });
});

describe("formatCountdown", () => {
  it("formats minutes and zero-padded seconds", () => {
    expect(formatCountdown(754)).toBe("12m34s");
  });

  it("formats under a minute with a 0 minute prefix", () => {
    expect(formatCountdown(45)).toBe("0m45s");
  });

  it("clamps negative input to zero", () => {
    expect(formatCountdown(-5)).toBe("0m00s");
  });

  it("rounds fractional seconds", () => {
    expect(formatCountdown(59.6)).toBe("1m00s");
  });
});
