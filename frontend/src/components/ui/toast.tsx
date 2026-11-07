import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { AlertTriangle, CheckCircle2, Info, X } from "lucide-react";

export type ToastTone = "success" | "error" | "info";

export interface ToastOptions {
  tone?: ToastTone;
  title: string;
  description?: string;
  action?: { label: string; onClick: () => void };
  /** ms before auto-dismiss; 0 keeps it until closed. Errors default to 0. */
  duration?: number;
}

interface ToastItem extends ToastOptions {
  id: number;
  tone: ToastTone;
}

interface ToastApi {
  show: (options: ToastOptions) => number;
  dismiss: (id: number) => void;
  success: (title: string, description?: string) => number;
  error: (title: string, description?: string) => number;
  info: (title: string, description?: string) => number;
}

const ToastContext = createContext<ToastApi | null>(null);

const TONE_STYLE: Record<ToastTone, { icon: typeof Info; className: string }> = {
  success: { icon: CheckCircle2, className: "text-success" },
  error: { icon: AlertTriangle, className: "text-danger" },
  info: { icon: Info, className: "text-info" },
};

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const nextId = useRef(1);
  const timers = useRef(new Map<number, ReturnType<typeof setTimeout>>());

  const dismiss = useCallback((id: number) => {
    clearTimeout(timers.current.get(id));
    timers.current.delete(id);
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const show = useCallback(
    (options: ToastOptions) => {
      const id = nextId.current++;
      const tone = options.tone ?? "info";
      // Errors stay until read; a failure that vanishes is worse than none.
      const duration = options.duration ?? (tone === "error" ? 0 : 4000);
      setToasts((prev) => [...prev.slice(-4), { ...options, id, tone }]);
      if (duration > 0) timers.current.set(id, setTimeout(() => dismiss(id), duration));
      return id;
    },
    [dismiss],
  );

  useEffect(() => {
    const pending = timers.current;
    return () => pending.forEach(clearTimeout);
  }, []);

  const api = useMemo<ToastApi>(
    () => ({
      show,
      dismiss,
      success: (title, description) => show({ tone: "success", title, description }),
      error: (title, description) => show({ tone: "error", title, description }),
      info: (title, description) => show({ tone: "info", title, description }),
    }),
    [show, dismiss],
  );

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div
        role="region"
        aria-label="Thông báo"
        aria-live="polite"
        className="pointer-events-none fixed bottom-10 right-4 z-toast flex w-80 flex-col gap-2"
      >
        {toasts.map((toast) => {
          const { icon: Icon, className } = TONE_STYLE[toast.tone];
          return (
            <div
              key={toast.id}
              role={toast.tone === "error" ? "alert" : "status"}
              className="pointer-events-auto flex items-start gap-2.5 rounded-lg border border-line bg-surface-2/95 px-3 py-2.5 shadow-popover backdrop-blur-md"
            >
              <Icon className={`mt-0.5 h-4 w-4 flex-shrink-0 ${className}`} />
              <div className="min-w-0 flex-1">
                <p className="text-sm font-medium text-fg">{toast.title}</p>
                {toast.description && (
                  <p className="mt-0.5 break-words text-xs text-fg-secondary">{toast.description}</p>
                )}
                {toast.action && (
                  <button
                    type="button"
                    onClick={() => {
                      toast.action?.onClick();
                      dismiss(toast.id);
                    }}
                    className="mt-1.5 text-xs font-medium text-brand-fg hover:underline"
                  >
                    {toast.action.label}
                  </button>
                )}
              </div>
              <button
                type="button"
                onClick={() => dismiss(toast.id)}
                className="rounded p-0.5 text-fg-muted hover:bg-surface-3 hover:text-fg"
                aria-label="Đóng thông báo"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
          );
        })}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastApi {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast must be used inside <ToastProvider>");
  return ctx;
}
