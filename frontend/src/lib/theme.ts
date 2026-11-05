/**
 * Theme preference: follow the OS ("system"), or pin light/dark.
 *
 * The palette itself lives in src/styles/globals.css as CSS variables; this
 * module only decides which set is active by writing `data-theme` on <html>.
 * index.html runs the same resolution inline before the bundle loads so the
 * first paint already has the right colours.
 */
export type ThemePreference = "system" | "light" | "dark";
export type ResolvedTheme = "light" | "dark";

const STORAGE_KEY = "cloakmanager.theme";
const DARK_QUERY = "(prefers-color-scheme: dark)";

const isPreference = (value: unknown): value is ThemePreference =>
  value === "system" || value === "light" || value === "dark";

let preference: ThemePreference = readPreference();
let media: MediaQueryList | null = null;
const listeners = new Set<() => void>();

function readPreference(): ThemePreference {
  if (typeof window === "undefined") return "system";
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    if (isPreference(stored)) return stored;
  } catch {
    // Private mode or blocked storage: fall back to following the OS.
  }
  return "system";
}

function prefersDark(): boolean {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return true;
  return window.matchMedia(DARK_QUERY).matches;
}

export function systemTheme(): ResolvedTheme {
  return prefersDark() ? "dark" : "light";
}

export function resolveTheme(pref: ThemePreference, systemIsDark: boolean): ResolvedTheme {
  if (pref === "light") return "light";
  if (pref === "dark") return "dark";
  return systemIsDark ? "dark" : "light";
}

export function resolvedTheme(): ResolvedTheme {
  return resolveTheme(preference, prefersDark());
}

function notify() {
  for (const listener of listeners) listener();
}

function applyTheme() {
  if (typeof document === "undefined") return;
  document.documentElement.dataset.theme = resolvedTheme();
}

function onSystemChange() {
  applyTheme();
  notify();
}

/**
 * Apply the stored preference and start listening to the OS. Called once at
 * startup, before React renders.
 */
export function initTheme() {
  if (typeof window === "undefined") return;
  if (!media && typeof window.matchMedia === "function") {
    media = window.matchMedia(DARK_QUERY);
    // Safari < 14 only has addListener.
    if (media.addEventListener) media.addEventListener("change", onSystemChange);
    else media.addListener?.(onSystemChange);
  }
  window.addEventListener("storage", (event) => {
    if (event.key !== STORAGE_KEY) return;
    const next = isPreference(event.newValue) ? event.newValue : "system";
    if (next === preference) return;
    preference = next;
    applyTheme();
    notify();
  });
  applyTheme();
}

export function getThemePreference(): ThemePreference {
  return preference;
}

export function setThemePreference(pref: ThemePreference) {
  preference = pref;
  try {
    window.localStorage.setItem(STORAGE_KEY, pref);
  } catch {
    // Not persisting is acceptable; the choice still applies to this session.
  }
  applyTheme();
  notify();
}

export function subscribeTheme(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
