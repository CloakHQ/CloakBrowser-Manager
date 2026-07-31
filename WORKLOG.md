# WORKLOG — KasmVNC 1.5 native-client migration

Branch: `feat/kasmvnc-1.5-native-client` · squashed to a single commit on top of
`main` (`a85b213`).

The branch replaces the KasmVNC 1.3.3 + noVNC compatibility bridge with
KasmVNC 1.5.0's native client behind an nginx data plane, and then hardens it
across nine rounds of multi-model code review. The original 68 commits were
squashed; this file is the record of what was decided, what was verified and
how, and what is still open.

The pre-squash history is preserved **locally only** as branch
`backup/pre-squash-kasmvnc` and tag `pre-squash-kasmvnc` (both at `2c53933`,
68 commits). Each of those commits documents one finding, its trigger, and how
its regression test was verified. Nothing was pushed; delete them when you no
longer want them.

---

## 1. Current state

| | |
|---|---|
| Backend tests | 257 passing (`~/anaconda3/bin/python -m pytest backend/tests -q`) |
| Frontend tests | 70 passing (`cd frontend && npm test -- --run`) |
| Type check | `npx tsc --noEmit` clean |
| Image | `cloakbrowser-manager:kasm15`, rebuilt and re-verified after every round |

Backend test deps are **not** in `backend/requirements.txt` — the suite needs
`fastapi`, `httpx`, `pytest-asyncio` (and `pydantic`, `websockets`). They were
installed into the anaconda base env for this work. `cloakbrowser` itself is
mocked in `backend/tests/conftest.py`, so the suite runs without the binary.

---

## 2. Review process

Nine rounds of `hydra-heads`, six models per round (claude/opus, codex,
gemini, goose/deepseek, kimi, opencode) — roughly 54 independent reviews. Each
round: review the current tree → verify every finding myself against the code →
fix accepted findings one commit at a time with a regression test → rebuild the
image and re-verify live → next round.

| Round | Findings | CRITICAL / HIGH | Regressions introduced by the previous round's fixes |
|---|---|---|---|
| 1 | 24 | 1 / 7 | — |
| 2 | 8 | 0 / 2 | 3 |
| 3 | 9 | 0 / 3 | 3 |
| 4 | 6 | 0 / 2 | 5 |
| 5 | 7 | 0 / 2 | 4 |
| 6 | 7 | 0 / 3 | 5 |
| 7 | 4 | 0 / 1 | 1 |
| 8 | 4 | **0 / 0** | 2 |
| 9 | 4 | **0 / 0** | 2 |

Every CRITICAL and HIGH found in any round is fixed, each with a regression test
verified to fail without its fix. Rounds 8 and 9 produced no critical or high
findings at all.

**The target was three consecutive clean rounds. That was not reached**, and
§5.1 explains why it is not reachable by continuing to patch: the remaining
MEDIUM is a design property, not a bug, and two reviewers correctly flag
opposite sides of it depending on which way the dial is set.

Round reports are under `tmp/2026-07-31-*/<provider>/response.md`
(gitignored, local only). The round-9 prompt — which carries the deferred-item
reasoning in a form the reviewers respect — is `tmp/hh-round9-prompt.md`.

---

## 3. What was verified, and how

Verification level matters for judging risk, so it is split explicitly.

### 3.1 Verified live against a running container

Re-run after every round, on the rebuilt image:

- Auto-launch on container start; `GET /status` reports `running`
- `/viewer/<token>/` → 200 (nginx `auth_request` → Basic creds injected → Kasm)
- Viewer WebSocket upgrade → `RFB 003.008` handshake through nginx
- `/viewer/<token>` (no slash) → 308 with a **relative** `Location`, args preserved
- `/viewer/<token>/api/...` → 403 (Kasm management API blocked from clients)
- Bad/expired token → 403 (not a 500 — the `auth_request` contract)
- `GET /kasm-stats` → 200; `GET /cdp/json/version` → 200
- `X-Forwarded-Proto: https` → CDP advertises `wss://` (verified both ways)
- `POST /stop` → `{"ok":true,"browser_closed":true}`; relaunch → 200
- Create → launch → **delete a running profile** → 200, dir removed, Xvnc gone
- `kill -9` nginx master → `FATAL:` log → container restart → `/healthz` 200
- `docker stop` → exit 0 in ~0.4s with the lifespan shutdown having closed
  browsers and Xvnc first
- Clipboard body at the API's declared maximum: 413 under nginx's 1m default,
  200 at the configured limit (retested at 12.6 MB for the escaped-unicode case)
- Compose env pass-through: zero `KASM_*` vars in the container when unset,
  both present when set
- Default compose creates a container with **no** device mappings (GPU-less
  hosts); the `docker-compose.gpu.yml` overlay maps `/dev/dri` as before

### 3.2 Verified in a real browser (agent-browser, live container)

- Login → profile select → viewer connects (`idle → connecting → connected`)
- Unclean drop (killed nginx workers) → `connected → reconnecting → connecting
  (fresh token) → connected`, automatic
- Profile stopped while viewing → terminal overlay with "Try again" / "Back to
  profile" — **not** a blank pane
- "Try again" while genuinely stopped → honestly returns to session-ended
- "Try again" after relaunch → recovers in place
- Launch from the terminal overlay → fresh controller (`idle → connecting →
  connected`), proving the remount
- 150s connected soak → exactly 3 heartbeat probes, **zero** state transitions
  (no flapping)

### 3.3 Verified empirically but out-of-band (not in the app)

- nginx `auth_request` status mapping: a 404 subrequest yields `500` to the
  client (`auth request unexpected status: 404`), 403 denies cleanly — proven
  with a throwaway nginx config, which is why `viewer-auth` returns 403
- Docker refuses to create a container when a `devices:` source path is missing
- Playwright's `BrowserContext.pages` is `self._pages.copy()` — a local list
  copy that cannot raise, which is why `_browser_alive` had to become a real
  probe
- Kasm's `get_sessions` returns the same single `manager` account with a stale
  `connected_since` whether or not a viewer is attached (see §5.2)
- `kasmvncpasswd -wro` overwrites cleanly on success

### 3.4 Unit-tested only — NOT exercised against a real browser

This is the important list. These paths are covered by tests that were each
verified to fail without their fix, but the scenarios were never reproduced
against real Chromium/Xvnc:

- Every wedged-teardown path (`_closing` claims, TTL expiry, claim release)
- Cancelled `stop()` and cancelled `launch()` (the `wait_for` interleavings)
- `launch()` abort cleanup, including context close on abort
- Browser death during the launch registration window
- Concurrent delete/launch races and the `_deleting` claim
- Xvnc surviving SIGKILL and the display being withheld
- Credential-file failure modes (`kasmvncpasswd` missing / empty / stale)
- `browser_alive: false` — the live window is sub-second because Playwright's
  close handler wins, so only the alive branch was seen for real

### 3.5 Never tested at all

- **Headless profiles end-to-end.** `headless: true` is a supported option and
  drove a HIGH finding in round 7 (a headless browser has no X server to lose,
  so killing Xvnc says nothing about whether it exited). That reasoning was
  never validated by actually running a headless profile. See §4.1.
- **More than one profile at a time.** Every live test used a single profile.
  Display/port allocation, the sequential auto-launch queue, parallel
  `cleanup_all`, and `stop_grace_period` under load are all untested with N > 1.
- **A real TLS deployment.** `X-Forwarded-Proto` and the relative 308 were
  verified by forging headers against the local container, not behind an actual
  Cloudflare Tunnel or ingress LB.
- **arm64.** The Dockerfile pins a SHA per architecture; only amd64 was built.
- **GPU acceleration.** The host has `/dev/dri/card1` but no `renderD128`, so
  `-hw3d` never actually engaged; only the "no render node → disabled" path ran.
- **H.264/H.265/AV1 streaming.** `-videoCodec auto` is set and the FFmpeg
  symlinks exist, but no session was confirmed to negotiate a video codec (the
  Xvnc log showed WebP in use).
- **Long-lived sessions.** Nothing ran past ~10 minutes, so the 1h viewer-token
  TTL rollover, the 600s claim ceiling, and the 30-minute nginx
  `proxy_read_timeout` on idle CDP sockets are all unobserved in practice.

---

## 4. Outstanding work

### 4.1 Identify the closing browser by DevTools GUID, not by CDP port — **do this first**

Round 9's only converged finding (claude and codex independently, both MEDIUM).

**The problem.** When `stop()` cannot close a browser within its bound, the
profile is recorded in `BrowserManager._closing` so a later launch or delete
cannot touch a `user_data_dir` that a live Chromium is still writing to. To
decide when that guard can be lifted, `is_wedged()` asks *"is anything still
listening on the CDP port?"* — and `_allocate_cdp_port()` cycles 5100-5199, so
a **later** profile's Chromium can end up bound to the port a stale claim
remembers. A port number is not an identity.

Every setting of that dial is wrong in one direction:

- **Trust the port** → an unrelated browser keeps the claim alive forever, and
  the profile is permanently un-launchable and un-deletable (kimi, round 8).
- **Cap it** (the current `CLOSING_CLAIM_MAX_S = 600`) → the guard is released
  on a browser that is demonstrably still alive, and `DELETE` then `rmtree`s
  `user_data_dir` underneath it. A headless profile is the clean example: it
  has no X server to lose, so nothing else forces it to exit (claude + codex,
  round 9).

Both sides are correct. Patching either one manufactures the other, which is
exactly why the clean-round streak never got past zero.

**The fix both reviewers propose, independently.** Capture the DevTools browser
GUID from `/json/version` (it is embedded in `webSocketDebuggerUrl`) at launch,
store it on `RunningProfile` and in the `_closing` claim, and compare it with a
bounded probe on expiry:

- port dead, or reports a **different** GUID → release the claim
- **same** GUID → the original browser genuinely still owns the directory;
  hold the guard (and consider terminating it explicitly rather than waiting)

Then `CLOSING_CLAIM_MAX_S` can be deleted entirely.

**Do it as its own change with its own review**, because:

1. It must also make `is_wedged()` **async** and move it **out of**
   `BrowserManager._lock`. It currently does a blocking loopback `connect()` on
   the event loop while holding the manager lock (flagged separately by claude
   and kimi as LOW). Adding an HTTP probe there without fixing that makes it
   materially worse.
2. It is in the subsystem that produced a defect in **seven of nine rounds**.
   Every incremental patch there has been individually correct and has opened
   the next window over (§5.1).

### 4.2 Other open round-9 findings

| Sev | Finding | Notes |
|---|---|---|
| MEDIUM | A wedged teardown is invisible to `/status`, so the UI offers a **Launch** button that answers 409 "Profile is already running" | Fix needs a lifecycle value (e.g. `stopping`) surfaced through `ProfileResponse`, which ripples into `StatusIndicator`, `LaunchButton` and `classify()`. Not cheap; do it with §4.1 since both touch the same state. |
| LOW | `is_wedged()` does a blocking TCP probe on the event loop, under `BrowserManager._lock` | Subsumed by §4.1 — fix together. |
| LOW | A near-cap claim refresh can stretch the advertised 600s ceiling to ~660s | Disappears if §4.1 removes the ceiling. |
| LOW | `reconnectNow()` clicked while a probe is in flight discards the replacement probe's verdict, so the manual retry is silently wasted | Self-contained frontend fix in `useViewerSession`. |
| LOW | `launch()` can return success for a profile a concurrent `stop()`/delete already tore down | Same family as §4.1; the launch path needs to re-check ownership before returning. |

### 4.3 Verification gaps worth closing

In rough priority order:

1. **Run a headless profile end-to-end.** The round-7 HIGH turned on headless
   behaviour and was reasoned about, not observed. Confirm that a `headless:
   true` profile launches, that stopping it behaves as the lifecycle code
   assumes, and that a wedged headless teardown is actually held by the guard.
2. **Run several profiles concurrently.** Launch 3-5, confirm display/port
   allocation, then `docker stop` and confirm the parallel `cleanup_all` fits
   inside `stop_grace_period: 60s` with every browser closed cleanly. The
   sequential-shutdown problem this replaced was real; the fix is untested at
   scale.
3. **Deploy behind a real TLS terminator** and confirm `wss://` CDP URLs, the
   relative 308, and that the viewer iframe is not blocked as mixed content.
4. **Leave a session connected for >1 hour** to observe the viewer-token TTL
   rollover, and idle a CDP WebSocket past 30 minutes against nginx's
   `proxy_read_timeout` (kimi flagged this as LOW in round 6; never verified).
5. **Confirm video-codec negotiation** actually happens, or drop the claim.
6. **Build arm64** — the SHA is pinned but unexercised.

---

## 5. Decisions worth knowing

### 5.1 The teardown lifecycle wants one owner

Six of the last eight rounds found a defect in browser teardown. The shape is
always the same, and codex named it exactly:

> any `await` between clearing one state and setting the next creates an
> unowned profile window.

Ownership is spread across four hand-maintained sets — `running`, `_launching`,
`_deleting`, `_closing` — with transitions written by hand across `await`
points. Over the rounds the unguarded window was closed in `stop()`, then found
again in `launch()`'s abort path; closed for the failure case, then found for
the whole-duration case; closed with a TTL, then found for headless. Each fix
was correct for the window it addressed.

The durable fix is a single guard acquired at entry to any teardown and
released at exit, covering every path — not another patch. §4.1 is the natural
place to start, since it already requires touching this code.

### 5.2 Deliberately not fixed (with the disproof of the obvious wrong fix)

Both zero-finding reviewers independently endorsed these as sound tradeoffs
rather than gaps. Do not "fix" them without reading this first.

**The connected heartbeat cannot prove the viewer's WebSocket is alive.**
`/status` only proves the two server processes exist. The obvious fix —
checking Kasm's `get_sessions` — **does not work**: verified live that it
returns the same single `manager` account with a stale `connected_since`
whether or not a viewer is attached. It reports the account, not the socket. A
correct fix needs client perf-stat freshness, and `get_frame_stats` 503s after
a **10s wait** when perf stats are off, so a careless implementation would
stall the heartbeat every 45s. Note also that the Kasm client sends keepalives
every ~5s, so a black-holed socket does eventually error via TCP RTO and emit
`disconnected`, which the existing machinery recovers from. This is delayed
recovery, not a permanent freeze.

**A stale `connection_state: "connected"` from the outgoing iframe document is
still accepted.** A same-frame navigation keeps the same `WindowProxy`, so
`event.source` cannot distinguish the outgoing document from the incoming one.
Gating it on the committed iframe `load` was tried and **reverted**: the Kasm
client's socket can open *before* `load` fires, so the gate dropped legitimate
`connected` messages and failed 29 tests. A correct fix needs a different
signal. The consequence today is a shortened reconnect cycle, not a lost
session.

**`/api/profiles/<id>/clipboard` and `/kasm-stats` have no frontend caller.**
The native Kasm client handles clipboard itself via
`clipboard_up`/`down`/`seamless`. Removing a public API surface is a product
decision, not a defect fix — flagged rather than deleted. `GET /clipboard`'s
unbounded `page.evaluate` (a wedged page hangs the request) lives on that
orphaned path; fix it or remove the endpoint.

### 5.3 Process notes that paid off

- **Every regression test was verified to fail without its fix.** Four of my
  own tests passed against broken code and had to be rewritten — a thread-name
  check that proves nothing because `TestClient` already runs the app off the
  main thread; a unit test that called an async helper directly instead of
  asserting the endpoint used it; a budget test whose timer advances did not
  map to budget increments; and a counter test where an intervening probe reset
  the counter anyway. A test that cannot fail is worse than no test.
- **Assumptions were checked against the code, not against habit.** One shipped
  fix rested on a false premise written into its own commit message ("the X
  server is gone, so the browser is dying") — true for every profile being
  tested, false for headless.
- **Two changes were thrown away rather than shipped**: a heartbeat refactor
  whose test passed with the fix removed (so the original was already correct),
  and the iframe `load` gate in §5.2. Untestable or regressive churn is how the
  defects got in.
