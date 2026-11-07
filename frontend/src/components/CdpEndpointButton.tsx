import { useState } from "react";
import { Check, Code2 } from "lucide-react";

interface CdpEndpointButtonProps {
  cdpUrl: string | null;
}

export function CdpEndpointButton({ cdpUrl }: CdpEndpointButtonProps) {
  const [copied, setCopied] = useState(false);

  if (!cdpUrl) return null;

  const copyEndpoint = () => {
    const endpoint = `${window.location.protocol}//${window.location.host}${cdpUrl}`;
    navigator.clipboard?.writeText(endpoint).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }).catch((error) => console.warn("[cdp] copy failed:", error));
  };

  return (
    <button
      onClick={copyEndpoint}
      className={`inline-flex items-center justify-center rounded-md p-1.5 transition-colors ${
        copied
          ? "bg-success/15 text-success"
          : "text-fg-muted hover:bg-surface-3 hover:text-fg"
      }`}
      title={copied ? "Đã sao chép!" : "Sao chép URL CDP"}
    >
      {copied ? <Check className="h-3.5 w-3.5" /> : <Code2 className="h-3.5 w-3.5" />}
    </button>
  );
}
