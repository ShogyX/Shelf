// Works domain: a library title and everything hanging off it — chapters, the reader payload,
// reading progress, per-work metadata links / related titles / queued hooks, series, health and
// update checks, and per-work file export (EPUB/CBZ, send-to-Kindle).
import { req, BASE } from "./http";

export interface Work {
  id: number;
  source_id: number | null;
  source_work_ref: string | null;
  title: string;
  author: string | null;
  cover_url: string | null;
  description: string | null;
  language: string | null;
  status: string;
  media_kind: string; // text | comic
  series: string | null; // series name (for library grouping), if known
  series_position: number | null; // this volume's position in the series (may be fractional)
  hooked: boolean;
  total_chapters_known: number;
  total_chapters_expected: number | null;
  chapters_fetched: number;
  start_chapter: number; // hooked from this chapter number (1 = from the beginning)
  health: string; // unknown | ok | incomplete | no_chapters | unreachable
  // One clear library state: gathering | ongoing | complete | incomplete.
  library_status: string;
  health_detail: string | null;
  last_checked_at: string | null;
  last_update_at: string | null;
  crawl_interval_s: number | null;
  crawl_window_start: number | null;
  crawl_window_end: number | null;
  shelf_ids: number[]; // which of the caller's bookshelves this work is on
  audiobook_work_id: number | null; // matching shared audiobook Work (the "listen" format), if any
}

export interface ProviderStats {
  provider: string;
  total: number;
  matched: number;
  unmatched: number;
  high_confidence: number;
  medium_confidence: number;
  low_confidence: number;
  match_ratio: number;
}

export interface MetadataStats {
  total_library_works: number;
  providers: ProviderStats[];
}

export interface CrawlPolicy {
  crawl_interval_s: number | null;
  crawl_window_start: number | null;
  crawl_window_end: number | null;
}

export interface WorkDetail extends Work {
  chapters_total: number;
  chapters_read: number;
  last_chapter_id: number | null;
  scroll_fraction: number;
  default_shelf_id: number | null; // the caller's own per-title default shelf (null = library only)
  // Display metadata (Wave 5) — filled at hook + the provider backfill tick.
  created_at: string | null;
  local_size: number | null;
  rating: number | null; // 0–10
  rating_count: number | null;
  year: number | null;
  genres: string[] | null;
  narrator: string | null;
  publisher: string | null;
  identifiers: Record<string, unknown> | null; // { isbn: [...], anilist: "..", ... }
  page_count: number | null;
}

export interface Chapter {
  id: number;
  work_id: number;
  index: number; // internal ordering position (may differ from the chapter number)
  number: number; // the chapter's human number (e.g. 700) — what to display
  title: string;
  fetch_status: string;
  has_content: boolean;
}

export interface ChapterList {
  items: Chapter[];
  total: number;
  limit: number;
  offset: number;
}

export interface ReaderContent {
  chapter_id: number;
  work_id: number;
  index: number;
  title: string;
  html: string;
  word_count: number;
  prev_chapter_id: number | null;
  next_chapter_id: number | null;
}

export interface Progress {
  work_id: number;
  last_chapter_id: number | null;
  scroll_fraction: number;
  paragraph_index: number;
  chapters_read: number;
  continue_chapter_id: number | null;
}

export interface ContinueItem {
  work_id: number;
  title: string;
  author: string | null;
  cover_url: string | null;
  chapter_id: number;
  chapter_index: number;
  chapter_title: string;
  paragraph_index: number;
  scroll_fraction: number;
  chapters_read: number;
  total_chapters: number;
  percent: number;
  updated_at: string;
}

// --- Audiobook playback ---
export interface AudioTrack {
  index: number;
  url: string;        // stream URL (already /api-prefixed by the backend)
  duration_s: number;
  mime: string;
  native: boolean;    // browser can play it directly; false → transcoded server-side
}
export interface AudioChapter {
  title: string;
  track_index: number;
  start_s: number;        // offset within its track
  global_start_s: number; // offset from the start of the whole book
}
export interface AudioManifest {
  work_id: number;
  title: string;
  author: string | null;
  cover_url: string | null;
  total_duration_s: number;
  tracks: AudioTrack[];
  chapters: AudioChapter[];
}
export interface AudioProgress {
  work_id: number;
  track: number;
  pos_s: number;
}
export interface ContinueListenItem {
  work_id: number;
  title: string;
  author: string | null;
  cover_url: string | null;
  track: number;
  pos_s: number;
  global_pos_s: number;
  total_duration_s: number;
  percent: number;
  updated_at: string;
}

export interface MetadataLink {
  id: number;
  work_id: number;
  provider: string;
  ref: string;
  matched_title: string | null;
  confidence: number;
  status: string; // auto | confirmed | rejected
  total_units: number | null;
  unit_kind: string | null;
  release_marker: string | null;
  url: string | null;
  provider_status: string | null;
  last_checked_at: string | null;
  expected_chapters: number | null;
  chapter_discrepancy: number | null;
  major_discrepancy: boolean;
}

export interface RelatedItem {
  title: string;
  relation: string;
  provider: string;
  ref: string | null;
  queued_status: string | null;
  in_library: boolean;
}

export interface WorkRelated {
  work_id: number;
  related: RelatedItem[];
}

export interface QueuedHook {
  id: number;
  title: string;
  author: string | null;
  media_kind: string;
  reason: string; // related | goodreads
  source: string | null;
  relation: string | null;
  status: string; // pending | hooked | failed
  related_work_id: number | null;
  hooked_work_id: number | null;
  detail: string | null;
  created_at: string | null;
}

export interface SeriesBook {
  title: string;
  author: string | null;
  year: number | null;
  position: number | null;
  cover_url: string | null;
  ref: string | null;
  catalog_id: number | null;
  hooked_work_id: number | null;
  in_library?: boolean;
}

export interface SeriesInfo {
  series: string | null;
  books: SeriesBook[];
}

export interface WorkUpdate {
  work_id: number;
  checked: boolean;
  new_chapters: number;
  metadata_changed: boolean;
  status: string | null;
  total_chapters_expected: number | null;
  error: string | null;
}

export interface CheckAllUpdates {
  works_checked: number;
  works_updated: number;
  new_chapters: number;
}

export interface WorkHealth {
  work_id: number;
  health: string;
  detail: string | null;
  fetched: number;
  failed: number;
  pending: number;
  listed: number;
  advertised: number | null;
  gaps: number[];
  actions: string[];
}

export interface MetaCandidate {
  provider: string;
  ref: string;
  title: string;
  author: string | null;
  year: number | null;
  cover_url: string | null;
  synopsis: string | null;
  media_kind: string;
}

export interface WorkProvenance {
  source_key: string | null;
  source_name: string | null;
  source_ref: string | null;
  source_url: string | null;
  filename: string | null;
  file_size: number | null;
  catalog_title: string | null;
  catalog_author: string | null;
  catalog_domain: string | null;
  catalog_url: string | null;
  request_title: string | null;
  request_author: string | null;
  request_origin: string | null;
  request_detail: string | null;
}

export const worksApi = {
  listWorks: (q?: string, opts?: { shelfId?: number }) => {
    const p = new URLSearchParams();
    if (q && q.trim()) p.set("q", q.trim());
    if (opts?.shelfId != null) p.set("shelf_id", String(opts.shelfId));
    const qs = p.toString();
    return req<Work[]>(`/works${qs ? `?${qs}` : ""}`);
  },
  getWork: (id: number) => req<WorkDetail>(`/works/${id}`),
  enrichWork: (id: number) => req<WorkDetail>(`/works/${id}/enrich`, { method: "POST" }),
  // Manually correct a library work's metadata (fix a wrong auto-match). Only the provided fields change.
  updateWorkMetadata: (id: number, body: Partial<{ title: string; author: string | null; cover_url: string | null; series: string | null; series_position: number | null; source_work_ref: string | null }>) =>
    req<WorkDetail>(`/works/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  // Search enabled metadata providers for candidates to re-match a work against.
  searchWorkMetadata: (id: number, q: string, author?: string | null) => {
    const p = new URLSearchParams({ q });
    if (author) p.set("author", author);
    return req<MetaCandidate[]>(`/works/${id}/metadata-search?${p.toString()}`);
  },
  // Where a work came from (source/file/catalog/original request) — to diagnose a wrong match.
  getWorkProvenance: (id: number) => req<WorkProvenance>(`/works/${id}/provenance`),
  setWorkDefaultShelf: (workId: number, shelfId: number | null) =>
    req<WorkDetail>(`/works/${workId}/default-shelf`, {
      method: "PUT",
      body: JSON.stringify({ shelf_id: shelfId }),
    }),
  deleteWork: (id: number) => req<{ deleted: number }>(`/works/${id}`, { method: "DELETE" }),

  listChapters: (id: number, limit = 500, offset = 0) =>
    req<ChapterList>(`/works/${id}/chapters?limit=${limit}&offset=${offset}`),
  // Fetch the COMPLETE chapter list (works can reach many thousands), paging through the
  // server's per-request cap so the reader's table of contents is never truncated.
  listAllChapters: async (id: number): Promise<Chapter[]> => {
    const page = 5000; // server max per request
    const first = await req<ChapterList>(`/works/${id}/chapters?limit=${page}&offset=0`);
    const items = [...first.items];
    for (let offset = page; offset < first.total; offset += page) {
      const next = await req<ChapterList>(`/works/${id}/chapters?limit=${page}&offset=${offset}`);
      items.push(...next.items);
      if (next.items.length === 0) break; // safety: never loop on an empty page
    }
    return items;
  },
  getChapter: (id: number) => req<ReaderContent>(`/chapters/${id}`),
  // Text cleanup: de-censor + reflow badly-scraped chapter HTML. Per-chapter returns the refreshed
  // reader content; the whole-title pass returns how many chapters changed.
  cleanChapter: (chapterId: number) =>
    req<ReaderContent>(`/chapters/${chapterId}/clean`, { method: "POST" }),
  cleanWork: (workId: number) =>
    req<{ cleaned: number; total: number }>(`/works/${workId}/clean`, { method: "POST" }),

  getProgress: (workId: number) => req<Progress>(`/works/${workId}/progress`),
  saveProgress: (
    workId: number,
    lastChapterId: number,
    scrollFraction: number,
    paragraphIndex = 0
  ) =>
    req<Progress>(`/works/${workId}/progress`, {
      method: "POST",
      body: JSON.stringify({
        last_chapter_id: lastChapterId,
        scroll_fraction: scrollFraction,
        paragraph_index: paragraphIndex,
      }),
    }),
  continueReading: () => req<ContinueItem[]>("/continue-reading"),
  clearProgress: (workId: number) =>
    req<{ cleared: number }>(`/works/${workId}/progress`, { method: "DELETE" }),

  getMetadataStats: () => req<MetadataStats>("/metadata-stats"),

  hook: (sourceKey: string, workRef: string, policy?: Partial<CrawlPolicy>, shelfId?: number) =>
    req<Work>("/works/hook", {
      method: "POST",
      body: JSON.stringify({
        source_key: sourceKey, work_ref: workRef, ...(policy ?? {}),
        ...(shelfId != null ? { shelf_id: shelfId } : {}),
      }),
    }),
  setCrawlPolicy: (workId: number, policy: Partial<CrawlPolicy>) =>
    req<Work>(`/works/${workId}/crawl-policy`, {
      method: "PATCH",
      body: JSON.stringify(policy),
    }),
  resumeWork: (workId: number) => req<Work>(`/works/${workId}/resume`, { method: "POST" }),
  pauseWork: (workId: number) => req<Work>(`/works/${workId}/pause`, { method: "POST" }),
  importFile: (file: File, shelfId?: number) => {
    const fd = new FormData();
    fd.append("file", file);
    if (shelfId != null) fd.append("shelf_id", String(shelfId));
    return req<Work>("/works/import", { method: "POST", body: fd });
  },

  exportEpubUrl: (workId: number, start = 1, limit?: number) => {
    const q = new URLSearchParams({ start: String(start) });
    if (limit) q.set("limit", String(limit));
    return `${BASE}/works/${workId}/export.epub?${q.toString()}`;
  },
  // Format-aware single-work download: CBZ for comics, EPUB for text (filename from the server).
  downloadUrl: (workId: number, start = 1, limit?: number) => {
    const q = new URLSearchParams({ start: String(start) });
    if (limit) q.set("limit", String(limit));
    return `${BASE}/works/${workId}/download?${q.toString()}`;
  },
  // Audiobook download: the single audio file, or a ZIP of a multi-file audiobook's folder.
  audioUrl: (workId: number) => `${BASE}/works/${workId}/audio`,
  // --- Audiobook in-app playback ---
  audioManifest: (workId: number) => req<AudioManifest>(`/works/${workId}/audio/manifest`),
  // A plain <audio src>: the session cookie auto-authenticates it (manifest URLs are /api-prefixed,
  // but the manifest may also be served from a track index directly).
  audioStreamUrl: (workId: number, track: number) => `${BASE}/works/${workId}/audio/stream/${track}`,
  getAudioProgress: (workId: number) => req<AudioProgress>(`/works/${workId}/audio/progress`),
  saveAudioProgress: (workId: number, track: number, posS: number) =>
    req<AudioProgress>(`/works/${workId}/audio/progress`, {
      method: "POST", body: JSON.stringify({ track, pos_s: posS }),
    }),
  continueListening: () => req<ContinueListenItem[]>("/continue-listening"),
  sendToKindle: (
    workId: number,
    body: { to?: string; kindle_email?: string; start?: number; limit?: number }
  ) =>
    req<{ sent: boolean; chapters: number; to: string }>(`/works/${workId}/send-to-kindle`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // --- Work completeness diagnostics ---
  repairWork: (workId: number) =>
    req<WorkHealth>(`/works/${workId}/repair`, { method: "POST" }),

  // --- Update tracker: re-check hooked titles for new content ---
  checkWorkUpdates: (workId: number) =>
    req<WorkUpdate>(`/works/${workId}/check-updates`, { method: "POST" }),
  checkAllUpdates: () =>
    req<CheckAllUpdates>("/works/check-updates", { method: "POST" }),

  // --- Metadata providers (ranobedb / goodreads): links, related titles, hook queue ---
  workMetadataLinks: (workId: number) =>
    req<MetadataLink[]>(`/works/${workId}/metadata`),
  workRelated: (workId: number) => req<WorkRelated>(`/works/${workId}/related`),
  queueRelated: (workId: number) =>
    req<{ work_id: number; queued: number }>(`/works/${workId}/queue-related`, {
      method: "POST",
    }),
  confirmMetadataLink: (id: number) =>
    req<MetadataLink>(`/metadata-links/${id}/confirm`, { method: "POST" }),
  deleteMetadataLink: (id: number) =>
    req<{ deleted: number }>(`/metadata-links/${id}`, { method: "DELETE" }),
  listQueuedHooks: (status?: string) =>
    req<QueuedHook[]>(`/queued-hooks${status ? `?status=${status}` : ""}`),
  processQueuedHooks: () =>
    req<{ processed: number; hooked: number }>(`/queued-hooks/process`, { method: "POST" }),
  deleteQueuedHook: (id: number) =>
    req<{ deleted: number }>(`/queued-hooks/${id}`, { method: "DELETE" }),

  // The full series a library work belongs to (each volume flagged in_library vs missing).
  workSeries: (workId: number) => req<SeriesInfo>(`/works/${workId}/series`),
};
