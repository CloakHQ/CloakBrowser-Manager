import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, act, screen } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { ProfileViewer } from "./ProfileViewer";

vi.mock("../lib/api", () => {
  class ApiError extends Error {
    constructor(
      public status: number,
      message: string,
    ) {
      super(message);
      this.name = "ApiError";
    }
  }
  return {
    api: {
      createViewerToken: vi.fn(),
      profileStatus: vi.fn(),
    },
    ApiError,
  };
});

import { api } from "../lib/api";

const mockApi = api as {
  createViewerToken: ReturnType<typeof vi.fn>;
  profileStatus: ReturnType<typeof vi.fn>;
};

beforeEach(() => {
  mockApi.createViewerToken.mockReset();
  mockApi.profileStatus.mockReset();
  mockApi.createViewerToken.mockResolvedValue({
    token: "tok-1",
    viewer_url: "/viewer/tok-1/",
    expires_in: 300,
  });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("ProfileViewer", () => {
  it("renders the native client in an iframe with the path override", async () => {
    const { container } = render(
      <ProfileViewer
        profileId="p1"
        cdpUrl="/cdp/abc"
        clipboardSync={true}
        onSessionEnded={() => {}}
      />,
    );
    await act(async () => {});

    const iframe = container.querySelector("iframe");
    expect(iframe).toBeInTheDocument();
    const src = iframe!.getAttribute("src")!;
    expect(src.startsWith("/viewer/tok-1/?")).toBe(true);
    const params = new URLSearchParams(src.split("?")[1]);
    expect(params.get("path")).toBe("viewer/tok-1/websockify");
    expect(params.get("clipboard_up")).toBe("true");
    expect(iframe!.getAttribute("allow")).toContain("clipboard-write");

    expect(screen.getByText("Connecting...")).toBeInTheDocument();
    expect(screen.getByTitle("Copy CDP endpoint URL")).toBeInTheDocument();
  });

  it("shows the reconnecting overlay with attempt info after a disconnect", async () => {
    vi.useFakeTimers();
    mockApi.profileStatus.mockResolvedValue({
      status: "running",
      xvnc_alive: true,
      browser_alive: true,
    });
    const { container } = render(
      <ProfileViewer
        profileId="p1"
        cdpUrl={null}
        clipboardSync={false}
        onSessionEnded={() => {}}
      />,
    );
    await act(async () => {});

    const iframe = container.querySelector("iframe")!;

    const send = (value: string) => {
      const ev = new Event("message");
      Object.defineProperty(ev, "data", {
        value: { action: "connection_state", value },
      });
      Object.defineProperty(ev, "source", { value: iframe.contentWindow });
      window.dispatchEvent(ev);
    };

    act(() => send("connected"));
    expect(screen.getByText("Connected")).toBeInTheDocument();

    act(() => send("disconnected"));
    expect(screen.getByText("Connection lost — reconnecting")).toBeInTheDocument();
    expect(screen.getByText(/Attempt 1/)).toBeInTheDocument();
    // iframe stays mounted (last frame preserved under the dim overlay)
    expect(container.querySelector("iframe")).toBeInTheDocument();
  });

  it("offers Reconnect now once the reconnect loop has been degraded for 60s", async () => {
    // The reconnecting overlay covers the frame and swallows pointer input, so
    // this button is the ONLY manual escape from a non-terminal reconnect
    // loop. The hook's `degraded` flag is tested at the hook level; what is
    // tested here is that the component actually renders the escape hatch it
    // gates, and that pressing it mints a new token instead of just clearing
    // the overlay.
    vi.useFakeTimers();
    mockApi.profileStatus.mockReturnValue(new Promise(() => {})); // never settles
    const { container } = render(
      <ProfileViewer
        profileId="p1"
        cdpUrl={null}
        clipboardSync={false}
        onSessionEnded={() => {}}
      />,
    );
    await act(async () => {});

    const iframe = container.querySelector("iframe")!;
    const send = (value: string) => {
      const ev = new Event("message");
      Object.defineProperty(ev, "data", {
        value: { action: "connection_state", value },
      });
      Object.defineProperty(ev, "source", { value: iframe.contentWindow });
      window.dispatchEvent(ev);
    };

    act(() => send("connected"));
    act(() => send("disconnected"));
    expect(screen.queryByText("Reconnect now")).not.toBeInTheDocument();

    await act(async () => {
      vi.advanceTimersByTime(60_000);
    });
    expect(screen.getByText("Still trying…")).toBeInTheDocument();
    const button = screen.getByText("Reconnect now");

    // The stuck probe is what left the user here; let the replacement one
    // answer so the click has an observable end-to-end effect.
    const tokensBefore = mockApi.createViewerToken.mock.calls.length;
    mockApi.profileStatus.mockResolvedValue({
      status: "running",
      xvnc_alive: true,
      browser_alive: true,
    });
    await act(async () => {
      button.click();
    });
    await act(async () => {});
    expect(mockApi.createViewerToken.mock.calls.length).toBe(tokensBefore + 1);
  });

  it("swallows wheel events over the display surface (issue #186)", async () => {
    // The iframe consumes wheel over the canvas, but the surrounding surface
    // does not: without a non-passive preventDefault a wheel gesture near the
    // edges scrolls the page behind the viewer and, on horizontal wheels,
    // triggers browser back-navigation out of the session entirely.
    const { container } = render(
      <ProfileViewer
        profileId="p1"
        cdpUrl={null}
        clipboardSync={false}
        onSessionEnded={() => {}}
      />,
    );
    await act(async () => {});

    const surface = container.querySelector("iframe")!.parentElement!;
    const event = new WheelEvent("wheel", {
      cancelable: true,
      bubbles: true,
      deltaX: -120,
    });
    act(() => {
      surface.dispatchEvent(event);
    });
    expect(event.defaultPrevented).toBe(true);
  });

  it("shows the session-ended overlay and wires the back button", async () => {
    vi.useFakeTimers();
    mockApi.profileStatus.mockResolvedValue({
      status: "stopped",
      xvnc_alive: null,
      browser_alive: null,
    });
    const onSessionEnded = vi.fn();
    const { container } = render(
      <ProfileViewer
        profileId="p1"
        cdpUrl={null}
        clipboardSync={false}
        onSessionEnded={onSessionEnded}
      />,
    );
    await act(async () => {});

    const iframe = container.querySelector("iframe")!;
    const send = (value: string) => {
      const ev = new Event("message");
      Object.defineProperty(ev, "data", {
        value: { action: "connection_state", value },
      });
      Object.defineProperty(ev, "source", { value: iframe.contentWindow });
      window.dispatchEvent(ev);
    };

    act(() => send("connected"));
    act(() => send("disconnected"));
    await act(async () => {
      vi.advanceTimersByTime(250);
    });
    await act(async () => {});

    expect(screen.getByText("Browser session ended")).toBeInTheDocument();
    act(() => {
      screen.getByText("Back to profile").click();
    });
    expect(onSessionEnded).toHaveBeenCalledTimes(1);
  });
});
