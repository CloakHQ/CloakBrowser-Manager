import { ExternalLink, Monitor } from "lucide-react";
import { useEffect, useState } from "react";
import { CdpEndpointButton } from "./CdpEndpointButton";

interface NativeWindowStatusProps {
  profileName: string;
  cdpUrl: string | null;
  profileId?: string;
  capturePreview?: boolean;
}

// Native mode has no VNC to embed, so we surface the periodic screenshot the
// backend captures (every ~30s) as a near-live preview of the native window.
const PREVIEW_REFRESH_MS = 15000;

export function NativeWindowStatus({
  profileName,
  cdpUrl,
  profileId,
  capturePreview,
}: NativeWindowStatusProps) {
  const [buster, setBuster] = useState(() => Date.now());
  const [loaded, setLoaded] = useState(false);

  const showPreview = Boolean(profileId) && capturePreview !== false;

  useEffect(() => {
    if (!showPreview) return;
    const id = setInterval(() => setBuster(Date.now()), PREVIEW_REFRESH_MS);
    return () => clearInterval(id);
  }, [showPreview]);

  return (
    <div className="flex h-full items-center justify-center p-8">
      <div className="card max-w-xl w-full p-8 text-center">
        {showPreview && (
          <div className={`mb-6 overflow-hidden rounded-xl border border-line bg-surface-0 shadow-card ${loaded ? "" : "hidden"}`}>
            <div className="flex items-center justify-between border-b border-line bg-surface-2/80 px-3 py-1.5 text-2xs text-fg-muted">
              <div className="flex items-center gap-1.5">
                <span className="h-2 w-2 rounded-full bg-success animate-pulse" />
                <span>Xem trước trực tiếp</span>
              </div>
              <span className="font-mono">{profileName}</span>
            </div>
            <img
              src={`/api/profiles/${profileId}/screenshot?t=${buster}`}
              onLoad={() => setLoaded(true)}
              alt="Ảnh xem trước trực tiếp"
              className="w-full object-contain max-h-72"
            />
          </div>
        )}
        <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-2xl bg-brand/10 border border-brand/25 text-brand-fg shadow-glow">
          <Monitor className="h-6 w-6" />
        </div>
        <h2 className="text-lg font-semibold text-fg">Đã mở trong cửa sổ gốc</h2>
        <p className="mt-2 text-sm text-fg-secondary leading-relaxed">
          {profileName} đang chạy trực tiếp trên máy này. Dùng cửa sổ CloakBrowser của hồ sơ để duyệt web.
        </p>
        <div className="mt-6 inline-flex items-center justify-center gap-2 rounded-lg border border-line bg-surface-2/50 px-3.5 py-2 text-xs text-fg-secondary">
          <ExternalLink className="h-3.5 w-3.5 text-brand-fg" />
          <span>Vẫn có thể tự động hóa qua CDP của Manager</span>
          <CdpEndpointButton cdpUrl={cdpUrl} />
        </div>
      </div>
    </div>
  );
}
