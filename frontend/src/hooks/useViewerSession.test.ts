import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useViewerSession, buildViewerUrl } from "./useViewerSession";

// Mock the api module (ApiError must be the same class the hook checks against)
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

import { api, ApiError } from "../lib/api";

const mockApi = api as {
  createViewerToken: ReturnType<typeof vi.fn>;
  profileStatus: ReturnType<typeof vi.fn>;
};

const ALIVE = { status: "running", xvnc_alive: true, browser_alive: true };
const XVNC_DEAD = { status: "running", xvnc_alive: false, browser_alive: true };
const BROWSER_DEAD = { status: "running", xvnc_alive: true, browser_alive: false };
const STOPPED = { status: "stopped", xvnc_alive: null, browser_alive: null };
const STOPPING = { status: "stopping", xvnc_alive: null, browser_alive: null };
const TOK1 = { token: "tok-1", viewer_url: "/viewer/tok-1/", expires_in: 300 };
const TOK2 = { token: "tok-2", viewer_url: "/viewer/tok-2/", expires_in: 300 };

// Fake iframe: contentWindow doubles as the message source for filtering.
const fakeContentWindow: { location: { href: string } } = {
  location: { href: "http://localhost/viewer/tok-1/" },
};
const fakeIframe = { contentWindow: fakeContentWindow } as unknown as HTMLIFrameElement;

function sendMessage(data: unknown, source: unknown = fakeContentWindow) {
  const ev = new Event("message");
  Object.defineProperty(ev, "data", { value: data });
  Object.defineProperty(ev, "source", { value: source });
  window.dispatchEvent(ev);
}

function sendConnectionState(value: string) {
  sendMessage({ action: "connection_state", value });
}

/**
 * The incoming document's boot marker. The real client posts it exactly once
 * per document, strictly before it can connect, and it is what hands the
 * document gate over — a re-pointed iframe is ignored until it arrives.
 */
function bootDocument() {
  sendConnectionState("init");
}

function setVisibility(value: string) {
  Object.defineProperty(document, "visibilityState", { value, configurable: true });
}

function setup(clipboardSync = true) {
  // deterministic jitter: delay = base * (0.5 + 0.5 * 1) = base
  const random = vi.fn(() => 1);
  const view = renderHook(() =>
    useViewerSession({ profileId: "p1", clipboardSync, random }),
  );
  act(() => {
    view.result.current.iframeRef.current = fakeIframe;
  });
  return view;
}

/** flush pending promise continuations */
async function flush() {
  await act(async () => {});
  await act(async () => {});
}

/** advance fake timers, then flush the promises those timers kicked off */
async function advance(ms: number) {
  await act(async () => {
    vi.advanceTimersByTime(ms);
  });
  await act(async () => {});
}

beforeEach(() => {
  vi.useFakeTimers();
  mockApi.createViewerToken.mockReset();
  mockApi.profileStatus.mockReset();
  mockApi.createViewerToken.mockResolvedValue(TOK1);
  setVisibility("visible");
});

afterEach(() => {
  vi.useRealTimers();
});

// ── buildViewerUrl ──────────────────────────────────────────────────────────

describe("buildViewerUrl", () => {
  it("includes the websockify path override and client flags", () => {
    const url = buildViewerUrl("/viewer/tok-1/", "tok-1", true);
    const [base, qs] = url.split("?");
    expect(base).toBe("/viewer/tok-1/");
    const p = new URLSearchParams(qs);
    expect(p.get("path")).toBe("viewer/tok-1/websockify");
    expect(p.get("autoconnect")).toBe("true");
    expect(p.get("reconnect")).toBe("true");
    expect(p.get("reconnect_delay")).toBe("2000");
    expect(p.get("resize")).toBe("scale");
    expect(p.get("enable_webp")).toBe("true");
    // idle_disconnect=0 would mean "disconnect instantly" in this client —
    // must be a large value instead
    expect(Number(p.get("idle_disconnect"))).toBeGreaterThan(0);
    expect(p.get("clipboard_up")).toBe("true");
    expect(p.get("clipboard_down")).toBe("true");
    expect(p.get("clipboard_seamless")).toBe("true");
  });

  it("reflects clipboardSync=false in the clipboard flags", () => {
    const p = new URLSearchParams(buildViewerUrl("/viewer/t/", "t", false).split("?")[1]);
    expect(p.get("clipboard_up")).toBe("false");
    expect(p.get("clipboard_down")).toBe("false");
    expect(p.get("clipboard_seamless")).toBe("false");
  });
});

// ── initial connect ─────────────────────────────────────────────────────────

describe("initial connect", () => {
  it("fetches a viewer token and builds the iframe src", async () => {
    const { result } = setup();
    await flush();
    expect(mockApi.createViewerToken).toHaveBeenCalledWith("p1");
    expect(result.current.state).toBe("connecting");
    expect(result.current.iframeSrc).not.toBeNull();
    const [base, qs] = result.current.iframeSrc!.split("?");
    expect(base).toBe("/viewer/tok-1/");
    expect(new URLSearchParams(qs).get("path")).toBe("viewer/tok-1/websockify");
  });

  it("transitions to connected on the client's connection_state message", async () => {
    const { result } = setup();
    await flush();
    act(() => sendConnectionState("connected"));
    expect(result.current.state).toBe("connected");
    expect(result.current.attempt).toBe(0);
  });

  it("ignores messages from a foreign source", async () => {
    const { result } = setup();
    await flush();
    act(() => sendMessage({ action: "connection_state", value: "connected" }, {}));
    expect(result.current.state).toBe("connecting");
  });

  it("404 on the initial token fetch ends the session", async () => {
    mockApi.createViewerToken.mockRejectedValue(new ApiError(404, "Profile not running"));
    const { result } = setup();
    await flush();
    expect(result.current.state).toBe("session-ended");
    expect(result.current.endReason).toBe("Browser session ended");
  });

  it("401 halts without retrying (app auth handler takes over)", async () => {
    mockApi.createViewerToken.mockRejectedValue(new ApiError(401, "Unauthorized"));
    const { result } = setup();
    await flush();
    // Terminal rather than a frozen "Connecting…": every timer is cleared by
    // the halt, so a non-terminal state here renders no overlay, no button and
    // has nothing left that could ever move it.
    expect(result.current.state).toBe("fatal");
    expect(result.current.endReason).toBe("Your session expired — sign in again");
    await advance(60_000);
    expect(mockApi.createViewerToken).toHaveBeenCalledTimes(1);
    expect(mockApi.profileStatus).not.toHaveBeenCalled();
  });

  it("a 429 on the token endpoint is retried, not treated as fatal", async () => {
    mockApi.profileStatus.mockResolvedValue(ALIVE);
    mockApi.createViewerToken.mockRejectedValueOnce(new ApiError(429, "Too Many Requests"));
    const { result } = setup();
    await flush();
    expect(result.current.state).toBe("reconnecting");
    expect(result.current.endReason).toBeNull();

    mockApi.createViewerToken.mockResolvedValueOnce(TOK2);
    await advance(250);
    expect(result.current.state).toBe("connecting");
  });

  it("a genuine client error is still fatal", async () => {
    mockApi.createViewerToken.mockRejectedValue(new ApiError(400, "Bad Request"));
    const { result } = setup();
    await flush();
    expect(result.current.state).toBe("fatal");
  });

  it("connect watchdog (15s without connected) enters reconnecting", async () => {
    mockApi.profileStatus.mockResolvedValue(ALIVE);
    const { result } = setup();
    await flush();
    expect(result.current.state).toBe("connecting");
    await advance(15_000);
    expect(result.current.state).toBe("reconnecting");
    expect(result.current.attempt).toBe(1);
  });
});

// ── reconnect flow ──────────────────────────────────────────────────────────

describe("reconnect flow", () => {
  async function reachConnected() {
    const view = setup();
    await flush();
    act(() => sendConnectionState("connected"));
    return view;
  }

  it("unclean disconnect → reconnecting with backoff, then fresh token → connected", async () => {
    mockApi.profileStatus.mockResolvedValue(ALIVE);
    const { result } = await reachConnected();

    act(() => sendConnectionState("disconnected"));
    expect(result.current.state).toBe("reconnecting");
    expect(result.current.attempt).toBe(1);
    // first backoff step with random()=1 is exactly 250ms
    expect(result.current.nextRetryAt).toBe(Date.now() + 250);

    mockApi.createViewerToken.mockResolvedValueOnce(TOK2);
    await advance(250);
    expect(mockApi.profileStatus).toHaveBeenCalledWith("p1");
    // profile alive → fresh token → iframe src rebuilt → connecting
    expect(result.current.state).toBe("connecting");
    expect(result.current.iframeSrc).toContain("tok-2");

    act(() => bootDocument());
    act(() => sendConnectionState("connected"));
    expect(result.current.state).toBe("connected");
    expect(result.current.attempt).toBe(0);
    expect(result.current.nextRetryAt).toBeNull();
  });

  it("keeps backing off while the Manager is unreachable", async () => {
    mockApi.profileStatus.mockRejectedValue(new TypeError("fetch failed"));
    const { result } = await reachConnected();

    act(() => sendConnectionState("disconnected"));
    expect(result.current.nextRetryAt).toBe(Date.now() + 250);

    await advance(250);
    expect(result.current.state).toBe("reconnecting");
    expect(result.current.attempt).toBe(2);
    expect(result.current.nextRetryAt).toBe(Date.now() + 1_000);

    await advance(1_000);
    expect(result.current.attempt).toBe(3);
    expect(result.current.nextRetryAt).toBe(Date.now() + 2_000);

    await advance(2_000);
    expect(result.current.attempt).toBe(4);
    expect(result.current.nextRetryAt).toBe(Date.now() + 4_000);
  });

  it("profile stopped → session-ended, no further retries", async () => {
    mockApi.profileStatus.mockResolvedValue({
      status: "stopped",
      xvnc_alive: null,
      browser_alive: null,
    });
    const { result } = await reachConnected();

    act(() => sendConnectionState("disconnected"));
    await advance(250);
    expect(result.current.state).toBe("session-ended");
    expect(result.current.endReason).toBe("Browser session ended");

    await advance(120_000);
    expect(mockApi.profileStatus).toHaveBeenCalledTimes(1);
  });

  it("profile starting → keeps retrying and recovers when it comes up", async () => {
    // Container restart with auto_launch: the profile is queued/launching, so
    // it is neither running nor gone. Terminating here would kill a session
    // that is seconds from coming back.
    const STARTING = { status: "starting", xvnc_alive: null, browser_alive: null };
    mockApi.profileStatus.mockResolvedValue(STARTING);
    const { result } = await reachConnected();

    act(() => sendConnectionState("disconnected"));
    await advance(250); // probe 1 → starting
    expect(result.current.state).toBe("reconnecting");
    await advance(1_000); // probe 2 → starting
    expect(result.current.state).toBe("reconnecting");
    await advance(2_000); // probe 3 → still starting, must NOT escalate
    expect(result.current.state).toBe("reconnecting");
    expect(result.current.endReason).toBeNull();

    // the profile finishes launching
    mockApi.profileStatus.mockResolvedValue(ALIVE);
    mockApi.createViewerToken.mockResolvedValueOnce(TOK2);
    await advance(4_000);
    expect(result.current.state).toBe("connecting");
    expect(result.current.iframeSrc).toContain("tok-2");
  });

  it("a 503 from the token endpoint is retried, not treated as fatal", async () => {
    mockApi.profileStatus.mockResolvedValue(ALIVE);
    mockApi.createViewerToken.mockRejectedValueOnce(new ApiError(503, "Profile is starting"));
    const { result } = setup();
    await flush();
    expect(result.current.state).toBe("reconnecting");
    expect(result.current.endReason).toBeNull();

    mockApi.createViewerToken.mockResolvedValueOnce(TOK2);
    await advance(250);
    expect(result.current.state).toBe("connecting");
  });

  it("running but xvnc dead → retries, escalates after 3 identical probes", async () => {
    mockApi.profileStatus.mockResolvedValue({
      status: "running",
      xvnc_alive: false,
      browser_alive: true,
    });
    const { result } = await reachConnected();

    act(() => sendConnectionState("disconnected"));
    await advance(250); // probe 1
    expect(result.current.state).toBe("reconnecting");
    await advance(1_000); // probe 2
    expect(result.current.state).toBe("reconnecting");
    await advance(2_000); // probe 3 → escalate
    expect(result.current.state).toBe("session-ended");
    expect(result.current.endReason).toBe("Display server stopped");
    expect(mockApi.profileStatus).toHaveBeenCalledTimes(3);
  });

  it("a token fetch resolving after the watchdog does not re-point the iframe", async () => {
    let resolveTok: (v: unknown) => void = () => {};
    mockApi.createViewerToken.mockReturnValueOnce(
      new Promise((r) => {
        resolveTok = r;
      }),
    );
    mockApi.profileStatus.mockReturnValue(new Promise(() => {})); // stay reconnecting
    const { result } = setup();
    await flush();
    expect(result.current.state).toBe("connecting");

    await advance(15_000); // watchdog takes over
    expect(result.current.state).toBe("reconnecting");

    // the superseded fetch finally resolves — its token belongs to a dead cycle
    await act(async () => {
      resolveTok({ token: "stale", viewer_url: "/viewer/stale/", expires_in: 3600 });
    });
    expect(result.current.iframeSrc).toBeNull();
  });

  it("a client self-recovery cancels the in-flight token fetch", async () => {
    // The client's own 2s retry can succeed while our probe->token fetch is
    // still in flight; applying that token would reload over a live session.
    let resolveTok: (v: unknown) => void = () => {};
    mockApi.profileStatus.mockResolvedValue(ALIVE);
    const { result } = await reachConnected();
    const srcWhileConnected = result.current.iframeSrc;

    act(() => sendConnectionState("disconnected"));
    mockApi.createViewerToken.mockReturnValueOnce(
      new Promise((r) => {
        resolveTok = r;
      }),
    );
    await advance(250); // probe -> alive -> connect(), token fetch pending

    act(() => sendConnectionState("connected")); // client got back on its own
    expect(result.current.state).toBe("connected");

    await act(async () => {
      resolveTok(TOK2);
    });
    expect(result.current.state).toBe("connected");
    expect(result.current.iframeSrc).toBe(srcWhileConnected); // no reload
  });

  it("ignores a stale 'disconnected' from the outgoing iframe document", async () => {
    // Same-frame navigation keeps the same WindowProxy, so the old client's
    // closing socket posts a "disconnected" that passes the source filter.
    mockApi.profileStatus.mockResolvedValue(ALIVE);
    const { result } = await reachConnected();

    act(() => sendConnectionState("disconnected")); // the real drop
    mockApi.createViewerToken.mockResolvedValueOnce(TOK2);
    await advance(250);
    expect(result.current.state).toBe("connecting");
    expect(result.current.iframeSrc).toContain("tok-2");
    const tokensBefore = mockApi.createViewerToken.mock.calls.length;

    // outgoing document's socket closes while the new one is still loading
    act(() => sendConnectionState("disconnected"));
    expect(result.current.state).toBe("connecting");
    expect(result.current.attempt).toBe(1);
    expect(result.current.iframeSrc).toContain("tok-2");

    // the load event is NOT the handover: it fires after subresources, so the
    // outgoing document can still be posting when it lands
    act(() => result.current.handleIframeLoad());
    act(() => sendConnectionState("disconnected"));
    expect(result.current.state).toBe("connecting");
    expect(result.current.attempt).toBe(1);

    // the incoming document's own boot marker is
    act(() => bootDocument());
    act(() => sendConnectionState("disconnected"));
    expect(result.current.state).toBe("reconnecting");
    expect(result.current.attempt).toBe(2);
    expect(mockApi.createViewerToken.mock.calls.length).toBe(tokensBefore);
  });

  it("a successful connection clears the terminal-classification counter", async () => {
    // Two xvnc-dead verdicts, then the CLIENT reconnects on its own (no probe
    // in between, so nothing else resets the counter). A later unrelated drop
    // must not have its first probe treated as the third in a row.
    mockApi.profileStatus.mockResolvedValue(XVNC_DEAD);
    const { result } = await reachConnected();

    act(() => sendConnectionState("disconnected"));
    await advance(250);    // verdict 1
    await advance(1_000);  // verdict 2
    expect(result.current.state).toBe("reconnecting");

    // the Kasm client gets back by itself — no status probe involved
    act(() => sendConnectionState("connected"));
    expect(result.current.state).toBe("connected");

    // a later, unrelated drop whose first probe reports xvnc-dead.
    // backoffLevel is not reset by a self-recovery, so allow for a long step.
    act(() => sendConnectionState("disconnected"));
    await advance(20_000);
    expect(result.current.state).toBe("reconnecting");   // not session-ended
    expect(result.current.endReason).toBeNull();
  });

  it("iframe load on disconnected.html triggers a reconnect", async () => {
    mockApi.profileStatus.mockResolvedValue(ALIVE);
    const { result } = await reachConnected();

    fakeContentWindow.location.href = "http://localhost/viewer/tok-1/disconnected.html";
    act(() => result.current.handleIframeLoad());
    expect(result.current.state).toBe("reconnecting");

    // a load of the client index is not a reconnect trigger
    mockApi.createViewerToken.mockResolvedValueOnce(TOK2);
    await advance(250);
    expect(result.current.state).toBe("connecting");
    fakeContentWindow.location.href = "http://localhost/viewer/tok-2/?path=...";
    act(() => result.current.handleIframeLoad());
    expect(result.current.state).toBe("connecting");
  });

  it("backoff resets after 30s of stable connected", async () => {
    mockApi.profileStatus.mockResolvedValue(ALIVE);
    const { result } = await reachConnected();

    // push backoff up one level with a failed drop, then recover
    act(() => sendConnectionState("disconnected"));
    mockApi.createViewerToken.mockResolvedValueOnce(TOK2);
    await advance(250);
    act(() => bootDocument());
    act(() => sendConnectionState("connected"));
    expect(result.current.state).toBe("connected");

    await advance(30_000); // stable window elapses → backoffLevel = 0

    act(() => sendConnectionState("disconnected"));
    expect(result.current.state).toBe("reconnecting");
    expect(result.current.nextRetryAt).toBe(Date.now() + 250); // back to step 1
  });

  it("marks the session degraded after 60s of reconnecting", async () => {
    mockApi.profileStatus.mockReturnValue(new Promise(() => {})); // never resolves
    const { result } = await reachConnected();

    act(() => sendConnectionState("disconnected"));
    expect(result.current.degraded).toBe(false);
    await advance(60_000);
    expect(result.current.state).toBe("reconnecting");
    expect(result.current.degraded).toBe(true);
  });

  it("a restart resets the data-plane failure budget", async () => {
    // The budget counts failures against ONE instance's viewer path. A restart
    // replaces that path, so carrying the count over ends the new session early.
    // Drive it by token fetches: each "alive" cycle mints exactly one.
    mockApi.profileStatus.mockResolvedValue(ALIVE);
    const { result } = setup();
    await flush();

    const cycles = async (n: number) => {
      const target = mockApi.createViewerToken.mock.calls.length + n;
      for (let i = 0; i < 60 && mockApi.createViewerToken.mock.calls.length < target; i++) {
        await advance(15_000);
        if (result.current.state === "session-ended") return;
      }
    };

    await cycles(9); // one short of MAX_ALIVE_RECONNECTS (10)
    expect(result.current.state).not.toBe("session-ended");

    // the profile restarts — the old viewer path is gone
    mockApi.profileStatus.mockResolvedValue({
      status: "starting",
      xvnc_alive: null,
      browser_alive: null,
    });
    await advance(60_000);
    expect(result.current.state).toBe("reconnecting");

    // the new instance must get a full budget, not the old remainder
    mockApi.profileStatus.mockResolvedValue(ALIVE);
    await cycles(5);
    expect(result.current.state).not.toBe("session-ended");
  });

  it("alive but unreachable eventually ends the session instead of looping", async () => {
    // Nothing in /status can see the viewer data plane (nginx, token, Kasm's
    // listener), so "running + alive" while the iframe never connects must
    // hit a failure budget rather than retry — and mint a token — forever.
    mockApi.profileStatus.mockResolvedValue(ALIVE);
    const { result } = setup();
    await flush();

    for (let i = 0; i < 15 && result.current.state !== "session-ended"; i++) {
      await advance(15_000); // watchdog
      await advance(15_000); // backoff + probe -> alive -> connect
    }

    expect(result.current.state).toBe("session-ended");
    expect(result.current.endReason).toContain("Can't reach this browser session");
    // budget respected: not an unbounded stream of tokens
    expect(mockApi.createViewerToken.mock.calls.length).toBeLessThanOrEqual(12);

    // and it must not keep probing afterwards
    const probes = mockApi.profileStatus.mock.calls.length;
    await advance(120_000);
    expect(mockApi.profileStatus.mock.calls.length).toBe(probes);
  });

  it("reconnectNow recovers from a terminal state", async () => {
    mockApi.profileStatus.mockResolvedValue({
      status: "stopped",
      xvnc_alive: null,
      browser_alive: null,
    });
    const { result } = await reachConnected();

    act(() => sendConnectionState("disconnected"));
    await advance(250);
    expect(result.current.state).toBe("session-ended");

    // the profile is back (or the classification was wrong) — retry in place
    mockApi.profileStatus.mockResolvedValue(ALIVE);
    mockApi.createViewerToken.mockResolvedValueOnce(TOK2);
    await act(async () => {
      result.current.reconnectNow();
    });
    expect(result.current.state).toBe("connecting");
    expect(result.current.endReason).toBeNull();
    expect(result.current.iframeSrc).toContain("tok-2");
  });

  it("reconnectNow resets backoff and retries immediately", async () => {
    mockApi.profileStatus.mockResolvedValue(ALIVE);
    const { result } = await reachConnected();

    act(() => sendConnectionState("disconnected"));
    expect(result.current.state).toBe("reconnecting");
    expect(mockApi.profileStatus).not.toHaveBeenCalled();

    mockApi.createViewerToken.mockResolvedValueOnce(TOK2);
    await act(async () => {
      result.current.reconnectNow();
    });
    expect(mockApi.profileStatus).toHaveBeenCalledTimes(1);
    expect(result.current.state).toBe("connecting");
  });
});

// ── connectivity signals ────────────────────────────────────────────────────

describe("connectivity signals", () => {
  async function reachReconnecting() {
    mockApi.profileStatus.mockResolvedValue(ALIVE);
    const view = setup();
    await flush();
    act(() => sendConnectionState("connected"));
    act(() => sendConnectionState("disconnected"));
    return view;
  }

  it("concurrent triggers do not stack probes or double-count the verdict", async () => {
    // xvnc-dead escalates after MAX_SAME_CLASSIFICATION (3) probes. Three
    // triggers firing in one round must not consume that budget at once.
    const { result } = await reachReconnecting();
    let resolveProbe: (v: unknown) => void = () => {};
    mockApi.profileStatus.mockReturnValue(
      new Promise((r) => {
        resolveProbe = r;
      }),
    );

    await act(async () => {
      window.dispatchEvent(new Event("online"));
      document.dispatchEvent(new Event("visibilitychange"));
      vi.advanceTimersByTime(250); // retry timer
    });
    expect(mockApi.profileStatus).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolveProbe({ status: "running", xvnc_alive: false, browser_alive: true });
    });
    expect(result.current.state).toBe("reconnecting"); // one verdict, not three
    expect(result.current.endReason).toBeNull();
  });

  it("a retry is never swallowed by a connect that outlives its watchdog", async () => {
    // The in-flight guard must cover the status probe only. Held across
    // connect(), a stalled token fetch swallows the retry the watchdog just
    // armed, and nothing re-arms — the viewer parks in "reconnecting" forever.
    mockApi.profileStatus.mockResolvedValue(ALIVE);
    const { result } = setup();
    await flush();
    act(() => sendConnectionState("connected"));
    act(() => sendConnectionState("disconnected"));

    let hang: (v: unknown) => void = () => {};
    mockApi.createViewerToken.mockReturnValueOnce(
      new Promise((r) => {
        hang = r;
      }),
    );
    await advance(250); // probe -> alive -> connect(); the token fetch stalls
    expect(result.current.state).toBe("connecting");

    await advance(15_000); // connect watchdog -> reconnecting, retry armed
    expect(result.current.state).toBe("reconnecting");

    const probesBefore = mockApi.profileStatus.mock.calls.length;
    mockApi.createViewerToken.mockResolvedValueOnce(TOK2);
    await advance(1_000); // that retry must actually run
    expect(mockApi.profileStatus.mock.calls.length).toBeGreaterThan(probesBefore);
    expect(result.current.state).toBe("connecting");
    expect(result.current.iframeSrc).toContain("tok-2");

    // the abandoned fetch resolving must not disturb the cycle that replaced it
    await act(async () => {
      hang({ token: "late", viewer_url: "/viewer/late/", expires_in: 3600 });
    });
    expect(result.current.iframeSrc).toContain("tok-2");
  });

  it("re-arms when a probe ends without scheduling anything", async () => {
    // A scheduleReconnect landing mid-probe bumps the generation, so the probe
    // discards its own result. The retry it consumed must still get a
    // successor rather than leaving the machine idle in "reconnecting".
    let resolveProbe: (v: unknown) => void = () => {};
    mockApi.profileStatus.mockReturnValueOnce(
      new Promise((r) => {
        resolveProbe = r;
      }),
    );
    const { result } = setup();
    await flush();
    act(() => sendConnectionState("connected"));
    act(() => sendConnectionState("disconnected"));
    await advance(250); // probe starts and stalls

    // the iframe lands on disconnected.html while the probe is outstanding
    fakeContentWindow.location.href = "http://localhost/viewer/tok-1/disconnected.html";
    act(() => result.current.handleIframeLoad());
    await advance(1_000); // that retry is skipped by the in-flight guard

    mockApi.profileStatus.mockResolvedValue(ALIVE);
    await act(async () => {
      resolveProbe(ALIVE);
    });
    expect(result.current.state).toBe("reconnecting");

    // nextRetryAt alone proves nothing here — a skipped retry leaves the old
    // value behind. What matters is that another attempt actually runs.
    const probesBefore = mockApi.profileStatus.mock.calls.length;
    await advance(60_000);
    expect(mockApi.profileStatus.mock.calls.length).toBeGreaterThan(probesBefore);
    fakeContentWindow.location.href = "http://localhost/viewer/tok-1/";
  });

  it("a superseded probe does not unlock the guard for its replacement", async () => {
    const { result } = await reachReconnecting();
    let resolveStuck: (v: unknown) => void = () => {};
    mockApi.profileStatus.mockReturnValueOnce(
      new Promise((r) => {
        resolveStuck = r;
      }),
    );
    await advance(250); // probe 1 starts and hangs
    expect(mockApi.profileStatus).toHaveBeenCalledTimes(1);

    // user hits "Reconnect now": supersedes the stuck probe
    let resolveSecond: (v: unknown) => void = () => {};
    mockApi.profileStatus.mockReturnValueOnce(
      new Promise((r) => {
        resolveSecond = r;
      }),
    );
    await act(async () => {
      result.current.reconnectNow();
    });
    expect(mockApi.profileStatus).toHaveBeenCalledTimes(2);

    // the abandoned probe finally resolves — it must not reopen the guard
    await act(async () => {
      resolveStuck(ALIVE);
    });
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(mockApi.profileStatus).toHaveBeenCalledTimes(2); // still only two

    await act(async () => {
      resolveSecond(ALIVE);
    });
  });

  it("keeps re-verifying while connected even with no events at all", async () => {
    // "connected" has no watchdog, no retry timer and no overlay. Without a
    // heartbeat, a silent half-open socket (or an offline blip whose "online"
    // never arrives) leaves a green dot over a frozen frame forever.
    mockApi.profileStatus.mockResolvedValue(ALIVE);
    const { result } = setup();
    await flush();
    act(() => sendConnectionState("connected"));
    expect(result.current.state).toBe("connected");

    const probesBefore = mockApi.profileStatus.mock.calls.length;
    await advance(50_000); // no events dispatched at all
    expect(mockApi.profileStatus.mock.calls.length).toBeGreaterThan(probesBefore);

    // and it notices when the profile is actually gone
    mockApi.profileStatus.mockResolvedValue({
      status: "stopped",
      xvnc_alive: null,
      browser_alive: null,
    });
    await advance(50_000);
    expect(result.current.state).toBe("session-ended");
  });

  it("a control-plane blip does not tear down a healthy viewer", async () => {
    // The viewer's socket is browser -> nginx -> Xvnc and never touches
    // FastAPI, so a 5xx (or a timed-out request) says nothing about it.
    mockApi.profileStatus.mockResolvedValue(ALIVE);
    const { result } = setup();
    await flush();
    act(() => sendConnectionState("connected"));

    mockApi.profileStatus.mockRejectedValue(new ApiError(502, "Bad Gateway"));
    await advance(50_000); // heartbeat fires and fails
    expect(result.current.state).toBe("connected");
    expect(result.current.attempt).toBe(0);

    // ...but a definitive answer is still acted on
    mockApi.profileStatus.mockResolvedValue({
      status: "stopped",
      xvnc_alive: null,
      browser_alive: null,
    });
    await advance(50_000);
    expect(result.current.state).toBe("session-ended");
  });

  it("a tab switch during a control-plane blip leaves a healthy viewer alone", async () => {
    // Switching tabs is not evidence about the viewer's socket, so a 502 on
    // the probe it triggers must not drop the overlay over a live frame.
    mockApi.profileStatus.mockResolvedValue(ALIVE);
    const { result } = setup();
    await flush();
    act(() => sendConnectionState("connected"));

    mockApi.profileStatus.mockRejectedValue(new ApiError(502, "Bad Gateway"));
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(result.current.state).toBe("connected");

    // a definitive answer still acts. Past the resume-probe interval, so this
    // exercises the blip/definitive distinction rather than the rate limit —
    // which has its own tests.
    await advance(6_000);
    mockApi.profileStatus.mockRejectedValue(new ApiError(404, "Not found"));
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(result.current.state).toBe("session-ended");
  });

  it("stops the heartbeat once the session is over", async () => {
    mockApi.profileStatus.mockResolvedValue(ALIVE);
    const { result } = setup();
    await flush();
    act(() => sendConnectionState("connected"));
    act(() => sendConnectionState("disconnected"));
    expect(result.current.state).toBe("reconnecting");

    mockApi.profileStatus.mockResolvedValue({
      status: "stopped",
      xvnc_alive: null,
      browser_alive: null,
    });
    await advance(250);
    expect(result.current.state).toBe("session-ended");

    const probes = mockApi.profileStatus.mock.calls.length;
    await advance(200_000);
    expect(mockApi.profileStatus.mock.calls.length).toBe(probes);
  });

  it("re-establishes after an outage instead of trusting a green dot", async () => {
    // The socket can go half-open across an outage without ever emitting a
    // close. /status says the profile is alive — which says nothing about OUR
    // connection — so the viewer would sit on "Connected" over a frozen frame
    // with no watchdog, no retry timer and no overlay.
    mockApi.profileStatus.mockResolvedValue(ALIVE);
    const { result } = setup();
    await flush();
    act(() => sendConnectionState("connected"));
    expect(result.current.state).toBe("connected");

    act(() => {
      window.dispatchEvent(new Event("offline"));
    });
    mockApi.createViewerToken.mockResolvedValueOnce(TOK2);
    await act(async () => {
      window.dispatchEvent(new Event("online"));
    });

    expect(result.current.state).toBe("reconnecting");
    await advance(250);
    expect(result.current.state).toBe("connecting");
    expect(result.current.iframeSrc).toContain("tok-2");
  });

  it("re-establishes after an outage even when the confirming probe fails", async () => {
    // Tolerating transient errors must not swallow the one case where the
    // outage itself is the evidence: otherwise a green dot sits over a frozen
    // frame with nothing scheduled.
    mockApi.profileStatus.mockResolvedValue(ALIVE);
    const { result } = setup();
    await flush();
    act(() => sendConnectionState("connected"));

    act(() => {
      window.dispatchEvent(new Event("offline"));
    });
    mockApi.profileStatus.mockRejectedValue(new ApiError(502, "Bad Gateway"));
    await act(async () => {
      window.dispatchEvent(new Event("online"));
    });

    expect(result.current.state).toBe("reconnecting");
  });

  it("never parks with nothing scheduled when the online event is lost", async () => {
    // navigator.onLine stays false and no "online" ever fires. The loop must
    // still make progress rather than waiting forever on an event.
    const nav = navigator as unknown as { onLine: boolean };
    const realOnLine = Object.getOwnPropertyDescriptor(Navigator.prototype, "onLine");
    Object.defineProperty(nav, "onLine", { value: false, configurable: true });
    try {
      mockApi.profileStatus.mockResolvedValue(ALIVE);
      const { result } = await reachReconnecting();
      expect(result.current.offline).toBe(true);

      const probes = mockApi.profileStatus.mock.calls.length;
      await advance(35_000);
      expect(mockApi.profileStatus.mock.calls.length).toBeGreaterThan(probes);
    } finally {
      if (realOnLine) Object.defineProperty(Navigator.prototype, "onLine", realOnLine);
      Object.defineProperty(nav, "onLine", { value: true, configurable: true });
    }
  });

  it("an offline blip while connecting still reaches a state with a way out", async () => {
    // "connecting" renders no overlay, so if its watchdog is dropped and the
    // matching "online" never fires, the pane reads "Connecting…" forever.
    mockApi.profileStatus.mockResolvedValue(ALIVE);
    const { result } = setup();
    await flush();
    expect(result.current.state).toBe("connecting");

    act(() => {
      window.dispatchEvent(new Event("offline"));
    });
    await advance(15_000); // the watchdog must still fire
    expect(result.current.state).toBe("reconnecting");
  });

  it("becoming visible re-arms the watchdog for a throttled connect", async () => {
    // A hidden tab throttles timers, so the watchdog may not have run. Coming
    // back must leave a live deadline rather than an untimed "Connecting…".
    mockApi.profileStatus.mockResolvedValue(ALIVE);
    const { result } = setup();
    await flush();
    expect(result.current.state).toBe("connecting");

    setVisibility("hidden");
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    setVisibility("visible");
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
    });

    // still connecting, but with a fresh 15s deadline that actually fires
    expect(result.current.state).toBe("connecting");
    await advance(15_000);
    expect(result.current.state).toBe("reconnecting");
  });

  it("offline pauses the retry timer; online attempts immediately", async () => {
    const { result } = await reachReconnecting();
    expect(result.current.nextRetryAt).not.toBeNull();

    act(() => {
      window.dispatchEvent(new Event("offline"));
    });
    expect(result.current.offline).toBe(true);
    expect(result.current.nextRetryAt).toBeNull();

    // the fast backoff is paused (the slow offline re-check is covered by
    // "never parks with nothing scheduled when the online event is lost")
    await advance(25_000);
    expect(mockApi.profileStatus).not.toHaveBeenCalled();

    mockApi.createViewerToken.mockResolvedValueOnce(TOK2);
    await act(async () => {
      window.dispatchEvent(new Event("online"));
    });
    expect(result.current.offline).toBe(false);
    expect(mockApi.profileStatus).toHaveBeenCalled();
    expect(result.current.state).toBe("connecting");
  });

  it("coming back online while connected re-verifies the profile", async () => {
    mockApi.profileStatus.mockResolvedValue({
      status: "stopped",
      xvnc_alive: null,
      browser_alive: null,
    });
    const view = setup();
    await flush();
    act(() => sendConnectionState("connected"));

    act(() => {
      window.dispatchEvent(new Event("offline"));
    });
    await act(async () => {
      window.dispatchEvent(new Event("online"));
    });

    // the profile died while we were offline — don't sit on a green dot
    expect(mockApi.profileStatus).toHaveBeenCalledTimes(1);
    expect(view.result.current.state).toBe("session-ended");
  });

  it("becoming visible while connected runs a status probe", async () => {
    mockApi.profileStatus.mockResolvedValue(ALIVE);
    const view = setup();
    await flush();
    act(() => sendConnectionState("connected"));

    setVisibility("hidden");
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(mockApi.profileStatus).not.toHaveBeenCalled();

    setVisibility("visible");
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(mockApi.profileStatus).toHaveBeenCalledTimes(1);
    expect(view.result.current.state).toBe("connected");
  });

  it("becoming visible while reconnecting attempts immediately", async () => {
    const { result } = await reachReconnecting();
    expect(mockApi.profileStatus).not.toHaveBeenCalled();

    mockApi.createViewerToken.mockResolvedValueOnce(TOK2);
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(mockApi.profileStatus).toHaveBeenCalledTimes(1);
    expect(result.current.state).toBe("connecting");
  });

  it("a slow resume probe cannot override a newer one", async () => {
    // online + visibilitychange both fire probeAfterResume. If the first,
    // slower reply lands last, a stale "stopped" would end a session the newer
    // probe already found healthy.
    let resolveStale: (v: unknown) => void = () => {};
    mockApi.profileStatus.mockReturnValueOnce(
      new Promise((r) => {
        resolveStale = r;
      }),
    );
    const view = setup();
    await flush();
    act(() => sendConnectionState("connected"));

    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange")); // probe 1 (stalls)
    });
    // Past the resume-probe interval so probe 2 is actually issued: the point
    // here is reply ORDERING between two concurrent probes, not the rate limit.
    // Measuring the interval from a probe's start (not its reply) is what lets
    // a stalled probe be superseded at all.
    await advance(6_000);
    mockApi.profileStatus.mockResolvedValue(ALIVE);
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange")); // probe 2 -> alive
    });
    expect(view.result.current.state).toBe("connected");

    // the stale probe now reports the profile gone — it must be ignored
    await act(async () => {
      resolveStale({ status: "stopped", xvnc_alive: null, browser_alive: null });
    });
    expect(view.result.current.state).toBe("connected");
  });

  it("a burst of tab switches costs one probe, not one per switch", async () => {
    mockApi.profileStatus.mockResolvedValue(ALIVE);
    const view = setup();
    await flush();
    act(() => sendConnectionState("connected"));

    for (let i = 0; i < 20; i++) {
      await act(async () => {
        document.dispatchEvent(new Event("visibilitychange"));
      });
    }
    expect(mockApi.profileStatus).toHaveBeenCalledTimes(1);
    expect(view.result.current.state).toBe("connected");

    // ...and the rate limit is a floor on the gap, not a one-shot latch.
    await advance(6_000);
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(mockApi.profileStatus).toHaveBeenCalledTimes(2);
  });

  it("the heartbeat is never suppressed by the resume-probe rate limit", async () => {
    // The heartbeat is the only timer `connected` has. If a tab switch could
    // eat it, a suppressed probe would have no successor.
    mockApi.profileStatus.mockResolvedValue(ALIVE);
    const view = setup();
    await flush();
    act(() => sendConnectionState("connected"));

    // switch tabs just before each heartbeat lands
    for (let i = 0; i < 3; i++) {
      await advance(44_000);
      await act(async () => {
        document.dispatchEvent(new Event("visibilitychange"));
      });
      await advance(1_000);
    }
    // 3 tab switches + 3 heartbeats, minus the heartbeats within 5s of a switch
    expect(mockApi.profileStatus.mock.calls.length).toBeGreaterThanOrEqual(3);
    expect(view.result.current.state).toBe("connected");
  });

  it("a drop while connected always probes, however recent the last one", async () => {
    // The rate limit must not swallow THE probe that matters: offline->online
    // under a live socket is the one case where no successor re-establishes it.
    mockApi.profileStatus.mockResolvedValue(ALIVE);
    const view = setup();
    await flush();
    act(() => sendConnectionState("connected"));

    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(mockApi.profileStatus).toHaveBeenCalledTimes(1);

    // no time passes: a plain trigger here would be suppressed
    act(() => {
      window.dispatchEvent(new Event("offline"));
    });
    await act(async () => {
      window.dispatchEvent(new Event("online"));
    });
    expect(mockApi.profileStatus).toHaveBeenCalledTimes(2);
    // ...and it re-establishes rather than trusting the process verdict
    expect(view.result.current.state).toBe("reconnecting");
  });

  it("resume probe that finds the profile stopped ends the session", async () => {
    mockApi.profileStatus.mockResolvedValue({
      status: "stopped",
      xvnc_alive: null,
      browser_alive: null,
    });
    const view = setup();
    await flush();
    act(() => sendConnectionState("connected"));

    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(view.result.current.state).toBe("session-ended");
    expect(view.result.current.endReason).toBe("Browser session ended");
  });
});

// ── debug log ───────────────────────────────────────────────────────────────

describe("debug log", () => {
  it("records state transitions with timestamps", async () => {
    mockApi.profileStatus.mockResolvedValue(ALIVE);
    const { result } = setup();
    await flush();
    act(() => sendConnectionState("connected"));
    act(() => sendConnectionState("disconnected"));

    const transitions = result.current.debugLog.map((e) => `${e.from}->${e.to}`);
    expect(transitions).toEqual([
      "idle->connecting",
      "connecting->connected",
      "connected->reconnecting",
    ]);
    expect(result.current.debugLog.every((e) => typeof e.at === "number")).toBe(true);
  });
});

// ── document identity gate ──────────────────────────────────────────────────

describe("document gate", () => {
  async function reachConnected() {
    const view = setup();
    await flush();
    act(() => bootDocument());
    act(() => sendConnectionState("connected"));
    return view;
  }

  /** connected → real drop → probe → fresh token: two live documents, one WindowProxy. */
  async function reachHandover() {
    mockApi.profileStatus.mockResolvedValue(ALIVE);
    const view = await reachConnected();
    act(() => sendConnectionState("disconnected"));
    mockApi.createViewerToken.mockResolvedValueOnce(TOK2);
    await advance(250);
    expect(view.result.current.state).toBe("connecting");
    expect(view.result.current.iframeSrc).toContain("tok-2");
    return view;
  }

  it("accepts the first document's reports before any init", async () => {
    // Nothing else can be posting yet, so there is no ambiguity to resolve —
    // and gating the very first load would mean a client that never reaches
    // "init" could never connect at all.
    mockApi.profileStatus.mockResolvedValue(ALIVE);
    const { result } = setup();
    await flush();
    act(() => sendConnectionState("connected"));
    expect(result.current.state).toBe("connected");
  });

  it("ignores a 'connected' from the outgoing document until the incoming one boots", async () => {
    const { result } = await reachHandover();
    const tokens = mockApi.createViewerToken.mock.calls.length;

    // the outgoing document's own 2s retry succeeds one beat too late
    act(() => sendConnectionState("connected"));
    expect(result.current.state).toBe("connecting");

    // ...and its unload then posts the matching close, which used to abort the
    // cycle that was about to succeed and mint yet another token
    act(() => sendConnectionState("disconnected"));
    expect(result.current.state).toBe("connecting");
    expect(result.current.attempt).toBe(1);
    expect(mockApi.createViewerToken.mock.calls.length).toBe(tokens);

    // the replacement boots: its reports count from here
    act(() => bootDocument());
    act(() => sendConnectionState("connected"));
    expect(result.current.state).toBe("connected");
  });

  it("accepts noVNC_initialized as the handover too", async () => {
    const { result } = await reachHandover();
    act(() => sendMessage({ action: "noVNC_initialized", value: null }));
    act(() => sendConnectionState("connected"));
    expect(result.current.state).toBe("connected");
  });

  it("ignores a stale idle_session_timeout from the outgoing document", async () => {
    const { result } = await reachHandover();
    act(() => sendMessage({ action: "idle_session_timeout", value: "Idle" }));
    expect(result.current.state).toBe("connecting");
    expect(result.current.attempt).toBe(1);

    // the live document's idle report is still acted on
    act(() => bootDocument());
    act(() => sendMessage({ action: "idle_session_timeout", value: "Idle" }));
    expect(result.current.state).toBe("reconnecting");
    expect(result.current.attempt).toBe(2);
  });

  it("keeps the attempt counter monotonic across a stale connected/disconnected pair", async () => {
    // Support reads this number off the overlay; a message from a document we
    // are in the middle of replacing must not reset it to 0.
    const { result } = await reachHandover();
    expect(result.current.attempt).toBe(1);
    act(() => sendConnectionState("connected"));
    expect(result.current.attempt).toBe(1);
    act(() => sendConnectionState("disconnected"));
    expect(result.current.attempt).toBe(1);
    expect(result.current.debugLog.map((e) => `${e.from}->${e.to}`)).toEqual([
      "idle->connecting",
      "connecting->connected",
      "connected->reconnecting",
      "reconnecting->connecting",
    ]);
  });

  it("a flapping outgoing document cannot defeat the data-plane budget", async () => {
    // Every accepted stale 'connected' used to reset aliveReconnects, so
    // MAX_ALIVE_RECONNECTS (10) was unreachable: an unbounded loop minting a
    // viewer token per lap that never reached a terminal state.
    mockApi.profileStatus.mockResolvedValue(ALIVE);
    const { result } = await reachConnected();
    act(() => sendConnectionState("disconnected"));

    for (let i = 0; i < 60 && result.current.state !== "session-ended"; i++) {
      await advance(15_000); // backoff/probe → alive → fresh token
      await advance(15_000); // connect watchdog
      act(() => sendConnectionState("connected"));
      act(() => sendConnectionState("disconnected"));
    }

    expect(result.current.state).toBe("session-ended");
    expect(result.current.endReason).toContain("Can't reach this browser session");
    expect(mockApi.createViewerToken.mock.calls.length).toBeLessThanOrEqual(12);
  });
});

// ── classification ──────────────────────────────────────────────────────────

describe("classification", () => {
  async function reachReconnecting() {
    const view = setup();
    await flush();
    act(() => sendConnectionState("connected"));
    act(() => sendConnectionState("disconnected"));
    return view;
  }

  it("profile stopping → session-ended with the shutting-down reason", async () => {
    mockApi.profileStatus.mockResolvedValue(STOPPING);
    const { result } = await reachReconnecting();

    await advance(250);
    expect(result.current.state).toBe("session-ended");
    expect(result.current.endReason).toBe("Browser session is shutting down");

    await advance(120_000);
    expect(mockApi.profileStatus).toHaveBeenCalledTimes(1);
  });

  it("never treats 'stopping' as transient", async () => {
    // Routing it to the "starting" branch spins forever by construction: that
    // branch has no failure budget and only a user action ends a teardown.
    // Meanwhile /viewer-token 404s and /viewer-auth 403s for this profile, so
    // there is no path from here back to a usable session.
    mockApi.profileStatus.mockResolvedValue(STOPPING);
    const { result } = await reachReconnecting();

    await advance(250);
    await advance(1_000);
    await advance(2_000);
    expect(result.current.state).not.toBe("reconnecting");
    expect(result.current.state).toBe("session-ended");
  });

  it("resume probe that finds the profile stopping ends the session", async () => {
    mockApi.profileStatus.mockResolvedValue(STOPPING);
    const view = setup();
    await flush();
    act(() => sendConnectionState("connected"));

    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(view.result.current.state).toBe("session-ended");
    expect(view.result.current.endReason).toBe("Browser session is shutting down");
  });

  it("running but browser dead → escalates with the browser message", async () => {
    mockApi.profileStatus.mockResolvedValue(BROWSER_DEAD);
    const { result } = await reachReconnecting();

    await advance(250); // probe 1
    expect(result.current.state).toBe("reconnecting");
    await advance(1_000); // probe 2
    expect(result.current.state).toBe("reconnecting");
    await advance(2_000); // probe 3 → escalate
    expect(result.current.state).toBe("session-ended");
    expect(result.current.endReason).toBe("Browser process stopped");
    expect(mockApi.profileStatus).toHaveBeenCalledTimes(3);
  });

  it("alternating xvnc-dead / browser-dead still escalates", async () => {
    // A dying box answers these alternately (the browser check is a real CDP
    // round-trip that times out intermittently). A per-class counter resets on
    // every flip, so the escalation was unreachable and no other budget
    // applies to these verdicts — the machine retried for the life of the tab.
    let flip = false;
    mockApi.profileStatus.mockImplementation(async () => {
      flip = !flip;
      return flip ? XVNC_DEAD : BROWSER_DEAD;
    });
    const { result } = await reachReconnecting();

    for (let i = 0; i < 20 && result.current.state !== "session-ended"; i++) {
      await advance(20_000);
    }
    expect(result.current.state).toBe("session-ended");
    expect(["Display server stopped", "Browser process stopped"]).toContain(
      result.current.endReason,
    );
  });
});

// ── escape hatches and failure bounds ───────────────────────────────────────

describe("escape hatches", () => {
  async function reachStalledReconnect() {
    mockApi.profileStatus.mockReturnValue(new Promise(() => {})); // never settles
    const view = setup();
    await flush();
    act(() => sendConnectionState("connected"));
    act(() => sendConnectionState("disconnected"));
    return view;
  }

  it("keeps the degraded button visible across a manual retry", async () => {
    // The click used to destroy the only affordance "reconnecting" has:
    // clearAllTimers() plus degraded = false, and armDegradedTimer is reached
    // only from scheduleReconnect — which an outstanding probe never reaches.
    const { result } = await reachStalledReconnect();
    await advance(60_000);
    expect(result.current.degraded).toBe(true);

    await act(async () => {
      result.current.reconnectNow();
    });
    expect(result.current.degraded).toBe(true);

    await advance(600_000);
    expect(result.current.state).toBe("reconnecting");
    expect(result.current.degraded).toBe(true);
  });

  it("a probe started by reconnectNow gets its own degraded deadline", async () => {
    const { result } = await reachStalledReconnect();
    await advance(10_000); // probe outstanding, not degraded yet
    expect(result.current.degraded).toBe(false);

    await act(async () => {
      result.current.reconnectNow();
    });
    expect(result.current.degraded).toBe(false);

    await advance(60_000);
    expect(result.current.state).toBe("reconnecting");
    expect(result.current.degraded).toBe(true);
  });

  it("exposes that a probe is in flight while the countdown is blank", async () => {
    // nextRetryAt is null for the whole probe (up to the 15s abort budget), so
    // the overlay drops its countdown and renders nothing in its place.
    let resolveProbe: (v: unknown) => void = () => {};
    mockApi.profileStatus.mockReturnValue(
      new Promise((r) => {
        resolveProbe = r;
      }),
    );
    const view = setup();
    await flush();
    act(() => sendConnectionState("connected"));
    act(() => sendConnectionState("disconnected"));
    expect(view.result.current.probing).toBe(false);

    await advance(250); // retry fires → probe starts
    expect(view.result.current.nextRetryAt).toBeNull();
    expect(view.result.current.probing).toBe(true);

    // ...and it clears again as soon as the probe has produced a decision
    await act(async () => {
      resolveProbe(XVNC_DEAD);
    });
    expect(view.result.current.probing).toBe(false);
    expect(view.result.current.nextRetryAt).not.toBeNull();
  });

  it("escalates after repeated total loss of the control plane", async () => {
    // One failure is no evidence (the viewer socket never touches FastAPI),
    // but nothing bounded the run: a dead uplink that fires no "offline" event
    // left the machine on a green dot, failing every heartbeat in silence.
    mockApi.profileStatus.mockResolvedValue(ALIVE);
    const { result } = setup();
    await flush();
    act(() => sendConnectionState("connected"));

    mockApi.profileStatus.mockRejectedValue(new TypeError("Failed to fetch"));
    await advance(45_000);
    expect(result.current.state).toBe("connected");
    await advance(45_000);
    expect(result.current.state).toBe("connected");
    await advance(45_000);
    expect(result.current.state).toBe("reconnecting");
  });

  it("does not escalate when a failed heartbeat is followed by a good one", async () => {
    // Guards the counter against being implemented as "escalate on any failure".
    mockApi.profileStatus.mockResolvedValue(ALIVE);
    const { result } = setup();
    await flush();
    act(() => sendConnectionState("connected"));

    for (let i = 0; i < 4; i++) {
      mockApi.profileStatus.mockRejectedValueOnce(new ApiError(502, "Bad Gateway"));
      await advance(45_000);
      expect(result.current.state).toBe("connected");
      await advance(45_000); // a good probe resets the streak
      expect(result.current.state).toBe("connected");
    }
  });

  it("a halted session is not restarted by a tab switch or an online event", async () => {
    mockApi.createViewerToken.mockRejectedValue(new ApiError(401, "Unauthorized"));
    mockApi.profileStatus.mockRejectedValue(new ApiError(401, "Unauthorized"));
    const { result } = setup();
    await flush();
    expect(mockApi.createViewerToken).toHaveBeenCalledTimes(1);

    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await advance(60_000);
    await act(async () => {
      window.dispatchEvent(new Event("online"));
    });
    await advance(60_000);

    expect(mockApi.createViewerToken).toHaveBeenCalledTimes(1);
    expect(mockApi.profileStatus).not.toHaveBeenCalled();
    expect(result.current.state).toBe("fatal");
  });

  it("an online burst does not restart a connect that has only just started", async () => {
    // Each restart minted a token, reloaded the whole client and re-armed the
    // watchdog, so a connect could never finish while the events kept coming.
    mockApi.profileStatus.mockResolvedValue(ALIVE);
    const { result } = setup();
    await flush();
    expect(result.current.state).toBe("connecting");

    await act(async () => {
      for (let i = 0; i < 20; i++) window.dispatchEvent(new Event("online"));
    });
    expect(mockApi.createViewerToken.mock.calls.length).toBeLessThanOrEqual(2);

    // ...but a connect that has been sitting there does get restarted
    mockApi.createViewerToken.mockResolvedValueOnce(TOK2);
    await advance(5_000);
    await act(async () => {
      window.dispatchEvent(new Event("online"));
    });
    expect(result.current.iframeSrc).toContain("tok-2");
  });

  it("a superseded probe's tail does not discard its replacement's verdict", async () => {
    // The abandoned probe re-arms "so a consumed retry always has a successor"
    // — but that bumps `generation`, which makes the live replacement stale and
    // throws away the connect() it was about to run.
    mockApi.profileStatus.mockResolvedValue(ALIVE);
    const view = setup();
    await flush();
    act(() => sendConnectionState("connected"));
    act(() => sendConnectionState("disconnected"));

    let resolveFirst: (v: unknown) => void = () => {};
    mockApi.profileStatus.mockReturnValueOnce(
      new Promise((r) => {
        resolveFirst = r;
      }),
    );
    await advance(250); // probe 1 starts and hangs

    let resolveSecond: (v: unknown) => void = () => {};
    mockApi.profileStatus.mockReturnValueOnce(
      new Promise((r) => {
        resolveSecond = r;
      }),
    );
    await act(async () => {
      view.result.current.reconnectNow(); // probe 2 starts and hangs
    });

    await act(async () => {
      resolveFirst(ALIVE); // the abandoned probe finally answers
    });
    mockApi.createViewerToken.mockResolvedValueOnce(TOK2);
    await act(async () => {
      resolveSecond(ALIVE);
    });

    expect(view.result.current.state).toBe("connecting");
    expect(view.result.current.iframeSrc).toContain("tok-2");
  });
});

// ── client self-reconnect grace window ──────────────────────────────────────

describe("client self-reconnect grace", () => {
  async function reachConnected() {
    mockApi.profileStatus.mockResolvedValue(ALIVE);
    const view = setup();
    await flush();
    act(() => sendConnectionState("connected"));
    return view;
  }

  it("takes over when the client's own retry does not recover", async () => {
    const { result } = await reachConnected();
    act(() => sendConnectionState("reconnecting"));
    expect(result.current.state).toBe("connected"); // client owns it for now

    await advance(12_000); // CLIENT_RECONNECT_GRACE_MS
    expect(result.current.state).toBe("reconnecting");
    expect(result.current.debugLog.at(-1)?.reason).toBe(
      "client self-reconnect did not recover",
    );
  });

  it("stands down when the client recovers inside the grace window", async () => {
    const { result } = await reachConnected();
    const tokens = mockApi.createViewerToken.mock.calls.length;

    act(() => sendConnectionState("reconnecting"));
    await advance(5_000);
    act(() => sendConnectionState("connected")); // same document, no new init
    expect(result.current.state).toBe("connected");
    expect(result.current.attempt).toBe(0);

    // the takeover timer must be disarmed, not merely outrun
    await advance(30_000);
    expect(result.current.state).toBe("connected");
    expect(mockApi.createViewerToken.mock.calls.length).toBe(tokens);
  });
});

// ── state-machine invariant ─────────────────────────────────────────────────

describe("liveness invariant", () => {
  /**
   * No non-terminal state may be reachable with nothing armed and nothing for
   * the user to click. Terminal states are exempt: ProfileViewer renders their
   * endReason with "Try again" (reconnectNow) and "Back to profile".
   */
  function assertHasAWayOut(state: string, degraded: boolean, endReason: string | null) {
    if (state === "session-ended" || state === "fatal") {
      expect(endReason).not.toBeNull();
      return;
    }
    expect(state).not.toBe("idle"); // only pre-start(); unreachable afterwards
    expect(vi.getTimerCount() > 0 || degraded).toBe(true);
  }

  const check = (r: { current: { state: string; degraded: boolean; endReason: string | null } }) =>
    assertHasAWayOut(r.current.state, r.current.degraded, r.current.endReason);

  it("connecting, connected and reconnecting always have a live timer", async () => {
    mockApi.profileStatus.mockResolvedValue(ALIVE);
    const { result } = setup();
    await flush();
    expect(result.current.state).toBe("connecting"); // watchdog
    check(result);

    act(() => sendConnectionState("connected")); // heartbeat + stable timer
    expect(result.current.state).toBe("connected");
    check(result);

    act(() => sendConnectionState("disconnected")); // retry + degraded timers
    expect(result.current.state).toBe("reconnecting");
    check(result);

    await advance(30_000); // through a probe/connect cycle
    check(result);
  });

  it("reconnecting has a way out with a probe outstanding, before and after a manual retry", async () => {
    mockApi.profileStatus.mockReturnValue(new Promise(() => {}));
    const { result } = setup();
    await flush();
    act(() => sendConnectionState("connected"));
    act(() => sendConnectionState("disconnected"));
    await advance(250); // probe started, nothing else scheduled but the deadline
    check(result);

    await advance(60_000); // deadline fires → the button is the way out
    expect(result.current.degraded).toBe(true);
    check(result);

    await act(async () => {
      result.current.reconnectNow();
    });
    check(result);
    await advance(600_000);
    check(result);
  });

  it("offline reconnecting keeps the slow re-check armed", async () => {
    const nav = navigator as unknown as { onLine: boolean };
    const realOnLine = Object.getOwnPropertyDescriptor(Navigator.prototype, "onLine");
    Object.defineProperty(nav, "onLine", { value: false, configurable: true });
    try {
      mockApi.profileStatus.mockResolvedValue(ALIVE);
      const { result } = setup();
      await flush();
      act(() => sendConnectionState("connected"));
      act(() => sendConnectionState("disconnected"));
      expect(result.current.offline).toBe(true);
      expect(result.current.nextRetryAt).toBeNull();
      check(result);
    } finally {
      if (realOnLine) Object.defineProperty(Navigator.prototype, "onLine", realOnLine);
      Object.defineProperty(nav, "onLine", { value: true, configurable: true });
    }
  });

  it("terminal states always carry a message for their overlay", async () => {
    mockApi.profileStatus.mockResolvedValue(STOPPED);
    const { result } = setup();
    await flush();
    act(() => sendConnectionState("connected"));
    act(() => sendConnectionState("disconnected"));
    await advance(250);
    expect(result.current.state).toBe("session-ended");
    check(result);

    mockApi.createViewerToken.mockRejectedValue(new ApiError(400, "Bad Request"));
    await act(async () => {
      result.current.reconnectNow();
    });
    expect(result.current.state).toBe("fatal");
    check(result);
  });
});
