import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { AlertTriangle } from "lucide-react";

export interface ConfirmOptions {
  title: string;
  /** Name the target and the consequence, e.g. what is deleted for good. */
  description?: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  tone?: "danger" | "default";
}

type ConfirmFn = (options: ConfirmOptions) => Promise<boolean>;

const ConfirmContext = createContext<ConfirmFn | null>(null);

interface Pending extends ConfirmOptions {
  resolve: (ok: boolean) => void;
}

/** Promise-based replacement for window.confirm with a real, accessible dialog. */
export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [pending, setPending] = useState<Pending | null>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const returnFocus = useRef<HTMLElement | null>(null);

  const confirm = useCallback<ConfirmFn>(
    (options) =>
      new Promise<boolean>((resolve) => {
        returnFocus.current = document.activeElement as HTMLElement | null;
        setPending({ ...options, resolve });
      }),
    [],
  );

  const close = useCallback(
    (ok: boolean) => {
      pending?.resolve(ok);
      setPending(null);
      returnFocus.current?.focus?.();
    },
    [pending],
  );

  useEffect(() => {
    if (!pending) return;
    // Focus the safe choice so a stray Enter never confirms a destructive action.
    cancelRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        close(false);
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [pending, close]);

  const danger = pending?.tone === "danger";

  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      {pending && (
        <div
          className="fixed inset-0 z-modal flex items-center justify-center bg-black/70 px-4 backdrop-blur-sm"
          onMouseDown={(e) => {
            if (e.target === e.currentTarget) close(false);
          }}
        >
          <div
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="confirm-title"
            aria-describedby={pending.description ? "confirm-desc" : undefined}
            className="card w-full max-w-md p-5 shadow-popover"
          >
            <div className="flex items-start gap-3">
              {danger && (
                <div className="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-lg border border-danger/25 bg-danger/10 text-danger">
                  <AlertTriangle className="h-4 w-4" />
                </div>
              )}
              <div className="min-w-0">
                <h2 id="confirm-title" className="text-base font-semibold text-fg">
                  {pending.title}
                </h2>
                {pending.description && (
                  <div id="confirm-desc" className="mt-1.5 text-sm leading-relaxed text-fg-secondary">
                    {pending.description}
                  </div>
                )}
              </div>
            </div>
            <div className="mt-5 flex justify-end gap-2">
              <button ref={cancelRef} type="button" className="btn-secondary" onClick={() => close(false)}>
                {pending.cancelLabel ?? "Hủy"}
              </button>
              <button
                type="button"
                className={danger ? "btn-danger" : "btn-primary"}
                onClick={() => close(true)}
              >
                {pending.confirmLabel ?? "Xác nhận"}
              </button>
            </div>
          </div>
        </div>
      )}
    </ConfirmContext.Provider>
  );
}

export function useConfirm(): ConfirmFn {
  const ctx = useContext(ConfirmContext);
  if (!ctx) throw new Error("useConfirm must be used inside <ConfirmProvider>");
  return ctx;
}
