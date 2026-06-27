// Catalog & discovery domain: the URL index (crawled sites/pages, blocks, full-text search), the
// discovered-works catalog (browse rows, categories, facets, hook/grab/remove), crawl tuning and
// operator identity, and the hybrid book catalog (Google Books / Open Library) sync.
import { req } from "./http";

export interface IndexSite {
  id: number;
  root_url: string;
  domain: string;
  title: string | null;
  status: string; // active | paused | done | failed | removed
  max_pages: number; // 0 = unlimited
  max_depth: number;
  same_host_only: boolean;
  // Restrict this source to certain media kinds (null/[] = all). A subset of ["text","comic"];
  // excludes the source from searches of other media types.
  allowed_media_kinds: string[] | null;
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
  refresh_hours: number; // how often hooked titles are checked for new chapter releases
}

export interface OperatorIdentity {
  user_agent: string;
  contact_email: string;
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
  listing_only?: boolean; // metadata listing (Google Books / Open Library / Hardcover) — no hook/grab
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
  media_label: string; // fine per-title badge: Novel | Book | Manga | Manhua | Webtoon | Comic
  media_category: string; // coarse section: Manga & Comics | Novel | Book
  chapters: number | null;
  is_adult: boolean; // 18+ content (shown with an 18+ badge; gated by the per-user opt-in)
  hooked_work_id: number | null;
  in_library: boolean; // the current user added it to THEIR library
  in_stock: boolean; // operator pre-fetched + hooked, but not in the user's library
  series: string | null; // series name when part of a known series (else null)
  series_count?: number; // >1 when this browse card represents that many collapsed per-volume cards
  sources: CatalogSource[];
}

export interface CatalogRow {
  kind: string; // popular | genre | theme
  slug: string;
  label: string;
  media_category: string; // Manga & Comics | Novel | Book — the section
  count: number;
  items: CatalogGroup[];
}

// A downloaded audiobook (shared pool) for the Discover "Audiobooks" lane.
export interface AudiobookItem {
  work_id: number;
  title: string;
  author: string | null;
  cover_url: string | null;
}

export interface CatalogCategory {
  kind: string; // genre | theme
  slug: string;
  label: string;
  media_category: string;
  count: number;
}

// The media CATEGORIES the Index organizes sections / filters / per-user-toggles / permissions by,
// in display order. The four comic subtypes collapse into one "Manga & Comics" category; each title
// still shows its fine media_label as a badge.
export const MEDIA_CATEGORIES = ["Manga & Comics", "Novel", "Book"] as const;

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

export interface BookCatalogConfig {
  enabled: boolean;
  hot_set_cap: number;
  closeness_threshold: number;
}

export interface BookCatalogStatus {
  config: BookCatalogConfig;
  book_rows: number;
  phase: string;
  last_full_at: string | null;
}

export const catalogApi = {
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
    body: {
      stop_after_idle_pages?: number; max_pages?: number; max_depth?: number;
      allowed_media_kinds?: string[] | null;
    }
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
  hookIndexPage: (id: number, shelfId?: number) =>
    req<import("./works").Work>(`/index/pages/${id}/hook${shelfId != null ? `?shelf_id=${shelfId}` : ""}`,
      { method: "POST" }),
  hookIndexSite: (id: number, shelfId?: number) =>
    req<import("./works").Work>(`/index/sites/${id}/hook${shelfId != null ? `?shelf_id=${shelfId}` : ""}`,
      { method: "POST" }),

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
  // Downloaded audiobooks (shared pool) for the Discover "Audiobooks" lane.
  catalogAudiobooks: () => req<AudiobookItem[]>("/catalog/audiobooks"),
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
  hookCatalog: (catalogId: number, startChapter?: number, shelfId?: number) => {
    const p = new URLSearchParams();
    if (startChapter && startChapter > 1) p.set("start_chapter", String(startChapter));
    if (shelfId != null) p.set("shelf_id", String(shelfId));
    const qs = p.toString();
    return req<import("./works").Work>(`/catalog/${catalogId}/hook${qs ? `?${qs}` : ""}`, { method: "POST" });
  },
  grabCatalog: (catalogId: number) =>
    req<{ ok: boolean; integration: string | null; message: string }>(
      `/catalog/${catalogId}/grab`,
      { method: "POST" }
    ),
  // Manually re-fetch a comic group's cover from AniList (covers are otherwise sticky). Admin-only.
  refetchGroupCover: (groupId: number) =>
    req<{ id: number; cover_url: string }>(
      `/catalog/groups/${groupId}/refetch-cover`,
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
  getOperatorIdentity: () => req<OperatorIdentity>("/operator/identity"),
  putOperatorIdentity: (body: Partial<OperatorIdentity>) =>
    req<OperatorIdentity>("/operator/identity", { method: "PUT", body: JSON.stringify(body) }),

  // --- Hybrid book catalog (Google Books + Open Library) ---
  getBookCatalogConfig: () => req<BookCatalogStatus>("/catalog/book-config"),
  putBookCatalogConfig: (body: Partial<BookCatalogConfig>) =>
    req<BookCatalogStatus>("/catalog/book-config", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  syncBookCatalog: () =>
    req<Record<string, unknown>>("/catalog/book-sync", { method: "POST" }),
};
