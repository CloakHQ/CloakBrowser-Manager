import { ExternalLink } from "lucide-react";
import type { Profile } from "../lib/api";

export function NativeProfileViewer({ profile }: { profile: Profile }) {
  return (
    <div className="flex h-full items-center justify-center p-8">
      <div className="max-w-lg rounded-xl border border-gray-800 bg-surface-1 p-8 text-center">
        <ExternalLink className="mx-auto mb-4 h-8 w-8 text-blue-400" />
        <h2 className="text-lg font-medium text-gray-100">Opened in macOS window</h2>
        <p className="mt-2 text-sm text-gray-400">
          {profile.name} runs through its native CloakBrowser profile. No VNC session needed.
        </p>
        {profile.notes && (
          <pre className="mt-5 whitespace-pre-wrap rounded-lg bg-surface-0 p-4 text-left text-xs text-gray-400">
            {profile.notes}
          </pre>
        )}
      </div>
    </div>
  );
}
