// Bookshelves domain: the user's named shelves and shelf membership, plus the bulk library ZIP
// download (which targets a shelf or an explicit set of work ids).
import { req, BASE } from "./http";

export interface Bookshelf {
  id: number;
  name: string;
  sort_order: number;
  // NB: no `auto_update` — chapter gathering is automatic for every releasing library title; the
  // legacy per-shelf toggle is a deprecated no-op (the nullable DB column is kept for back-compat).
  auto_kindle: boolean;
  notify_on_add: boolean;
  notify_email: boolean;
  goodreads_target: boolean;
  goodreads_shelf: string | null;
  watch_path: string | null;
  count: number;
}

export interface BookshelfCreate {
  name: string;
  auto_kindle?: boolean;
  notify_on_add?: boolean;
  notify_email?: boolean;
  goodreads_target?: boolean;
  goodreads_shelf?: string | null;
  watch_path?: string | null;
  work_ids?: number[];
}

// bulkDownloadUrl is module-local so both it and downloadLibrary reference the exact same URL
// builder without depending on the assembled `api` object (preserves the original behavior where
// downloadLibrary called api.bulkDownloadUrl at runtime).
function bulkDownloadUrl(payload: { work_ids?: number[]; shelf_id?: number }) {
  const p = new URLSearchParams();
  if (payload.work_ids?.length) p.set("ids", payload.work_ids.join(","));
  if (payload.shelf_id != null) p.set("shelf_id", String(payload.shelf_id));
  return `${BASE}/library/download?${p.toString()}`;
}

export const bookshelvesApi = {
  // --- Bookshelves ---
  listBookshelves: () => req<Bookshelf[]>("/bookshelves"),
  createBookshelf: (payload: BookshelfCreate) =>
    req<Bookshelf>("/bookshelves", { method: "POST", body: JSON.stringify(payload) }),
  updateBookshelf: (id: number, patch: Partial<Bookshelf>) =>
    req<Bookshelf>(`/bookshelves/${id}`, { method: "PATCH", body: JSON.stringify(patch) }),
  deleteBookshelf: (id: number) =>
    req<{ deleted: number }>(`/bookshelves/${id}`, { method: "DELETE" }),
  addWorkToShelf: (shelfId: number, workId: number) =>
    req<Bookshelf>(`/bookshelves/${shelfId}/works/${workId}`, { method: "POST" }),
  removeWorkFromShelf: (shelfId: number, workId: number) =>
    req<Bookshelf>(`/bookshelves/${shelfId}/works/${workId}`, { method: "DELETE" }),

  // Same-origin URL for the bulk ZIP download (GET so it can be hit by a plain <a download>).
  bulkDownloadUrl,

  // Bulk download selected works / a shelf as a ZIP of EPUBs. Triggered via a real <a download>
  // click within the user gesture — a fetch()+programmatic blob click is silently dropped by
  // iOS Safari (gesture lost across the await) and races URL.revokeObjectURL on desktop.
  downloadLibrary: async (payload: { work_ids?: number[]; shelf_id?: number }) => {
    const a = document.createElement("a");
    a.href = bulkDownloadUrl(payload);
    a.download = "shelf-library.zip";
    a.rel = "noopener";
    document.body.appendChild(a);
    a.click();
    a.remove();
  },
};
