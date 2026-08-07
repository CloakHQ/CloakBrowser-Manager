import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, act, screen } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { ProfileViewer, pickFramebufferCanvas, captureLastFrame } from "./ProfileViewer";

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
      viewerAttached: vi.fn(),
    },
    ApiError,
  };
});

import { api } from "../lib/api";

const mockApi = api as {
  createViewerToken: ReturnType<typeof vi.fn>;
  profileStatus: ReturnType<typeof vi.fn>;
  viewerAttached: ReturnType<typeof vi.fn>;
};

beforeEach(() => {
  mockApi.createViewerToken.mockReset();
  mockApi.profileStatus.mockReset();
  mockApi.viewerAttached.mockReset();
  mockApi.viewerAttached.mockResolvedValue({ viewer_attached: true, clients: 1 });
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
    // iframe stays mounted: a remount would drop the session the client is
    // trying to recover in place.
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

// ── last-frame preservation ─────────────────────────────────────────────────
//
// The client destroys its own framebuffer canvas on disconnect, so the frame
// has to be copied out while the connection is still up. These cover the two
// halves: finding the right canvas among the six the client builds, and
// copying it without ever reading pixels back.

/** A canvas in `doc` shaped like one of the client's. */
function addCanvas(
  doc: Document,
  { w, h, attach = true, style = {} }:
    { w: number; h: number; attach?: boolean; style?: Partial<CSSStyleDeclaration> },
) {
  const c = doc.createElement("canvas");
  c.width = w;
  c.height = h;
  Object.assign(c.style, style);
  if (attach) doc.body.appendChild(c);
  return c;
}

/**
 * A stand-in for the client's document.
 *
 * Must be a real iframe document, not document.implementation.createHTMLDocument:
 * that has no browsing context, so `defaultView` is null and getComputedStyle
 * is unreachable — the visibility filter would silently never run and these
 * tests would pass on the size comparison alone.
 */
function ensureBody(doc: Document): HTMLElement {
  // An iframe pointed at a URL jsdom will not load has a completely empty
  // document — no documentElement, let alone a body.
  if (!doc.documentElement) doc.appendChild(doc.createElement("html"));
  if (!doc.body) doc.documentElement.appendChild(doc.createElement("body"));
  return doc.body;
}

const clientFrames: HTMLIFrameElement[] = [];
function clientDoc(): Document {
  const frame = document.createElement("iframe");
  document.body.appendChild(frame);
  clientFrames.push(frame);
  const doc = frame.contentDocument!;
  expect(doc.defaultView).not.toBeNull();
  return doc;
}

afterEach(() => {
  clientFrames.splice(0).forEach((f) => f.remove());
});

describe("pickFramebufferCanvas", () => {
  it("picks the screen out of the canvases the client builds", () => {
    const doc = clientDoc();
    // the real set, per ui-BOjwDkC7.js. The two detached ones are the reason
    // the search must go through the document rather than a kept reference:
    // querySelectorAll never sees them, which is what excludes them.
    addCanvas(doc, { w: 4096, h: 4096, attach: false });            // backbuffer
    addCanvas(doc, { w: 1920, h: 1080, attach: false });            // WebGL surface
    addCanvas(doc, { w: 32, h: 32, style: { visibility: "hidden" } }); // cursor
    addCanvas(doc, { w: 0, h: 0 });                                  // watermark
    const screenCanvas = addCanvas(doc, { w: 1280, h: 720 });        // framebuffer

    expect(pickFramebufferCanvas(doc)).toBe(screenCanvas);
  });

  it("ignores a framebuffer that has not drawn its first frame yet", () => {
    // Display's constructor sets width/height to 0 before connecting.
    const doc = clientDoc();
    addCanvas(doc, { w: 0, h: 0 });
    expect(pickFramebufferCanvas(doc)).toBeNull();
  });

  it("ignores a display:none canvas and returns null for no document", () => {
    const doc = clientDoc();
    addCanvas(doc, { w: 800, h: 600, style: { display: "none" } });
    expect(pickFramebufferCanvas(doc)).toBeNull();
    expect(pickFramebufferCanvas(null)).toBeNull();
  });

  it("a hidden canvas never wins, however large", () => {
    // Pins the visibility filter itself: without it the cursor canvas — which
    // the client positions fixed at z-index 65535 — could outrank the screen.
    const doc = clientDoc();
    const screenCanvas = addCanvas(doc, { w: 800, h: 600 });
    addCanvas(doc, { w: 4096, h: 4096, style: { visibility: "hidden" } });
    expect(pickFramebufferCanvas(doc)).toBe(screenCanvas);
  });

  it("prefers the largest when more than one qualifies", () => {
    const doc = clientDoc();
    addCanvas(doc, { w: 100, h: 100 });
    const big = addCanvas(doc, { w: 1920, h: 1080 });
    addCanvas(doc, { w: 200, h: 200 });
    expect(pickFramebufferCanvas(doc)).toBe(big);
  });
});

describe("captureLastFrame", () => {
  /** dest canvas with a stubbed 2D context — jsdom has no real one. */
  function fakeDest() {
    const drawImage = vi.fn();
    const dest = document.createElement("canvas");
    vi.spyOn(dest, "getContext").mockReturnValue(
      { drawImage } as unknown as CanvasRenderingContext2D,
    );
    return { dest, drawImage };
  }

  function iframeWith(doc: Document | null): HTMLIFrameElement {
    return { contentDocument: doc } as unknown as HTMLIFrameElement;
  }

  it("downscales the frame and preserves its aspect ratio", () => {
    const doc = clientDoc();
    addCanvas(doc, { w: 1920, h: 1080 });
    const { dest, drawImage } = fakeDest();

    expect(captureLastFrame(iframeWith(doc), dest)).toBe(true);
    // 1920 -> the 640 cap, and the height follows it
    expect([dest.width, dest.height]).toEqual([640, 360]);
    expect(drawImage).toHaveBeenCalledWith(expect.anything(), 0, 0, 640, 360);
  });

  it("never upscales a frame that is already small", () => {
    const doc = clientDoc();
    addCanvas(doc, { w: 320, h: 240 });
    const { dest } = fakeDest();

    expect(captureLastFrame(iframeWith(doc), dest)).toBe(true);
    expect([dest.width, dest.height]).toEqual([320, 240]);
  });

  it("reports failure instead of throwing when there is nothing to copy", () => {
    const { dest } = fakeDest();
    const empty = clientDoc();

    expect(captureLastFrame(null, dest)).toBe(false);
    expect(captureLastFrame(iframeWith(empty), null)).toBe(false);
    expect(captureLastFrame(iframeWith(empty), dest)).toBe(false);
    expect(captureLastFrame(iframeWith(null), dest)).toBe(false);
  });

  it("survives a cross-origin iframe", () => {
    // Accessing contentDocument across origins throws in a real browser.
    const hostile = {
      get contentDocument(): Document {
        throw new DOMException("blocked", "SecurityError");
      },
    } as unknown as HTMLIFrameElement;
    const { dest } = fakeDest();
    expect(captureLastFrame(hostile, dest)).toBe(false);
  });

  it("survives a drawImage that throws", () => {
    const doc = clientDoc();
    addCanvas(doc, { w: 800, h: 600 });
    const dest = document.createElement("canvas");
    vi.spyOn(dest, "getContext").mockReturnValue({
      drawImage: () => {
        throw new DOMException("tainted", "SecurityError");
      },
    } as unknown as CanvasRenderingContext2D);

    expect(captureLastFrame(iframeWith(doc), dest)).toBe(false);
  });
});

describe("the reconnect overlay dims the last frame", () => {
  /**
   * jsdom has no 2D context, so the copy is stubbed at the prototype. What is
   * under test is the wiring — captured while connected, revealed only while
   * reconnecting — not the pixels.
   */
  function stubCanvas2D() {
    return vi
      .spyOn(HTMLCanvasElement.prototype, "getContext")
      .mockReturnValue({ drawImage: vi.fn() } as unknown as CanvasRenderingContext2D);
  }

  async function connected() {
    mockApi.profileStatus.mockResolvedValue({
      status: "running",
      xvnc_alive: true,
      browser_alive: true,
    });
    const view = render(
      <ProfileViewer
        profileId="p1"
        cdpUrl={null}
        clipboardSync={false}
        onSessionEnded={() => {}}
      />,
    );
    await act(async () => {});
    const iframe = view.container.querySelector("iframe")!;
    // give the client a frame to be copied. The iframe points at a URL jsdom
    // will not load, so its document has no body to append to.
    const doc = iframe.contentDocument!;
    const frame = doc.createElement("canvas");
    frame.width = 1920;
    frame.height = 1080;
    ensureBody(doc).appendChild(frame);
    expect(frame.isConnected).toBe(true);

    const send = (value: string) => {
      const ev = new Event("message");
      Object.defineProperty(ev, "data", {
        value: { action: "connection_state", value },
      });
      Object.defineProperty(ev, "source", { value: iframe.contentWindow });
      window.dispatchEvent(ev);
    };
    return { ...view, send };
  }

  /** the snapshot canvas is ours, not the client's (which lives in the iframe) */
  const snapshotOf = (container: HTMLElement) =>
    container.querySelector("canvas") as HTMLCanvasElement | null;

  it("holds the frame while connected and reveals it on a drop", async () => {
    vi.useFakeTimers();
    const spy = stubCanvas2D();
    try {
      const { container, send } = await connected();

      // mounted from the start: it has to exist as a draw target the whole
      // time the connection is up, or there is nothing to reveal later
      expect(snapshotOf(container)).not.toBeNull();

      act(() => send("connected"));
      await act(async () => {
        vi.advanceTimersByTime(2_000);
      });
      // ...but stays out of the way while the live client is visible
      expect(snapshotOf(container)!.style.display).toBe("none");

      act(() => send("disconnected"));
      expect(screen.getByText("Connection lost — reconnecting")).toBeInTheDocument();
      expect(snapshotOf(container)!.style.display).toBe("block");
    } finally {
      spy.mockRestore();
    }
  });

  it("stays hidden if no frame was ever captured", async () => {
    // A connection that drops before the client ever painted has nothing to
    // show; a blank canvas over the pane would be worse than no canvas.
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
    await act(async () => {
      vi.advanceTimersByTime(2_000);
    });
    act(() => send("disconnected"));

    expect(screen.getByText("Connection lost — reconnecting")).toBeInTheDocument();
    expect(snapshotOf(container)!.style.display).toBe("none");
  });
});

// ── toolbar controls ────────────────────────────────────────────────────────

describe("toolbar controls", () => {
  async function mountViewer(cdpUrl: string | null = "/cdp/abc") {
    mockApi.profileStatus.mockResolvedValue({
      status: "running",
      xvnc_alive: true,
      browser_alive: true,
    });
    const view = render(
      <ProfileViewer
        profileId="p1"
        cdpUrl={cdpUrl}
        clipboardSync={false}
        onSessionEnded={() => {}}
      />,
    );
    await act(async () => {});
    return view;
  }

  it("enters and leaves fullscreen, and follows the browser out of it", async () => {
    // jsdom implements none of the Fullscreen API.
    const requestFullscreen = vi.fn();
    const exitFullscreen = vi.fn();
    Object.defineProperty(Element.prototype, "requestFullscreen", {
      value: requestFullscreen, configurable: true, writable: true,
    });
    Object.defineProperty(document, "exitFullscreen", {
      value: exitFullscreen, configurable: true, writable: true,
    });
    const setFullscreenElement = (el: Element | null) =>
      Object.defineProperty(document, "fullscreenElement", {
        value: el, configurable: true, writable: true,
      });
    setFullscreenElement(null);

    await mountViewer();
    act(() => screen.getByTitle("Fullscreen").click());
    expect(requestFullscreen).toHaveBeenCalledTimes(1);

    const exitButton = screen.getByTitle("Exit fullscreen");
    setFullscreenElement(document.body);
    act(() => exitButton.click());
    expect(exitFullscreen).toHaveBeenCalledTimes(1);
    expect(screen.getByTitle("Fullscreen")).toBeInTheDocument();

    // the user can also leave via Esc, which only fires the event — without
    // the listener the button would stay stuck on "Exit fullscreen"
    setFullscreenElement(document.body);
    act(() => {
      document.dispatchEvent(new Event("fullscreenchange"));
    });
    expect(screen.getByTitle("Exit fullscreen")).toBeInTheDocument();

    setFullscreenElement(null);
    act(() => {
      document.dispatchEvent(new Event("fullscreenchange"));
    });
    expect(screen.getByTitle("Fullscreen")).toBeInTheDocument();
  });

  it("opens the same viewer URL the iframe uses in a new tab", async () => {
    const { container } = await mountViewer();
    const iframe = container.querySelector("iframe")!;
    const iframeSrc = iframe.getAttribute("src")!;
    const windowOpen = vi.fn();
    vi.stubGlobal("open", windowOpen);

    act(() => screen.getByTitle("Open in new tab").click());

    expect(windowOpen).toHaveBeenCalledWith(iframeSrc, "_blank");
    vi.unstubAllGlobals();
  });

  it("does not offer Open in new tab before a viewer session exists", () => {
    // No token fetch has resolved yet — iframeSrc is still null.
    mockApi.createViewerToken.mockReturnValue(new Promise(() => {})); // never settles
    render(
      <ProfileViewer profileId="p1" cdpUrl={null} clipboardSync={false} onSessionEnded={() => {}} />,
    );
    expect(screen.queryByTitle("Open in new tab")).not.toBeInTheDocument();
  });

  it("copies the CDP endpoint as an absolute URL and confirms it briefly", async () => {
    // cdp_url is a path; pasting it into a CDP client needs the origin.
    vi.useFakeTimers();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText }, configurable: true,
    });

    await mountViewer("/cdp/abc");
    await act(async () => {
      screen.getByTitle("Copy CDP endpoint URL").click();
    });

    expect(writeText).toHaveBeenCalledWith(
      `${window.location.protocol}//${window.location.host}/cdp/abc`,
    );
    expect(screen.getByTitle("Copied!")).toBeInTheDocument();

    // and it reverts, so the toolbar does not claim a stale success
    await act(async () => {
      vi.advanceTimersByTime(2_000);
    });
    expect(screen.getByTitle("Copy CDP endpoint URL")).toBeInTheDocument();
  });

  it("does not claim success when the clipboard write is refused", async () => {
    // Denied permission / insecure context rejects rather than throwing.
    const writeText = vi.fn().mockRejectedValue(new Error("denied"));
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText }, configurable: true,
    });
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});

    await mountViewer("/cdp/abc");
    await act(async () => {
      screen.getByTitle("Copy CDP endpoint URL").click();
    });

    expect(screen.getByTitle("Copy CDP endpoint URL")).toBeInTheDocument();
    expect(warn).toHaveBeenCalled();
    warn.mockRestore();
  });

  it("hides the CDP button when the profile exposes no endpoint", async () => {
    await mountViewer(null);
    expect(screen.queryByTitle("Copy CDP endpoint URL")).not.toBeInTheDocument();
  });

  it("toggles clipboard sync and says the change is not retroactive", async () => {
    // The flag is baked into the iframe URL, so it can only apply on the next
    // connect — the title has to say so or the toggle reads as broken.
    await mountViewer();
    const enable = screen.getByTitle(/Enable clipboard sync/);
    expect(enable.getAttribute("title")).toContain("applies on next connect");

    act(() => enable.click());
    expect(screen.getByTitle(/Disable clipboard sync/)).toBeInTheDocument();
  });

  it("does not copy frames while the tab is hidden", async () => {
    // Nothing is being painted, so the copies would be identical.
    vi.useFakeTimers();
    const getContext = vi.spyOn(HTMLCanvasElement.prototype, "getContext");
    const visibility = vi
      .spyOn(document, "visibilityState", "get")
      .mockReturnValue("hidden");
    try {
      const { container } = await mountViewer();
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
      getContext.mockClear();
      await act(async () => {
        vi.advanceTimersByTime(10_000);
      });
      expect(getContext).not.toHaveBeenCalled();
    } finally {
      visibility.mockRestore();
      getContext.mockRestore();
    }
  });

  it("gives up quietly when the canvas has no 2D context", async () => {
    // The real jsdom/headless case, and any browser that refuses the context.
    const doc = clientDoc();
    addCanvas(doc, { w: 800, h: 600 });
    const dest = document.createElement("canvas");
    vi.spyOn(dest, "getContext").mockReturnValue(null);
    expect(
      captureLastFrame({ contentDocument: doc } as unknown as HTMLIFrameElement, dest),
    ).toBe(false);
  });
});
