import { useState, type FormEvent } from "react";
import { ArrowRight, Lock, Shield } from "lucide-react";
import { api } from "../lib/api";

interface LoginPageProps {
  onSuccess: () => void;
}

export function LoginPage({ onSuccess }: LoginPageProps) {
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      await api.login(token);
      onSuccess();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Đăng nhập thất bại");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="h-screen flex items-center justify-center bg-surface-0 px-4">
      <div className="card w-full max-w-sm p-8">
        <div className="flex flex-col items-center text-center mb-6">
          <div className="relative h-12 w-12 rounded-2xl bg-gradient-to-br from-brand to-brand-deep flex items-center justify-center mb-4 shadow-glow">
            <Shield className="h-6 w-6 text-white" />
          </div>
          <h1 className="text-lg font-semibold text-fg tracking-tight">
            CloakBrowser Manager (Custom)
          </h1>
          <p className="text-xs text-fg-secondary mt-1">Nhập mã truy cập để mở khóa phiên quản trị</p>
        </div>
        <form onSubmit={handleSubmit} className="space-y-3.5">
          <div className="relative">
            <Lock className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-fg-muted" />
            <input
              type="password"
              className="input h-10 pl-9"
              placeholder="Mã truy cập"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              autoFocus
            />
          </div>
          {error && (
            <div className="rounded-lg bg-danger/10 border border-danger/25 px-3 py-2 text-danger text-xs">
              {error}
            </div>
          )}
          <button
            type="submit"
            disabled={loading || !token}
            className="btn-primary h-10 w-full disabled:opacity-50"
          >
            <span>{loading ? "Đang xác thực..." : "Mở khóa"}</span>
            {!loading && <ArrowRight className="h-4 w-4" />}
          </button>
        </form>
      </div>
    </div>
  );
}
