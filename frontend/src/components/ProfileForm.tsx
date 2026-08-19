import { Check, RotateCcw, Save, Trash2, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { HostOS, Profile, ProfileCreateData, ViewerMode } from "../lib/api";

interface ProfileFormProps {
  profile: Profile | null; // null = create mode
  hostOs: HostOS | null;
  viewerMode: ViewerMode | null;
  onSave: (data: ProfileCreateData) => Promise<void>;
  onDelete?: () => Promise<void>;
  onReset?: () => Promise<void>;
  onCancel: () => void;
}

const RESOLUTION_PRESETS: Record<string, { width: number; height: number }> = {
  "1920 × 1080 (Full HD)": { width: 1920, height: 1080 },
  "2560 × 1440 (QHD)": { width: 2560, height: 1440 },
  "1366 × 768 (HD)": { width: 1366, height: 768 },
  "1440 × 900": { width: 1440, height: 900 },
  "1536 × 864": { width: 1536, height: 864 },
  "1280 × 720 (720p)": { width: 1280, height: 720 },
};

const TAG_COLORS = [
  "#6366f1", // indigo
  "#22c55e", // green
  "#f59e0b", // amber
  "#ef4444", // red
  "#06b6d4", // cyan
  "#a855f7", // purple
  "#f97316", // orange
  "#ec4899", // pink
];

export function ProfileForm({ profile, hostOs, viewerMode, onSave, onDelete, onReset, onCancel }: ProfileFormProps) {
  const isEdit = profile !== null;

  const [form, setForm] = useState<ProfileCreateData>({
    name: "",
    screen_width: 1920,
    screen_height: 1080,
    gpu_family: "auto",
    humanize: false,
    human_preset: "default",
    geoip: true,
    clipboard_sync: true,
    auto_launch: false,
    allow_3p_cookies: true,
    set_google_default: true,
    capture_preview: true,
    extension_paths: [],
    launch_args: [],
    tags: [],
  });

  const [previewError, setPreviewError] = useState(false);
  const [previewBuster, setPreviewBuster] = useState(0);

  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [resetDone, setResetDone] = useState(false);
  const savedTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const resetTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  useEffect(() => () => {
    clearTimeout(savedTimer.current);
    clearTimeout(resetTimer.current);
  }, []);
  const [tagInput, setTagInput] = useState("");
  const [tagColor, setTagColor] = useState<string | null>("#6366f1");
  const [extensionPathInput, setExtensionPathInput] = useState("");
  const [launchArgInput, setLaunchArgInput] = useState("");

  useEffect(() => {
    if (profile) {
      setForm({
        name: profile.name,
        fingerprint_seed: profile.fingerprint_seed,
        proxy: profile.proxy,
        timezone: profile.timezone,
        locale: profile.locale,
        screen_width: profile.screen_width,
        screen_height: profile.screen_height,
        gpu_family: profile.gpu_family,
        humanize: profile.humanize,
        human_preset: profile.human_preset,
        geoip: profile.geoip,
        clipboard_sync: profile.clipboard_sync,
        auto_launch: profile.auto_launch,
        extension_paths: profile.extension_paths ?? [],
        allow_3p_cookies: profile.allow_3p_cookies,
        set_google_default: profile.set_google_default,
        capture_preview: profile.capture_preview,
        launch_args: profile.launch_args ?? [],
        notes: profile.notes,
        tags: profile.tags ?? [],
      });
    }
    // Re-fetch the preview for the newly selected profile (bust the cache).
    setPreviewError(false);
    setPreviewBuster(Date.now());
  }, [profile?.id]);

  useEffect(() => {
    if (hostOs === "macos") {
      setForm((previous) => ({ ...previous, gpu_family: "auto" }));
    }
  }, [hostOs]);

  const set = <K extends keyof ProfileCreateData>(key: K, value: ProfileCreateData[K]) => {
    setForm((prev) => ({ ...prev, [key]: value }));
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!form.name.trim()) return;
    setSaving(true);
    try {
      await onSave(form);
      setSaved(true);
      clearTimeout(savedTimer.current);
      savedTimer.current = setTimeout(() => setSaved(false), 1500);
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async () => {
    if (!onDelete) return;
    if (!confirm("Delete this profile? Browser data will be permanently removed.")) return;
    setDeleting(true);
    try {
      await onDelete();
    } finally {
      setDeleting(false);
    }
  };

  const handleReset = async () => {
    if (!onReset) return;
    if (
      !confirm(
        "Reset this profile? Cookies, history and site data will be wiped and a " +
          "new fingerprint generated. Bookmarks, settings and default search are kept.",
      )
    )
      return;
    setResetting(true);
    try {
      await onReset();
      setResetDone(true);
      clearTimeout(resetTimer.current);
      resetTimer.current = setTimeout(() => setResetDone(false), 1500);
    } finally {
      setResetting(false);
    }
  };

  const randomizeSeed = () => {
    set("fingerprint_seed", Math.floor(Math.random() * 90000) + 10000);
  };

  const currentResolution = Object.entries(RESOLUTION_PRESETS).find(
    ([, v]) => v.width === form.screen_width && v.height === form.screen_height,
  )?.[0] ?? "custom";

  const addTag = () => {
    const tag = tagInput.trim();
    if (!tag) return;
    if (form.tags?.some((t) => t.tag === tag)) return;
    set("tags", [...(form.tags ?? []), { tag, color: tagColor }]);
    setTagInput("");
  };

  const removeTag = (tag: string) => {
    set("tags", (form.tags ?? []).filter((t) => t.tag !== tag));
  };

  const addExtensionPath = () => {
    const path = extensionPathInput.trim();
    if (!path || (form.extension_paths ?? []).includes(path)) return;
    set("extension_paths", [...(form.extension_paths ?? []), path]);
    setExtensionPathInput("");
  };

  const removeExtensionPath = (index: number) => {
    set("extension_paths", (form.extension_paths ?? []).filter((_, i) => i !== index));
  };

  const addLaunchArg = () => {
    const arg = launchArgInput.trim();
    if (!arg) return;
    if ((form.launch_args ?? []).includes(arg)) return;
    set("launch_args", [...(form.launch_args ?? []), arg]);
    setLaunchArgInput("");
  };

  const removeLaunchArg = (idx: number) => {
    set("launch_args", (form.launch_args ?? []).filter((_, i) => i !== idx));
  };

  return (
    <form onSubmit={handleSubmit} className="p-6 max-w-3xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-2">
          <h2 className="text-lg font-semibold">
            {isEdit ? "Edit Profile" : "New Profile"}
          </h2>
          {isEdit && onReset && (
            <button
              type="button"
              onClick={handleReset}
              disabled={resetting || resetDone}
              className="btn-secondary flex items-center gap-1.5"
            >
              {resetDone ? <Check className="h-3.5 w-3.5" /> : <RotateCcw className="h-3.5 w-3.5" />}
              <span>{resetDone ? "Reset done" : resetting ? "Resetting..." : "Reset"}</span>
            </button>
          )}
          {isEdit && onDelete && (
            <button
              type="button"
              onClick={handleDelete}
              disabled={deleting}
              className="btn-danger flex items-center gap-1.5"
            >
              <Trash2 className="h-3.5 w-3.5" />
              <span>{deleting ? "Deleting..." : "Delete"}</span>
            </button>
          )}
        </div>
        <div className="flex items-center gap-2">
          <button type="button" onClick={onCancel} className="btn-secondary">
            Cancel
          </button>
          <button type="submit" disabled={saving || saved} className="btn-primary flex items-center gap-1.5">
            {saved ? <Check className="h-3.5 w-3.5" /> : <Save className="h-3.5 w-3.5" />}
            <span>{saved ? "Saved" : saving ? "Saving..." : isEdit ? "Save" : "Create"}</span>
          </button>
        </div>
      </div>

      <div className="space-y-5">
        {/* Last preview — the last frame captured before the browser stopped */}
        {isEdit && !previewError && (
          <section>
            <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-3">Last preview</h3>
            <img
              src={`/api/profiles/${profile!.id}/screenshot?t=${previewBuster}`}
              onError={() => setPreviewError(true)}
              alt="Last browser preview"
              className="w-full rounded-md border border-border bg-surface-1 object-contain max-h-72"
            />
          </section>
        )}

        {/* Basic */}
        <section>
          <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-3">Basic</h3>
          <div className="grid grid-cols-2 gap-3">
            <div className="col-span-2">
              <label className="label">Profile Name</label>
              <input
                className="input"
                value={form.name}
                onChange={(e) => set("name", e.target.value)}
                placeholder="e.g. Amazon Seller #1"
                required
              />
            </div>
            <div className="col-span-2">
              <label className="label">Fingerprint Seed</label>
              <div className="flex gap-2">
                <input
                  className="input flex-1 no-spin"
                  type="number"
                  value={form.fingerprint_seed ?? ""}
                  onChange={(e) => set("fingerprint_seed", e.target.value ? Number(e.target.value) : null)}
                  placeholder="Auto (random)"
                />
                <button
                  type="button"
                  onClick={randomizeSeed}
                  className="btn-secondary px-2.5"
                  title="Randomize seed"
                >
                  <svg className="h-5 w-5" viewBox="0 0 32 32" fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round">
                    {/* Right face - lightest */}
                    <polygon points="28,10 16,16 16,28 28,22" fill="currentColor" opacity="0.06" />
                    <polygon points="28,10 16,16 16,28 28,22" />
                    {/* Left face - medium shade */}
                    <polygon points="4,10 16,16 16,28 4,22" fill="currentColor" opacity="0.2" />
                    <polygon points="4,10 16,16 16,28 4,22" />
                    {/* Top face - brightest */}
                    <polygon points="16,3 28,10 16,16 4,10" fill="currentColor" opacity="0.1" />
                    <polygon points="16,3 28,10 16,16 4,10" />
                    {/* Dots on top face (3 - diagonal) */}
                    <circle cx="11.5" cy="8.5" r="1" fill="currentColor" opacity="0.7" />
                    <circle cx="16" cy="9.5" r="1" fill="currentColor" opacity="0.7" />
                    <circle cx="20.5" cy="10.5" r="1" fill="currentColor" opacity="0.7" />
                    {/* Dots on left face (5 - dice pattern) */}
                    <circle cx="7.5" cy="14" r="0.9" fill="currentColor" opacity="0.6" />
                    <circle cx="12.5" cy="16.5" r="0.9" fill="currentColor" opacity="0.6" />
                    <circle cx="10" cy="19" r="0.9" fill="currentColor" opacity="0.6" />
                    <circle cx="7.5" cy="22" r="0.9" fill="currentColor" opacity="0.6" />
                    <circle cx="12.5" cy="24.5" r="0.9" fill="currentColor" opacity="0.6" />
                    {/* Dots on right face (2 - diagonal) */}
                    <circle cx="20" cy="15" r="0.9" fill="currentColor" opacity="0.5" />
                    <circle cx="24" cy="20" r="0.9" fill="currentColor" opacity="0.5" />
                  </svg>
                </button>
              </div>
            </div>
          </div>
        </section>

        {/* Network */}
        <section>
          <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-3">Network</h3>
          <div className="space-y-3">
            <div>
              <label className="label">Proxy</label>
              <input
                className="input"
                value={form.proxy ?? ""}
                onChange={(e) => set("proxy", e.target.value || null)}
                placeholder="http://user:pass@host:port"
              />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="label">Timezone</label>
                <input
                  className="input"
                  value={form.timezone ?? ""}
                  onChange={(e) => set("timezone", e.target.value || null)}
                  placeholder="America/New_York"
                />
              </div>
              <div>
                <label className="label">Locale</label>
                <input
                  className="input"
                  value={form.locale ?? ""}
                  onChange={(e) => set("locale", e.target.value || null)}
                  placeholder="en-US"
                />
              </div>
            </div>
            <label className="flex items-center gap-2 text-sm text-gray-300 cursor-pointer">
              <input
                type="checkbox"
                checked={form.geoip ?? false}
                onChange={(e) => set("geoip", e.target.checked)}
                className="rounded border-border bg-surface-2"
              />
              Auto-detect timezone/locale from the proxy exit, or host public IP without a proxy (GeoIP)
            </label>
          </div>
        </section>

        {/* Hardware */}
        <section>
          <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-3">Hardware</h3>
          <div className="space-y-3">
            <div>
              <label className="label">Screen Resolution</label>
              <select
                className="input"
                value={currentResolution}
                onChange={(e) => {
                  const preset = RESOLUTION_PRESETS[e.target.value];
                  if (preset) {
                    set("screen_width", preset.width);
                    set("screen_height", preset.height);
                  }
                }}
              >
                {Object.keys(RESOLUTION_PRESETS).map((name) => (
                  <option key={name} value={name}>{name}</option>
                ))}
                <option value="custom">Custom</option>
              </select>
            </div>
            {currentResolution === "custom" && (
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="label">Width</label>
                  <input
                    className="input"
                    type="number"
                    value={form.screen_width ?? 1920}
                    onChange={(e) => set("screen_width", Number(e.target.value))}
                  />
                </div>
                <div>
                  <label className="label">Height</label>
                  <input
                    className="input"
                    type="number"
                    value={form.screen_height ?? 1080}
                    onChange={(e) => set("screen_height", Number(e.target.value))}
                  />
                </div>
              </div>
            )}
            <div>
              <label className="label">GPU Family</label>
              {hostOs === "macos" ? (
                <select className="input" value="auto" disabled>
                  <option value="auto">Apple Silicon (automatic)</option>
                </select>
              ) : hostOs === null ? (
                <select className="input" value="auto" disabled>
                  <option value="auto">Automatic (detecting runtime…)</option>
                </select>
              ) : (
                <select
                  className="input"
                  value={form.gpu_family ?? "auto"}
                  onChange={(e) => set("gpu_family", e.target.value as "auto" | "nvidia" | "intel")}
                >
                  <option value="auto">Auto (from seed)</option>
                  <option value="nvidia">NVIDIA</option>
                  <option value="intel">Intel</option>
                </select>
              )}
              <p className="text-xs text-gray-500 mt-1">
                {hostOs === "macos"
                  ? "The seed selects a coherent Apple Silicon model and matching hardware profile."
                  : "The seed selects a coherent GPU model, CPU, memory, and screen profile within the family."}
              </p>
            </div>
          </div>
        </section>

        {/* Behavior */}
        <section>
          <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-3">Behavior</h3>
          <div className="space-y-3">
            <label className="flex items-center gap-2 text-sm text-gray-300 cursor-pointer">
              <input
                type="checkbox"
                checked={form.humanize ?? false}
                onChange={(e) => set("humanize", e.target.checked)}
                className="rounded border-border bg-surface-2"
              />
              Human-like mouse, keyboard, and scroll behavior
            </label>
            {form.humanize && (
              <div>
                <label className="label">Human Preset</label>
                <select
                  className="input"
                  value={form.human_preset}
                  onChange={(e) => set("human_preset", e.target.value)}
                >
                  <option value="default">Default (normal speed)</option>
                  <option value="careful">Careful (slower, deliberate)</option>
                </select>
              </div>
            )}
            {viewerMode === "vnc" && (
              <label className="flex items-center gap-2 text-sm text-gray-300 cursor-pointer">
                <input
                  type="checkbox"
                  checked={form.clipboard_sync ?? true}
                  onChange={(e) => set("clipboard_sync", e.target.checked)}
                  className="rounded border-border bg-surface-2"
                />
                Enable clipboard sync by default in VNC viewer
              </label>
            )}
            <label className="flex items-center gap-2 text-sm text-gray-300 cursor-pointer">
              <input
                type="checkbox"
                checked={form.auto_launch ?? false}
                onChange={(e) => set("auto_launch", e.target.checked)}
                className="rounded border-border bg-surface-2"
              />
              Launch automatically when Manager starts
            </label>
            <label className="flex items-start gap-2 text-sm text-gray-300 cursor-pointer">
              <input
                type="checkbox"
                checked={form.capture_preview ?? true}
                onChange={(e) => set("capture_preview", e.target.checked)}
                className="rounded border-border bg-surface-2 mt-0.5"
              />
              <span>
                Save a preview screenshot of the browser
                <span className="block text-xs text-gray-500">
                  Captures the page periodically while running, shown here after the profile stops.
                </span>
              </span>
            </label>
          </div>
        </section>

        {/* Compatibility */}
        <section>
          <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-3">Compatibility</h3>
          <label className="flex items-center gap-2 text-sm text-gray-300 cursor-pointer">
            <input
              type="checkbox"
              checked={form.allow_3p_cookies ?? false}
              onChange={(e) => set("allow_3p_cookies", e.target.checked)}
              className="rounded border-border bg-surface-2"
            />
            Allow third-party cookies for login, SSO, and challenge flows
          </label>
          <label className="flex items-start gap-2 text-sm text-gray-300 cursor-pointer mt-3">
            <input
              type="checkbox"
              checked={form.set_google_default ?? true}
              onChange={(e) => set("set_google_default", e.target.checked)}
              className="rounded border-border bg-surface-2 mt-0.5"
            />
            <span>
              Set Google as the default search engine
              <span className="block text-xs text-gray-500">
                Adds a few seconds to the profile's first launch (one-time setup).
              </span>
            </span>
          </label>
        </section>

        {/* Tags */}
        <section>
          <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-3">Tags</h3>
          {(form.tags ?? []).length > 0 && (
            <div className="flex flex-wrap gap-1.5 mb-3">
              {(form.tags ?? []).map((t) => (
                <span
                  key={t.tag}
                  className="inline-flex items-center gap-1 text-xs px-2 py-1 rounded-full bg-surface-3 text-gray-300"
                  style={t.color ? { backgroundColor: `${t.color}20`, color: t.color } : undefined}
                >
                  {t.tag}
                  <button
                    type="button"
                    onClick={() => removeTag(t.tag)}
                    className="hover:opacity-70"
                  >
                    <X className="h-3 w-3" />
                  </button>
                </span>
              ))}
            </div>
          )}
          <div className="flex gap-2 items-center">
            <div className="flex gap-1">
              {TAG_COLORS.map((c) => (
                <button
                  key={c}
                  type="button"
                  onClick={() => setTagColor(c)}
                  className="w-4 h-4 rounded-full border-2 transition-transform"
                  style={{
                    backgroundColor: c,
                    borderColor: tagColor === c ? "#fff" : "transparent",
                    transform: tagColor === c ? "scale(1.2)" : undefined,
                  }}
                />
              ))}
            </div>
            <input
              className="input flex-1"
              value={tagInput}
              onChange={(e) => setTagInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); addTag(); } }}
              placeholder="Add tag..."
            />
            <button type="button" onClick={addTag} className="btn-secondary text-xs">
              Add
            </button>
          </div>
        </section>

        {/* Extensions */}
        <section>
          <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-3">Extensions</h3>
          <p className="text-xs text-gray-500 mb-2">Add one unpacked Chrome extension directory per path.</p>
          {(form.extension_paths ?? []).length > 0 && (
            <div className="space-y-1.5 mb-3">
              {(form.extension_paths ?? []).map((path, index) => (
                <div key={`${path}-${index}`} className="flex items-center gap-2 rounded-md bg-surface-3 px-2 py-1.5">
                  <code className="flex-1 truncate text-xs text-gray-300">{path}</code>
                  <button type="button" onClick={() => removeExtensionPath(index)} className="hover:opacity-70">
                    <X className="h-3 w-3" />
                  </button>
                </div>
              ))}
            </div>
          )}
          <div className="flex gap-2">
            <input
              className="input flex-1 font-mono"
              value={extensionPathInput}
              onChange={(e) => setExtensionPathInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); addExtensionPath(); } }}
              placeholder="/data/extensions/ublock"
            />
            <button type="button" onClick={addExtensionPath} className="btn-secondary text-xs">Add</button>
          </div>
        </section>

        {/* Advanced */}
        <details className="rounded-md border border-border bg-surface-1 p-3">
          <summary className="cursor-pointer text-xs font-semibold text-gray-400 uppercase tracking-wider">
            Advanced launch arguments
          </summary>
          <p className="text-xs text-gray-500 my-3">
            Unrestricted Chromium flags. Advanced arguments can override Manager-controlled behavior.
          </p>
          {(form.launch_args ?? []).length > 0 && (
            <div className="flex flex-wrap gap-1.5 mb-3">
              {(form.launch_args ?? []).map((arg, index) => (
                <span
                  key={`${arg}-${index}`}
                  className="inline-flex items-center gap-1 text-xs px-2 py-1 rounded-full bg-surface-3 text-gray-300 font-mono"
                >
                  {arg}
                  <button type="button" onClick={() => removeLaunchArg(index)} className="hover:opacity-70">
                    <X className="h-3 w-3" />
                  </button>
                </span>
              ))}
            </div>
          )}
          <div className="flex gap-2">
            <input
              className="input flex-1 font-mono"
              value={launchArgInput}
              onChange={(e) => setLaunchArgInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); addLaunchArg(); } }}
              placeholder="--disable-features=Foo"
            />
            <button type="button" onClick={addLaunchArg} className="btn-secondary text-xs">Add</button>
          </div>
        </details>

        {/* Notes */}
        <section>
          <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-3">Notes</h3>
          <textarea
            className="input min-h-[80px] resize-y"
            value={form.notes ?? ""}
            onChange={(e) => set("notes", e.target.value || null)}
            placeholder="Optional notes about this profile..."
          />
        </section>
      </div>

    </form>
  );
}
