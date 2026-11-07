import { useCallback, useEffect, useState } from "react";
import {
  CalendarClock,
  ExternalLink,
  Globe,
  KeyRound,
  Loader2,
  Plus,
  RefreshCw,
  ShoppingCart,
  Trash2,
  Wallet,
} from "lucide-react";
import {
  api,
  type KiotProxyKeyInfo,
  type KiotProxyPackage,
  type KiotProxyPackagePeriod,
  type ProxyAccount,
  type ProxyAccountSync,
} from "../lib/api";
import { useConfirm } from "./ui/confirm";
import { useToast } from "./ui/toast";

const PROVIDER_LABEL: Record<string, string> = {
  kiotproxy: "KiotProxy",
  tmproxy: "TMProxy",
};

const PROVIDER_SITE: Record<string, string> = {
  kiotproxy: "https://app.kiotproxy.com/",
  tmproxy: "https://tmproxy.com/",
};

type Provider = "kiotproxy" | "tmproxy";
type SyncState = ProxyAccountSync | { error: string };

function isError(state: SyncState | undefined): state is { error: string } {
  return Boolean(state && "error" in state);
}

function formatDateTime(value: string | number | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString("vi-VN");
}

function formatMoney(value: number | null | undefined): string {
  if (value == null) return "—";
  return `${value.toLocaleString("vi-VN")} đ`;
}

/** Expiry badge shared by both provider key tables. */
function ExpiryBadge({ value }: { value: string | number | null | undefined }) {
  if (!value) return <span className="text-2xs text-fg-muted">không hạn</span>;
  const when = new Date(value);
  if (Number.isNaN(when.getTime())) {
    return <span className="text-2xs text-fg-muted">{value}</span>;
  }
  const days = Math.ceil((when.getTime() - Date.now()) / 86_400_000);
  const relative =
    days < 0 ? "đã hết hạn" : days === 0 ? "hết hạn hôm nay" : `còn ${days} ngày`;
  const tone =
    days < 0 ? "badge-danger" : days <= 3 ? "badge-warning" : "badge-neutral";
  return (
    <span className={`badge ${tone}`} title={formatDateTime(value)}>
      {relative}
    </span>
  );
}

function KeyRow({
  accountId,
  entry,
  onChanged,
}: {
  accountId: string;
  entry: KiotProxyKeyInfo;
  onChanged: () => Promise<void>;
}) {
  const confirm = useConfirm();
  const toast = useToast();
  const [periods, setPeriods] = useState(1);
  const [region, setRegion] = useState<"bac" | "trung" | "nam" | "random">("random");
  const [busy, setBusy] = useState<"extend" | "delete" | "rotate" | null>(null);
  const keyId = entry.id;

  const run = async (
    kind: "extend" | "delete" | "rotate",
    action: () => Promise<unknown>,
    success: string,
  ) => {
    if (!keyId) return;
    setBusy(kind);
    try {
      await action();
      await onChanged();
      toast.success(success);
    } catch (err) {
      toast.show({
        tone: "error",
        title: "Không thực hiện được",
        description: err instanceof Error ? err.message : undefined,
      });
    } finally {
      setBusy(null);
    }
  };

  const handleDelete = async () => {
    const ok = await confirm({
      title: `Xóa key ${entry.key_masked ?? ""}?`,
      description:
        "Key bị xóa vĩnh viễn khỏi tài khoản KiotProxy và không thể khôi phục.",
      confirmLabel: "Xóa key",
      tone: "danger",
    });
    if (!ok) return;
    await run(
      "delete",
      () => api.deleteProxyKey(accountId, keyId as string),
      "Đã xóa key KiotProxy",
    );
  };

  const active = entry.status === "ACTIVE" && !entry.deleted;

  return (
    <li className="space-y-2 border-b border-line/60 px-3 py-2.5 last:border-0">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-xs text-fg">{entry.key_masked ?? "—"}</span>
        <span className={active ? "badge badge-success" : "badge badge-neutral"}>
          {entry.deleted ? "đã xóa" : active ? "hoạt động" : entry.status ?? "không rõ"}
        </span>
        {entry.quantity_day != null && (
          <span className="text-2xs text-fg-muted">{entry.quantity_day} ngày</span>
        )}
        <ExpiryBadge value={entry.expiration_at} />
        <span className="ml-auto text-2xs text-fg-muted">
          {formatDateTime(entry.expiration_at)}
        </span>
      </div>
      {entry.description && (
        <p className="text-2xs text-fg-muted">{entry.description}</p>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <label className="flex items-center gap-1 text-2xs text-fg-muted">
          Vùng
          <select
            className="input h-7 py-0 text-2xs"
            value={region}
            onChange={(e) => setRegion(e.target.value as typeof region)}
          >
            <option value="random">Ngẫu nhiên</option>
            <option value="bac">Miền Bắc</option>
            <option value="trung">Miền Trung</option>
            <option value="nam">Miền Nam</option>
          </select>
        </label>
        <button
          type="button"
          disabled={busy !== null || !active}
          onClick={() =>
            run(
              "rotate",
              () => api.rotateProxyKey(accountId, keyId as string, region),
              "Đã đổi IP cho key",
            )
          }
          className="btn-secondary px-2 py-1 text-2xs"
        >
          {busy === "rotate" ? (
            <Loader2 className="h-3 w-3 animate-spin" />
          ) : (
            <RefreshCw className="h-3 w-3" />
          )}
          Đổi IP
        </button>

        <label className="flex items-center gap-1 text-2xs text-fg-muted">
          Kỳ
          <input
            type="number"
            min={1}
            max={365}
            value={periods}
            onChange={(e) => setPeriods(Math.max(1, Number(e.target.value) || 1))}
            className="input h-7 w-16 py-0 text-2xs"
          />
        </label>
        <button
          type="button"
          disabled={busy !== null || !active}
          onClick={() =>
            run(
              "extend",
              () => api.extendProxyKey(accountId, keyId as string, periods),
              "Đã gia hạn key KiotProxy",
            )
          }
          className="btn-secondary px-2 py-1 text-2xs"
        >
          {busy === "extend" ? (
            <Loader2 className="h-3 w-3 animate-spin" />
          ) : (
            <CalendarClock className="h-3 w-3" />
          )}
          Gia hạn
        </button>

        <button
          type="button"
          disabled={busy !== null}
          onClick={handleDelete}
          className="ml-auto rounded p-1 text-fg-muted transition-colors hover:bg-danger/10 hover:text-danger disabled:opacity-40"
          title="Xóa key khỏi tài khoản KiotProxy"
        >
          {busy === "delete" ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <Trash2 className="h-3.5 w-3.5" />
          )}
        </button>
      </div>
    </li>
  );
}

function KiotProxyKeys({
  account,
  state,
  refresh,
}: {
  account: ProxyAccount;
  state: ProxyAccountSync;
  refresh: () => Promise<void>;
}) {
  const toast = useToast();
  const [packages, setPackages] = useState<KiotProxyPackage[]>([]);
  const [periods, setPeriods] = useState<KiotProxyPackagePeriod[]>([]);
  const [packageId, setPackageId] = useState("");
  const [periodId, setPeriodId] = useState("");
  const [quantity, setQuantity] = useState(1);
  const [numberOfPeriods, setNumberOfPeriods] = useState(1);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.kiotproxyPackages().then(setPackages).catch(() => setPackages([]));
  }, []);

  useEffect(() => {
    if (!packageId) {
      setPeriods([]);
      setPeriodId("");
      return;
    }
    api
      .kiotproxyPackagePeriods(packageId)
      .then((list) => {
        setPeriods(list);
        setPeriodId(list[0]?.id ?? "");
      })
      .catch(() => setPeriods([]));
  }, [packageId]);

  const handleOrder = async () => {
    if (!packageId) return;
    setBusy(true);
    try {
      const result = await api.orderProxyKeys(account.id, {
        package_id: packageId,
        quantity,
        number_of_periods: numberOfPeriods,
        package_period_id: periodId || null,
      });
      await refresh();
      toast.success(`Đã mua ${result.keys.length} key KiotProxy`);
    } catch (err) {
      toast.show({
        tone: "error",
        title: "Không mua được key",
        description: err instanceof Error ? err.message : undefined,
      });
    } finally {
      setBusy(false);
    }
  };

  const keys = state.keys ?? [];
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3 text-2xs text-fg-muted">
        <span className="inline-flex items-center gap-1">
          <Wallet className="h-3 w-3" />
          Số dư: {formatMoney(state.user?.balance)}
        </span>
        <span>Tài khoản: {state.user?.username ?? "—"}</span>
        <span>
          Key: {keys.length}
          {state.user?.max_key != null ? ` / ${state.user.max_key}` : ""}
        </span>
      </div>

      <ul className="divide-y divide-line overflow-hidden rounded-lg border border-line bg-surface-1">
        {keys.length === 0 && (
          <li className="px-3 py-4 text-center text-xs text-fg-muted">
            Tài khoản chưa có key nào.
          </li>
        )}
        {keys.map((entry, index) => (
          <KeyRow
            key={entry.id ?? `key-${index}`}
            accountId={account.id}
            entry={entry}
            onChanged={refresh}
          />
        ))}
      </ul>

      <details className="rounded-lg border border-line bg-surface-1 p-3">
        <summary className="flex cursor-pointer items-center gap-2 text-xs font-medium text-fg">
          <ShoppingCart className="h-3.5 w-3.5 text-brand-fg" />
          Mua key mới
        </summary>
        <div className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-2">
          <label className="text-2xs text-fg-muted">
            Gói
            <select
              className="input mt-1 w-full text-xs"
              value={packageId}
              onChange={(e) => setPackageId(e.target.value)}
            >
              <option value="">Chọn gói…</option>
              {packages.map((pkg) => (
                <option key={pkg.id} value={pkg.id}>
                  {pkg.name}
                  {pkg.price != null ? ` — ${formatMoney(pkg.price)}` : ""}
                </option>
              ))}
            </select>
          </label>
          <label className="text-2xs text-fg-muted">
            Kỳ
            <select
              className="input mt-1 w-full text-xs"
              value={periodId}
              onChange={(e) => setPeriodId(e.target.value)}
              disabled={periods.length === 0}
            >
              <option value="">Mặc định</option>
              {periods.map((period) => (
                <option key={period.id} value={period.id}>
                  {period.name ?? `${period.quantity ?? ""} ${period.period ?? ""}`}
                  {period.unitPrice != null ? ` — ${formatMoney(period.unitPrice)}` : ""}
                </option>
              ))}
            </select>
          </label>
          <label className="text-2xs text-fg-muted">
            Số lượng key
            <input
              type="number"
              min={1}
              max={100}
              value={quantity}
              onChange={(e) => setQuantity(Math.max(1, Number(e.target.value) || 1))}
              className="input mt-1 w-full text-xs"
            />
          </label>
          <label className="text-2xs text-fg-muted">
            Số kỳ
            <input
              type="number"
              min={1}
              max={365}
              value={numberOfPeriods}
              onChange={(e) =>
                setNumberOfPeriods(Math.max(1, Number(e.target.value) || 1))
              }
              className="input mt-1 w-full text-xs"
            />
          </label>
        </div>
        <button
          type="button"
          onClick={handleOrder}
          disabled={busy || !packageId}
          className="btn-primary mt-3 px-3 py-1.5 text-xs"
        >
          {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : <Plus className="h-3 w-3" />}
          Mua key
        </button>
      </details>
    </div>
  );
}

function TmProxyDetails({
  account,
  state,
  refresh,
}: {
  account: ProxyAccount;
  state: ProxyAccountSync;
  refresh: () => Promise<void>;
}) {
  const toast = useToast();
  const [note, setNote] = useState(state.tmproxy?.note ?? "");
  const [busy, setBusy] = useState(false);
  const [rotating, setRotating] = useState(false);

  useEffect(() => {
    setNote(state.tmproxy?.note ?? "");
  }, [state.tmproxy?.note]);

  const handleRotate = async () => {
    setRotating(true);
    try {
      const info = await api.rotateProxyAccount(account.id);
      await refresh();
      toast.success(
        "Đã đổi IP TMProxy",
        info.host ?? info.real_ip ?? undefined,
      );
    } catch (err) {
      toast.show({
        tone: "error",
        title: "Không đổi được IP",
        description: err instanceof Error ? err.message : undefined,
      });
    } finally {
      setRotating(false);
    }
  };

  const handleSaveNote = async () => {
    setBusy(true);
    try {
      await api.setProxyAccountNote(account.id, note);
      await refresh();
      toast.success("Đã lưu ghi chú TMProxy");
    } catch (err) {
      toast.show({
        tone: "error",
        title: "Không lưu được ghi chú",
        description: err instanceof Error ? err.message : undefined,
      });
    } finally {
      setBusy(false);
    }
  };

  const info = state.tmproxy;
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-2 text-2xs sm:grid-cols-4">
        <div className="rounded-lg border border-line bg-surface-2/50 px-3 py-2">
          <p className="text-fg-muted">Gói</p>
          <p className="mt-0.5 font-medium text-fg">{info?.plan ?? "—"}</p>
        </div>
        <div className="rounded-lg border border-line bg-surface-2/50 px-3 py-2">
          <p className="text-fg-muted">Hết hạn</p>
          <div className="mt-0.5 flex items-center gap-1.5">
            <ExpiryBadge value={info?.expired_at} />
          </div>
        </div>
        <div className="rounded-lg border border-line bg-surface-2/50 px-3 py-2">
          <p className="text-fg-muted">IP hôm nay</p>
          <p className="mt-0.5 font-medium text-fg">
            {info?.ip_used_today ?? 0}
            {info?.max_ip_per_day != null ? ` / ${info.max_ip_per_day}` : ""}
          </p>
        </div>
        <div className="rounded-lg border border-line bg-surface-2/50 px-3 py-2">
          <p className="text-fg-muted">Giá / ngày</p>
          <p className="mt-0.5 font-medium text-fg">{formatMoney(info?.price_per_day)}</p>
        </div>
      </div>

      <p className="text-2xs text-fg-muted">
        Hết hạn chính xác: {formatDateTime(info?.expired_at)} · Thời gian sống proxy:{" "}
        {info?.timeout ? `${info.timeout}s` : "không giới hạn"}
      </p>

      <div className="flex flex-wrap items-end gap-2">
        <label className="min-w-[16rem] flex-1 text-2xs text-fg-muted">
          Ghi chú
          <input
            className="input mt-1 w-full text-xs"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            maxLength={500}
            placeholder="VD: key cho nhóm Shopee"
          />
        </label>
        <button
          type="button"
          onClick={handleSaveNote}
          disabled={busy}
          className="btn-secondary px-3 py-1.5 text-xs"
        >
          {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : null}
          Lưu ghi chú
        </button>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={handleRotate}
          disabled={rotating}
          className="btn-secondary px-3 py-1.5 text-xs"
          title="Lấy proxy mới cho key TMProxy này"
        >
          {rotating ? (
            <Loader2 className="h-3 w-3 animate-spin" />
          ) : (
            <RefreshCw className="h-3 w-3" />
          )}
          Đổi IP
        </button>
        <p className="text-2xs text-fg-muted">
          TMProxy không có API mua/gia hạn/xóa key. Gia hạn trên trang TMProxy rồi bấm
          làm mới để cập nhật hạn.
        </p>
      </div>
    </div>
  );
}

/**
 * Account-level proxy key management. Unlike the Proxy page (which assigns an
 * exit to a profile), this lists what a provider account actually holds and lets
 * the user extend, rotate, buy or delete keys without leaving the Manager.
 */
export function ProxyProvidersPanel() {
  const confirm = useConfirm();
  const toast = useToast();
  const [accounts, setAccounts] = useState<ProxyAccount[]>([]);
  const [syncs, setSyncs] = useState<Record<string, SyncState>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [provider, setProvider] = useState<Provider>("kiotproxy");
  const [label, setLabel] = useState("");
  const [secret, setSecret] = useState("");
  const [adding, setAdding] = useState(false);

  const syncAccount = useCallback(async (id: string) => {
    try {
      const data = await api.syncProxyAccount(id);
      setSyncs((prev) => ({ ...prev, [id]: data }));
    } catch (err) {
      setSyncs((prev) => ({
        ...prev,
        [id]: { error: err instanceof Error ? err.message : "Không tải được dữ liệu" },
      }));
    }
  }, []);

  const load = useCallback(async () => {
    setError(null);
    try {
      const list = await api.listProxyAccounts();
      setAccounts(list);
      await Promise.all(list.map((account) => syncAccount(account.id)));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Không tải được tài khoản proxy");
    } finally {
      setLoading(false);
    }
  }, [syncAccount]);

  useEffect(() => {
    void load();
  }, [load]);

  const handleAdd = async () => {
    if (!secret.trim()) return;
    setAdding(true);
    setError(null);
    try {
      await api.createProxyAccount({
        provider,
        secret: secret.trim(),
        label: label.trim() || null,
      });
      setSecret("");
      setLabel("");
      await load();
      toast.success("Đã thêm tài khoản proxy");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Không thêm được tài khoản");
    } finally {
      setAdding(false);
    }
  };

  const handleDelete = async (account: ProxyAccount) => {
    const ok = await confirm({
      title: `Xóa ${PROVIDER_LABEL[account.provider]} ${account.secret_masked ?? ""}?`,
      description:
        "Chỉ xóa khỏi Manager. Key/tài khoản bên nhà cung cấp không bị ảnh hưởng.",
      confirmLabel: "Xóa khỏi Manager",
      tone: "danger",
    });
    if (!ok) return;
    try {
      await api.deleteProxyAccount(account.id);
      await load();
      toast.success("Đã xóa tài khoản khỏi Manager");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Không xóa được tài khoản");
    }
  };

  const refresh = (id: string) => syncAccount(id);

  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-6 px-6 py-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-xl font-bold tracking-tight text-fg">Nhà cung cấp proxy</h1>
          <p className="mt-1 text-xs text-fg-muted">
            Quản lý key TMProxy và KiotProxy ở cấp tài khoản: xem hạn, gia hạn, đổi IP,
            mua hoặc xóa key.
          </p>
        </div>
      </div>

      <div className="card space-y-4 p-5 shadow-card">
        <div className="flex items-center gap-2">
          <Plus className="h-4 w-4 text-brand-fg" />
          <h2 className="text-sm font-bold text-fg">Thêm tài khoản mới</h2>
        </div>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-[11rem_1fr_1.5fr_auto]">
          <label className="text-xs font-semibold text-fg-muted">
            Nhà cung cấp
            <select
              className="input mt-1 w-full text-xs"
              value={provider}
              onChange={(e) => setProvider(e.target.value as Provider)}
            >
              <option value="kiotproxy">KiotProxy</option>
              <option value="tmproxy">TMProxy</option>
            </select>
          </label>
          <label className="text-xs font-semibold text-fg-muted">
            Tên gợi nhớ (nhãn)
            <input
              className="input mt-1 w-full text-xs"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              placeholder="VD: tài khoản chính"
              maxLength={200}
            />
          </label>
          <label className="text-xs font-semibold text-fg-muted">
            {provider === "kiotproxy" ? "API token KiotProxy" : "API key TMProxy"}
            <input
              type="password"
              className="input mt-1 w-full font-mono text-xs"
              value={secret}
              onChange={(e) => setSecret(e.target.value)}
              placeholder={
                provider === "kiotproxy"
                  ? "Token ở trang tài khoản KiotProxy, không phải key proxy"
                  : "Dán API key TMProxy"
              }
              autoComplete="off"
            />
          </label>
          <button
            type="button"
            onClick={handleAdd}
            disabled={adding || !secret.trim()}
            className="btn-primary self-end h-9 px-4 text-xs font-semibold"
          >
            {adding ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Plus className="h-3.5 w-3.5" />}
            <span>Thêm tài khoản</span>
          </button>
        </div>
        <p className="text-xs text-fg-muted">
          KiotProxy cần <span className="font-semibold text-fg-secondary">API token tài khoản</span>{" "}
          (dùng để liệt kê/gia hạn/mua key). Key proxy dán vào hồ sơ vẫn dùng như cũ.
        </p>
        {error && <p className="text-xs text-danger font-medium">{error}</p>}
      </div>

      {loading && (
        <div className="flex items-center gap-2 text-xs text-fg-muted py-4">
          <Loader2 className="h-4 w-4 animate-spin text-brand-fg" />
          <span>Đang tải danh sách tài khoản proxy…</span>
        </div>
      )}

      {!loading && accounts.length === 0 && (
        <div className="empty-state card p-12">
          <div className="mb-2 flex h-14 w-14 items-center justify-center rounded-2xl border border-info/30 bg-info/10 text-info">
            <Globe className="h-7 w-7" />
          </div>
          <p className="text-base font-bold text-fg">Chưa có tài khoản proxy</p>
          <p className="max-w-md text-xs text-fg-muted mt-1">
            Thêm API token KiotProxy hoặc API key TMProxy để xem danh sách key và hạn sử
            dụng ngay tại đây.
          </p>
        </div>
      )}

      <div className="space-y-4">
        {accounts.map((account) => {
          const state = syncs[account.id];
          return (
            <section key={account.id} className="card overflow-hidden shadow-card">
              <header className="flex flex-wrap items-center gap-3 border-b border-line/80 bg-surface-2/40 px-4.5 py-3">
                <span className="badge badge-info">
                  {PROVIDER_LABEL[account.provider] ?? account.provider}
                </span>
                <div className="min-w-0">
                  <p className="truncate text-xs font-bold text-fg">
                    {account.label || account.secret_masked || account.id}
                  </p>
                  <p className="font-mono text-[11px] text-fg-muted">{account.secret_masked}</p>
                </div>
                <div className="ml-auto flex items-center gap-2">
                  <button
                    type="button"
                    onClick={() => refresh(account.id)}
                    className="btn-secondary h-7 px-2.5 text-xs"
                    title="Tải lại dữ liệu từ nhà cung cấp"
                  >
                    <RefreshCw className="h-3 w-3" />
                    <span>Làm mới</span>
                  </button>
                  <button
                    type="button"
                    onClick={() =>
                      api
                        .openExternal(
                          PROVIDER_SITE[account.provider] ?? "https://kiotproxy.com/",
                        )
                        .catch(() => undefined)
                    }
                    className="btn-ghost h-7 px-2.5 text-xs"
                    title="Mở trang quản lý của nhà cung cấp"
                  >
                    <ExternalLink className="h-3 w-3" />
                    <span>Trang chủ</span>
                  </button>
                  <button
                    type="button"
                    onClick={() => handleDelete(account)}
                    className="rounded-lg p-1.5 text-fg-muted transition-colors hover:bg-danger/10 hover:text-danger"
                    title="Xóa khỏi Manager"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                </div>
              </header>

              <div className="p-4.5">
                {!state && (
                  <div className="flex items-center gap-2 text-xs text-fg-muted">
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    Đang tải…
                  </div>
                )}
                {isError(state) && (
                  <p className="text-xs text-danger font-medium">
                    Không tải được dữ liệu: {state.error}
                  </p>
                )}
                {state && !isError(state) && account.provider === "kiotproxy" && (
                  <KiotProxyKeys
                    account={account}
                    state={state}
                    refresh={() => refresh(account.id)}
                  />
                )}
                {state && !isError(state) && account.provider === "tmproxy" && (
                  <TmProxyDetails
                    account={account}
                    state={state}
                    refresh={() => refresh(account.id)}
                  />
                )}
              </div>
            </section>
          );
        })}
      </div>

      <p className="flex items-center gap-1.5 text-xs text-fg-muted">
        <KeyRound className="h-3.5 w-3.5 text-brand-fg" />
        <span>Thông tin key/tài khoản chỉ lưu trên máy này và không được trả lại nguyên văn qua API.</span>
      </p>
    </div>
  );
}
