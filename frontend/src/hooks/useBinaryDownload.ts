import { useEffect, useState } from "react";
import { api } from "../lib/api";

export interface BinaryDownloadStatus {
  downloading: boolean;
  percent: number | null;
  state: string | null;
}

const IDLE: BinaryDownloadStatus = { downloading: false, percent: null, state: null };

/**
 * Polls /api/status for CloakBrowser's first-launch Chromium download, so the
 * UI can show a progress banner instead of a launch that just looks slow or
 * hung. Keeps polling for the life of the app (the endpoint is cheap and
 * auth-exempt) rather than only while a download is suspected, since that's
 * the only way to notice one starting.
 */
export function useBinaryDownload(): BinaryDownloadStatus {
  const [status, setStatus] = useState<BinaryDownloadStatus>(IDLE);

  useEffect(() => {
    let cancelled = false;

    const poll = async () => {
      try {
        const data = await api.getStatus();
        if (cancelled) return;
        setStatus({
          downloading: data.binary_downloading,
          percent: data.binary_download_percent,
          state: data.binary_download_state,
        });
      } catch {
        // Transient network hiccup — keep showing the last known state
        // rather than flashing the banner off.
      }
    };

    poll();
    const interval = setInterval(poll, 1000);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, []);

  return status;
}
