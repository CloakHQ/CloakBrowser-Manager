import { useCallback, useEffect, useState } from "react";
import { FileManager, type File as ManagerFile } from "@cubone/react-file-manager";
import "@cubone/react-file-manager/dist/style.css";
import { api, downloadFileUrl } from "../lib/api";

interface DownloadsBrowserProps {
  profileId: string;
}

// The Downloads dir is flat (see browser_manager.py's _finalize_download —
// nothing this Manager writes ever creates a subdirectory), so folder
// navigation, create/move/copy/rename are all switched off: the only real
// operations here are looking at what a profile downloaded, saving a copy,
// and clearing space.
const PERMISSIONS = {
  create: false, upload: false, move: false, copy: false, rename: false,
  download: true, delete: true,
};

export function DownloadsBrowser({ profileId }: DownloadsBrowserProps) {
  const [files, setFiles] = useState<ManagerFile[]>([]);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.listDownloads(profileId);
      // The library's `size` is `number | undefined` (no `null`) — our API
      // sends `null` for a directory, so convert at this one boundary.
      setFiles(data.map((f) => ({ ...f, size: f.size ?? undefined })));
    } catch (err) {
      console.warn("[downloads] failed to load:", err);
    } finally {
      setLoading(false);
    }
  }, [profileId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const handleDownload = (selected: ManagerFile[]) => {
    for (const file of selected) {
      if (file.isDirectory) continue;
      // A new tab per file: the server's Content-Disposition: attachment
      // (FileResponse's `filename=`) makes the browser save rather than
      // navigate, and the blank tab closes itself once that starts.
      window.open(downloadFileUrl(profileId, file.path), "_blank");
    }
  };

  const handleDelete = async (selected: ManagerFile[]) => {
    try {
      await Promise.all(
        selected.filter((f) => !f.isDirectory).map((f) => api.deleteDownload(profileId, f.path)),
      );
    } catch (err) {
      alert(err instanceof Error ? err.message : "Failed to delete file");
    } finally {
      await refresh();
    }
  };

  return (
    <FileManager
      files={files}
      isLoading={loading}
      layout="list"
      height={360}
      enableFilePreview={false}
      permissions={PERMISSIONS}
      onDownload={handleDownload}
      onDelete={handleDelete}
      onRefresh={refresh}
    />
  );
}
