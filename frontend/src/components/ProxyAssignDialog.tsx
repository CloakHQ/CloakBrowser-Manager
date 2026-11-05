import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  Check,
  Globe,
  KeyRound,
  Link2Off,
  Loader2,
  Search,
  ShieldCheck,
  X,
} from "lucide-react";
import {
  api,
  type Profile,
  type ProfileCreateData,
  type ProxyKeyOption,
  type ProxyProvider,
  type TmProxyOption,
} from "../lib/api";
import { PROVIDER_LABEL, profileProxyLabel } from "./ProfileTableParts";
import { useToast } from "./ui/toast";

type Mode = ProxyProvider | "direct";
type KeySource = "random" | "specific" | "manual";
type RotationMode = "per_launch" | "sticky" | "manual";

interface ProxyAssignDialogProps {
  /** Profiles offered for selection (already the whole list). */
  profiles: Profile[];
  initialSelection: string[];
  onClose: () => void;
  /** Called after every successful write so the caller can refresh status. */
  onApplied: () => void;
}

const MODES: { key: Mode; label: string; icon: typeof Globe; hint: string }[] = [
  { key: "none", label: "Proxy thủ công", icon: Globe, hint: "Một địa chỉ dùng chung cho cả nhóm" },
  { key: "kiotproxy", label: "KiotProxy", icon: KeyRound, hint: "Dùng key đã lưu hoặc key dán tay" },
  { key: "tmproxy", label: "TMProxy", icon: KeyRound, hint: "Dùng key đã lưu hoặc key dán tay" },
  { key: "direct", label: "Bỏ proxy", icon: Link2Off, hint: "Đi trực tiếp, không qua proxy" },
];

const ROTATION_MODES: { key: RotationMode; label: string; hint: string }[] = [
  { key: "per_launch", label: "Mỗi lần mở", hint: "Lấy IP mới khi khởi chạy" },
  { key: "sticky", label: "Giữ IP", hint: "Dùng lại đến khi hết hạn" },
  { key: "manual", label: "Thủ công", hint: "Chỉ đổi khi bấm nút" },
];

/** Accepts host:port, user:pass@host:port and any scheme//host:port form. */
export function isProxyAddress(value: string): boolean {
  const trimmed = value.trim();
  if (!trimmed || /\s/.test(trimmed)) return false;
  const withoutScheme = trimmed.replace(/^[a-z][a-z0-9+.-]*:\/\//i, "");
  const hostPort = withoutScheme.includes("@")
    ? withoutScheme.slice(withoutScheme.lastIndexOf("@") + 1)
    : withoutScheme;
  return /^[^:/]+:\d{1,5}(\/.*)?$/i.test(hostPort);
}

/**
 * Assign one proxy setting to many profiles at once. Writes go through the
 * normal per-profile update endpoint, one at a time, so validation and the
 * "takes effect on next launch" behaviour stay exactly the same as editing a
 * profile by hand.
 */
export function ProxyAssignDialog({
  profiles,
  initialSelection,
  onClose,
  onApplied,
}: ProxyAssignDialogProps) {
  const toast = useToast();
  const [selected, setSelected] = useState<Set<string>>(() => new Set(initialSelection));
  const [search, setSearch] = useState("");
  const [mode, setMode] = useState<Mode>("none");
  const [keySource, setKeySource] = useState<KeySource>("random");
  const [keyChoice, setKeyChoice] = useState("");
  const [proxyUrl, setProxyUrl] = useState("");
  const [providerKey, setProviderKey] = useState("");
  const [rotationMode, setRotationMode] = useState<RotationMode>("per_launch");
  const [kiotRegion, setKiotRegion] = useState<"bac" | "trung" | "nam" | "random">("random");
  const [tmLocation, setTmLocation] = useState(0);
  const [tmIsp, setTmIsp] = useState(0);
  const [locations, setLocations] = useState<TmProxyOption[]>([]);
  const [isps, setIsps] = useState<TmProxyOption[]>([]);
  const [keyOptions, setKeyOptions] = useState<ProxyKeyOption[]>([]);
  const [keysLoading, setKeysLoading] = useState(false);
  const [keysError, setKeysError] = useState<string | null>(null);
  const [testing, setTesting] = useState(false);
  const [test, setTest] = useState<{ ok: boolean; text: string } | null>(null);
  const [applying, setApplying] = useState(false);
  const [progress, setProgress] = useState({ done: 0, total: 0 });
  const [errors, setErrors] = useState<{ name: string; message: string }[]>([]);
  const searchRef = useRef<HTMLInputElement>(null);
  const returnFocus = useRef<HTMLElement | null>(document.activeElement as HTMLElement | null);

  const providerMode = mode === "kiotproxy" || mode === "tmproxy";

  useEffect(() => {
    searchRef.current?.focus();
  }, []);

  useEffect(() => {
    if (mode !== "tmproxy" || locations.length > 0) return;
    api.tmproxyLocations().then(setLocations).catch(() => setLocations([]));
    api.tmproxyIsps().then(setIsps).catch(() => setIsps([]));
  }, [mode, locations.length]);

  useEffect(() => {
    setKeyChoice("");
    setKeysError(null);
    if (!providerMode) {
      setKeyOptions([]);
      setKeysLoading(false);
      return;
    }
    let cancelled = false;
    setKeysLoading(true);
    api
      .availableProxyKeys(mode)
      .then((options) => {
        if (!cancelled) setKeyOptions(options);
      })
      .catch((err) => {
        if (!cancelled) {
          setKeysError(err instanceof Error ? err.message : "Không tải được danh sách key");
        }
      })
      .finally(() => {
        if (!cancelled) setKeysLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [mode, providerMode]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !applying) {
        e.preventDefault();
        onClose();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [applying, onClose]);

  const close = () => {
    onClose();
    returnFocus.current?.focus?.();
  };

  const visible = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return profiles;
    return profiles.filter(
      (p) =>
        p.name.toLowerCase().includes(q) ||
        (profileProxyLabel(p) ?? "").toLowerCase().includes(q),
    );
  }, [profiles, search]);

  const groupedKeys = useMemo(() => {
    const groups = new Map<string, { id: string; label: string; options: ProxyKeyOption[] }>();
    for (const option of keyOptions) {
      const group = groups.get(option.account_id) ?? {
        id: option.account_id,
        label: option.account_label || option.account_masked || option.account_id,
        options: [],
      };
      group.options.push(option);
      groups.set(option.account_id, group);
    }
    return [...groups.values()];
  }, [keyOptions]);

  const allVisibleSelected = visible.length > 0 && visible.every((p) => selected.has(p.id));

  const toggle = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const toggleAllVisible = () =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (allVisibleSelected) visible.forEach((p) => next.delete(p.id));
      else visible.forEach((p) => next.add(p.id));
      return next;
    });

  const chosen = profiles.filter((p) => selected.has(p.id));
  const runningChosen = chosen.filter((p) => p.status !== "stopped").length;

  const manualValid = isProxyAddress(proxyUrl);
  const availableKeys = keyOptions.filter((option) => option.key_id && !option.error);
  const canApply =
    chosen.length > 0 &&
    !applying &&
    (mode === "direct" ||
      (mode === "none" && manualValid) ||
      (providerMode &&
        (keySource === "manual"
          ? providerKey.trim().length > 0
          : keySource === "random"
            ? availableKeys.length > 0
            : keyChoice.length > 0)));

  const handleTest = async () => {
    if (!manualValid) {
      setTest({ ok: false, text: "Địa chỉ proxy phải có dạng host:port hoặc user:pass@host:port" });
      return;
    }
    setTesting(true);
    setTest(null);
    try {
      const result = await api.testProxy(proxyUrl.trim());
      setTest({
        ok: Boolean(result.ok),
        text: result.ok
          ? `${result.ip ?? "?"}${
              result.city || result.country
                ? ` · ${[result.city, result.country].filter(Boolean).join(", ")}`
                : ""
            }${result.latency_ms != null ? ` · ${result.latency_ms}ms` : ""}`
          : result.error || "Không kết nối được tới proxy",
      });
    } catch (err) {
      setTest({ ok: false, text: err instanceof Error ? err.message : "Kiểm tra proxy thất bại" });
    } finally {
      setTesting(false);
    }
  };

  const payload = (): Partial<ProfileCreateData> => {
    if (mode === "direct") {
      return {
        proxy: null,
        proxy_provider: "none",
        proxy_provider_key: "",
        proxy_account_id: null,
        proxy_key_id: null,
      };
    }
    if (mode === "none") {
      return {
        proxy: proxyUrl.trim(),
        proxy_provider: "none",
        proxy_provider_key: "",
        proxy_account_id: null,
        proxy_key_id: null,
      };
    }

    const base: Partial<ProfileCreateData> = {
      proxy: null,
      proxy_provider: mode,
      proxy_provider_key: "",
      proxy_account_id: null,
      proxy_key_id: null,
      proxy_rotation_mode: rotationMode,
      ...(mode === "kiotproxy"
        ? { kiotproxy_region: kiotRegion }
        : { tmproxy_location_id: tmLocation, tmproxy_isp_id: tmIsp }),
    };
    if (keySource === "manual") {
      return { ...base, proxy_provider_key: providerKey.trim() };
    }
    if (keySource === "random") {
      return { ...base, proxy_key_id: "random" };
    }
    const [accountId, ...rest] = keyChoice.split("::");
    return {
      ...base,
      proxy_account_id: accountId,
      proxy_key_id: rest.join("::") || accountId,
    };
  };

  const handleApply = async () => {
    if (!canApply) return;
    const body = payload();
    setApplying(true);
    setErrors([]);
    setProgress({ done: 0, total: chosen.length });
    const failed: { name: string; message: string }[] = [];
    for (const [index, profile] of chosen.entries()) {
      try {
        await api.updateProfile(profile.id, body);
        onApplied();
      } catch (err) {
        failed.push({
          name: profile.name,
          message: err instanceof Error ? err.message : "Không lưu được",
        });
      }
      setProgress({ done: index + 1, total: chosen.length });
    }
    setApplying(false);

    const ok = chosen.length - failed.length;
    if (failed.length === 0) {
      toast.success(
        mode === "direct" ? `Đã bỏ proxy cho ${ok} hồ sơ` : `Đã gán proxy cho ${ok} hồ sơ`,
        "Proxy mới có hiệu lực từ lần khởi chạy tiếp theo.",
      );
      close();
      return;
    }
    setErrors(failed);
    if (ok > 0) toast.info(`Đã gán cho ${ok} hồ sơ, ${failed.length} hồ sơ lỗi`);
  };

  return (
    <div
      className="fixed inset-0 z-modal flex items-center justify-center bg-black/70 px-4 backdrop-blur-sm"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget && !applying) close();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="assign-title"
        className="card flex max-h-[90vh] w-full max-w-3xl flex-col overflow-hidden shadow-popover"
      >
        <div className="flex items-start justify-between gap-3 border-b border-line px-5 py-3.5">
          <div>
            <h2 id="assign-title" className="text-base font-semibold text-fg">
              Gán proxy hàng loạt
            </h2>
            <p className="mt-0.5 text-xs text-fg-muted">
              Cùng một proxy hoặc cùng một key cho nhiều hồ sơ. Không cần mở từng hồ sơ.
            </p>
          </div>
          <button
            type="button"
            onClick={close}
            disabled={applying}
            className="rail-item"
            aria-label="Đóng"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="grid min-h-0 flex-1 gap-0 overflow-y-auto md:grid-cols-[minmax(0,1fr)_16rem]">
          {/* Proxy settings */}
          <div className="space-y-4 p-5">
            <div>
              <p className="label">Nguồn proxy</p>
              <div className="grid grid-cols-2 gap-2">
                {MODES.map(({ key, label, icon: Icon, hint }) => {
                  const active = mode === key;
                  return (
                    <button
                      key={key}
                      type="button"
                      aria-pressed={active}
                      onClick={() => {
                        setMode(key);
                        setTest(null);
                      }}
                      className={`flex items-start gap-2 rounded-lg border p-2.5 text-left transition-colors ${
                        active
                          ? "border-brand/40 bg-brand-subtle"
                          : "border-line bg-surface-2/40 hover:border-line-strong hover:bg-surface-2/70"
                      }`}
                    >
                      <Icon
                        className={`mt-0.5 h-3.5 w-3.5 flex-shrink-0 ${
                          active ? "text-brand-fg" : "text-fg-muted"
                        }`}
                      />
                      <span className="min-w-0">
                        <span
                          className={`block text-xs font-medium ${
                            active ? "text-fg" : "text-fg-secondary"
                          }`}
                        >
                          {label}
                        </span>
                        <span className="mt-0.5 block text-2xs leading-snug text-fg-muted">
                          {hint}
                        </span>
                      </span>
                    </button>
                  );
                })}
              </div>
            </div>

            {mode === "none" && (
              <div>
                <label className="label" htmlFor="bulk-proxy">
                  Địa chỉ proxy
                </label>
                <div className="flex gap-2">
                  <input
                    id="bulk-proxy"
                    className="input flex-1 font-mono text-xs"
                    value={proxyUrl}
                    onChange={(e) => {
                      setProxyUrl(e.target.value);
                      setTest(null);
                    }}
                    placeholder="http://user:pass@host:port"
                    spellCheck={false}
                  />
                  <button
                    type="button"
                    onClick={handleTest}
                    disabled={testing || !proxyUrl.trim()}
                    className="btn-secondary whitespace-nowrap text-xs"
                  >
                    {testing && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                    Kiểm tra
                  </button>
                </div>
                {test && !testing && (
                  <p
                    className={`mt-1.5 text-xs font-medium ${
                      test.ok ? "text-success" : "text-danger"
                    }`}
                  >
                    {test.ok ? "✓ " : ""}
                    {test.text}
                  </p>
                )}
                <p className="mt-2 text-2xs leading-relaxed text-fg-muted">
                  Một proxy thủ công dùng chung được cho nhiều hồ sơ, kể cả khi chạy
                  cùng lúc. Manager chỉ hiển thị host, không hiển thị tên đăng nhập hay
                  mật khẩu.
                </p>
              </div>
            )}

            {providerMode && (
              <div className="space-y-3">
                <div>
                  <p className="label">Key nguồn</p>
                  <div className="grid grid-cols-3 gap-1.5">
                    {(
                      [
                        { key: "random", label: "Ngẫu nhiên" },
                        { key: "specific", label: "Ghim key" },
                        { key: "manual", label: "Dán tay" },
                      ] as { key: KeySource; label: string }[]
                    ).map((item) => (
                      <button
                        key={item.key}
                        type="button"
                        aria-pressed={keySource === item.key}
                        onClick={() => setKeySource(item.key)}
                        className={`rounded-md border px-2 py-1.5 text-xs transition-colors ${
                          keySource === item.key
                            ? "border-brand/40 bg-brand-subtle font-medium text-fg"
                            : "border-line bg-surface-2/40 text-fg-secondary hover:border-line-strong"
                        }`}
                      >
                        {item.label}
                      </button>
                    ))}
                  </div>
                </div>

                {keySource !== "manual" && (
                  <div>
                    {keySource === "specific" ? (
                      <select
                        className="input text-xs"
                        aria-label="Key đã lưu"
                        value={keyChoice}
                        onChange={(e) => setKeyChoice(e.target.value)}
                        disabled={keysLoading}
                      >
                        <option value="">
                          {keysLoading ? "Đang tải key…" : "Chọn key đã lưu…"}
                        </option>
                        {groupedKeys.map((group) => (
                          <optgroup key={group.id} label={group.label}>
                            {group.options.map((option) => {
                              if (option.error) {
                                return (
                                  <option
                                    key={`${option.account_id}-error`}
                                    value={`__error_${option.account_id}`}
                                    disabled
                                  >
                                    {group.label}: {option.error}
                                  </option>
                                );
                              }
                              const value = `${option.account_id}::${option.key_id ?? ""}`;
                              return (
                                <option
                                  key={value}
                                  value={value}
                                  disabled={!option.key_id || option.in_use}
                                >
                                  {option.key_masked ?? option.key_id ?? "key"}
                                  {option.status ? ` · ${option.status}` : ""}
                                  {option.in_use ? " · đang dùng" : ""}
                                  {option.cooldown_remaining > 0
                                    ? ` · chờ ${option.cooldown_remaining}s`
                                    : ""}
                                </option>
                              );
                            })}
                          </optgroup>
                        ))}
                      </select>
                    ) : (
                      <p className="rounded-lg border border-line bg-surface-2/40 px-3 py-2 text-2xs leading-relaxed text-fg-muted">
                        Mỗi lần mở, Manager chọn key còn trống và chưa bị hồ sơ khác
                        giữ. {availableKeys.length} key khả dụng.
                      </p>
                    )}
                    {keysError && (
                      <p className="mt-1.5 text-2xs font-medium text-danger">{keysError}</p>
                    )}
                  </div>
                )}

                {keySource === "manual" && (
                  <div>
                    <label className="label" htmlFor="bulk-provider-key">
                      API key {mode === "kiotproxy" ? "KiotProxy" : "TMProxy"}
                    </label>
                    <input
                      id="bulk-provider-key"
                      className="input font-mono text-xs"
                      type="password"
                      autoComplete="off"
                      spellCheck={false}
                      value={providerKey}
                      onChange={(e) => setProviderKey(e.target.value)}
                      placeholder="Dán key của nhà cung cấp"
                    />
                    <p className="mt-1.5 flex items-start gap-1.5 text-2xs leading-relaxed text-fg-muted">
                      <AlertTriangle className="mt-0.5 h-3 w-3 flex-shrink-0 text-warning" />
                      <span>
                        Key dán tay được lưu vào từng hồ sơ và không hiển thị lại. Nếu
                        gán cùng key cho nhiều hồ sơ, chúng không thể chạy đồng thời.
                      </span>
                    </p>
                  </div>
                )}

                <div>
                  <label className="label" htmlFor="bulk-rotation-mode">
                    Chế độ đổi IP
                  </label>
                  <div className="grid grid-cols-3 gap-1.5">
                    {ROTATION_MODES.map((item) => (
                      <button
                        key={item.key}
                        type="button"
                        aria-pressed={rotationMode === item.key}
                        onClick={() => setRotationMode(item.key)}
                        title={item.hint}
                        className={`rounded-md border px-2 py-1.5 text-xs transition-colors ${
                          rotationMode === item.key
                            ? "border-brand/40 bg-brand-subtle font-medium text-fg"
                            : "border-line bg-surface-2/40 text-fg-secondary hover:border-line-strong"
                        }`}
                      >
                        {item.label}
                      </button>
                    ))}
                  </div>
                  <p className="mt-1.5 text-2xs text-fg-muted">
                    {ROTATION_MODES.find((item) => item.key === rotationMode)?.hint}
                  </p>
                </div>

                {mode === "kiotproxy" ? (
                  <div>
                    <label className="label" htmlFor="bulk-region">
                      Vùng
                    </label>
                    <select
                      id="bulk-region"
                      className="input text-xs"
                      value={kiotRegion}
                      onChange={(e) => setKiotRegion(e.target.value as typeof kiotRegion)}
                    >
                      <option value="random">Ngẫu nhiên toàn hệ thống</option>
                      <option value="bac">Miền Bắc</option>
                      <option value="trung">Miền Trung</option>
                      <option value="nam">Miền Nam</option>
                    </select>
                  </div>
                ) : (
                  <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                    <div>
                      <label className="label" htmlFor="bulk-tm-location">
                        Khu vực
                      </label>
                      <select
                        id="bulk-tm-location"
                        className="input text-xs"
                        value={tmLocation}
                        onChange={(e) => setTmLocation(Number(e.target.value))}
                      >
                        <option value={0}>Ngẫu nhiên</option>
                        {locations.map((item) => (
                          <option key={item.id} value={item.id}>
                            {item.name ?? `#${item.id}`}
                          </option>
                        ))}
                      </select>
                    </div>
                    <div>
                      <label className="label" htmlFor="bulk-tm-isp">
                        Nhà mạng
                      </label>
                      <select
                        id="bulk-tm-isp"
                        className="input text-xs"
                        value={tmIsp}
                        onChange={(e) => setTmIsp(Number(e.target.value))}
                      >
                        <option value={0}>Ngẫu nhiên</option>
                        {isps.map((item) => (
                          <option key={item.id} value={item.id}>
                            {item.name ?? `#${item.id}`}
                          </option>
                        ))}
                      </select>
                    </div>
                  </div>
                )}
              </div>
            )}

            {mode === "direct" && (
              <p className="rounded-lg border border-line bg-surface-2/40 px-3 py-2.5 text-xs text-fg-secondary">
                Các hồ sơ đã chọn sẽ bỏ cấu hình proxy (thủ công hoặc nhà cung cấp) và
                đi trực tiếp bằng IP của máy. Có thể gán lại proxy bất cứ lúc nào.
              </p>
            )}

            <p className="flex items-start gap-1.5 text-2xs text-fg-muted">
              <ShieldCheck className="mt-0.5 h-3 w-3 flex-shrink-0 text-success" />
              {mode === "direct"
                ? "Không cần khởi động lại Manager."
                : "Proxy mới áp dụng từ lần khởi chạy tiếp theo; hồ sơ đang chạy không bị đổi giữa chừng."}
            </p>
            {runningChosen > 0 && mode !== "direct" && (
              <p className="flex items-start gap-1.5 text-2xs text-warning">
                <AlertTriangle className="mt-0.5 h-3 w-3 flex-shrink-0" />
                {runningChosen} hồ sơ đang chạy sẽ giữ proxy hiện tại cho tới khi được
                khởi chạy lại.
              </p>
            )}

            {errors.length > 0 && (
              <div
                role="alert"
                className="max-h-32 overflow-y-auto rounded-lg border border-danger/25 bg-danger/10 p-2.5"
              >
                <p className="mb-1 text-xs font-medium text-danger">
                  {errors.length} hồ sơ không lưu được:
                </p>
                <ul className="space-y-0.5 text-2xs text-danger/90">
                  {errors.map((e) => (
                    <li key={e.name} className="truncate">
                      {e.name} — {e.message}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>

          {/* Profile picker */}
          <div className="flex min-h-0 flex-col border-t border-line md:border-l md:border-t-0">
            <div className="space-y-2 border-b border-line p-3">
              <div className="flex items-center justify-between">
                <p className="text-2xs font-semibold uppercase tracking-wider text-fg-muted">
                  Hồ sơ ({chosen.length}/{profiles.length})
                </p>
                <button
                  type="button"
                  onClick={toggleAllVisible}
                  className="text-2xs font-medium text-brand-fg hover:underline"
                >
                  {allVisibleSelected ? "Bỏ chọn" : "Chọn tất cả"}
                </button>
              </div>
              <label className="relative block">
                <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-fg-muted" />
                <input
                  ref={searchRef}
                  type="search"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder="Tìm hồ sơ…"
                  aria-label="Tìm hồ sơ để gán proxy"
                  className="input py-1.5 pl-8 text-xs"
                />
              </label>
            </div>
            <ul className="min-h-0 flex-1 overflow-y-auto p-1.5">
              {visible.length === 0 && (
                <li className="px-2 py-6 text-center text-xs text-fg-muted">
                  Không có hồ sơ khớp.
                </li>
              )}
              {visible.map((p) => {
                const checked = selected.has(p.id);
                const current = profileProxyLabel(p);
                return (
                  <li key={p.id}>
                    <label
                      className={`flex cursor-pointer items-start gap-2 rounded-md px-2 py-1.5 text-xs transition-colors ${
                        checked
                          ? "bg-brand-subtle text-fg"
                          : "text-fg-secondary hover:bg-surface-3/60"
                      }`}
                    >
                      <span
                        className={`mt-0.5 flex h-3.5 w-3.5 flex-shrink-0 items-center justify-center rounded border ${
                          checked
                            ? "border-brand bg-brand text-white"
                            : "border-line-strong bg-surface-0"
                        }`}
                        aria-hidden="true"
                      >
                        {checked && <Check className="h-2.5 w-2.5" />}
                      </span>
                      <input
                        type="checkbox"
                        className="sr-only"
                        checked={checked}
                        onChange={() => toggle(p.id)}
                      />
                      <span className="min-w-0 flex-1">
                        <span className="block truncate font-medium">{p.name}</span>
                        <span className="block truncate text-2xs text-fg-muted">
                          {current ?? "trực tiếp"}
                          {p.status !== "stopped" && " · đang chạy"}
                        </span>
                      </span>
                    </label>
                  </li>
                );
              })}
            </ul>
          </div>
        </div>

        <div className="flex items-center justify-between gap-3 border-t border-line px-5 py-3">
          <p className="text-2xs text-fg-muted">
            {applying
              ? `Đang gán ${progress.done}/${progress.total}…`
              : mode === "direct"
                ? "Bỏ proxy cho các hồ sơ đã chọn"
                : `${PROVIDER_LABEL[mode === "none" ? "manual" : mode] ?? mode} cho ${chosen.length} hồ sơ`}
          </p>
          <div className="flex gap-2">
            <button type="button" className="btn-secondary" onClick={close} disabled={applying}>
              Hủy
            </button>
            <button
              type="button"
              className="btn-primary"
              onClick={handleApply}
              disabled={!canApply}
            >
              {applying && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              {applying ? "Đang gán…" : `Gán cho ${chosen.length} hồ sơ`}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
