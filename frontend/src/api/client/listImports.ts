// List-imports domain: a user imports an external reading list/library (AniList, Goodreads, Open
// Library, Hardcover, MAL, Amazon wishlist), curates which titles to keep + the media variant, then
// subscribes; the backend monitors it and auto-fetches newly-added titles. Mirrors the per-domain
// client style (typed functions on a single exported object, requests via the shared `req`).
import { req } from "./http";

export type ListVariant = "ebook" | "audiobook" | "both";

export interface ListProvider {
  key: string;
  label: string;
  lists: string[]; // selectable sub-lists (e.g. goodreads: ["to-read","currently-reading","read"]); may be empty
}

export interface ListPreviewItem {
  title: string;
  author: string | null;
  media_kind: string;
  cover_url: string | null;
  match_catalog_id: number | null; // a quick local catalog match (null = will search when added)
  match_title: string | null;
  match_author: string | null;
}

export interface ListPreview {
  provider: string;
  list_ref: string;
  list_name: string | null;
  count: number;
  items: ListPreviewItem[];
}

export interface ListResolveItem {
  title: string;
  author?: string | null;
}

export interface ListConfirmItem {
  title: string;
  author?: string | null;
  selected: boolean;       // false → baselined (never fetched); true → fetched now
  variant?: ListVariant;   // optional per-item override of the subscription variant
}

export interface ListConfirm {
  provider: string;
  list_ref: string;
  list_name?: string | null;
  display_name: string;
  variant: ListVariant;
  target_shelf_id?: number | null;
  auto_series?: boolean;        // also fetch the rest of each fetched title's series now
  auto_follow_series?: boolean; // start a series follow so future volumes auto-fetch
  items: ListConfirmItem[]; // the FULL previewed list — selected flags drive acquisition
}

export interface ListSubscription {
  id: number;
  provider: string;
  list_ref: string;
  list_name: string | null;
  display_name: string;
  variant: ListVariant;
  target_shelf_id: number | null;
  auto_series: boolean;
  auto_follow_series: boolean;
  active: boolean;
  auto_added: number;
  last_checked_at: string | null;
  last_error: string | null;
  created_at: string | null;
}

export interface ListSubUpdate {
  variant?: ListVariant;
  target_shelf_id?: number | null;
  auto_series?: boolean;
  auto_follow_series?: boolean;
  active?: boolean;
  list_name?: string | null;
  list_ref?: string;
  display_name?: string;
}

export const listImportsApi = {
  listProviders: () => req<{ providers: ListProvider[] }>("/list-imports/providers"),
  previewList: (body: { provider: string; list_ref: string; list_name?: string | null }) =>
    req<ListPreview>("/list-imports/preview", { method: "POST", body: JSON.stringify(body) }),
  // Resolve a chunk of previewed titles catalog-first then upstream (server-capped at 30 per call).
  resolveList: (items: ListResolveItem[]) =>
    req<ListPreviewItem[]>("/list-imports/resolve", { method: "POST", body: JSON.stringify({ items }) }),
  // Re-fetch an added list's current titles + covers (for the manage cover-row).
  listItems: (id: number) => req<ListPreview>(`/list-imports/${id}/items`),
  listImports: () => req<ListSubscription[]>("/list-imports"),
  createImport: (body: ListConfirm) =>
    req<ListSubscription>("/list-imports", { method: "POST", body: JSON.stringify(body) }),
  patchImport: (id: number, body: ListSubUpdate) =>
    req<ListSubscription>(`/list-imports/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteImport: (id: number) =>
    req<{ deleted: boolean }>(`/list-imports/${id}`, { method: "DELETE" }),
  syncImport: (id: number) =>
    req<ListSubscription>(`/list-imports/${id}/sync`, { method: "POST" }),
};
