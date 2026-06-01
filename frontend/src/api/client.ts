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
  hooked: boolean;
  total_chapters_known: number;
  total_chapters_expected: number | null;
  chapters_fetched: number;
  health: string; // unknown | ok | incomplete | no_chapters | unreachable
  health_detail: string | null;
  last_checked_at: string | null;
  last_update_at: string | null;
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
  index: number;
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
  status: string; // active | paused | done | failed
  max_pages: number;
  max_depth: number;
  same_host_only: boolean;
  last_error: string | null;
  pages_total: number;
  pages_fetched: number;
  pages_pending: number;
  pages_failed: number;
  words: number;
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
  site_id: number;
  domain: string;
  work_url: string;
  chapters_advertised: number | null;
  chapters_listed: number | null;
  health: string;
  health_detail: string | null;
  hooked_work_id: number | null;
}

export interface CatalogGroup {
  norm_key: string;
  title: string;
  author: string | null;
  cover_url: string | null;
  synopsis: string | null;
  language: string | null;
  media_kind: string;
  chapters: number | null;
  hooked_work_id: number | null;
  sources: CatalogSource[];
}

export interface CatalogStats {
  entries: number;
  titles: number;
  hooked: number;
  sites: number;
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

  listWorks: (q?: string) =>
    req<Work[]>(`/works${q && q.trim() ? `?q=${encodeURIComponent(q.trim())}` : ""}`),
  getWork: (id: number) => req<WorkDetail>(`/works/${id}`),
  deleteWork: (id: number) => req<{ deleted: number }>(`/works/${id}`, { method: "DELETE" }),
  listChapters: (id: number, limit = 500, offset = 0) =>
    req<ChapterList>(`/works/${id}/chapters?limit=${limit}&offset=${offset}`),
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

  listSources: () => req<Source[]>("/sources"),
  listAdapters: () => req<AdapterInfo[]>("/adapters"),
  updateSource: (id: number, patch: Partial<Source>) =>
    req<Source>(`/sources/${id}`, { method: "PATCH", body: JSON.stringify(patch) }),

  listJobs: () => req<Job[]>("/jobs"),
  pauseJob: (id: number) => req<Job>(`/jobs/${id}/pause`, { method: "POST" }),
  resumeJob: (id: number) => req<Job>(`/jobs/${id}/resume`, { method: "POST" }),

  hook: (sourceKey: string, workRef: string) =>
    req<Work>("/works/hook", {
      method: "POST",
      body: JSON.stringify({ source_key: sourceKey, work_ref: workRef }),
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

  exportEpubUrl: (workId: number, start = 1, limit?: number) => {
    const q = new URLSearchParams({ start: String(start) });
    if (limit) q.set("limit", String(limit));
    return `${BASE}/works/${workId}/export.epub?${q.toString()}`;
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
  addIndexSite: (body: {
    url: string;
    max_pages?: number;
    max_depth?: number;
    same_host_only?: boolean;
  }) => req<IndexSite>("/index/sites", { method: "POST", body: JSON.stringify(body) }),
  pauseIndexSite: (id: number) =>
    req<IndexSite>(`/index/sites/${id}/pause`, { method: "POST" }),
  resumeIndexSite: (id: number) =>
    req<IndexSite>(`/index/sites/${id}/resume`, { method: "POST" }),
  deleteIndexSite: (id: number) =>
    req<{ deleted: number }>(`/index/sites/${id}`, { method: "DELETE" }),
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
  listCatalog: (q?: string, opts?: { siteId?: number; hooked?: boolean; limit?: number }) => {
    const p = new URLSearchParams();
    if (q && q.trim()) p.set("q", q.trim());
    if (opts?.siteId != null) p.set("site_id", String(opts.siteId));
    if (opts?.hooked != null) p.set("hooked", String(opts.hooked));
    if (opts?.limit != null) p.set("limit", String(opts.limit));
    const qs = p.toString();
    return req<CatalogGroup[]>(`/catalog${qs ? `?${qs}` : ""}`);
  },
  catalogStats: () => req<CatalogStats>("/catalog/stats"),
  hookCatalog: (catalogId: number) =>
    req<Work>(`/catalog/${catalogId}/hook`, { method: "POST" }),

  // --- Work completeness diagnostics ---
  diagnoseWork: (workId: number) => req<WorkHealth>(`/works/${workId}/diagnose`),
  repairWork: (workId: number) =>
    req<WorkHealth>(`/works/${workId}/repair`, { method: "POST" }),

  // --- Update tracker: re-check hooked titles for new content ---
  checkWorkUpdates: (workId: number) =>
    req<WorkUpdate>(`/works/${workId}/check-updates`, { method: "POST" }),
  checkAllUpdates: () =>
    req<CheckAllUpdates>("/works/check-updates", { method: "POST" }),

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
