import { describe, it, expect, vi } from "vitest";
import { render, screen, act } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { LaunchButton } from "./LaunchButton";
import type { ProfileLifecycle } from "../lib/api";

function renderButton(status: ProfileLifecycle) {
  const onLaunch = vi.fn().mockResolvedValue(undefined);
  const onStop = vi.fn().mockResolvedValue(undefined);
  render(<LaunchButton status={status} onLaunch={onLaunch} onStop={onStop} />);
  return { onLaunch, onStop, button: screen.getByRole("button") };
}

describe("LaunchButton", () => {
  it("offers Launch for a stopped profile", async () => {
    const { onLaunch, button } = renderButton("stopped");
    expect(button).toHaveTextContent("Launch");
    expect(button).not.toBeDisabled();
    await act(async () => button.click());
    expect(onLaunch).toHaveBeenCalledTimes(1);
  });

  it("offers Stop for a running profile", async () => {
    const { onStop, button } = renderButton("running");
    expect(button).toHaveTextContent("Stop");
    expect(button).not.toBeDisabled();
    await act(async () => button.click());
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it("offers nothing but a spinner while the profile is starting", () => {
    const { button } = renderButton("starting");
    expect(button).toBeDisabled();
    expect(button).toHaveTextContent("Launching...");
  });

  it("offers nothing but a spinner while the profile is stopping", async () => {
    // A "stopping" profile refuses BOTH launch and stop with 409 until
    // teardown completes, so an enabled button of either kind is a guaranteed
    // error toast. Before the Record-driven busy branch this state fell
    // straight through to an enabled "Launch".
    const { onLaunch, onStop, button } = renderButton("stopping");
    expect(button).toBeDisabled();
    expect(button).toHaveTextContent("Shutting down...");
    await act(async () => button.click());
    expect(onLaunch).not.toHaveBeenCalled();
    expect(onStop).not.toHaveBeenCalled();
  });
});

describe("in-flight action vs the 3s status poll", () => {
  it("keeps saying Stopping when the poll flips running -> stopping mid-request", async () => {
    // The poll is up to 3s stale and flips to `stopping` while the stop POST is
    // still on the wire. Deriving the busy label from the polled status then
    // rendered "Launching..." over a teardown — the exact dot/button
    // contradiction the `stopping` state was introduced to remove.
    let resolveStop: () => void = () => {};
    const onStop = vi.fn(() => new Promise<void>((r) => { resolveStop = r; }));
    const onLaunch = vi.fn().mockResolvedValue(undefined);

    const { rerender } = render(
      <LaunchButton status="running" onLaunch={onLaunch} onStop={onStop} />,
    );
    await act(async () => screen.getByRole("button").click());
    expect(onStop).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button")).toHaveTextContent("Stopping...");

    // the poll lands mid-request
    rerender(<LaunchButton status="stopping" onLaunch={onLaunch} onStop={onStop} />);
    expect(screen.getByRole("button")).toHaveTextContent("Stopping...");
    expect(screen.getByRole("button")).not.toHaveTextContent("Launching");

    await act(async () => { resolveStop(); });
    // once our action settles, the polled state takes over
    expect(screen.getByRole("button")).toHaveTextContent("Shutting down...");
    expect(onLaunch).not.toHaveBeenCalled();
  });
});
