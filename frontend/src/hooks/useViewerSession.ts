import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, type ProfileStatus } from "../lib/api";

/**
 * Manager-owned reconnect state machine for the embedded KasmVNC 1.5.0 client.
 *
 * Why this exists: the KasmVNC client's built-in `reconnect=true` only retries
 * CLEAN disconnects (verified in assets/ui-*.js, `disconnectFinished`:
 * `if (!n.detail.clean) updateVisualState("disconnected")` — no retry).
 * Unclean drops (e.g. WebSocket close 1006) go straight to `disconnected` and
 * never recover on their own, so the Manager owns real reconnect here.
 *
 * Verified postMessage contract (KasmVNC 1.5.0, assets/ui-BOjwDkC7.js):
 *   Outbound (iframe → parent), all `parent.postMessage({action, value}, "*")`:
 *     updateVisualState(n):  { action: "connection_state", value: n }
 *                            n ∈ init|connecting|connected|disconnecting|disconnected|reconnecting
 *     boot:                  { action: "noVNC_initialized", value: null }
 *     clipboardRx:           { action: "clipboardrx", value: <text> }
 *     disconnectedRx:        { action: "disconnectrx", value: <reason> }
 *     idle timeout:          { action: "idle_session_timeout", value: "Idle session timeout exceeded" }
 *     (plus control_open/control_close/togglenav/fullscreen/enable_audio/
 *      smartcard_status/can_control_displays — not used here)
 *   Inbound (parent → iframe), receiveMessage switch on `data.action`:
 *     "clipboardsnd"     → rfb.clipboardPasteFrom(data.value)
 *     "setvideoquality"  → parseInt(data.qualityLevel ?? data.value)
 *     "resize"           → forceSetting("resize", data.value) + applyResizeMode()
 *     "terminate"        → rfb.terminate()
 *     "set_perf_stats"   → forceSetting("enable_perf_stats", data.value)
 *     "set_idle_timeout" → idle_disconnect = ceil(data.value / 60)
 *     "enable_hidpi", "enable_threading", "control_displays" (replies
 *     "can_control_displays"), "set_streaming_mode", "set_gop",
 *     "show_keyboard_controls"/"hide_keyboard_controls" (separate listener)
 *   Graceful server disconnect: disconnectedRx schedules
 *   `window.location.replace("disconnected.html")` after 3s when
 *   `detail.serverNotice.graceful` — an iframe load ending in
 *   `/disconnected.html` is therefore a terminal server-side disconnect.
 */

export type ViewerState =
  | "idle"
  | "connecting"
  | "connected"
  | "reconnecting"
  | "session-ended"
  | "fatal";

export interface DebugEntry {
  at: number;
  from: ViewerState;
  to: ViewerState;
  reason: string;
}

export interface ViewerSessionSnapshot {
  state: ViewerState;
  iframeSrc: string | null;
  /** navigator.onLine went false — timers paused until "online". */
  offline: boolean;
  /** reconnecting continuously for >60s — show "Still trying…" UI. */
  degraded: boolean;
  /** consecutive reconnect attempts since last stable connection. */
  attempt: number;
  /** epoch ms when the next reconnect attempt fires (null = none scheduled). */
  nextRetryAt: number | null;
  /**
   * a status probe is outstanding. nextRetryAt is null for its whole duration
   * (up to the 15s api.ts abort budget), so without this the reconnect overlay
   * drops its countdown and renders nothing in its place — the UI reads as
   * frozen exactly while the machine is working.
   */
  probing: boolean;
  /** terminal message for session-ended / fatal overlays. */
  endReason: string | null;
  /** ring of the last ~20 state transitions, for support. */
  debugLog: DebugEntry[];
}

export interface UseViewerSessionOptions {
  profileId: string;
  clipboardSync: boolean;
  /** injectable RNG for deterministic backoff jitter in tests. */
  random?: () => number;
}

const CONNECT_WATCHDOG_MS = 15_000;
/** Client retries clean drops every reconnect_delay (2s); take over if it can't. */
const CLIENT_RECONNECT_GRACE_MS = 12_000;
const STABLE_RESET_MS = 30_000;
const DEGRADED_AFTER_MS = 60_000;
const BACKOFF_STEPS_MS = [250, 1_000, 2_000, 4_000, 8_000, 15_000];
/**
 * Consecutive NON-alive process verdicts before we call it. Counted across
 * classes, not per class: a box whose Xvnc has died and whose Chromium is dying
 * with it answers xvnc-dead and browser-dead alternately (the browser probe is
 * a real CDP round-trip and times out intermittently), and a per-class counter
 * resets on every flip — MAX_NON_ALIVE_PROBES is then unreachable and no other
 * budget applies to these verdicts, so the machine retries forever.
 */
const MAX_NON_ALIVE_PROBES = 3;
/**
 * Consecutive failures of the connected-state probe before we stop believing
 * the green dot. One failure says nothing (the viewer socket is
 * browser -> nginx -> Xvnc and never touches FastAPI), but total inability to
 * reach the control plane over several minutes is itself evidence — without a
 * bound, a dead uplink that never fires an "offline" event leaves the machine
 * parked in `connected` forever, failing every heartbeat in silence.
 */
const MAX_CONNECTED_PROBE_FAILURES = 3;
/**
 * How long a connect must have been running before an "online" event is allowed
 * to restart it. A VPN flap or Wi-Fi roam emits a burst of "online"; restarting
 * per event mints a token and reloads the whole client each time, and re-arms
 * the watchdog, so the connect can never finish. A young connect needs no help:
 * it completes, or its watchdog lands it in "reconnecting", which retries.
 */
const CONNECT_RESTART_MIN_AGE_MS = 3_000;
/**
 * Failure budget for "the Manager says this profile is fine, but we still
 * can't connect". Nothing in `/status` can observe the viewer data plane
 * (nginx, the token, Kasm's HTTP/WS listener), so without a budget a broken
 * data plane is an unbounded retry loop that mints a token per attempt and
 * never reaches a terminal state. ~10 attempts ≈ 2-3 minutes with the
 * watchdog and backoff — long enough to ride out a real outage.
 */
const MAX_ALIVE_RECONNECTS = 10;
const UNREACHABLE_REASON =
  "Can't reach this browser session — it's running, but the display isn't responding";
const AUTH_EXPIRED_REASON = "Your session expired — sign in again";
/**
 * navigator.onLine is a weak signal (often just "an interface exists"), and the
 * matching "online" event is not guaranteed to arrive. Re-check on a slow timer
 * so the loop never parks with nothing scheduled waiting for an event.
 */
const OFFLINE_RECHECK_MS = 30_000;
/**
 * "connected" is the only non-terminal state with no timer of its own: no
 * watchdog, no retry, and no overlay offering a way out. Every other state
 * either counts down or shows the user a button. A silent half-open socket, or
 * an offline blip whose "online" never arrives, would otherwise leave a green
 * dot over a frozen frame indefinitely. Re-verify on a slow beat.
 */
const CONNECTED_HEARTBEAT_MS = 45_000;
/**
 * Floor on the gap between two resume probes.
 *
 * probeAfterResume orders its REPLIES (resumeSeq) but had no admission control,
 * so every trigger issued a request: "online" bursts during a VPN flap and
 * ordinary tab switching each cost one control-plane round-trip, and a user
 * cycling tabs produced one probe per switch for the life of the session.
 * Coalescing on an in-flight probe would not fix that — when the control plane
 * answers quickly each switch still finds no probe running — so the bound has
 * to be on the rate, not on concurrency.
 *
 * Skipping is safe because `connected` always has the heartbeat interval armed,
 * so a suppressed probe always has a successor. The one case where a skip would
 * lose information — the network dropped under a live socket — bypasses this
 * entirely; see droppedWhileConnected.
 *
 * Measured from the START of a probe, not its reply, so a probe that never
 * resolves stops blocking after one interval and the next trigger supersedes it.
 */
const RESUME_PROBE_MIN_INTERVAL_MS = 5_000;
/**
 * Consecutive "no viewer socket attached" readings before we act on them.
 *
 * /status can only prove the PROCESSES are alive. A half-open viewer socket
 * therefore leaves a green dot over a frozen frame until the client's keepalive
 * and TCP RTO eventually fire — minutes. /viewer-attached is the exact signal:
 * KasmVNC's bottleneckStats map is keyed by peerEndpoint, written per client
 * from VNCSConnectionST::sendStats and erased ONLY in ~VNCSConnectionST
 * (GetAPIMessager.cxx, KasmVNC 1.5.0) — no idle eviction, no wholesale clear,
 * and reads are non-destructive. So an empty map means every connection object
 * is gone.
 *
 * Two in a row rather than one because the entry does not exist until the first
 * writeUpdate() tick reaches sendStats, so a just-connected client reads empty
 * for a beat. The resume-probe rate limit puts >=5s between these readings.
 */
const MAX_DETACHED_PROBES = 2;
/** 4xx codes that mean "later", not "never". */
const RETRYABLE_4XX = new Set([408, 429]);
const DEBUG_LOG_LIMIT = 20;
/**
 * How often the suspend detector ticks. Under NORMAL operation (including
 * ordinary background-tab timer throttling) each tick observes a gap close
 * to this value — fake-timer tests confirm consecutive interval fires always
 * see exactly this period, since a discrete timer engine cannot skip queued
 * callbacks. A gap far larger than this is therefore not throttling; it is
 * evidence the JS engine itself stopped running (system sleep, a frozen
 * background tab, a mobile OS suspending the browser process) for that long.
 */
const SUSPEND_DETECTOR_INTERVAL_MS = 2_000;
/**
 * Gap size that counts as "we just resumed from a suspend". Ordinary Chrome
 * background-tab throttling can stretch a 2s interval to roughly a minute
 * once a tab has been hidden for several minutes — treating THAT as a resume
 * signal too is not a bug, it is exactly the reinforcement this detector
 * exists to add: a stale connection sitting in a throttled background tab
 * gets re-verified the moment its own timer finally gets to run, instead of
 * waiting on visibilitychange/online, neither of which is guaranteed to fire
 * around a real suspend on every platform.
 */
const SUSPEND_GAP_THRESHOLD_MS = 10_000;

/**
 * Iframe URL for the native client. `path` overrides the default root-absolute
 * `websockify` so the WS goes through the Manager's /viewer/<token>/ prefix.
 *
 * NOTE: `idle_disconnect=0` would NOT disable the idle timeout in this client —
 * `parseFloat("0")` is finite, so the 20-minute fallback is skipped and any
 * idle second trips `c > l` (ui bundle, session-timeout interval). A large
 * value is used instead to effectively disable it.
 */
export function buildViewerUrl(
  viewerUrl: string,
  token: string,
  clipboardSync: boolean,
  cacheBust = 0,
  streamMode?: string | null,
): string {
  const params = new URLSearchParams({
    path: `viewer/${token}/websockify`,
    autoconnect: "true",
    reconnect: "true",
    reconnect_delay: "2000",
    resize: "scale",
    idle_disconnect: "1440",
    enable_webp: "true",
    clipboard_up: String(clipboardSync),
    clipboard_down: String(clipboardSync),
    clipboard_seamless: String(clipboardSync),
    // ignored by the client; forces an iframe reload when a fresh token
    // happens to produce an identical URL
    _r: String(cacheBust),
  });
  // Only present when the server runs an NVENC codec. The client's automatic
  // selection cannot reach those at all — its candidate list is hardcoded to
  // the VAAPI and software variants, so against an NVENC-only server it
  // silently settles on JPEG/WebP and the GPU encoder never runs. This
  // parameter sets `forcedCodecs`, which is the one path that bypasses that
  // list. Left off otherwise on purpose: forcedCodecs is checked BEFORE the
  // client's own "fall back to image mode after an encoding error" branch, so
  // setting it unconditionally would disable that recovery for codecs the
  // client already picks correctly by itself.
  if (streamMode) params.set("kasmvnc_mode_preference", streamMode);
  return `${viewerUrl}?${params.toString()}`;
}

type Classification =
  | "alive"
  | "starting"
  | "xvnc-dead"
  | "browser-dead"
  | "stopping"
  | "stopped";
/** Classifications that can end the session; "alive"/"starting" never do. */
type TerminalClassification = "xvnc-dead" | "browser-dead" | "stopping" | "stopped";
/** Terminal on the FIRST probe, unlike the process probes below. */
type ControlPlaneTerminal = "stopping" | "stopped";

function classify(s: ProfileStatus): Classification {
  // "starting" (container restart, auto-launch queue) is transient by
  // definition — the profile is on its way up, not gone.
  if (s.status === "starting") return "starting";
  // Teardown in flight (a bounded close, or a wedged one). The Manager has
  // already dropped this profile out of `running`, so /viewer-token 404s and
  // /api/viewer-auth 403s: THIS session can never come back, however the
  // teardown ends. Treating it as transient — the shape of the "starting"
  // branch — spins in "reconnecting" with no exit, because that branch has no
  // failure budget at all and only a user action can end the teardown.
  // A relaunch remounts the viewer under a new key, so ending here costs
  // nothing.
  if (s.status === "stopping") return "stopping";
  if (s.status !== "running") return "stopped";
  if (s.xvnc_alive === false) return "xvnc-dead";
  if (s.browser_alive === false) return "browser-dead";
  return "alive";
}

const CLASSIFICATION_REASON: Record<TerminalClassification, string> = {
  stopped: "Browser session ended",
  stopping: "Browser session is shutting down",
  "xvnc-dead": "Display server stopped",
  "browser-dead": "Browser process stopped",
};

/**
 * The control plane is authoritative about these two, so one probe is enough.
 * xvnc-dead/browser-dead are process probes that can flap and get a streak
 * budget instead.
 */
const CONTROL_PLANE_TERMINAL: ReadonlySet<Classification> = new Set<Classification>([
  "stopped",
  "stopping",
]);

function isControlPlaneTerminal(cls: Classification): cls is ControlPlaneTerminal {
  return CONTROL_PLANE_TERMINAL.has(cls);
}

function isAuthError(err: unknown): boolean {
  return err instanceof ApiError && err.status === 401;
}

function isNotFound(err: unknown): boolean {
  return err instanceof ApiError && err.status === 404;
}

function errMsg(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

interface ControllerDeps {
  profileId: string;
  getClipboardSync: () => boolean;
  getIframe: () => HTMLIFrameElement | null;
  random: () => number;
  onChange: (snapshot: ViewerSessionSnapshot) => void;
}

interface Controller {
  start: () => void;
  handleMessage: (event: MessageEvent) => void;
  handleIframeLoad: () => void;
  reconnectNow: () => void;
  destroy: () => void;
}

/**
 * The Page Lifecycle API's "freeze"/"resume" document events. Not in every
 * TS DOM lib version (support is Chromium-led), so `document` is narrowed to
 * this shape locally rather than widening the global lib target for two
 * listeners. addEventListener with an unrecognised type is a silent no-op in
 * browsers that lack the API, so registering it unconditionally is safe.
 */
type PageLifecycleEventTarget = EventTarget & {
  addEventListener(type: "freeze" | "resume", listener: () => void): void;
  removeEventListener(type: "freeze" | "resume", listener: () => void): void;
};

function createViewerController(deps: ControllerDeps): Controller {
  const { profileId, getClipboardSync, getIframe, random, onChange } = deps;

  let state: ViewerState = "idle";
  let iframeSrc: string | null = null;
  let offline = typeof navigator !== "undefined" && !navigator.onLine;
  let degraded = false;
  let attempt = 0;
  let nextRetryAt: number | null = null;
  let probing = false;
  let endReason: string | null = null;
  const debugLog: DebugEntry[] = [];

  let destroyed = false;
  /**
   * Bumped by EVERY state-originating event, not just connect(): a token fetch
   * that resolves after the machine has moved on must not apply its result.
   * Anything awaiting re-checks this and bails if it no longer matches.
   */
  let generation = 0;
  let backoffLevel = 0;
  let connectSeq = 0;
  /** consecutive non-alive, non-starting probe verdicts (any mix of classes). */
  let nonAliveStreak = 0;
  /** consecutive "alive" probes that still failed to produce a connection. */
  let aliveReconnects = 0;
  /** consecutive failures of the probe that re-verifies a live connection. */
  let connectedProbeFailures = 0;
  /** Date.now() when the current connect cycle started (online debounce). */
  let connectStartedAt = 0;
  /** iframe reported its own clean-drop reconnect; we give it a grace window. */
  let clientReconnecting = false;
  /** a status probe is outstanding — re-entrant triggers must not stack. */
  let probeInFlight = false;
  /** identifies the current probe so a superseded one cannot clear the flag. */
  let probeSeq = 0;
  /** the network dropped while we were connected — the socket is suspect. */
  let droppedWhileConnected = false;
  /** orders concurrent resume probes so a stale reply cannot win. */
  let resumeSeq = 0;
  /** Date.now() when the last resume probe STARTED; null = none yet. */
  let lastResumeProbeAt: number | null = null;
  /**
   * Date.now() of the last time resyncAfterResume actually acted on the
   * "reconnecting"/"connecting" branches (see RESUME_PROBE_MIN_INTERVAL_MS
   * gate below). "connected" already rate-limits itself inside
   * probeAfterResume and is deliberately left out of this one — duplicating
   * a second gate there would mean reimplementing (and risking disagreeing
   * with) the droppedWhileConnected exemption that function already gets
   * right.
   */
  let lastResyncActionAt: number | null = null;
  /** consecutive definitive "no viewer attached" readings. */
  let detachedStreak = 0;
  /**
   * We have seen this connection reported as attached at least once.
   *
   * The gate that makes the detached signal fail-safe. bottleneckStats is only
   * ever written when the server has an apimessager; where it does not, the map
   * is never populated and /viewer-attached answers `false` forever — which,
   * acted on blindly, would tear down every healthy session on a fixed beat.
   * Requiring a confirmed `true` first means the signal can only fire where it
   * has already demonstrated it works, and reduces to a no-op everywhere else.
   */
  let sawViewerAttached = false;
  /** 401: retries are intentionally stopped; the safety net must not re-arm. */
  let halted = false;
  /**
   * Document identity for the embedded client.
   *
   * A same-frame navigation keeps the same WindowProxy, so
   * `event.source === iframe.contentWindow` cannot tell the OUTGOING document
   * from the incoming one. The outgoing one is not quiet: with
   * `reconnect=true` it keeps retrying every reconnect_delay until it unloads,
   * so it can post an ordinary "connected" (and then "disconnected") *after* we
   * have already re-pointed the iframe. Accepted, that pair flips the machine
   * to connected — clearing the watchdog, the attempt counter, the terminal
   * streak and the MAX_ALIVE_RECONNECTS budget — and straight back out, i.e. an
   * unbounded loop that mints a viewer token per lap and can never terminate.
   *
   * `generation` cannot be the identity: it is also the promise-cancellation
   * epoch, and every bump for that purpose silently opens the gate. The iframe
   * `load` event cannot either — it fires after subresources and genuinely
   * loses the race against the outgoing document's messages. The client's own
   * boot marker can: ui-BOjwDkC7.js runs `updateVisualState("init")` and posts
   * `noVNC_initialized` BEFORE `o.connect()`, exactly once per document (the
   * in-document reconnect() path re-enters connect() and never re-emits
   * either), so it strictly precedes any "connected" from that document and can
   * never arrive from an already-booted outgoing one.
   */
  let pendingDoc = 0;
  let liveDoc = 0;

  let watchdogTimer: ReturnType<typeof setTimeout> | null = null;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;
  let stableTimer: ReturnType<typeof setTimeout> | null = null;
  let heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  let degradedTimer: ReturnType<typeof setTimeout> | null = null;
  let clientGraceTimer: ReturnType<typeof setTimeout> | null = null;
  /** Runs for the controller's whole lifetime — see armSuspendDetector. */
  let suspendDetectorTimer: ReturnType<typeof setInterval> | null = null;
  let lastSuspendTickAt = Date.now();

  function clearTimer(id: ReturnType<typeof setTimeout> | null): null {
    if (id !== null) clearTimeout(id);
    return null;
  }

  function clearAllTimers() {
    if (heartbeatTimer !== null) {
      clearInterval(heartbeatTimer);
      heartbeatTimer = null;
    }
    watchdogTimer = clearTimer(watchdogTimer);
    retryTimer = clearTimer(retryTimer);
    stableTimer = clearTimer(stableTimer);
    degradedTimer = clearTimer(degradedTimer);
    clientGraceTimer = clearTimer(clientGraceTimer);
    // suspendDetectorTimer is deliberately NOT cleared here: it runs for the
    // whole controller lifetime (armed once in start(), disarmed only in
    // destroy()), independent of which per-cycle timers a state transition
    // clears — a suspend can happen from any state, including ones with no
    // other timer of their own.
  }

  /**
   * Detects "the JS engine itself was not running for a while" — a laptop
   * lid closing, a fully backgrounded/frozen tab, a mobile OS suspending the
   * browser process — none of which are guaranteed to fire "offline" or
   * visibilitychange on every platform. A setInterval's own queued callbacks
   * cannot be skipped by a live engine, so an observed gap far larger than
   * the interval itself is direct evidence time passed that this machine
   * could not react to. See resyncAfterResume for what happens next.
   */
  function armSuspendDetector() {
    if (suspendDetectorTimer !== null) return;
    lastSuspendTickAt = Date.now();
    suspendDetectorTimer = setInterval(() => {
      const now = Date.now();
      const gap = now - lastSuspendTickAt;
      lastSuspendTickAt = now;
      if (gap > SUSPEND_GAP_THRESHOLD_MS) {
        resyncAfterResume(`resumed after an apparent suspend (${Math.round(gap / 1000)}s gap)`);
      }
    }, SUSPEND_DETECTOR_INTERVAL_MS);
  }

  function disarmSuspendDetector() {
    if (suspendDetectorTimer !== null) {
      clearInterval(suspendDetectorTimer);
      suspendDetectorTimer = null;
    }
  }

  function emit() {
    onChange({
      state,
      iframeSrc,
      offline,
      degraded,
      attempt,
      nextRetryAt,
      probing,
      endReason,
      debugLog: [...debugLog],
    });
  }

  function transition(to: ViewerState, reason: string) {
    if (destroyed) return;
    if (state !== to) {
      debugLog.push({ at: Date.now(), from: state, to, reason });
      if (debugLog.length > DEBUG_LOG_LIMIT) debugLog.shift();
      console.debug(`[viewer] ${state} -> ${to}: ${reason}`);
      state = to;
    }
    emit();
  }

  /** Fetch a fresh viewer token and (re)point the iframe at it. */
  async function connect(reason: string) {
    const gen = ++generation;
    probing = false;
    connectStartedAt = Date.now();
    transition("connecting", reason);
    armConnectWatchdog();
    try {
      const tok = await api.createViewerToken(profileId);
      if (destroyed || gen !== generation) return;
      // Re-pointing the iframe starts a document handover: from here until the
      // incoming document posts its "init" there are two live documents behind
      // one WindowProxy, so nothing either of them says can be attributed. The
      // FIRST load is exempt — there is no outgoing document to confuse it
      // with, and closing the gate before any document has ever booted would
      // mean a client that never reaches "init" can never connect at all.
      if (iframeSrc !== null) pendingDoc += 1;
      iframeSrc = buildViewerUrl(
        tok.viewer_url, tok.token, getClipboardSync(), connectSeq++, tok.stream_mode,
      );
      emit();
    } catch (err) {
      if (destroyed || gen !== generation) return;
      if (isAuthError(err)) return haltForAuth();
      if (isNotFound(err)) return endSession(CLASSIFICATION_REASON.stopped);
      // other 4xx: the request itself is wrong — retrying is pointless.
      // 408/429 are the exceptions: they explicitly mean "try again", and the
      // status probe already retries them, so treating them as terminal here
      // ends a session the very next request would have recovered.
      if (err instanceof ApiError && err.status < 500 && !RETRYABLE_4XX.has(err.status)) {
        endReason = errMsg(err);
        return transition("fatal", `viewer-token rejected: ${errMsg(err)}`);
      }
      scheduleReconnect(`viewer-token fetch failed: ${errMsg(err)}`);
    }
  }

  function armConnectWatchdog() {
    // The halt has to hold: this is the one timer that can resurrect the retry
    // loop (it calls scheduleReconnect), and a resurrected loop hammers
    // endpoints that can only answer 401 again. haltForAuth() also makes the
    // state terminal, which blocks the same path today — this keeps the rule
    // attached to the flag rather than to one particular state.
    if (halted) return;
    watchdogTimer = clearTimer(watchdogTimer);
    watchdogTimer = setTimeout(() => {
      watchdogTimer = null;
      if (state === "connecting") {
        scheduleReconnect("connect watchdog fired (no connected within 15s)");
      }
    }, CONNECT_WATCHDOG_MS);
  }

  /** Enter reconnecting and arm the next attempt with backoff + full jitter. */
  function scheduleReconnect(reason: string) {
    if (destroyed || halted || state === "session-ended" || state === "fatal") return;
    probing = false;
    if (heartbeatTimer !== null) {
      clearInterval(heartbeatTimer);
      heartbeatTimer = null;
    }
    // A connect() whose token fetch is still in flight is now superseded —
    // without this its late result re-points the iframe from a dead cycle and
    // races the retry we are about to arm.
    generation += 1;
    watchdogTimer = clearTimer(watchdogTimer);
    stableTimer = clearTimer(stableTimer);
    clientGraceTimer = clearTimer(clientGraceTimer);
    clientReconnecting = false;
    retryTimer = clearTimer(retryTimer);
    attempt += 1;
    transition("reconnecting", reason);
    armDegradedTimer();

    if (typeof navigator !== "undefined" && !navigator.onLine) {
      offline = true;
      nextRetryAt = null; // no countdown while the browser says we're offline
      emit();
      // Slow re-check rather than waiting solely for "online": if that event
      // never fires (or navigator.onLine is simply wrong) the loop would sit
      // here forever with no timer armed.
      retryTimer = setTimeout(() => {
        retryTimer = null;
        void attemptReconnect();
      }, OFFLINE_RECHECK_MS);
      return;
    }
    offline = false;

    const base = BACKOFF_STEPS_MS[Math.min(backoffLevel, BACKOFF_STEPS_MS.length - 1)]!;
    backoffLevel += 1;
    const delay = Math.round(base * (0.5 + 0.5 * random())); // full jitter: 50–100%
    nextRetryAt = Date.now() + delay;
    emit();
    retryTimer = setTimeout(() => {
      retryTimer = null;
      void attemptReconnect();
    }, delay);
  }

  /** One reconnect attempt: classify profile liveness, then act. */
  async function attemptReconnect() {
    // The retry timer, "online" and visibilitychange can all fire this, and
    // state only changes after the first probe resolves — so without a guard
    // 2-3 probes run concurrently and EACH bumps nonAliveStreak. A single drop
    // would then reach MAX_NON_ALIVE_PROBES in one round and show a premature
    // "Display server stopped".
    //
    // The guard covers the PROBE ONLY. Holding it across connect() would let a
    // token fetch that outlives the 15s watchdog block the retry that watchdog
    // just armed — and a skipped retry re-arms nothing, parking the viewer in
    // "reconnecting" with no scheduled attempt. So connect() is returned as a
    // follow-up and run after the flag is released; it advances the state
    // synchronously, so no second probe can slip in behind it.
    if (destroyed || halted || state !== "reconnecting" || probeInFlight) return;
    // Every probe must be covered by the degraded deadline, however it was
    // started. armDegradedTimer() is otherwise only reached from
    // scheduleReconnect, so a probe started by reconnectNow() (which clears all
    // timers) leaves "reconnecting" with no timer, no countdown and no button —
    // the escape hatch destroyed by the click that used it.
    armDegradedTimer();
    const mySeq = ++probeSeq;
    probeInFlight = true;
    probing = true;
    let followUp: (() => Promise<void>) | null = null;
    try {
      followUp = await runReconnectProbe();
    } finally {
      // Only clear if we are still the current probe. reconnectNow() can
      // supersede a stuck one; without this, that abandoned probe's finally
      // would later clear the flag out from under its replacement.
      if (probeSeq === mySeq) {
        probeInFlight = false;
        probing = false;
      }
    }
    if (followUp) {
      await followUp();
      return;
    }
    // Invariant: a consumed retry always leaves a successor scheduled. If this
    // probe's result was discarded because the machine moved on mid-probe, the
    // retry that started it is already spent and whatever moved the machine may
    // have had its own retry skipped by the in-flight guard. Re-arm rather than
    // stall in "reconnecting" with nothing pending.
    //
    // Ownership is part of that invariant: a probe SUPERSEDED by reconnectNow()
    // has a live replacement which is already responsible for the schedule.
    // Re-arming anyway bumps `generation`, which makes that replacement stale,
    // so its verdict — and the connect() it was about to run — is thrown away.
    if (
      !destroyed &&
      !halted &&
      !offline &&
      state === "reconnecting" &&
      probeSeq === mySeq &&
      retryTimer === null
    ) {
      scheduleReconnect("probe ended without scheduling a retry");
    }
  }

  /** Returns a follow-up action to run once the probe guard is released. */
  async function runReconnectProbe(): Promise<(() => Promise<void>) | null> {
    const gen = generation;
    nextRetryAt = null;
    emit();

    let status: ProfileStatus;
    try {
      status = await api.profileStatus(profileId);
    } catch (err) {
      if (destroyed || gen !== generation) return null;
      if (isAuthError(err)) {
        haltForAuth();
        return null;
      }
      if (isNotFound(err)) {
        endSession(CLASSIFICATION_REASON.stopped);
        return null;
      }
      // Manager unreachable (or 5xx) — stay in reconnecting, keep backing off
      scheduleReconnect(`status probe failed: ${errMsg(err)}`);
      return null;
    }
    // the iframe may have recovered on its own while we were probing
    if (destroyed || gen !== generation || state !== "reconnecting") return null;

    const cls = classify(status);

    if (cls === "alive") {
      nonAliveStreak = 0;
      aliveReconnects += 1;
      if (aliveReconnects > MAX_ALIVE_RECONNECTS) {
        // The profile is healthy but the viewer data plane isn't. Say so
        // instead of retrying (and minting a token) forever.
        endSession(UNREACHABLE_REASON);
        return null;
      }
      return () => connect("profile alive; reconnecting with fresh token");
    }
    if (cls === "starting") {
      // Coming up (container restart / auto-launch). Keep waiting and do not
      // let it count toward the terminal escalation below. The data-plane
      // budget refers to the OLD instance's viewer path, which is gone —
      // carrying it over would terminate the new session early.
      nonAliveStreak = 0;
      aliveReconnects = 0;
      scheduleReconnect("profile is starting; waiting for it to come up");
      return null;
    }
    if (isControlPlaneTerminal(cls)) {
      endSession(CLASSIFICATION_REASON[cls]);
      return null;
    }

    // xvnc-dead / browser-dead: process probes, so give them a streak before
    // believing them. The streak counts ANY non-alive verdict — see
    // MAX_NON_ALIVE_PROBES — and the class only picks the message.
    nonAliveStreak += 1;
    if (nonAliveStreak >= MAX_NON_ALIVE_PROBES) {
      // display/browser dead 3 probes in a row while the record says running
      endSession(CLASSIFICATION_REASON[cls]);
    } else {
      scheduleReconnect(`profile running but ${cls}; retrying`);
    }
    return null;
  }

  function endSession(reason: string) {
    generation += 1;  // terminal: discard anything still in flight
    clearAllTimers();
    nextRetryAt = null;
    probing = false;
    endReason = reason;
    transition("session-ended", reason);
  }

  /** 401: the global unauthorized handler takes over; stop everything here. */
  function haltForAuth() {
    generation += 1;  // the app's 401 handler owns the session now
    halted = true;
    clearAllTimers();
    nextRetryAt = null;
    probing = false;
    endReason = AUTH_EXPIRED_REASON;
    // Halting from "connecting" left the machine in a state that renders no
    // overlay and no button with every timer cleared — nothing could ever move
    // again and the user had no way out inside the viewer. A terminal state is
    // what the halt actually is; reconnectNow() clears `halted`, so "Try again"
    // still works once the global 401 handler has signed the user back in.
    transition("fatal", "401 from manager; retries halted (session expired)");
  }

  function handleConnected() {
    const fromClientReconnect = state === "connected" && clientReconnecting;
    if (state !== "connecting" && state !== "reconnecting" && !fromClientReconnect) return;
    clientReconnecting = false;
    // The client is up. If our own connect() is mid-token-fetch, its result
    // would swap iframeSrc and tear down the connection that just recovered.
    generation += 1;
    watchdogTimer = clearTimer(watchdogTimer);
    retryTimer = clearTimer(retryTimer);
    clientGraceTimer = clearTimer(clientGraceTimer);
    degradedTimer = clearTimer(degradedTimer);
    degraded = false;
    offline = false;
    droppedWhileConnected = false;
    attempt = 0;
    aliveReconnects = 0;
    // A successful connection disproves whatever the last probes concluded.
    // Left standing, two earlier "xvnc-dead" verdicts would make the FIRST
    // probe of some later, unrelated drop the third in a row and end the
    // session outright.
    nonAliveStreak = 0;
    connectedProbeFailures = 0;
    // Both are per-connection: this is a different socket, so the attach
    // baseline has to be re-earned before the detached signal can fire again.
    detachedStreak = 0;
    sawViewerAttached = false;
    nextRetryAt = null;
    probing = false;
    transition("connected", "client reported connected");
    armConnectedHeartbeat();
    stableTimer = clearTimer(stableTimer);
    stableTimer = setTimeout(() => {
      stableTimer = null;
      if (state === "connected") {
        backoffLevel = 0;
        console.debug("[viewer] stable for 30s; backoff reset");
      }
    }, STABLE_RESET_MS);
  }

  /**
   * True while exactly one document can be speaking for the iframe. See
   * pendingDoc/liveDoc: between a re-point and the incoming document's "init"
   * the outgoing document is still live and still posting, and nothing in the
   * MessageEvent distinguishes them.
   */
  function fromLiveDocument(): boolean {
    return liveDoc === pendingDoc;
  }

  function handleConnectionState(value: unknown) {
    switch (value) {
      case "init":
        // The incoming document has booted. This is posted once per document,
        // strictly before the client can connect, so it — and only it — can
        // hand the gate over.
        liveDoc = pendingDoc;
        break;
      case "connected":
        handleConnected();
        break;
      case "reconnecting":
        // The client retries CLEAN drops itself every 2s. Give it a grace
        // window; if it can't get back, we take over.
        if (state === "connected" && !clientReconnecting) {
          clientReconnecting = true;
          clientGraceTimer = clearTimer(clientGraceTimer);
          clientGraceTimer = setTimeout(() => {
            clientGraceTimer = null;
            if (clientReconnecting) {
              scheduleReconnect("client self-reconnect did not recover");
            }
          }, CLIENT_RECONNECT_GRACE_MS);
        }
        break;
      case "disconnected":
      case "disconnecting":
        if (state === "connecting" || state === "connected" || clientReconnecting) {
          scheduleReconnect(`client reported ${value}`);
        }
        break;
      // "init" / "connecting": informational only
    }
  }

  function handleMessage(event: MessageEvent) {
    const iframe = getIframe();
    if (!iframe || event.source !== iframe.contentWindow) return;
    const data = event.data as { action?: unknown; value?: unknown } | null;
    if (!data || typeof data !== "object") return;
    switch (data.action) {
      case "noVNC_initialized":
        // Same boot marker as connection_state:"init", posted immediately
        // after it. Either one opens the gate; both are per-document.
        liveDoc = pendingDoc;
        break;
      case "connection_state":
        // One gate for every document-scoped report. "init" is exempt (it IS
        // the handover) and is filtered inside handleConnectionState.
        if (data.value !== "init" && !fromLiveDocument()) break;
        handleConnectionState(data.value);
        break;
      case "idle_session_timeout":
        // Ungated, this aborted a fresh connect cycle on a message from the
        // document being torn down — the same class of bug the 'disconnected'
        // path defends against, with no defence at all.
        if (!fromLiveDocument()) break;
        scheduleReconnect("client reported idle session timeout");
        break;
      // clipboardrx / disconnectrx: no action needed — clipboard is handled
      // natively via clipboard_up/down/seamless.
    }
  }

  function handleIframeLoad() {
    const iframe = getIframe();
    if (!iframe) return;
    // NOTE: this deliberately does NOT hand the document gate over. `load`
    // fires after subresources, so the outgoing document can still post ahead
    // of it; only the client's own "init" is ordered correctly.
    let href: string | null = null;
    try {
      href = iframe.contentWindow?.location.href ?? null;
    } catch {
      return; // not same-origin; nothing to inspect
    }
    if (!href) return;
    // graceful server-side disconnect → client redirects to disconnected.html
    if (href.endsWith("/disconnected.html") || href.endsWith("disconnected.html")) {
      scheduleReconnect("server-side disconnect (disconnected.html)");
    }
    // a load of the client index = page booted, not yet connected —
    // connection_state messages follow.
  }

  function handleOffline() {
    offline = true;
    // A WebSocket can go half-open across an outage without ever emitting a
    // close, so /status coming back "alive" afterwards proves nothing about
    // OUR connection. Remember to re-establish rather than trust it.
    if (state === "connected") droppedWhileConnected = true;
    // Replace the pending retry with the slow re-check rather than leaving
    // nothing armed; "online" may never arrive.
    retryTimer = clearTimer(retryTimer);
    if (state === "reconnecting" && !halted) {
      retryTimer = setTimeout(() => {
        retryTimer = null;
        void attemptReconnect();
      }, OFFLINE_RECHECK_MS);
    }
    nextRetryAt = null;
    // The connect watchdog is deliberately NOT cleared: it is the only thing
    // that gives "connecting" a heartbeat, and "online" is not guaranteed to
    // arrive (interface changes, VPN transitions, suspend/resume). Left armed,
    // a connect that dies during the outage still lands in "reconnecting",
    // which has both a retry loop and a visible way out.
    emit();
  }

  /**
   * The single dispatcher for "something suggests we might have just come
   * back from a gap the machine could not observe" — an "online" event, the
   * suspend detector's drift check, a bfcache restore (pageshow persisted),
   * a Page Lifecycle "resume", window focus, or the tab becoming visible.
   * All of these are evidence of the SAME underlying thing (time passed
   * un-monitored), so one dispatcher decides what to do in each state rather
   * than every signal source reinventing that mapping — and rather than
   * having each source individually decide, and disagree over time, which
   * states are worth re-verifying.
   */
  function resyncAfterResume(reason: string) {
    offline = typeof navigator !== "undefined" ? !navigator.onLine : offline;
    emit();
    // A halted machine is halted on purpose: every endpoint it would touch
    // can only answer 401 again, and only explicit user action (reconnectNow)
    // clears the flag.
    if (halted) return;
    if (state === "reconnecting" || state === "connecting") {
      // Rate-limited the same way probeAfterResume's own resume probe
      // already is (same constant, same reasoning): "focus" in particular
      // fires on every alt-tab, and attemptReconnect() here runs a full
      // probe-then-reconnect cycle IMMEDIATELY, bypassing the backoff
      // schedule entirely — unlike the retryTimer's own scheduled calls to
      // attemptReconnect(), which this gate does not touch at all (it only
      // guards resync-TRIGGERED entries into these branches). Without this,
      // a burst of focus events while reconnecting could burn through the
      // whole MAX_ALIVE_RECONNECTS budget in seconds and end a session that
      // the normal backoff-paced retries would have recovered.
      if (
        lastResyncActionAt !== null &&
        Date.now() - lastResyncActionAt < RESUME_PROBE_MIN_INTERVAL_MS
      ) {
        return;
      }
      lastResyncActionAt = Date.now();
    }
    if (state === "reconnecting") {
      void attemptReconnect();
    } else if (state === "connecting") {
      // Debounced: a burst of resume signals would otherwise restart the
      // connect once per event, so it could never finish. See
      // CONNECT_RESTART_MIN_AGE_MS.
      if (Date.now() - connectStartedAt >= CONNECT_RESTART_MIN_AGE_MS) {
        void connect(reason);
      } else {
        // Too young to restart outright, but the watchdog itself may be
        // stale — background-tab throttling (or the very suspend this
        // function exists to detect) can delay a real setTimeout by minutes.
        // Re-arm it so a connect that silently died during the gap is not
        // left waiting on a callback that already should have fired.
        armConnectWatchdog();
      }
    } else if (state === "connected") {
      // The dangerous case, not the safe one: the profile may have died
      // during the gap, and a black-holed TCP connection can leave the
      // client silent for minutes (OS retransmit timeout) while the toolbar
      // shows a green dot over a frozen frame. Re-verify.
      void probeAfterResume();
    }
    // idle: connect() has not even started yet, nothing to resync.
    // session-ended/fatal: terminal on purpose — only reconnectNow() (an
    // explicit user click) reopens them, per its own comment on why.
  }

  function handleOnline() {
    resyncAfterResume("network back online");
  }

  function handleVisibility() {
    if (document.visibilityState !== "visible") return;
    resyncAfterResume("tab became visible");
  }

  function handleFocus() {
    resyncAfterResume("window focused");
  }

  /**
   * `pageshow` with `persisted: true` fires when the page is restored from
   * the browser's back/forward cache — every timer in this controller was
   * frozen along with the rest of the page for that whole window, which
   * ordinary "offline"/"online" and even visibilitychange are not guaranteed
   * to bracket on every browser (notably iOS Safari's aggressive tab
   * suspension). An ordinary load has `persisted: false` and needs no
   * special handling — connect() from start() already covers it.
   */
  function handlePageShow(event: PageTransitionEvent) {
    if (!event.persisted) return;
    resyncAfterResume("restored from back/forward cache");
  }

  /**
   * Page Lifecycle API: fires just before the page stops running JS
   * entirely (a backgrounded tab nearing discard, some mobile suspend
   * paths). Nothing productive runs until "resume" or a fresh load, so there
   * is nothing to pause here — only reset the drift baseline, so the FIRST
   * suspend-detector tick after resume does not measure a gap that started
   * before the freeze was even observed. Not load-bearing: a stale baseline
   * would only ever OVER-report the gap, never under-report it.
   */
  function handleFreeze() {
    lastSuspendTickAt = Date.now();
  }

  function handlePageLifecycleResume() {
    resyncAfterResume("Page Lifecycle API resume event");
  }

  /** Lightweight authoritative probe when the tab becomes visible again. */
  async function probeAfterResume() {
    // Rate-limit the environment-driven triggers. droppedWhileConnected is
    // exempt: that flag means the network went away UNDER a live socket, so the
    // usual justification for skipping — "the heartbeat will ask again shortly"
    // — is exactly wrong. The socket is already suspect and nothing else will
    // re-establish it, so that probe must always run.
    if (
      !droppedWhileConnected &&
      lastResumeProbeAt !== null &&
      Date.now() - lastResumeProbeAt < RESUME_PROBE_MIN_INTERVAL_MS
    ) {
      return;
    }
    lastResumeProbeAt = Date.now();
    // "online" and visibilitychange can both land here, and neither the
    // generation nor the connected state changes while they are outstanding —
    // so without a sequence an older, slower reply can act after a newer one
    // has already decided (e.g. a stale "stopped" ending a session the newer
    // probe just found healthy).
    const seq = ++resumeSeq;
    const gen = generation;
    try {
      const status = await api.profileStatus(profileId);
      if (seq !== resumeSeq) return;
      // only act if we're still the live generation and still connected
      if (destroyed || gen !== generation || state !== "connected") return;
      connectedProbeFailures = 0;
      const cls = classify(status);
      if (isControlPlaneTerminal(cls)) endSession(CLASSIFICATION_REASON[cls]);
      else if (cls !== "alive") scheduleReconnect(`resume probe: ${cls}`);
      else if (droppedWhileConnected) {
        // Process liveness is not connection liveness. The network went away
        // under a live socket, so re-establish instead of leaving a green dot
        // over a frozen frame with no watchdog, no retry and no overlay.
        droppedWhileConnected = false;
        scheduleReconnect("network dropped while connected; re-establishing");
      }
      // alive, and no outage to account for. The processes being up still says
      // nothing about OUR socket, so ask the one endpoint that can tell.
      else await checkViewerAttached(gen, seq);
    } catch (err) {
      if (seq !== resumeSeq) return;
      if (destroyed || gen !== generation || state !== "connected") return;
      if (isAuthError(err)) return haltForAuth();
      if (isNotFound(err)) return endSession(CLASSIFICATION_REASON.stopped);
      // Every caller of this probe (heartbeat, tab switch, "online") is an
      // event about the ENVIRONMENT, not about our socket — which runs
      // browser -> nginx -> Xvnc and never touches FastAPI. A 5xx from the
      // control plane, an outer tunnel error, or a request that merely timed
      // out is therefore no evidence at all that the session is broken.
      // Tearing it down drops the reconnect overlay over a live frame (which
      // also swallows pointer input) and reloads the iframe on the next
      // successful probe. Definitive answers above still act; a genuinely dead
      // socket still surfaces through the client's own disconnect.
      if (droppedWhileConnected) {
        // The network went away under a live socket and we still cannot reach
        // the Manager to confirm anything. Tolerating this one would leave a
        // green dot over a frozen frame with nothing scheduled — the outage
        // itself is the evidence, so re-establish.
        droppedWhileConnected = false;
        scheduleReconnect("network dropped while connected; re-establishing");
        return;
      }
      // ...but not forever. A single failure is no evidence; N in a row means
      // we have had no contact with the control plane for minutes, which is the
      // shape of a dead uplink that never fired an "offline" event. Without
      // this the heartbeat can fail silently for the life of the tab while the
      // UI shows a green dot.
      connectedProbeFailures += 1;
      if (connectedProbeFailures >= MAX_CONNECTED_PROBE_FAILURES) {
        connectedProbeFailures = 0;
        scheduleReconnect(
          `control plane unreachable for ${MAX_CONNECTED_PROBE_FAILURES} probes: ${errMsg(err)}`,
        );
        return;
      }
      console.debug(`[viewer] resume probe failed, ignoring: ${errMsg(err)}`);
    }
  }

  /**
   * Second half of a resume probe: the processes are alive, so ask whether a
   * viewer socket is actually attached. This is the only check that can see a
   * half-open socket — /status cannot, and the client itself will not notice
   * until its keepalive and TCP RTO expire.
   */
  async function checkViewerAttached(gen: number, seq: number) {
    let attached: boolean | null;
    try {
      ({ viewer_attached: attached } = await api.viewerAttached(profileId));
    } catch (err) {
      // No evidence either way. Deliberately does NOT touch detachedStreak, and
      // does NOT count toward connectedProbeFailures — that budget belongs to
      // /status, the probe that decides whether the profile is alive at all.
      // Double-counting one unreachable control plane across both would halve
      // the tolerance the /status budget was chosen to give.
      console.debug(`[viewer] attach probe unavailable: ${errMsg(err)}`);
      return;
    }
    if (seq !== resumeSeq) return;
    if (destroyed || gen !== generation || state !== "connected") return;
    // null is "could not tell", which is not evidence of a dead socket.
    if (attached === null) return;
    if (attached) {
      sawViewerAttached = true;
      detachedStreak = 0;
      return;
    }
    // Never act without a baseline for THIS connection — see sawViewerAttached.
    if (!sawViewerAttached) return;
    detachedStreak += 1;
    if (detachedStreak >= MAX_DETACHED_PROBES) {
      detachedStreak = 0;
      scheduleReconnect("viewer socket is no longer attached");
    }
  }

  /** Slow re-verification while connected — the state with no other timer. */
  function armConnectedHeartbeat() {
    if (heartbeatTimer !== null) clearInterval(heartbeatTimer);
    heartbeatTimer = setInterval(() => {
      if (state === "connected") void probeAfterResume();
    }, CONNECTED_HEARTBEAT_MS);
  }

  function armDegradedTimer() {
    if (degradedTimer !== null || degraded) return;
    degradedTimer = setTimeout(() => {
      degradedTimer = null;
      if (state === "reconnecting") {
        degraded = true;
        emit();
      }
    }, DEGRADED_AFTER_MS);
  }

  return {
    start() {
      window.addEventListener("message", handleMessage);
      window.addEventListener("online", handleOnline);
      window.addEventListener("offline", handleOffline);
      document.addEventListener("visibilitychange", handleVisibility);
      window.addEventListener("focus", handleFocus);
      window.addEventListener("pageshow", handlePageShow);
      (document as PageLifecycleEventTarget).addEventListener("freeze", handleFreeze);
      (document as PageLifecycleEventTarget).addEventListener("resume", handlePageLifecycleResume);
      armSuspendDetector();
      void connect("initial connect");
    },
    handleMessage,
    handleIframeLoad,
    /**
     * Manual retry from the overlay: reset the failure budget and go now.
     * Also valid from the terminal states — a session-ended overlay with no
     * way back in place is a dead end, and the classification that produced
     * it can be wrong (a wedged data plane that has since recovered).
     */
    reconnectNow() {
      if (destroyed) return;
      // Explicit user intent supersedes whatever is in flight, including a
      // probe that never resolved (which is why the button exists).
      generation += 1;
      probeSeq += 1;
      probeInFlight = false;
      probing = false;
      halted = false;  // explicit user intent is the only thing that clears it
      clearAllTimers();
      backoffLevel = 0;
      aliveReconnects = 0;
      nonAliveStreak = 0;
      connectedProbeFailures = 0;
      clientReconnecting = false;
      nextRetryAt = null;
      if (state === "reconnecting") {
        // `degraded` deliberately survives: the button that produced this click
        // is the only affordance "reconnecting" has, and the probe we are about
        // to start may never resolve. Clearing it here removed the escape hatch
        // for good on a wedged control plane.
        void attemptReconnect();
        return;
      }
      degraded = false;
      attempt = 0;
      endReason = null;
      void connect("manual retry");
    },
    destroy() {
      destroyed = true;
      generation += 1;
      clearAllTimers();
      disarmSuspendDetector();
      window.removeEventListener("message", handleMessage);
      window.removeEventListener("online", handleOnline);
      window.removeEventListener("offline", handleOffline);
      document.removeEventListener("visibilitychange", handleVisibility);
      window.removeEventListener("focus", handleFocus);
      window.removeEventListener("pageshow", handlePageShow);
      (document as PageLifecycleEventTarget).removeEventListener("freeze", handleFreeze);
      (document as PageLifecycleEventTarget).removeEventListener("resume", handlePageLifecycleResume);
    },
  };
}

const INITIAL_SNAPSHOT: ViewerSessionSnapshot = {
  state: "idle",
  iframeSrc: null,
  offline: false,
  degraded: false,
  attempt: 0,
  nextRetryAt: null,
  probing: false,
  endReason: null,
  debugLog: [],
};

export function useViewerSession(options: UseViewerSessionOptions) {
  const { profileId, clipboardSync, random } = options;
  const [snapshot, setSnapshot] = useState<ViewerSessionSnapshot>(INITIAL_SNAPSHOT);
  const iframeRef = useRef<HTMLIFrameElement | null>(null);
  const controllerRef = useRef<Controller | null>(null);
  // Latest-value refs so the controller never needs re-creation for these:
  // changing the clipboard toggle applies on the next (re)connect by design.
  const clipboardSyncRef = useRef(clipboardSync);
  clipboardSyncRef.current = clipboardSync;
  const randomRef = useRef(random ?? Math.random);
  randomRef.current = random ?? Math.random;

  useEffect(() => {
    const controller = createViewerController({
      profileId,
      getClipboardSync: () => clipboardSyncRef.current,
      getIframe: () => iframeRef.current,
      random: () => randomRef.current(),
      onChange: setSnapshot,
    });
    controllerRef.current = controller;
    controller.start();
    return () => {
      controller.destroy();
      controllerRef.current = null;
    };
  }, [profileId]);

  const handleIframeLoad = useCallback(() => {
    controllerRef.current?.handleIframeLoad();
  }, []);

  const reconnectNow = useCallback(() => {
    controllerRef.current?.reconnectNow();
  }, []);

  return { ...snapshot, iframeRef, handleIframeLoad, reconnectNow };
}
