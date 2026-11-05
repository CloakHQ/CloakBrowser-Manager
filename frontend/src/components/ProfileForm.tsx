import { Check, ChevronDown, Copy, Cpu, FileText, Fingerprint, Globe, Image, Loader2, Puzzle, RotateCcw, Save, ShieldCheck, Sliders, Tag, Terminal, Trash2, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError } from "../lib/api";
import { KEY_STATE_LABEL } from "../lib/labels";
import { useConfirm } from "./ui/confirm";
import { useToast } from "./ui/toast";
import type {
  HostOS,
  LicenseKeyEntry,
  Profile,
  ProfileCreateData,
  ProxyProviderInfo,
  ProxyTestResult,
  TmProxyOption,
  ViewerMode,
} from "../lib/api";

interface ProfileFormProps {
  profile: Profile | null; // null = create mode
  hostOs: HostOS | null;
  viewerMode: ViewerMode | null;
  onSave: (data: ProfileCreateData) => Promise<void>;
  onDelete?: () => Promise<void>;
  onReset?: () => Promise<void>;
  onDuplicate?: (includeBrowserState: boolean) => Promise<void>;
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

const EMPTY_FORM: ProfileCreateData = {
  name: "",
  proxy_provider: "none",
  kiotproxy_region: "random",
  tmproxy_location_id: 0,
  tmproxy_isp_id: 0,
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
  restore_session: true,
  transparent_proxy: false,
  portable_cookies: false,
  license_key_id: null,
  extension_paths: [],
  launch_args: [],
  tags: [],
};

/** Every editable field, including the ones that come back null from the API. */
function formFromProfile(profile: Profile | null): ProfileCreateData {
  if (!profile) return { ...EMPTY_FORM };
  return {
    name: profile.name,
    fingerprint_seed: profile.fingerprint_seed,
    proxy: profile.proxy,
    proxy_provider: profile.proxy_provider ?? "none",
    kiotproxy_region: profile.kiotproxy_region ?? "random",
    tmproxy_location_id: profile.tmproxy_location_id ?? 0,
    tmproxy_isp_id: profile.tmproxy_isp_id ?? 0,
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
    restore_session: profile.restore_session,
    transparent_proxy: profile.transparent_proxy,
    portable_cookies: profile.portable_cookies,
    license_key_id: profile.license_key_id,
    launch_args: profile.launch_args ?? [],
    notes: profile.notes,
    tags: profile.tags ?? [],
  };
}

type Tab = "basic" | "network" | "hardware" | "options";

const TABS: { key: Tab; label: string }[] = [
  { key: "basic", label: "Cơ bản" },
  { key: "network", label: "Mạng & proxy" },
  { key: "hardware", label: "Phần cứng" },
  { key: "options", label: "Tùy chọn" },
];

/** Stable comparison key for dirty tracking (the provider key is write-only). */
function serialize(form: ProfileCreateData, providerKey: string): string {
  return JSON.stringify({ ...form, __providerKey: providerKey });
}

export function ProfileForm({ profile, hostOs, viewerMode, onSave, onDelete, onReset, onDuplicate, onCancel }: ProfileFormProps) {
  const isEdit = profile !== null;
  const confirm = useConfirm();
  const toast = useToast();
  const [tab, setTab] = useState<Tab>("basic");

  const [form, setForm] = useState<ProfileCreateData>(() => formFromProfile(null));

  const [keys, setKeys] = useState<LicenseKeyEntry[]>([]);
  const [previewError, setPreviewError] = useState(false);
  const [previewBuster, setPreviewBuster] = useState(0);

  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [resetDone, setResetDone] = useState(false);
  const [duplicating, setDuplicating] = useState(false);
  const [duplicateMenuOpen, setDuplicateMenuOpen] = useState(false);
  const duplicateMenuRef = useRef<HTMLDivElement>(null);
  const duplicateTriggerRef = useRef<HTMLButtonElement>(null);
  const [testingProxy, setTestingProxy] = useState(false);
  const [proxyTest, setProxyTest] = useState<ProxyTestResult | null>(null);
  // Provider key is write-only: the server returns only "set" + a masked hint.
  const [providerKey, setProviderKey] = useState("");
  const [providerTest, setProviderTest] = useState<ProxyProviderInfo | null>(null);
  const [providerTestError, setProviderTestError] = useState<string | null>(null);
  const [testingProvider, setTestingProvider] = useState(false);
  const [tmLocations, setTmLocations] = useState<TmProxyOption[]>([]);
  const [tmIsps, setTmIsps] = useState<TmProxyOption[]>([]);
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

  // Snapshot of what the server last confirmed. Anything else on screen is an
  // unsaved change, so Cancel can warn instead of silently discarding it.
  const baseline = useRef<string>(serialize(form, ""));
  const current = useMemo(() => serialize(form, providerKey), [form, providerKey]);
  const dirty = current !== baseline.current;

  useEffect(() => {
    if (profile) {
      const next = formFromProfile(profile);
      setForm(next);
      baseline.current = serialize(next, "");
    }
    // Re-fetch the preview for the newly selected profile (bust the cache).
    setPreviewError(false);
    setPreviewBuster(Date.now());
    setProviderKey("");
    setProviderTest(null);
    setProviderTestError(null);
    setTab("basic");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [profile?.id]);

  // A reset re-rolls the seed server-side; pick it up so the next save does not
  // write the old fingerprint back, and treat it as the new baseline.
  useEffect(() => {
    if (profile) {
      setForm((prev) => {
        if (prev.fingerprint_seed === profile.fingerprint_seed) return prev;
        const next = { ...prev, fingerprint_seed: profile.fingerprint_seed };
        baseline.current = serialize(next, "");
        return next;
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [profile?.fingerprint_seed]);

  // The pool is only needed for the pin selector; a failed fetch just leaves
  // "any free key" as the only choice, which is the default anyway.
  useEffect(() => {
    api
      .listKeys()
      .then(setKeys)
      .catch(() => setKeys([]));
    // These are optional helpers for the TMProxy form; if the provider is
    // unreachable the user can still type location/ISP ids below.
    api.tmproxyLocations().then(setTmLocations).catch(() => setTmLocations([]));
    api.tmproxyIsps().then(setTmIsps).catch(() => setTmIsps([]));
  }, []);
  useEffect(() => {
    if (hostOs === "macos") {
      setForm((previous) => ({ ...previous, gpu_family: "auto" }));
    }
  }, [hostOs]);

  const set = <K extends keyof ProfileCreateData>(key: K, value: ProfileCreateData[K]) => {
    setForm((prev) => ({ ...prev, [key]: value }));
  };

  const handleTestProxy = async () => {
    if (!form.proxy) return;
    setProxyTest(null);
    setTestingProxy(true);
    try {
      setProxyTest(await api.testProxy(form.proxy));
    } catch (err) {
      const message =
        err instanceof ApiError ? err.message : "Kiểm tra proxy thất bại";
      setProxyTest({ ok: false, error: message });
    } finally {
      setTestingProxy(false);
    }
  };

  const handleTestProvider = async () => {
    if (!providerKey.trim() || form.proxy_provider === "none") return;
    setProviderTest(null);
    setProviderTestError(null);
    setTestingProvider(true);
    try {
      if (form.proxy_provider === "kiotproxy") {
        setProviderTest(
          await api.kiotproxyNew(providerKey.trim(), form.kiotproxy_region ?? "random"),
        );
      } else {
        setProviderTest(
          await api.tmproxyNew(
            providerKey.trim(),
            form.tmproxy_location_id ?? 0,
            form.tmproxy_isp_id ?? 0,
          ),
        );
      }
    } catch (err) {
      setProviderTestError(
        err instanceof ApiError ? err.message : "Không lấy được proxy từ nhà cung cấp",
      );
    } finally {
      setTestingProvider(false);
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!form.name.trim()) return;
    setSaving(true);
    try {
      const payload: ProfileCreateData = { ...form };
      const storedProvider = profile?.proxy_provider ?? "none";
      if (payload.proxy_provider === "none") {
        // Clearing the source also clears the stored provider key.
        payload.proxy_provider_key = "";
      } else if (providerKey.trim()) {
        payload.proxy_provider_key = providerKey.trim();
      } else if (payload.proxy_provider !== storedProvider) {
        // Switching provider with an empty input must not reuse the old key.
        payload.proxy_provider_key = "";
      } else {
        delete payload.proxy_provider_key;
      }
      await onSave(payload);
      setProviderKey("");
      baseline.current = serialize(payload, "");
      setSaved(true);
      toast.success(isEdit ? `Đã lưu "${payload.name}"` : `Đã tạo hồ sơ "${payload.name}"`);
      clearTimeout(savedTimer.current);
      savedTimer.current = setTimeout(() => setSaved(false), 1500);
    } catch {
      // The hook already put the error in the banner; never show "saved".
    } finally {
      setSaving(false);
    }
  };

  const handleCancel = async () => {
    if (dirty) {
      const ok = await confirm({
        title: "Bỏ thay đổi chưa lưu?",
        description: "Các thay đổi trên form sẽ mất và hồ sơ giữ nguyên cấu hình đã lưu.",
        confirmLabel: "Bỏ thay đổi",
        cancelLabel: "Tiếp tục sửa",
        tone: "danger",
      });
      if (!ok) return;
    }
    onCancel();
  };

  const handleDelete = async () => {
    if (!onDelete) return;
    const ok = await confirm({
      title: `Xóa hồ sơ "${profile?.name ?? ""}"?`,
      description: "Cookie, phiên đăng nhập, lịch sử và tệp của hồ sơ sẽ bị xóa vĩnh viễn.",
      confirmLabel: "Xóa hồ sơ",
      tone: "danger",
    });
    if (!ok) return;
    setDeleting(true);
    try {
      await onDelete();
    } catch {
      // Shown in the banner; the form stays open on the undeleted profile.
    } finally {
      setDeleting(false);
    }
  };

  const handleReset = async () => {
    if (!onReset) return;
    const ok = await confirm({
      title: `Đặt lại hồ sơ "${profile?.name ?? ""}"?`,
      description:
        "Cookie, lịch sử và dữ liệu trang sẽ bị xóa và tạo vân tay mới. " +
        "Dấu trang, cài đặt và công cụ tìm kiếm mặc định sẽ được giữ lại.",
      confirmLabel: "Đặt lại",
      tone: "danger",
    });
    if (!ok) return;
    setResetting(true);
    try {
      await onReset();
      setResetDone(true);
      clearTimeout(resetTimer.current);
      resetTimer.current = setTimeout(() => setResetDone(false), 1500);
    } catch {
      // Shown in the banner; never report a failed reset as done.
    } finally {
      setResetting(false);
    }
  };

  useEffect(() => {
    if (!duplicateMenuOpen) return;
    const onPointerDown = (e: MouseEvent) => {
      if (!duplicateMenuRef.current?.contains(e.target as Node)) setDuplicateMenuOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      setDuplicateMenuOpen(false);
      // A focused menu item is about to unmount; hand focus back to the trigger
      // so keyboard users are not dropped onto the document body.
      duplicateTriggerRef.current?.focus();
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [duplicateMenuOpen]);

  const handleDuplicate = async (includeBrowserState: boolean) => {
    setDuplicateMenuOpen(false);
    if (!onDuplicate) return;
    const message = includeBrowserState
      ? "Sao chép hồ sơ này kèm dữ liệu trình duyệt? Một hồ sơ mới với cùng " +
        "cài đặt, vân tay, cookie và phiên đăng nhập sẽ được tạo."
      : "Sao chép hồ sơ này? Một hồ sơ mới với cùng cài đặt và vân tay sẽ được " +
        "tạo. Dữ liệu trình duyệt (cookie, lịch sử) không được sao chép.";
    const ok = await confirm({
      title: includeBrowserState ? "Sao chép kèm dữ liệu trình duyệt?" : "Sao chép hồ sơ?",
      description: message,
      confirmLabel: "Sao chép",
    });
    if (!ok) return;
    setDuplicating(true);
    try {
      await onDuplicate(includeBrowserState);
    } catch {
      // Shown in the banner; the form stays on the source profile.
    } finally {
      setDuplicating(false);
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
    <form onSubmit={handleSubmit} className="p-6 max-w-4xl mx-auto">
      {/* Header bar */}
      <div className="card p-4 mb-6 flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2.5">
          <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-brand/15 border border-brand/25 text-brand">
            <Fingerprint className="h-5 w-5" />
          </div>
          <div className="mr-2">
            <h2 className="text-base font-semibold text-fg leading-tight">
              {isEdit ? "Sửa hồ sơ" : "Hồ sơ mới"}
            </h2>
            <p className="text-2xs text-fg-muted">
              {isEdit ? `Cấu hình danh tính · ${profile?.name}` : "Thiết lập danh tính trình duyệt độc lập"}
            </p>
          </div>
          {isEdit && onDuplicate && (
            <div ref={duplicateMenuRef} className="relative flex items-stretch">
              <button
                type="button"
                onClick={() => handleDuplicate(false)}
                disabled={duplicating}
                title="Chỉ sao chép cài đặt và vân tay"
                className="btn-secondary text-xs flex items-center gap-1.5 rounded-r-none"
              >
                <Copy className="h-3.5 w-3.5" />
                <span>{duplicating ? "Đang sao chép..." : "Sao chép"}</span>
              </button>
              <button
                ref={duplicateTriggerRef}
                type="button"
                onClick={() => setDuplicateMenuOpen((open) => !open)}
                disabled={duplicating}
                aria-haspopup="menu"
                aria-expanded={duplicateMenuOpen}
                aria-label="Tùy chọn sao chép"
                className="btn-secondary flex items-center rounded-l-none border-l-0 px-2"
              >
                <ChevronDown className="h-3.5 w-3.5" />
              </button>
              {duplicateMenuOpen && (
                <div
                  role="menu"
                  className="absolute left-0 top-full z-20 mt-1.5 w-64 overflow-hidden rounded-xl border border-line-strong bg-surface-1 py-1 shadow-popover"
                >
                  <button
                    type="button"
                    role="menuitem"
                    onClick={() => handleDuplicate(false)}
                    className="w-full px-3.5 py-2.5 text-left hover:bg-surface-2 transition-colors"
                  >
                    <div className="text-xs font-medium text-fg">Chỉ cài đặt và vân tay</div>
                    <div className="text-2xs text-fg-muted">Bắt đầu với dữ liệu trình duyệt trống</div>
                  </button>
                  <button
                    type="button"
                    role="menuitem"
                    disabled={profile?.status !== "stopped"}
                    onClick={() => handleDuplicate(true)}
                    className="w-full px-3.5 py-2.5 text-left hover:bg-surface-2 transition-colors disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:bg-transparent"
                  >
                    <div className="text-xs font-medium text-fg">Kèm dữ liệu trình duyệt</div>
                    <div className="text-2xs text-fg-muted">
                      {profile?.status === "stopped"
                        ? "Cookie, phiên đăng nhập và lịch sử"
                        : "Dừng hồ sơ trước"}
                    </div>
                  </button>
                </div>
              )}
            </div>
          )}
          {isEdit && onReset && (
            <button
              type="button"
              onClick={handleReset}
              disabled={resetting || resetDone}
              className="btn-secondary text-xs flex items-center gap-1.5"
            >
              {resetDone ? <Check className="h-3.5 w-3.5 text-success" /> : <RotateCcw className="h-3.5 w-3.5" />}
              <span>{resetDone ? "Đã đặt lại" : resetting ? "Đang đặt lại..." : "Đặt lại"}</span>
            </button>
          )}
          {isEdit && onDelete && (
            <button
              type="button"
              onClick={handleDelete}
              disabled={deleting}
              className="btn-danger text-xs flex items-center gap-1.5"
            >
              <Trash2 className="h-3.5 w-3.5" />
              <span>{deleting ? "Đang xóa..." : "Xóa"}</span>
            </button>
          )}
        </div>
        <div className="flex items-center gap-2 ml-auto">
          <button type="button" onClick={handleCancel} className="btn-secondary text-xs">
            Hủy
          </button>
          <button type="submit" disabled={saving || saved} className="btn-primary text-xs flex items-center gap-1.5">
            {saved ? <Check className="h-3.5 w-3.5" /> : <Save className="h-3.5 w-3.5" />}
            <span>{saved ? "Đã lưu" : saving ? "Đang lưu..." : isEdit ? "Lưu" : "Tạo"}</span>
          </button>
        </div>
      </div>

      {/* Tab strip — keeps the long form from reading as one wall of cards. */}
      <div className="mb-4 flex flex-wrap items-center gap-1 border-b border-line" role="tablist">
        {TABS.map((t) => (
          <button
            key={t.key}
            type="button"
            role="tab"
            aria-selected={tab === t.key}
            onClick={() => setTab(t.key)}
            className={`-mb-px border-b-2 px-3 py-2 text-xs font-medium transition-colors ${
              tab === t.key
                ? "border-brand text-fg"
                : "border-transparent text-fg-muted hover:border-line-strong hover:text-fg-secondary"
            }`}
          >
            {t.label}
          </button>
        ))}
        {dirty && (
          <span className="ml-auto pb-2 pr-1 text-2xs text-warning">Có thay đổi chưa lưu</span>
        )}
      </div>

      <div className="space-y-4">
        {/* Last preview — the last frame captured before the browser stopped */}
        {isEdit && !previewError && (
          <section className="section-card" hidden={tab !== "basic"}>
            <div className="flex items-center gap-2 mb-3">
              <Image className="h-4 w-4 text-brand" />
              <h3 className="text-xs font-semibold text-fg uppercase tracking-wider">Ảnh xem trước gần nhất</h3>
            </div>
            <img
              src={`/api/profiles/${profile!.id}/screenshot?t=${previewBuster}`}
              onError={() => setPreviewError(true)}
              alt="Ảnh xem trước trình duyệt gần nhất"
              className="w-full rounded-lg border border-line bg-surface-0 object-contain max-h-72"
            />
          </section>
        )}

        {/* Basic */}
        <section className="section-card" hidden={tab !== "basic"}>
          <div className="flex items-center gap-2 mb-4">
            <Fingerprint className="h-4 w-4 text-brand" />
            <h3 className="text-xs font-semibold text-fg uppercase tracking-wider">Cơ bản</h3>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <label className="label">Tên hồ sơ</label>
              <input
                className="input"
                value={form.name}
                onChange={(e) => set("name", e.target.value)}
                placeholder="vd: Amazon Seller #1"
                required
              />
            </div>
            <div>
              <label className="label">Seed vân tay</label>
              <div className="flex gap-2">
                <input
                  className="input flex-1 no-spin font-mono"
                  type="number"
                  value={form.fingerprint_seed ?? ""}
                  onChange={(e) => set("fingerprint_seed", e.target.value ? Number(e.target.value) : null)}
                  placeholder="Tự động (ngẫu nhiên)"
                />
                <button
                  type="button"
                  onClick={randomizeSeed}
                  className="btn-secondary px-2.5"
                  title="Tạo seed ngẫu nhiên"
                >
                  <svg className="h-5 w-5" viewBox="0 0 32 32" fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round">
                    <polygon points="28,10 16,16 16,28 28,22" fill="currentColor" opacity="0.06" />
                    <polygon points="28,10 16,16 16,28 28,22" />
                    <polygon points="4,10 16,16 16,28 4,22" fill="currentColor" opacity="0.2" />
                    <polygon points="4,10 16,16 16,28 4,22" />
                    <polygon points="16,3 28,10 16,16 4,10" fill="currentColor" opacity="0.1" />
                    <polygon points="16,3 28,10 16,16 4,10" />
                    <circle cx="11.5" cy="8.5" r="1" fill="currentColor" opacity="0.7" />
                    <circle cx="16" cy="9.5" r="1" fill="currentColor" opacity="0.7" />
                    <circle cx="20.5" cy="10.5" r="1" fill="currentColor" opacity="0.7" />
                    <circle cx="7.5" cy="14" r="0.9" fill="currentColor" opacity="0.6" />
                    <circle cx="12.5" cy="16.5" r="0.9" fill="currentColor" opacity="0.6" />
                    <circle cx="10" cy="19" r="0.9" fill="currentColor" opacity="0.6" />
                    <circle cx="7.5" cy="22" r="0.9" fill="currentColor" opacity="0.6" />
                    <circle cx="12.5" cy="24.5" r="0.9" fill="currentColor" opacity="0.6" />
                    <circle cx="20" cy="15" r="0.9" fill="currentColor" opacity="0.5" />
                    <circle cx="24" cy="20" r="0.9" fill="currentColor" opacity="0.5" />
                  </svg>
                </button>
              </div>
            </div>
          </div>
        </section>

        {/* Network */}
        <section className="section-card" hidden={tab !== "network"}>
          <div className="flex items-center gap-2 mb-4">
            <Globe className="h-4 w-4 text-info" />
            <h3 className="text-xs font-semibold text-fg uppercase tracking-wider">Mạng</h3>
          </div>
          <div className="space-y-3.5">
            <div>
              <label className="label">Nguồn proxy</label>
              <select
                className="input"
                value={form.proxy_provider ?? "none"}
                onChange={(e) => {
                  set("proxy_provider", e.target.value as "none" | "kiotproxy" | "tmproxy");
                  setProxyTest(null);
                  setProviderTest(null);
                  setProviderTestError(null);
                }}
              >
                <option value="none">Tùy chỉnh (nhập tay)</option>
                <option value="kiotproxy">KiotProxy (xoay theo key)</option>
                <option value="tmproxy">TMProxy (xoay theo key)</option>
              </select>
            </div>

            {form.proxy_provider === "none" || !form.proxy_provider ? (
              <div>
                <label className="label">Proxy</label>
                <div className="flex gap-2">
                  <input
                    className="input flex-1 font-mono"
                    value={form.proxy ?? ""}
                    onChange={(e) => {
                      set("proxy", e.target.value || null);
                      setProxyTest(null);
                    }}
                    placeholder="http://user:pass@host:port"
                  />
                  <button
                    type="button"
                    className="btn-secondary text-xs whitespace-nowrap disabled:opacity-60 disabled:cursor-not-allowed flex items-center gap-1.5"
                    onClick={handleTestProxy}
                    disabled={!form.proxy || testingProxy}
                  >
                    {testingProxy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                    Kiểm tra
                  </button>
                </div>
                {proxyTest && !testingProxy && (
                  <p
                    className={`text-xs mt-1.5 font-medium ${proxyTest.ok ? "text-success" : "text-danger"}`}
                  >
                    {proxyTest.ok
                      ? `✓ ${proxyTest.ip}` +
                        (proxyTest.city || proxyTest.country
                          ? ` · ${[proxyTest.city, proxyTest.country].filter(Boolean).join(", ")}`
                          : "") +
                        (proxyTest.latency_ms != null ? ` · ${proxyTest.latency_ms}ms` : "")
                      : proxyTest.error || "Kiểm tra proxy thất bại"}
                  </p>
                )}
              </div>
            ) : (
              <div className="space-y-3">
                <div>
                  <label className="label">
                    API key {form.proxy_provider === "kiotproxy" ? "KiotProxy" : "TMProxy"}
                  </label>
                  <input
                    className="input font-mono"
                    type="password"
                    autoComplete="off"
                    spellCheck={false}
                    value={providerKey}
                    onChange={(e) => {
                      setProviderKey(e.target.value);
                      setProviderTest(null);
                      setProviderTestError(null);
                    }}
                    placeholder={
                      profile?.proxy_provider === form.proxy_provider &&
                      profile?.proxy_provider_key_set
                        ? `Đã lưu: ${profile.proxy_provider_key_masked ?? "•••"} · nhập để thay`
                        : "Nhập API key của nhà cung cấp"
                    }
                  />
                </div>

                {form.proxy_provider === "kiotproxy" ? (
                  <div>
                    <label className="label">Vùng</label>
                    <select
                      className="input"
                      value={form.kiotproxy_region ?? "random"}
                      onChange={(e) =>
                        set(
                          "kiotproxy_region",
                          e.target.value as "bac" | "trung" | "nam" | "random",
                        )
                      }
                    >
                      <option value="random">Ngẫu nhiên toàn hệ thống</option>
                      <option value="bac">Miền Bắc</option>
                      <option value="trung">Miền Trung</option>
                      <option value="nam">Miền Nam</option>
                    </select>
                  </div>
                ) : (
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                    <div>
                      <label className="label">Khu vực</label>
                      <select
                        className="input"
                        value={form.tmproxy_location_id ?? 0}
                        onChange={(e) => set("tmproxy_location_id", Number(e.target.value))}
                      >
                        <option value={0}>Ngẫu nhiên</option>
                        {tmLocations.map((location) => (
                          <option key={location.id} value={location.id}>
                            {location.name ?? `#${location.id}`}
                          </option>
                        ))}
                      </select>
                    </div>
                    <div>
                      <label className="label">Nhà mạng (ISP)</label>
                      <select
                        className="input"
                        value={form.tmproxy_isp_id ?? 0}
                        onChange={(e) => set("tmproxy_isp_id", Number(e.target.value))}
                      >
                        <option value={0}>Ngẫu nhiên</option>
                        {tmIsps.map((isp) => (
                          <option key={isp.id} value={isp.id}>
                            {isp.name ?? `#${isp.id}`}
                          </option>
                        ))}
                      </select>
                    </div>
                  </div>
                )}

                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    className="btn-secondary text-xs whitespace-nowrap disabled:opacity-60 disabled:cursor-not-allowed flex items-center gap-1.5"
                    onClick={handleTestProvider}
                    disabled={!providerKey.trim() || testingProvider}
                  >
                    {testingProvider && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                    Lấy proxy mới
                  </button>
                  <p className="text-2xs text-fg-muted">
                    Proxy được lấy mới mỗi lần mở hồ sơ. Mỗi key chỉ dùng cho một hồ sơ đang chạy.
                  </p>
                </div>

                {providerTest && !testingProvider && (
                  <p className="text-xs font-medium text-success">
                    ✓ {providerTest.real_ip ?? providerTest.host}
                    {providerTest.location ? ` · ${providerTest.location}` : ""}
                    {providerTest.isp ? ` · ${providerTest.isp}` : ""}
                  </p>
                )}
                {providerTestError && !testingProvider && (
                  <p className="text-xs font-medium text-danger">{providerTestError}</p>
                )}
              </div>
            )}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              <div>
                <label className="label">Múi giờ</label>
                <input
                  className="input"
                  value={form.timezone ?? ""}
                  onChange={(e) => set("timezone", e.target.value || null)}
                  placeholder="America/New_York"
                />
              </div>
              <div>
                <label className="label">Ngôn ngữ</label>
                <input
                  className="input"
                  value={form.locale ?? ""}
                  onChange={(e) => set("locale", e.target.value || null)}
                  placeholder="en-US"
                />
              </div>
            </div>
            <label className="option-row items-center">
              <input
                type="checkbox"
                checked={form.geoip ?? false}
                onChange={(e) => set("geoip", e.target.checked)}
                className="h-4 w-4 rounded border-line bg-surface-2"
              />
              <span>
                Tự động nhận diện múi giờ/ngôn ngữ từ IP thoát của proxy, hoặc IP công cộng của máy nếu không dùng proxy (GeoIP)
              </span>
            </label>
          </div>
        </section>

        {/* Hardware */}
        <section className="section-card" hidden={tab !== "hardware"}>
          <div className="flex items-center gap-2 mb-4">
            <Cpu className="h-4 w-4 text-warning" />
            <h3 className="text-xs font-semibold text-fg uppercase tracking-wider">Phần cứng</h3>
          </div>
          <div className="space-y-3.5">
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              <div>
                <label className="label">Độ phân giải màn hình</label>
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
                  <option value="custom">Tùy chỉnh</option>
                </select>
              </div>
              <div>
                <label className="label">Dòng GPU</label>
                {hostOs === "macos" ? (
                  <select className="input" value="auto" disabled>
                    <option value="auto">Apple Silicon (tự động)</option>
                  </select>
                ) : hostOs === null ? (
                  <select className="input" value="auto" disabled>
                    <option value="auto">Tự động (đang nhận diện môi trường…)</option>
                  </select>
                ) : (
                  <select
                    className="input"
                    value={form.gpu_family ?? "auto"}
                    onChange={(e) => set("gpu_family", e.target.value as "auto" | "nvidia" | "intel")}
                  >
                    <option value="auto">Tự động (theo seed)</option>
                    <option value="nvidia">NVIDIA</option>
                    <option value="intel">Intel</option>
                  </select>
                )}
              </div>
            </div>
            {currentResolution === "custom" && (
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="label">Chiều rộng</label>
                  <input
                    className="input"
                    type="number"
                    value={form.screen_width ?? 1920}
                    onChange={(e) => set("screen_width", Number(e.target.value))}
                  />
                </div>
                <div>
                  <label className="label">Chiều cao</label>
                  <input
                    className="input"
                    type="number"
                    value={form.screen_height ?? 1080}
                    onChange={(e) => set("screen_height", Number(e.target.value))}
                  />
                </div>
              </div>
            )}
            <p className="text-xs text-fg-muted">
              {hostOs === "macos"
                ? "Seed sẽ chọn một mẫu Apple Silicon và cấu hình phần cứng tương ứng."
                : "Seed sẽ chọn GPU, CPU, bộ nhớ và cấu hình màn hình đồng bộ trong cùng dòng."}
            </p>
          </div>
        </section>

        {/* Behavior */}
        <section className="section-card" hidden={tab !== "hardware"}>
          <div className="flex items-center gap-2 mb-4">
            <Sliders className="h-4 w-4 text-success" />
            <h3 className="text-xs font-semibold text-fg uppercase tracking-wider">Hành vi</h3>
          </div>
          <div className="space-y-2.5">
            <label className="option-row items-center">
              <input
                type="checkbox"
                checked={form.humanize ?? false}
                onChange={(e) => set("humanize", e.target.checked)}
                className="h-4 w-4 rounded border-line bg-surface-2"
              />
              <span>Mô phỏng chuột, bàn phím và cuộn như người thật</span>
            </label>
            {form.humanize && (
              <div className="pl-7 pb-1">
                <label className="label">Mức mô phỏng</label>
                <select
                  className="input"
                  value={form.human_preset}
                  onChange={(e) => set("human_preset", e.target.value)}
                >
                  <option value="default">Mặc định (tốc độ bình thường)</option>
                  <option value="careful">Thận trọng (chậm và kỹ hơn)</option>
                </select>
              </div>
            )}
            {viewerMode === "vnc" && (
              <label className="option-row items-center">
                <input
                  type="checkbox"
                  checked={form.clipboard_sync ?? true}
                  onChange={(e) => set("clipboard_sync", e.target.checked)}
                  className="h-4 w-4 rounded border-line bg-surface-2"
                />
                <span>Bật đồng bộ clipboard mặc định trong trình xem VNC</span>
              </label>
            )}
            <label className="option-row items-center">
              <input
                type="checkbox"
                checked={form.auto_launch ?? false}
                onChange={(e) => set("auto_launch", e.target.checked)}
                className="h-4 w-4 rounded border-line bg-surface-2"
              />
              <span>Tự động khởi chạy khi Manager bắt đầu</span>
            </label>
            <label className="option-row">
              <input
                type="checkbox"
                checked={form.capture_preview ?? true}
                onChange={(e) => set("capture_preview", e.target.checked)}
                className="h-4 w-4 rounded border-line bg-surface-2 mt-0.5"
              />
              <span>
                <span className="font-medium text-fg">Lưu ảnh xem trước của trình duyệt</span>
                <span className="block text-xs text-fg-muted mt-0.5">
                  Chụp trang định kỳ khi đang chạy, hiển thị ở đây sau khi hồ sơ dừng.
                </span>
              </span>
            </label>
            <label className="option-row">
              <input
                type="checkbox"
                checked={form.restore_session ?? true}
                onChange={(e) => set("restore_session", e.target.checked)}
                className="h-4 w-4 rounded border-line bg-surface-2 mt-0.5"
              />
              <span>
                <span className="font-medium text-fg">Khôi phục các tab trước khi khởi chạy</span>
                <span className="block text-xs text-fg-muted mt-0.5">
                  Mở lại các tab đang mở khi hồ sơ này dừng lần trước.
                </span>
              </span>
            </label>
          </div>
        </section>

        {/* Compatibility */}
        <section className="section-card" hidden={tab !== "options"}>
          <div className="flex items-center gap-2 mb-4">
            <ShieldCheck className="h-4 w-4 text-brand" />
            <h3 className="text-xs font-semibold text-fg uppercase tracking-wider">Tương thích</h3>
          </div>
          <div className="space-y-2.5">
            <label className="option-row items-center">
              <input
                type="checkbox"
                checked={form.allow_3p_cookies ?? false}
                onChange={(e) => set("allow_3p_cookies", e.target.checked)}
                className="h-4 w-4 rounded border-line bg-surface-2"
              />
              <span>Cho phép cookie bên thứ ba cho đăng nhập, SSO và các luồng thử thách</span>
            </label>
            <label className="option-row">
              <input
                type="checkbox"
                checked={form.transparent_proxy ?? false}
                onChange={(e) => set("transparent_proxy", e.target.checked)}
                className="h-4 w-4 rounded border-line bg-surface-2 mt-0.5"
              />
              <span>
                <span className="font-medium text-fg">Kết nối proxy trong suốt</span>
                <span className="block text-xs text-fg-muted mt-0.5">
                  Chế độ kết nối proxy mạnh hơn. Yêu cầu proxy http:// có tên đăng nhập và mật khẩu.
                </span>
              </span>
            </label>
            <label className="option-row">
              <input
                type="checkbox"
                checked={form.portable_cookies ?? false}
                onChange={(e) => set("portable_cookies", e.target.checked)}
                className="h-4 w-4 rounded border-line bg-surface-2 mt-0.5"
              />
              <span>
                <span className="font-medium text-fg">Cookie di động</span>
                <span className="block text-xs text-fg-muted mt-0.5">
                  Cho phép chuyển hồ sơ này sang máy khác kèm phiên đăng nhập. Chỉ cookie được lưu khi bật tùy chọn này mới di động được, nên hãy bật trước khi đăng nhập.
                </span>
              </span>
            </label>
            {keys.length > 0 && (
              <div className="pt-1">
                <label className="label">Key bản quyền</label>
                <select
                  className="input"
                  value={form.license_key_id ?? ""}
                  onChange={(e) => set("license_key_id", e.target.value || null)}
                >
                  <option value="">Key trống bất kỳ khi khởi chạy</option>
                  {keys.map((entry) => (
                    <option key={entry.id} value={entry.id}>
                      {[
                        entry.key_masked,
                        entry.state === "valid" ? (entry.plan === "free" ? "miễn phí" : entry.plan) : null,
                        entry.leased_to ? `đang dùng bởi ${entry.leased_to}` : null,
                        entry.state === "valid" ? null : KEY_STATE_LABEL[entry.state] ?? entry.state,
                      ]
                        .filter(Boolean)
                        .join(" · ")}
                    </option>
                  ))}
                </select>
                <p className="mt-1 text-xs text-fg-muted">
                  Ghim sẽ giữ một key cho hồ sơ này; để "key trống bất kỳ" thì
                  Manager sẽ thuê key chưa dùng.
                </p>
              </div>
            )}
            <label className="option-row">
              <input
                type="checkbox"
                checked={form.set_google_default ?? true}
                onChange={(e) => set("set_google_default", e.target.checked)}
                className="h-4 w-4 rounded border-line bg-surface-2 mt-0.5"
              />
              <span>
                <span className="font-medium text-fg">Đặt Google làm công cụ tìm kiếm mặc định</span>
                <span className="block text-xs text-fg-muted mt-0.5">
                  Thêm vài giây vào lần khởi chạy đầu tiên của hồ sơ (thiết lập một lần).
                </span>
              </span>
            </label>
          </div>
        </section>

        {/* Tags */}
        <section className="section-card" hidden={tab !== "options"}>
          <div className="flex items-center gap-2 mb-4">
            <Tag className="h-4 w-4 text-info" />
            <h3 className="text-xs font-semibold text-fg uppercase tracking-wider">Thẻ</h3>
          </div>
          {(form.tags ?? []).length > 0 && (
            <div className="flex flex-wrap gap-1.5 mb-3">
              {(form.tags ?? []).map((t) => (
                <span
                  key={t.tag}
                  className="inline-flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-full border border-line bg-surface-3 text-fg-secondary"
                  style={
                    t.color
                      ? { backgroundColor: `${t.color}18`, color: t.color, borderColor: `${t.color}35` }
                      : undefined
                  }
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
          <div className="flex flex-wrap gap-2.5 items-center">
            <div className="flex gap-1.5 rounded-lg border border-line bg-surface-0/70 px-2.5 py-1.5">
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
              className="input flex-1 min-w-[180px]"
              value={tagInput}
              onChange={(e) => setTagInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); addTag(); } }}
              placeholder="Thêm thẻ..."
            />
            <button type="button" onClick={addTag} className="btn-secondary text-xs">
              Thêm
            </button>
          </div>
        </section>

        {/* Extensions */}
        <section className="section-card" hidden={tab !== "options"}>
          <div className="flex items-center gap-2 mb-1.5">
            <Puzzle className="h-4 w-4 text-warning" />
            <h3 className="text-xs font-semibold text-fg uppercase tracking-wider">Tiện ích</h3>
          </div>
          <p className="text-xs text-fg-muted mb-3">Mỗi đường dẫn là một thư mục tiện ích Chrome chưa đóng gói.</p>
          {(form.extension_paths ?? []).length > 0 && (
            <div className="space-y-1.5 mb-3">
              {(form.extension_paths ?? []).map((path, index) => (
                <div key={`${path}-${index}`} className="flex items-center gap-2 rounded-lg border border-line bg-surface-2/60 px-3 py-1.5">
                  <code className="flex-1 truncate text-xs text-fg-secondary font-mono">{path}</code>
                  <button type="button" onClick={() => removeExtensionPath(index)} className="text-fg-muted hover:text-danger">
                    <X className="h-3.5 w-3.5" />
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
            <button type="button" onClick={addExtensionPath} className="btn-secondary text-xs">Thêm</button>
          </div>
        </section>

        {/* Advanced */}
        <details className="section-card group" hidden={tab !== "options"}>
          <summary className="cursor-pointer flex items-center gap-2 text-xs font-semibold text-fg uppercase tracking-wider list-none">
            <Terminal className="h-4 w-4 text-fg-muted group-open:text-brand transition-colors" />
            <span>Tham số khởi chạy nâng cao</span>
            <ChevronDown className="ml-auto h-4 w-4 text-fg-muted transition-transform group-open:rotate-180" />
          </summary>
          <p className="text-xs text-fg-muted my-3">
            Cờ Chromium không giới hạn. Tham số nâng cao có thể ghi đè hành vi do Manager kiểm soát.
          </p>
          {(form.launch_args ?? []).length > 0 && (
            <div className="flex flex-wrap gap-1.5 mb-3">
              {(form.launch_args ?? []).map((arg, index) => (
                <span
                  key={`${arg}-${index}`}
                  className="inline-flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-full border border-line bg-surface-2 text-fg-secondary font-mono"
                >
                  {arg}
                  <button type="button" onClick={() => removeLaunchArg(index)} className="hover:text-danger">
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
            <button type="button" onClick={addLaunchArg} className="btn-secondary text-xs">Thêm</button>
          </div>
        </details>

        {/* Notes */}
        <section className="section-card" hidden={tab !== "basic"}>
          <div className="flex items-center gap-2 mb-3">
            <FileText className="h-4 w-4 text-fg-secondary" />
            <h3 className="text-xs font-semibold text-fg uppercase tracking-wider">Ghi chú</h3>
          </div>
          <textarea
            className="input min-h-[88px] resize-y"
            value={form.notes ?? ""}
            onChange={(e) => set("notes", e.target.value || null)}
            placeholder="Ghi chú tùy chọn về hồ sơ này..."
          />
        </section>
      </div>
    </form>
  );
}
