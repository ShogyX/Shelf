// Downloads & acquisition domain: the acquisition pipeline (Prowlarr search → SABnzbd/qBittorrent
// download), the download-job queue, fetch-source routing/priority, and the one-click acquire.
import { req } from "./http";
import type { SeriesBook, SeriesInfo } from "./works";

export interface ReleaseCandidate {
  title: string;
  indexer: string | null;
  guid: string | null;
  size: number;
  size_mb: number;
  fmt: string | null;
  is_audiobook: boolean;
  language: string | null;
  confidence: number;
  score: number;
  accepted: boolean;
  auto_ok: boolean;
  reason: string;
}

export interface DownloadJob {
  id: number;
  catalog_work_id: number | null;
  title: string;
  release_title: string | null;
  indexer: string | null;
  size: number;
  fmt: string | null;
  status: string; // queued | downloading | completed | imported | failed | deferred
  verifying: boolean; // downloaded, awaiting the verification gate
  percent: number;    // coarse stage-based progress (0..100)
  grab_kind: string; // manual | auto
  work_id: number | null;
  error: string | null;
  not_before: string | null; // when a deferred (daily-cap) grab retries
  created_at: string | null;
  updated_at: string | null;
  completed_at: string | null;
}

// An acquire/grab can come back "gated" (HTTP 200) when the title is known-unavailable and not yet
// due for a re-check — surface it as an informational hint, not an error.
export interface GatedResult {
  status: "gated";
  next_check_at: string;
}

export const downloadsApi = {
  // --- Acquisition pipeline (Prowlarr search → SABnzbd download) ---
  catalogSeries: (catalogId: number) => req<SeriesInfo>(`/catalog/${catalogId}/series`),
  acquireSeries: (
    catalogId: number, body: { refs?: string[]; all?: boolean; specials?: boolean; shelf_id?: number }
  ) =>
    req<{ results: Array<Record<string, unknown>> }>(`/catalog/${catalogId}/series/acquire`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  // Author roster ("request all by {author}"): count is the FULL roster (the acquire is server-capped).
  catalogAuthor: (catalogId: number) =>
    req<{ author: string | null; books: SeriesBook[]; count: number }>(`/catalog/${catalogId}/author`),
  acquireAuthor: (
    catalogId: number, body: { refs?: string[]; all?: boolean; shelf_id?: number }
  ) =>
    req<{ results: Array<Record<string, unknown>> }>(`/catalog/${catalogId}/author/acquire`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  grabPipeline: (catalogId: number, opts?: { guid?: string; fuzz?: boolean; shelfId?: number }) => {
    const p = new URLSearchParams();
    if (opts?.guid) p.set("guid", opts.guid);
    if (opts?.fuzz) p.set("fuzz", "true");
    if (opts?.shelfId != null) p.set("shelf_id", String(opts.shelfId));
    const qs = p.toString();
    return req<DownloadJob | GatedResult>(`/catalog/${catalogId}/grab-pipeline${qs ? `?${qs}` : ""}`, {
      method: "POST",
    });
  },
  listDownloads: (status?: string) =>
    req<DownloadJob[]>(`/downloads${status ? `?status=${status}` : ""}`),
  deleteDownload: (id: number) =>
    req<{ deleted: number }>(`/downloads/${id}`, { method: "DELETE" }),

  // --- Acquisition routing (fetch-source priority + one-click acquire) ---
  getFetchPriority: () =>
    req<{ routes: string[]; global: string[]; effective: string[] }>("/fetch-priority"),
  // Acquisition order is global + admin-only; there is no per-user setter.
  setGlobalFetchPriority: (order: string[]) =>
    req<{ global: string[] }>("/fetch-priority/global", {
      method: "PUT",
      body: JSON.stringify({ order }),
    }),
  acquireCatalog: (
    id: number,
    route?: string,
    shelfId?: number,
    variant?: "ebook" | "audiobook" | "both",
  ) => {
    const p = new URLSearchParams();
    if (route) p.set("route", route);
    if (shelfId != null) p.set("shelf_id", String(shelfId));
    if (variant) p.set("variant", variant);
    const qs = p.toString();
    type One = { route: string | null; status: string; work_id?: number; job_id?: number; detail?: string };
    // variant="both" returns { ebook, audiobook }; otherwise a single result (or a GatedResult).
    return req<One | GatedResult | { ebook: One | GatedResult; audiobook: One | GatedResult }>(
      `/catalog/${id}/acquire${qs ? `?${qs}` : ""}`,
      { method: "POST" }
    );
  },
};
