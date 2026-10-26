import { useEffect, useState } from "react";
import { ChevronDown, ChevronUp, Gauge } from "lucide-react";
import { api, type ProxyHealth, type ProxyHealthRow, type ProxyVerdict } from "../lib/api";

/**
 * Sticky proxy-speed badge, top left.
 *
 * Answers one question without opening anything: how fast is the line this profile is on, and how does that compare
 * with what a page needs to paint? A green IP on a slow exit still shows a spinner forever, so speed is judged on its
 * own axis (kbps against the expectation, and the TLS handshake time).
 */

const POLL_MS = 5000;
/** The line is the first thing worth knowing, so the add-on is shown on every load; hiding lasts for the session only. */

const TONE: Record<ProxyVerdict, { dot: string; bar: string; text: string; label: string }> = {
  slow: { dot: "bg-red-500", bar: "bg-red-500", text: "text-red-400", label: "too slow to draw" },
  handshake: { dot: "bg-amber-500", bar: "bg-amber-500", text: "text-amber-400", label: "slow handshake" },
  unmeasured: { dot: "bg-gray-600", bar: "bg-gray-600", text: "text-gray-500", label: "not measured" },
  good: { dot: "bg-emerald-500", bar: "bg-emerald-500", text: "text-emerald-400", label: "healthy" },
};

export function pickRow(rows: ProxyHealthRow[], nativeName?: string | null): ProxyHealthRow | null {
  if (!rows.length) return null;
  if (nativeName) {
    const own = rows.find((row) => row.profile === nativeName);
    if (own) return own;
  }
  return rows[0] ?? null; // the endpoint sorts worst first
}

/** "12 / 100 kbps" plus how far that is from the floor. */
export function describe(row: ProxyHealthRow, expectation: number): { value: string; delta: string; fill: number } {
  if (row.kbps === null) {
    return { value: "—", delta: "no measurement yet", fill: 0 };
  }
  const fill = Math.max(0.02, Math.min(1, (row.ratio ?? row.kbps / expectation) || 0));
  const pct = Math.round(((row.ratio ?? row.kbps / expectation) || 0) * 100);
  const delta = pct >= 100 ? `${pct}% of expectation` : `${pct}% of expectation · ${100 - pct}% short`;
  return { value: `${row.kbps} / ${expectation} kbps`, delta, fill };
}

export function ProxySpeedBadge({ nativeName, sidebarOpen = true }: { nativeName?: string | null; sidebarOpen?: boolean }) {
  const [data, setData] = useState<ProxyHealth | null>(null);
  const [failed, setFailed] = useState(false);
  const [open, setOpen] = useState(true);
  const [shown, setShown] = useState(true);

  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const schedule = () => {
      if (alive) timer = setTimeout(load, POLL_MS);
    };

    // Sync throws, rejections and slow answers are all the same here: the badge says "unavailable" and keeps its
    // place. It must never take the app down because the Manager restarted.
    const load = () => {
      try {
        Promise.resolve(api.proxyHealth()).then(
          (next) => {
            if (alive) {
              setData(next);
              setFailed(false);
            }
            schedule();
          },
          () => {
            if (alive) setFailed(true);
            schedule();
          },
        );
      } catch {
        if (alive) setFailed(true);
        schedule();
      }
    };

    load();
    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
    };
  }, []);

  const row = pickRow(data?.profiles ?? [], nativeName);
  const expectation = data?.expectation.kbps ?? 100;
  const tone = TONE[row?.verdict ?? "unmeasured"];
  const described = row ? describe(row, expectation) : null;

  if (!shown) {
    return (
      <button
        type="button"
        onClick={() => setShown(true)}
        className={`fixed top-2 z-40 flex items-center gap-1 rounded-md border border-border bg-surface-1/90 px-1.5 py-1 text-[10px] text-gray-500 backdrop-blur hover:text-gray-300 ${sidebarOpen ? "left-[17rem]" : "left-2"}`}
        title="Show the proxy-speed add-on"
        data-testid="proxy-speed-restore"
      >
        <Gauge className="h-3 w-3" /> proxy
      </button>
    );
  }

  return (
    <div
      className={`fixed top-2 z-40 w-60 rounded-lg border border-border bg-surface-1/95 backdrop-blur shadow-lg ${sidebarOpen ? "left-[17rem]" : "left-2"}`}
      data-testid="proxy-speed-badge"
    >
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left"
        title={open ? "Collapse proxy speed" : "Expand proxy speed"}
      >
        <Gauge className="h-3.5 w-3.5 text-gray-500" />
        <span className="text-[10px] font-medium uppercase tracking-wide text-gray-500">Proxy</span>
        <span className={`ml-auto h-1.5 w-1.5 rounded-full ${tone.dot}`} />
        <span className="max-w-[7rem] truncate text-[11px] text-gray-300">
          {failed ? "unavailable" : row?.profile ?? "no proxied profile"}
        </span>
        {open ? <ChevronUp className="h-3 w-3 text-gray-600" /> : <ChevronDown className="h-3 w-3 text-gray-600" />}
      </button>
      <button
        type="button"
        onClick={() => setShown(false)}
        className="absolute right-1 top-1 rounded p-0.5 text-gray-600 hover:text-gray-300"
        title="Hide the proxy-speed add-on"
        aria-label="Hide the proxy-speed add-on"
      >
        <span className="text-[10px] leading-none">✕</span>
      </button>

      {open && row && described && (
        <div className="px-2.5 pb-2" role="status" aria-live="polite">
          <div className="flex items-baseline gap-1">
            <span className={`text-sm font-semibold tabular-nums ${row.verdict === "good" ? "text-gray-200" : tone.text}`}>
              {row.kbps ?? "—"}
            </span>
            <span className="text-[10px] text-gray-500">/ {expectation} kbps expected</span>
          </div>

          <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-surface-0">
            <div className={`h-full rounded-full ${tone.bar}`} style={{ width: `${described.fill * 100}%` }} />
          </div>

          <div className={`mt-1 text-[10px] ${tone.text}`}>
            {described.delta}
            {row.verdict === "slow" && " · " + tone.label}
          </div>

          <div className="mt-1 flex items-center gap-1.5 text-[10px] text-gray-400">
            <span className="max-w-[9rem] truncate" title={row.timezone ?? undefined}>
              {[row.city, row.country].filter(Boolean).join(", ") || "place unknown"}
            </span>
            {row.place_match === false && (
              <span className="rounded bg-red-500/15 px-1 py-px text-red-400" title={`exit is ${row.timezone ?? "?"}, profile expects ${row.profile_timezone ?? "?"}`}>
                place
              </span>
            )}
            {row.family && <span className="rounded bg-surface-0 px-1 py-px uppercase text-gray-500">{row.family}</span>}
            {row.drifted.length > 0 && (
              <span className="rounded bg-amber-500/15 px-1 py-px text-amber-400" title={`${row.drifted.join(", ")} address moving`}>
                drift
              </span>
            )}
          </div>

          <div className="mt-0.5 flex items-center gap-1.5 text-[10px] text-gray-500">
            {row.tls_sec !== null && <span>TLS {row.tls_sec.toFixed(2)}s</span>}
            {row.ip && <span className="ml-auto max-w-[7rem] truncate tabular-nums">{row.ip}</span>}
          </div>

          <div className="mt-1 border-t border-border/60 pt-1 text-[10px] text-gray-600">
            expect ≥{expectation} kbps · TLS ≤{data?.expectation.tls_sec ?? 2.5}s
          </div>

          {row.kbps === null && (
            <div className="mt-1 text-[10px] text-gray-600">cloak proxy check {row.profile} --speed</div>
          )}
        </div>
      )}

      {open && !row && !failed && (
        <div className="px-2.5 pb-2 text-[10px] text-gray-500">No native profile with a proxy.</div>
      )}
    </div>
  );
}
