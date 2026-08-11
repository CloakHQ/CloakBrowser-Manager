import { ExternalLink, Monitor } from "lucide-react";
import { CdpEndpointButton } from "./CdpEndpointButton";

interface NativeWindowStatusProps {
  profileName: string;
  cdpUrl: string | null;
}

export function NativeWindowStatus({ profileName, cdpUrl }: NativeWindowStatusProps) {
  return (
    <div className="flex h-full items-center justify-center p-8">
      <div className="max-w-lg rounded-xl border border-border bg-surface-1 p-8 text-center">
        <Monitor className="mx-auto mb-4 h-10 w-10 text-accent" />
        <h2 className="text-lg font-medium text-gray-100">Opened in a native window</h2>
        <p className="mt-2 text-sm text-gray-400">
          {profileName} is running directly on this computer. Use its CloakBrowser window to browse.
        </p>
        <div className="mt-5 flex items-center justify-center gap-2 text-xs text-gray-500">
          <ExternalLink className="h-3.5 w-3.5" />
          <span>Automation remains available through Manager CDP</span>
          <CdpEndpointButton cdpUrl={cdpUrl} />
        </div>
      </div>
    </div>
  );
}
