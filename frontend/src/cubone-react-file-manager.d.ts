// @cubone/react-file-manager ships no type declarations (package.json has no
// "types"/"typings" field, and "main" points at unbuilt src/index.js) — this
// covers only the props DownloadsBrowser.tsx actually uses. See its README
// on npm for the full prop list if more of it is ever needed.
declare module "@cubone/react-file-manager" {
  import type { ComponentType } from "react";

  export interface File {
    name: string;
    isDirectory: boolean;
    path: string;
    updatedAt?: string;
    size?: number;
  }

  export interface FileManagerPermissions {
    create?: boolean;
    upload?: boolean;
    move?: boolean;
    copy?: boolean;
    rename?: boolean;
    download?: boolean;
    delete?: boolean;
  }

  export interface FileManagerProps {
    files: File[];
    isLoading?: boolean;
    layout?: "list" | "grid";
    height?: string | number;
    width?: string | number;
    enableFilePreview?: boolean;
    permissions?: FileManagerPermissions;
    initialPath?: string;
    onDownload?: (files: File[]) => void;
    onDelete?: (files: File[]) => void;
    onRefresh?: () => void;
    onFolderChange?: (path: string) => void;
    onFileOpen?: (file: File) => void;
    onRename?: (file: File, newName: string) => void;
    onError?: (error: { type: string; message: string }, file: File) => void;
  }

  export const FileManager: ComponentType<FileManagerProps>;
}
