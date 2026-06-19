// Watched local folders domain: directories Shelf scans for importable files.
import { req } from "./http";

export interface WatchedFolder {
  id: number;
  path: string;
  display_name: string | null;
  recursive: boolean;
  enabled: boolean;
  file_count: number;
  works: number;
  last_scan_at: string | null;
  last_error: string | null;
}

export const foldersApi = {
  // --- Watched local folders ---
  listFolders: () => req<WatchedFolder[]>("/local-folders"),
  addFolder: (path: string, recursive = true, displayName?: string) =>
    req<WatchedFolder>("/local-folders", {
      method: "POST",
      body: JSON.stringify({ path, recursive, display_name: displayName }),
    }),
  rescanFolder: (id: number) =>
    req<WatchedFolder>(`/local-folders/${id}/rescan`, { method: "POST" }),
  deleteFolder: (id: number, removeWorks = true) =>
    req<{ deleted: number }>(`/local-folders/${id}?remove_works=${removeWorks}`, {
      method: "DELETE",
    }),
};
