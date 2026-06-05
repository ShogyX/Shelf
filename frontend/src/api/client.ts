// Thin typed REST client for the Shelf API.

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
  hooked: boolean;
  total_chapters_known: number;
  total_chapters_expected: number | null;
  chapters_fetched: number;
  start_chapter: number; // hooked from this chapter number (1 = from the beginning)
  health: string; // unknown | ok | incomplete | no_chapters | unreachable
  health_detail: string | null;
  last_checked_at: string | null;
  last_update_at: string | null;
  crawl_interval_s: number | null;
  crawl_daily_limit: number | null;
  crawl_window_start: number | null;
  crawl_window_end: number | null;
  shelf_ids: number[]; // which of the caller's bookshelves this work is on
}

export interface Bookshelf {
  id: number;
  name: string;
  sort_order: number;
  auto_update: boolean;
  auto_kindle: boolean;
  notify_on_add: boolean;
  goodreads_target: boolean;
  goodreads_shelf: string | null;
  count: number;
}

export interface BookshelfCreate {
  name: string;
  auto_update?: boolean;
  auto_kindle?: boolean;
  notify_on_add?: boolean;
  goodreads_target?: boolean;
  goodreads_shelf?: string | null;
  work_ids?: number[];
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
  crawl_daily_limit: number | null;
  crawl_window_start: number | null;
  crawl_window_end: number | null;
}

export interface WorkDetail extends Work {
  chapters_total: number;
  chapters_read: number;
  last_chapter_id: number | null;
  scroll_fraction: number;
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

export interface Source {
  id: number;
  key: string;
  display_name: string;
  base_url: string | null;
  adapter_key: string;
  license_basis: string;
  tos_permitted: boolean;
  robots_respected: boolean;
  render_js: boolean;
  min_request_interval_s: number;
  max_daily_requests: number;
  has_auth: boolean;       // a credential (e.g. J-Novel token) is stored
  supports_auth: boolean;  // this source accepts an access token
  auth_token?: string;     // write-only: set to store, "" to clear (never returned)
}

export interface AdapterInfo {
  key: string;
  display_name: string;
  license_basis: string;
  tos_permitted_default: boolean;
  needs_attestation: boolean;
  description: string;
  enabled: boolean;
}

export interface Job {
  id: number;
  work_id: number;
  kind: string;
  status: string;
  attempts: number;
  last_error: string | null;
  cursor: Record<string, unknown> | null;
  scheduled_for: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface ReaderPrefs {
  fontFamily: string;
  fontSize: number;
  lineHeight: number;
  letterSpacing: number;
  paragraphSpacing: number;
  measure: number;
  justify: boolean;
  mode: "scroll" | "paginated";
  textColor: string;
  bgColor: string;
  textLightness: number | null; // null = follow theme
  bgLightness: number | null;
  fabX: number | null; // free-floating position: viewport fraction 0..1 (null=default)
  fabY: number | null;
  fabSide: "left" | "right" | "top" | "bottom"; // legacy docked edge (unused)
  fabPos: number; // legacy edge position (unused)
  fabHidden: boolean; // user hid the floating controls (reveal tab brings them back)
  textPosition: number; // 0=left … 50=center … 100=right
  // Camouflage "work mode": restyle the reader to look like work content.
  workMode: "off" | "docs" | "article" | "email";
  // --- Comic / manga / webtoon (media_kind === "comic") image reading ---
  comicMode: "continuous" | "single"; // vertical strip (webtoon) vs one page per screen (manga)
  comicFit: "width" | "height"; // fit each page to the viewport width or height
  comicZoom: number; // zoom multiplier on top of the fit (1 = 100%)
  comicGap: number; // px gap between pages in continuous mode (0 = seamless webtoon)
}

export interface DeliveryConfig {
  smtp_host?: string | null;
  smtp_port?: number | null;
  smtp_username?: string | null;
  smtp_from?: string | null;
  smtp_security?: string | null; // none | starttls | ssl
  smtp_password?: string; // write-only
  email_to?: string | null;
  smtp_password_set?: boolean; // read-only
}

export interface AppSettings {
  theme: string;
  reader_prefs: ReaderPrefs;
  kindle_email: string | null;
  smtp_configured: boolean;
  delivery: DeliveryConfig;
  apprise_url: string | null; // per-user push target (ntfy/Pushover/Telegram/…)
}

export interface GoodreadsConnection {
  connected: boolean;
  id?: number | null;
  enabled?: boolean;
  goodreads_user_id?: string | null;
  shelf?: string | null;
  last_sync_at?: string | null;
  last_error?: string | null;
}

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

export interface IndexSite {
  id: number;
  root_url: string;
  domain: string;
  title: string | null;
  status: string; // active | paused | done | failed | removed
  max_pages: number; // 0 = unlimited
  max_depth: number;
  same_host_only: boolean;
  stop_after_idle_pages: number; // 0 → uses global default
  pages_since_new_title: number;
  last_error: string | null;
  cooldown_until: string | null; // when set + future: throttling after pushback (paused, not stopped)
  consecutive_errors: number;    // transient errors in a row (drives cooldown escalation)
  status_reason: string | null;  // human explanation of why it's done/paused/cooling/failed
  pages_total: number;
  pages_fetched: number;
  pages_pending: number;
  pages_failed: number;
  words: number;
  titles_found: number;
  requests: number;
  duration_seconds: number;
  last_activity_at: string | null;
  created_at: string;
}

export interface IndexConfig {
  stop_after_idle_pages: number;
  max_pages: number; // 0 = unlimited
}

export interface CrawlTuning {
  tick_seconds: number;
  chapters_per_tick: number;
  parallel_fetches: number;
}

export interface IndexBlock {
  id: number;
  scope: string; // url | domain
  value: string;
  reason: string | null;
  title: string | null;
  created_at: string;
}

export interface IndexedPage {
  id: number;
  site_id: number;
  url: string;
  title: string | null;
  description: string | null;
  author: string | null;
  cover_url: string | null;
  site_name: string | null;
  page_type: string | null;
  word_count: number;
  depth: number;
  status: string;
  hooked_work_id: number | null;
  fetched_at: string | null;
  snippet: string | null;
  last_error?: string | null;
  attempts?: number;
  next_attempt_at?: string | null;
  html?: string | null;
  domain?: string | null;
}

export interface IndexSearchResult {
  page_id: number;
  site_id: number;
  url: string;
  title: string | null;
  description: string | null;
  author: string | null;
  cover_url: string | null;
  snippet: string;
  score: number;
}

export interface CatalogSource {
  catalog_id: number;
  title: string | null;
  author: string | null;
  cover_url: string | null;
  synopsis: string | null;
  site_id: number | null;
  domain: string;
  work_url: string;
  provider: string; // web_index | readarr | kapowarr
  kind: string; // online | readarr | kapowarr
  media_kind: string; // text | comic
  media_label: string; // Novel | Book | Manga | Webtoon | Comic
  integration_id: number | null;
  chapters_advertised: number | null;
  chapters_listed: number | null;
  health: string;
  health_detail: string | null;
  hooked_work_id: number | null;
  grab_status: string | null;
}

export type IntegrationKind =
  | "readarr"
  | "kapowarr"
  | "ranobedb"
  | "goodreads"
  | "googlebooks"
  | "anilist"
  | "novelupdates";

export interface Integration {
  id: number;
  kind: IntegrationKind;
  name: string;
  base_url: string;
  enabled: boolean;
  root_folder: string | null;
  auto_map_folders: boolean;
  config: Record<string, string> | null;
  is_metadata: boolean;
  has_api_key: boolean;
  last_sync_at: string | null;
  last_error: string | null;
  catalog_count: number;
}

export interface IntegrationTest {
  ok: boolean;
  app: string | null;
  version: string | null;
  detail: string | null;
  root_folders: string[];
  error: string | null;
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

export interface CatalogGroup {
  id: number; // representative catalog id — stable unique key
  norm_key: string;
  title: string;
  author: string | null;
  cover_url: string | null;
  synopsis: string | null;
  language: string | null;
  media_kind: string;
  media_label: string; // Novel | Book | Manga | Webtoon | Comic
  chapters: number | null;
  hooked_work_id: number | null;
  sources: CatalogSource[];
}

export interface CatalogRow {
  kind: string; // popular | genre | theme
  slug: string;
  label: string;
  media_bucket: string; // comic | text
  count: number;
  items: CatalogGroup[];
}

export interface CatalogCategory {
  kind: string; // genre | theme
  slug: string;
  label: string;
  media_bucket: string;
  count: number;
}

export interface CatalogStats {
  entries: number;
  titles: number;
  hooked: number;
  sites: number;
}

export interface IndexStats {
  sites_total: number;
  sites_active: number;
  sites_paused: number;
  sites_done: number;
  sites_failed: number;
  pages_total: number;
  pages_fetched: number;
  pages_pending: number;
  pages_failed: number;
  titles_found: number;
  requests_made: number;
  words_indexed: number;
  time_spent_seconds: number;
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

export interface User {
  id: number;
  username: string;
  display_name: string | null;
  role: "admin" | "user";
  is_active: boolean;
  created_at: string;
}

export interface Me {
  authenticated: boolean;
  needs_setup: boolean;
  user: User | null;
}

const BASE = "/api";

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(BASE + path, {
    headers: init?.body && !(init.body instanceof FormData)
      ? { "Content-Type": "application/json" }
      : undefined,
    credentials: "include", // send the session cookie
    ...init,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = await res.json();
      detail = j.detail ?? detail;
    } catch {
      /* ignore */
    }
    throw new ApiError(detail, res.status);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  health: () => req<{ status: string }>("/health"),

  listWorks: (q?: string, opts?: { shelfId?: number }) => {
    const p = new URLSearchParams();
    if (q && q.trim()) p.set("q", q.trim());
    if (opts?.shelfId != null) p.set("shelf_id", String(opts.shelfId));
    const qs = p.toString();
    return req<Work[]>(`/works${qs ? `?${qs}` : ""}`);
  },
  getWork: (id: number) => req<WorkDetail>(`/works/${id}`),
  deleteWork: (id: number) => req<{ deleted: number }>(`/works/${id}`, { method: "DELETE" }),

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

  // Same-origin URL for the bulk ZIP download (GET so it can be hit by a plain <a download>).
  bulkDownloadUrl: (payload: { work_ids?: number[]; shelf_id?: number }) => {
    const p = new URLSearchParams();
    if (payload.work_ids?.length) p.set("ids", payload.work_ids.join(","));
    if (payload.shelf_id != null) p.set("shelf_id", String(payload.shelf_id));
    return `${BASE}/library/download?${p.toString()}`;
  },

  // Bulk download selected works / a shelf as a ZIP of EPUBs. Triggered via a real <a download>
  // click within the user gesture — a fetch()+programmatic blob click is silently dropped by
  // iOS Safari (gesture lost across the await) and races URL.revokeObjectURL on desktop.
  downloadLibrary: async (payload: { work_ids?: number[]; shelf_id?: number }) => {
    const a = document.createElement("a");
    a.href = api.bulkDownloadUrl(payload);
    a.download = "shelf-library.zip";
    a.rel = "noopener";
    document.body.appendChild(a);
    a.click();
    a.remove();
  },

  listSources: () => req<Source[]>("/sources"),
  listAdapters: () => req<AdapterInfo[]>("/adapters"),
  updateSource: (id: number, patch: Partial<Source>) =>
    req<Source>(`/sources/${id}`, { method: "PATCH", body: JSON.stringify(patch) }),

  listJobs: () => req<Job[]>("/jobs"),
  reapJobs: () => req<{ revived: number }>("/jobs/reap", { method: "POST" }),
  retryJob: (id: number) => req<Job>(`/jobs/${id}/retry`, { method: "POST" }),
  deleteJob: (id: number) => req<{ deleted: number }>(`/jobs/${id}`, { method: "DELETE" }),
  pauseJob: (id: number) => req<Job>(`/jobs/${id}/pause`, { method: "POST" }),
  resumeJob: (id: number) => req<Job>(`/jobs/${id}/resume`, { method: "POST" }),

  hook: (sourceKey: string, workRef: string, policy?: Partial<CrawlPolicy>) =>
    req<Work>("/works/hook", {
      method: "POST",
      body: JSON.stringify({ source_key: sourceKey, work_ref: workRef, ...(policy ?? {}) }),
    }),
  setCrawlPolicy: (workId: number, policy: Partial<CrawlPolicy>) =>
    req<Work>(`/works/${workId}/crawl-policy`, {
      method: "PATCH",
      body: JSON.stringify(policy),
    }),
  unhook: (workId: number) => req<Work>(`/works/${workId}/unhook`, { method: "POST" }),
  importFile: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return req<Work>("/works/import", { method: "POST", body: fd });
  },

  getSettings: () => req<AppSettings>("/settings"),
  saveSettings: (patch: Partial<AppSettings>) =>
    req<AppSettings>("/settings", { method: "PUT", body: JSON.stringify(patch) }),

  // --- Per-user Goodreads (each user connects their own want-to-read shelf) ---
  getMyGoodreads: () => req<GoodreadsConnection>("/me/goodreads"),
  connectGoodreads: (body: { goodreads_user_id: string; shelf?: string; enabled?: boolean }) =>
    req<GoodreadsConnection>("/me/goodreads", { method: "PUT", body: JSON.stringify(body) }),
  syncGoodreads: () => req<GoodreadsConnection>("/me/goodreads/sync", { method: "POST" }),
  disconnectGoodreads: () =>
    req<{ disconnected: boolean }>("/me/goodreads", { method: "DELETE" }),

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
  sendToKindle: (
    workId: number,
    body: { to?: string; kindle_email?: string; start?: number; limit?: number }
  ) =>
    req<{ sent: boolean; chapters: number; to: string }>(`/works/${workId}/send-to-kindle`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

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

  // --- URL index ---
  listIndexSites: () => req<IndexSite[]>("/index/sites"),
  indexStats: () => req<IndexStats>("/index/stats"),
  addIndexSite: (body: {
    url: string;
    max_pages?: number;
    max_depth?: number;
    same_host_only?: boolean;
    update_indexed?: boolean; // re-fetch already-indexed pages on re-add (default: resume only)
  }) => req<IndexSite>("/index/sites", { method: "POST", body: JSON.stringify(body) }),
  updateIndexSite: (
    id: number,
    body: { stop_after_idle_pages?: number; max_pages?: number; max_depth?: number }
  ) => req<IndexSite>(`/index/sites/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  getIndexConfig: () => req<IndexConfig>("/index/config"),
  putIndexConfig: (stop_after_idle_pages: number) =>
    req<IndexConfig>("/index/config", {
      method: "PUT",
      body: JSON.stringify({ stop_after_idle_pages }),
    }),
  pauseIndexSite: (id: number) =>
    req<IndexSite>(`/index/sites/${id}/pause`, { method: "POST" }),
  resumeIndexSite: (id: number) =>
    req<IndexSite>(`/index/sites/${id}/resume`, { method: "POST" }),
  // Soft-remove by default (stops crawling, keeps indexed content). Pass { purge: true } to
  // permanently delete the indexed pages + catalog entries too.
  deleteIndexSite: (id: number, opts?: { purge?: boolean }) =>
    req<{ removed?: number; deleted?: number; purged: boolean }>(
      `/index/sites/${id}${opts?.purge ? "?purge=true" : ""}`,
      { method: "DELETE" }
    ),
  listIndexPages: (siteId?: number, status?: string, limit = 50, offset = 0) => {
    const q = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    if (siteId != null) q.set("site_id", String(siteId));
    if (status) q.set("status", status);
    return req<IndexedPage[]>(`/index/pages?${q.toString()}`);
  },
  getIndexPage: (id: number) => req<IndexedPage>(`/index/pages/${id}`),
  searchIndex: (q: string, siteId?: number, limit = 30) => {
    const p = new URLSearchParams({ q, limit: String(limit) });
    if (siteId != null) p.set("site_id", String(siteId));
    return req<IndexSearchResult[]>(`/index/search?${p.toString()}`);
  },
  hookIndexPage: (id: number) =>
    req<Work>(`/index/pages/${id}/hook`, { method: "POST" }),
  hookIndexSite: (id: number) =>
    req<Work>(`/index/sites/${id}/hook`, { method: "POST" }),

  // --- Discovered-works catalog ---
  listCatalog: (
    q?: string,
    opts?: {
      siteId?: number; hooked?: boolean; limit?: number; offset?: number; live?: boolean;
      media?: string; domain?: string; sort?: string;
    }
  ) => {
    const p = new URLSearchParams();
    if (q && q.trim()) p.set("q", q.trim());
    if (opts?.siteId != null) p.set("site_id", String(opts.siteId));
    if (opts?.hooked != null) p.set("hooked", String(opts.hooked));
    if (opts?.limit != null) p.set("limit", String(opts.limit));
    if (opts?.offset != null) p.set("offset", String(opts.offset));
    if (opts?.media) p.set("media", opts.media);
    if (opts?.domain) p.set("domain", opts.domain);
    if (opts?.sort) p.set("sort", opts.sort);
    if (opts?.live) p.set("live", "true");
    const qs = p.toString();
    return req<CatalogGroup[]>(`/catalog${qs ? `?${qs}` : ""}`);
  },
  catalogFacets: () => req<{ media: string[]; domains: string[] }>("/catalog/facets"),
  catalogStats: () => req<CatalogStats>("/catalog/stats"),
  catalogRows: (media?: string) =>
    req<CatalogRow[]>(`/catalog/rows${media ? `?media=${encodeURIComponent(media)}` : ""}`),
  catalogCategories: (media?: string) =>
    req<{ categories: CatalogCategory[] }>(
      `/catalog/categories${media ? `?media=${encodeURIComponent(media)}` : ""}`
    ),
  catalogBrowse: (
    opts: { dimension: string; value?: string; media?: string; sort?: string; limit?: number; offset?: number }
  ) => {
    const p = new URLSearchParams();
    p.set("dimension", opts.dimension);
    if (opts.value) p.set("value", opts.value);
    if (opts.media) p.set("media", opts.media);
    if (opts.sort) p.set("sort", opts.sort);
    if (opts.limit != null) p.set("limit", String(opts.limit));
    if (opts.offset != null) p.set("offset", String(opts.offset));
    return req<CatalogGroup[]>(`/catalog/browse?${p.toString()}`);
  },
  hookCatalog: (catalogId: number, startChapter?: number) =>
    req<Work>(
      `/catalog/${catalogId}/hook` +
        (startChapter && startChapter > 1 ? `?start_chapter=${startChapter}` : ""),
      { method: "POST" }
    ),
  grabCatalog: (catalogId: number) =>
    req<{ ok: boolean; integration: string | null; message: string }>(
      `/catalog/${catalogId}/grab`,
      { method: "POST" }
    ),
  removeCatalog: (catalogId: number, opts?: { block?: boolean; blockDomain?: boolean }) => {
    const p = new URLSearchParams();
    if (opts?.block === false) p.set("block", "false");
    if (opts?.blockDomain) p.set("block_domain", "true");
    const qs = p.toString();
    return req<{ deleted: number; blocked: { scope: string; value: string } | null }>(
      `/catalog/${catalogId}${qs ? `?${qs}` : ""}`,
      { method: "DELETE" }
    );
  },
  purgeBroken: (block = true) =>
    req<{ removed: number }>(
      `/catalog/purge-broken${block ? "" : "?block=false"}`,
      { method: "POST" }
    ),

  // --- Operator blocklist (barred URLs/domains) ---
  listBlocks: () => req<IndexBlock[]>("/index/blocks"),
  addBlock: (body: { scope: "url" | "domain"; value: string; reason?: string }) =>
    req<IndexBlock>("/index/blocks", { method: "POST", body: JSON.stringify(body) }),
  deleteBlock: (id: number) =>
    req<{ deleted: number }>(`/index/blocks/${id}`, { method: "DELETE" }),

  // --- Crawl speed (live-editable) ---
  getCrawlTuning: () => req<CrawlTuning>("/index/crawl-tuning"),
  putCrawlTuning: (body: Partial<CrawlTuning>) =>
    req<CrawlTuning>("/index/crawl-tuning", { method: "PUT", body: JSON.stringify(body) }),

  // --- Work completeness diagnostics ---
  diagnoseWork: (workId: number) => req<WorkHealth>(`/works/${workId}/diagnose`),
  repairWork: (workId: number) =>
    req<WorkHealth>(`/works/${workId}/repair`, { method: "POST" }),

  // --- Update tracker: re-check hooked titles for new content ---
  checkWorkUpdates: (workId: number) =>
    req<WorkUpdate>(`/works/${workId}/check-updates`, { method: "POST" }),
  checkAllUpdates: () =>
    req<CheckAllUpdates>("/works/check-updates", { method: "POST" }),

  // --- Integrations (Readarr / Kapowarr) ---
  listIntegrations: () => req<Integration[]>("/integrations"),
  addIntegration: (body: {
    kind: IntegrationKind;
    base_url?: string;
    api_key?: string;
    name?: string;
    root_folder?: string;
    auto_map_folders?: boolean;
    config?: Record<string, string>;
  }) => req<Integration>("/integrations", { method: "POST", body: JSON.stringify(body) }),
  updateIntegration: (
    id: number,
    body: Partial<{
      name: string;
      base_url: string;
      api_key: string;
      enabled: boolean;
      root_folder: string;
      auto_map_folders: boolean;
      config: Record<string, string>;
    }>
  ) => req<Integration>(`/integrations/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteIntegration: (id: number) =>
    req<{ deleted: number }>(`/integrations/${id}`, { method: "DELETE" }),
  testIntegration: (id: number) =>
    req<IntegrationTest>(`/integrations/${id}/test`, { method: "POST" }),
  syncIntegration: (id: number) =>
    req<Record<string, unknown>>(`/integrations/${id}/sync`, { method: "POST" }),

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

  // --- Auth / users ---
  me: () => req<Me>("/auth/me"),
  login: (username: string, password: string) =>
    req<User>("/auth/login", { method: "POST", body: JSON.stringify({ username, password }) }),
  logout: () => req<{ ok: boolean }>("/auth/logout", { method: "POST" }),
  setupAdmin: (username: string, password: string, displayName?: string) =>
    req<User>("/auth/setup", {
      method: "POST",
      body: JSON.stringify({ username, password, display_name: displayName }),
    }),
  listUsers: () => req<User[]>("/users"),
  createUser: (body: { username: string; password: string; role: string; display_name?: string }) =>
    req<User>("/users", { method: "POST", body: JSON.stringify(body) }),
  updateUser: (
    id: number,
    body: { password?: string; role?: string; is_active?: boolean; display_name?: string }
  ) => req<User>(`/users/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteUser: (id: number) => req<{ deleted: number }>(`/users/${id}`, { method: "DELETE" }),
};
