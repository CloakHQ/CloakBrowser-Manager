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
    expect(result.current.state).toBe("connecting");
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

    // once the new document commits, its reports count again
    act(() => result.current.handleIframeLoad());
    act(() => sendConnectionState("disconnected"));
    expect(result.current.state).toBe("reconnecting");
    expect(result.current.attempt).toBe(2);
    expect(mockApi.createViewerToken.mock.calls.length).toBe(tokensBefore);
  });

  it("a successful connection clears the terminal-classification counter", async () => {
    // Two xvnc-dead verdicts, then the CLIENT reconnects on its own (no probe
    // in between, so nothing else resets the counter). A later unrelated drop
    // must not have its first probe treated as the third in a row.
    const XVNC_DEAD = { status: "running", xvnc_alive: false, browser_alive: true };
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

    // a definitive answer still acts
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
