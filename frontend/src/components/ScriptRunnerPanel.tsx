import { useCallback, useEffect, useMemo, useState } from "react";
import {
  CheckCircle2,
  Clock3,
  FileCode2,
  Play,
  RefreshCw,
  Square,
  Terminal,
  XCircle,
} from "lucide-react";
import {
  api,
  type Profile,
  type ScriptArguments,
  type ScriptDefinition,
  type ScriptParameter,
  type ScriptRun,
} from "../lib/api";

interface ScriptRunnerPanelProps {
  profiles: Profile[];
  selectedProfileId: string | null;
}

const statusStyles: Record<ScriptRun["status"], string> = {
  running: "text-yellow-300",
  succeeded: "text-emerald-300",
  failed: "text-red-300",
  stopped: "text-gray-400",
};

function titleFor(name: string) {
  return name
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function initialArguments(script: ScriptDefinition | null): ScriptArguments {
  if (!script) return {};
  return script.parameters.reduce<ScriptArguments>((acc, parameter) => {
    if (parameter.kind === "flag") {
      acc[parameter.name] = false;
    } else if (parameter.default !== null) {
      acc[parameter.name] = parameter.default;
    } else {
      acc[parameter.name] = "";
    }
    return acc;
  }, {});
}

function hasRequiredValues(script: ScriptDefinition | null, args: ScriptArguments) {
  if (!script) return false;
  return script.parameters.every((parameter) => {
    if (!parameter.required || parameter.kind === "flag") return true;
    const value = args[parameter.name];
    return value !== null && value !== undefined && value !== "";
  });
}

function coerceInputValue(parameter: ScriptParameter, raw: string) {
  if (raw === "") return "";
  if (parameter.value_type === "integer") {
    const value = Number.parseInt(raw, 10);
    return Number.isNaN(value) ? "" : value;
  }
  if (parameter.value_type === "number") {
    const value = Number.parseFloat(raw);
    return Number.isNaN(value) ? "" : value;
  }
  return raw;
}

function StatusIcon({ status }: { status: ScriptRun["status"] }) {
  if (status === "running") return <Clock3 className="h-3.5 w-3.5 animate-pulse" />;
  if (status === "succeeded") return <CheckCircle2 className="h-3.5 w-3.5" />;
  if (status === "failed") return <XCircle className="h-3.5 w-3.5" />;
  return <Square className="h-3.5 w-3.5" />;
}

export function ScriptRunnerPanel({ profiles, selectedProfileId }: ScriptRunnerPanelProps) {
  const [scripts, setScripts] = useState<ScriptDefinition[]>([]);
  const [selectedScriptId, setSelectedScriptId] = useState<string | null>(null);
  const [profileId, setProfileId] = useState<string>("");
  const [args, setArgs] = useState<ScriptArguments>({});
  const [currentRun, setCurrentRun] = useState<ScriptRun | null>(null);
  const [loading, setLoading] = useState(true);
  const [runningAction, setRunningAction] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const runningProfiles = useMemo(
    () => profiles.filter((profile) => profile.status === "running"),
    [profiles],
  );
  const selectedScript = scripts.find((script) => script.id === selectedScriptId) ?? null;
  const canRun =
    Boolean(selectedScript && profileId) &&
    hasRequiredValues(selectedScript, args) &&
    currentRun?.status !== "running";

  const loadScripts = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.listScripts();
      setScripts(data);
      setSelectedScriptId((prev) => prev ?? data[0]?.id ?? null);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load scripts");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadScripts();
  }, [loadScripts]);

  useEffect(() => {
    if (selectedScript) {
      setArgs(initialArguments(selectedScript));
    }
  }, [selectedScript]);

  useEffect(() => {
    const selectedRunning =
      selectedProfileId && runningProfiles.some((profile) => profile.id === selectedProfileId)
        ? selectedProfileId
        : "";
    const nextProfileId = selectedRunning || runningProfiles[0]?.id || "";
    setProfileId((prev) =>
      prev && runningProfiles.some((profile) => profile.id === prev) ? prev : nextProfileId,
    );
  }, [runningProfiles, selectedProfileId]);

  useEffect(() => {
    if (currentRun?.status !== "running") return;
    const timer = window.setInterval(async () => {
      try {
        const run = await api.getScriptRun(currentRun.id);
        setCurrentRun(run);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to refresh script run");
      }
    }, 1000);
    return () => window.clearInterval(timer);
  }, [currentRun]);

  const handleRun = async () => {
    if (!selectedScript || !profileId) return;
    setRunningAction(true);
    try {
      const run = await api.runScript(selectedScript.id, profileId, args);
      setCurrentRun(run);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to run script");
    } finally {
      setRunningAction(false);
    }
  };

  const handleStop = async () => {
    if (!currentRun || currentRun.status !== "running") return;
    setRunningAction(true);
    try {
      setCurrentRun(await api.stopScriptRun(currentRun.id));
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to stop script");
    } finally {
      setRunningAction(false);
    }
  };

  const updateArgument = (parameter: ScriptParameter, value: string | boolean) => {
    setArgs((prev) => ({
      ...prev,
      [parameter.name]:
        parameter.kind === "flag" ? Boolean(value) : coerceInputValue(parameter, String(value)),
    }));
  };

  return (
    <div className="h-full flex flex-col bg-surface-0">
      <div className="px-5 py-4 border-b border-border bg-surface-1">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <Terminal className="h-4 w-4 text-accent" />
            <h2 className="text-sm font-semibold">Scripts</h2>
          </div>
          <button
            onClick={loadScripts}
            className="text-gray-500 hover:text-gray-300 p-1"
            title="Refresh scripts"
            disabled={loading}
          >
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
          </button>
        </div>
      </div>

      {error && (
        <div className="px-5 py-2 bg-red-600/15 border-b border-red-600/30 text-red-400 text-sm">
          {error}
        </div>
      )}

      <div className="flex-1 min-h-0 grid grid-cols-1 md:grid-cols-[280px_minmax(0,1fr)]">
        <div className="border-r border-border overflow-y-auto p-3">
          {scripts.length === 0 && !loading && (
            <div className="text-xs text-gray-500 px-2 py-6 text-center">No scripts found</div>
          )}
          {scripts.map((script) => (
            <button
              key={script.id}
              onClick={() => setSelectedScriptId(script.id)}
              className={`w-full text-left px-3 py-2.5 rounded-md mb-1 transition-colors ${
                selectedScriptId === script.id
                  ? "bg-surface-3 border border-border-hover"
                  : "hover:bg-surface-2 border border-transparent"
              }`}
            >
              <div className="flex items-center gap-2">
                <FileCode2 className="h-3.5 w-3.5 text-gray-400" />
                <span className="text-sm font-medium truncate">{script.name}</span>
              </div>
              <div className="mt-1 ml-5 text-xs text-gray-500 truncate">{script.filename}</div>
            </button>
          ))}
        </div>

        <div className="min-w-0 overflow-y-auto">
          <div className="max-w-5xl mx-auto p-5 space-y-5">
            <div className="grid grid-cols-1 lg:grid-cols-[minmax(0,380px)_minmax(0,1fr)] gap-5">
              <div className="space-y-4">
                <div>
                  <label className="label" htmlFor="script-profile">
                    Browser Profile
                  </label>
                  <select
                    id="script-profile"
                    className="input"
                    value={profileId}
                    onChange={(e) => setProfileId(e.target.value)}
                  >
                    {runningProfiles.length === 0 && <option value="">No running profiles</option>}
                    {runningProfiles.map((profile) => (
                      <option key={profile.id} value={profile.id}>
                        {profile.name}
                      </option>
                    ))}
                  </select>
                </div>

                {selectedScript && (
                  <div>
                    <div className="text-sm font-medium">{selectedScript.name}</div>
                    {selectedScript.description && (
                      <p className="text-xs text-gray-500 mt-1">{selectedScript.description}</p>
                    )}
                  </div>
                )}

                {selectedScript?.parameters.map((parameter) => (
                  <div key={parameter.name}>
                    {parameter.kind === "flag" ? (
                      <label className="flex items-center gap-2 text-sm text-gray-300">
                        <input
                          type="checkbox"
                          className="h-4 w-4 rounded border-border bg-surface-2"
                          checked={Boolean(args[parameter.name])}
                          onChange={(e) => updateArgument(parameter, e.target.checked)}
                        />
                        <span>{titleFor(parameter.name)}</span>
                      </label>
                    ) : (
                      <>
                        <label className="label" htmlFor={`script-arg-${parameter.name}`}>
                          {titleFor(parameter.name)}
                          {parameter.required && <span className="text-red-300"> *</span>}
                        </label>
                        {parameter.choices ? (
                          <select
                            id={`script-arg-${parameter.name}`}
                            className="input"
                            value={String(args[parameter.name] ?? "")}
                            onChange={(e) => updateArgument(parameter, e.target.value)}
                          >
                            {parameter.choices.map((choice) => (
                              <option key={choice} value={choice}>
                                {choice}
                              </option>
                            ))}
                          </select>
                        ) : (
                          <input
                            id={`script-arg-${parameter.name}`}
                            className="input"
                            type={
                              parameter.value_type === "integer" ||
                              parameter.value_type === "number"
                                ? "number"
                                : "text"
                            }
                            value={String(args[parameter.name] ?? "")}
                            onChange={(e) => updateArgument(parameter, e.target.value)}
                          />
                        )}
                      </>
                    )}
                    {parameter.help && (
                      <div className="text-[11px] text-gray-500 mt-1 leading-relaxed">
                        {parameter.help}
                      </div>
                    )}
                  </div>
                ))}

                <div className="flex items-center gap-2 pt-2">
                  <button
                    onClick={handleRun}
                    disabled={!canRun || runningAction}
                    className="btn-primary flex items-center gap-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    <Play className="h-3.5 w-3.5" />
                    <span>Run</span>
                  </button>
                  {currentRun?.status === "running" && (
                    <button
                      onClick={handleStop}
                      disabled={runningAction}
                      className="btn-secondary flex items-center gap-1.5 disabled:opacity-50"
                    >
                      <Square className="h-3.5 w-3.5" />
                      <span>Stop</span>
                    </button>
                  )}
                </div>
              </div>

              <div className="min-w-0 border border-border bg-surface-1 rounded-md overflow-hidden">
                <div className="flex items-center justify-between gap-3 px-3 py-2 border-b border-border">
                  <div className="flex items-center gap-2 min-w-0">
                    <Terminal className="h-3.5 w-3.5 text-gray-500 flex-shrink-0" />
                    <span className="text-xs font-medium text-gray-300 truncate">
                      {currentRun ? currentRun.script_name : "Run Output"}
                    </span>
                  </div>
                  {currentRun && (
                    <div
                      className={`flex items-center gap-1.5 text-xs ${statusStyles[currentRun.status]}`}
                    >
                      <StatusIcon status={currentRun.status} />
                      <span className="capitalize">{currentRun.status}</span>
                      {currentRun.exit_code !== null && <span>({currentRun.exit_code})</span>}
                    </div>
                  )}
                </div>
                <pre className="h-[520px] overflow-auto p-3 text-xs leading-relaxed text-gray-300 whitespace-pre-wrap break-words bg-black/30">
                  {currentRun?.log || "No output yet."}
                </pre>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
