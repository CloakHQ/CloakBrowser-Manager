import { AlertTriangle, Check, ChevronRight, Eraser, RotateCcw, Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { LucideIcon } from "lucide-react";

type ActionId = "reset" | "clear" | "delete";

interface DangerZoneProps {
  // Resolve to false when the action failed, so no "done" state is shown.
  onReset?: () => Promise<boolean | void>;
  onClearData?: () => Promise<boolean | void>;
  onDelete?: () => Promise<void>;
}

interface Action {
  id: ActionId;
  title: string;
  description: string;
  button: string;
  busy: string;
  done: string;
  icon: LucideIcon;
  run: () => Promise<boolean | void>;
}

/**
 * Collapsed, red "Destructive Operations" block at the end of the profile form.
 * Every action takes two clicks: the first only asks, the second runs it.
 */
export function DangerZone({ onReset, onClearData, onDelete }: DangerZoneProps) {
  const [pending, setPending] = useState<ActionId | null>(null);
  const [busy, setBusy] = useState<ActionId | null>(null);
  const [done, setDone] = useState<ActionId | null>(null);
  const doneTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(doneTimer.current), []);

  const actions: Action[] = [];
  if (onReset) {
    actions.push({
      id: "reset",
      title: "Reset identity",
      description:
        "Deletes cookies, logins, history and site data, and gives the profile a new fingerprint. " +
        "Keeps its settings, bookmarks and default search engine.",
      button: "Reset identity",
      busy: "Resetting...",
      done: "Identity reset",
      icon: RotateCcw,
      run: onReset,
    });
  }
  if (onClearData) {
    actions.push({
      id: "clear",
      title: "Clear browser data",
      description:
        "Deletes this profile's entire browser data folder: cookies, logins, history, bookmarks, " +
        "extension data and browser preferences. Manager settings, the fingerprint and the profile's " +
        "uploaded and downloaded files are kept. The next launch starts like a fresh browser.",
      button: "Clear browser data",
      busy: "Clearing...",
      done: "Browser data cleared",
      icon: Eraser,
      run: onClearData,
    });
  }
  if (onDelete) {
    actions.push({
      id: "delete",
      title: "Delete profile",
      description: "Deletes the profile with its settings, browser data and files.",
      button: "Delete profile",
      busy: "Deleting...",
      done: "Deleted",
      icon: Trash2,
      run: onDelete,
    });
  }
  if (actions.length === 0) return null;

  const confirm = async (action: Action) => {
    setPending(null);
    setBusy(action.id);
    try {
      const ok = await action.run();
      if (ok === false) return;
      setDone(action.id);
      clearTimeout(doneTimer.current);
      doneTimer.current = setTimeout(() => setDone(null), 2000);
    } finally {
      setBusy(null);
    }
  };

  return (
    <details className="group rounded-md border border-red-900/60 bg-red-950/20 p-3">
      <summary className="flex cursor-pointer list-none items-center gap-2 text-xs font-semibold uppercase tracking-wider text-red-400">
        <ChevronRight className="h-3.5 w-3.5 transition-transform group-open:rotate-90" />
        <AlertTriangle className="h-3.5 w-3.5" />
        Destructive Operations
      </summary>
      <div className="mt-3 divide-y divide-red-900/40">
        {actions.map((action) => {
          const Icon = action.icon;
          return (
            <div key={action.id} className="flex flex-col gap-2 py-3 first:pt-0 last:pb-0">
              <div className="flex items-start justify-between gap-4">
                <div>
                  <div className="text-sm text-gray-200">{action.title}</div>
                  <p className="mt-0.5 text-xs text-gray-500">{action.description}</p>
                </div>
                {pending !== action.id && (
                  <button
                    type="button"
                    onClick={() => setPending(action.id)}
                    disabled={busy !== null || done === action.id}
                    className="btn-danger flex shrink-0 items-center gap-1.5"
                  >
                    {done === action.id ? <Check className="h-3.5 w-3.5" /> : <Icon className="h-3.5 w-3.5" />}
                    <span>
                      {done === action.id ? action.done : busy === action.id ? action.busy : action.button}
                    </span>
                  </button>
                )}
              </div>
              {pending === action.id && (
                <div
                  role="alert"
                  className="flex items-center justify-between gap-3 rounded-md border border-red-800/70 bg-red-950/40 px-3 py-2"
                >
                  <span className="text-xs text-red-300">
                    Stops the profile if it is running. This cannot be undone.
                  </span>
                  <div className="flex shrink-0 gap-2">
                    <button type="button" onClick={() => setPending(null)} className="btn-secondary">
                      Cancel
                    </button>
                    <button
                      type="button"
                      onClick={() => confirm(action)}
                      className="btn flex items-center gap-1.5 bg-red-600 text-white hover:bg-red-500"
                    >
                      <Icon className="h-3.5 w-3.5" />
                      <span>Confirm: {action.button}</span>
                    </button>
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </details>
  );
}
