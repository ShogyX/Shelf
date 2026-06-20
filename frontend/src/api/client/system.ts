// System & settings domain: health, request statistics, storage/system config, app + reader
// settings, the global SMTP server, the Index layout default, instance backups/restore, and the
// missing-content ledger.
import { req, BASE } from "./http";

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
  // "auto" picks per media format: long strips (webtoon/manhua) → continuous; pages (manga) → single.
  comicMode: "auto" | "continuous" | "single"; // vertical strip vs one page per screen
  comicFit: "auto" | "width" | "height"; // fit each page to the viewport width or height ("auto" by layout)
  comicZoom: number; // zoom multiplier on top of the fit (1 = 100%)
  comicGap: number; // px gap between pages in continuous mode (0 = seamless webtoon)
  // Index page: media categories the user has HIDDEN (empty = show all). Stored as hidden (not
  // enabled) so a newly-added category is visible by default.
  indexHiddenCategories: string[];
  // Index layout edits (all optional; absent = default order, nothing hidden):
  indexCategoryOrder?: string[];   // category names in the user's preferred order
  indexHiddenLanes?: string[];     // hidden genre/popular lanes, keyed "<category>|<kind>|<slug>"
  indexLaneOrder?: string[];       // lane keys ("<category>|<kind>|<slug>") in the user's order
  // When true, the four fields above are this user's PERSONAL layout (overriding the global
  // default). When false/absent, the user follows the admin's global default layout.
  indexLayoutCustom?: boolean;
}

// The shape stored for both the global default and (mirrored in the four ReaderPrefs fields) a
// user's personal layout. Categories are media-section names; lanes are "<category>|<kind>|<slug>".
export interface IndexLayout {
  categoryOrder: string[];
  hiddenCategories: string[];
  laneOrder: string[];
  hiddenLanes: string[];
}

export interface DeliveryConfig {
  // The SMTP server is now global (admin-configured); a user only sets their recipient.
  email_to?: string | null;
}

export interface AppSettings {
  theme: string;
  reader_prefs: ReaderPrefs;
  kindle_email: string | null;
  smtp_configured: boolean;       // is the shared mail server set up (read-only)
  smtp_from: string | null;       // the shared sending address (admin-configured; read-only)
  delivery: DeliveryConfig;
  apprise_url: string | null; // per-user push target (ntfy/Pushover/Telegram/…)
}

export interface GlobalSmtp {
  smtp_host: string | null;
  smtp_port: number;
  smtp_username: string | null;
  smtp_from: string | null;
  smtp_security: string;          // none | starttls | ssl
  smtp_password_set: boolean;     // read-only (password never returned)
  configured: boolean;
}

export interface RequestStats {
  window_hours: number;
  total: number;
  rates: { per_second: number; per_minute: number; per_hour: number; per_day: number; current_hour: number };
  by_category: { category: string; count: number }[];
  by_outcome: { outcome: string; count: number }[];
  by_host: { host: string; count: number }[];
  series: { bucket: string; total: number; by_outcome: Record<string, number>; by_category?: Record<string, number> }[];
  outcomes: string[];
  categories: string[];
}

// Acquisition-pipeline outcomes for the Statistics page.
export interface PipelineStats {
  downloads: {
    by_route: { route: string; imported: number; failed: number; active: number }[];
    totals: { imported: number; failed: number; active: number };
  };
  web_fetch: { hooked: number };
  requests: { resolved: number; unavailable: number; open: number; searching: number };
  failure_reasons: { reason: string; count: number; label: string }[];
}

export interface PathSlot { override: string; effective: string }
export interface PathMapping { remote: string; local: string }
export interface StorageState {
  image_cache_dir: PathSlot;
  covers_dir: PathSlot;
  backups_dir: PathSlot;
  stock_dir: string;
  sab_library_path: string;
  sab_category: string;
  sab_path_mappings: PathMapping[];
  sab_configured: boolean;
  libgen_download_dir: string;
  libgen_configured: boolean;
  audiobook_library_path: string;
  watched_folders: { id: number; path: string; enabled: boolean; name: string }[];
  migrated?: Record<string, number>;
}
export interface StoragePatch {
  media_dir: string; covers_dir: string; backup_dir: string; stock_dir: string;
  sab_library_path: string; sab_category: string; sab_path_mappings: PathMapping[];
  libgen_download_dir: string; audiobook_library_path: string; migrate: boolean;
}

export interface SystemConfig {
  values: Record<string, string | number | boolean>;
  overridden: string[];
}

// A title Shelf couldn't find — the "missing content" ledger. For a non-admin the list is scoped
// to their own requests (requested_at set; requester_count/requesters null); for an admin every row
// carries the requester rollup.
export interface MissingRequest {
  id: number;
  title: string;
  author: string | null;
  status: "open" | "searching" | "unavailable" | "resolved";
  failure_reason:
    | "no_match" | "all_broken" | "rate_limited" | "blocked"
    | "unverified" | "timeout" | "error" | null;
  last_provider: string | null;
  attempts: number;
  first_requested_at: string;
  last_attempt_at: string | null;
  next_check_at: string | null;
  resolved_at: string | null;
  requested_at: string | null;        // when THIS user requested it (non-admin scope)
  requester_count: number | null;     // admin only
  requesters: string[] | null;        // admin only ("system" for an unattributed request)
  origin?: "request" | "goodreads" | "series"; // "goodreads" = waiting to be hooked · "series" = auto-pulled sibling
  origin_detail?: string | null;      // for origin="series": the series it was pulled from
  catalog_work_id?: number | null;    // representative catalog row (opens the series modal)
  series?: string | null;             // series name (from the catalog row; no detect at list time)
  series_position?: number | null;    // volume number within the series, when known
  sources?: MissingSource[];          // per-source search state (empty for legacy rows)
}

export interface MissingSource {
  source: "torrent" | "pipeline" | "libgen";
  status: "pending" | "searching" | "no_match" | "exhausted" | "unavailable" | "matched" | "skipped";
  reason: string | null;
  last_attempt_at: string | null;
  next_retry_at: string | null;
  attempts: number;
}

export interface MissingStats {
  total: number;
  total_unavailable: number;
  by_status: Record<string, number>;
  by_reason: Record<string, number>;
  next_due_at: string | null;
}

// Interactive restore: per-section choice of what to do with a backup's data.
export type RestoreMode = "skip" | "merge" | "replace";

export interface RestoreSection {
  key: string;          // accounts | settings | integrations | sources | library | catalog | acquisition
  label: string;
  description: string;
  in_backup: boolean;   // does the backup carry this section at all?
  backup_rows: number;  // rows in the backup
  target_rows: number;  // rows currently on this instance (what's at stake)
}

export interface RestorePlan {
  name: string;        // the stored backup this plan is for
  manifest: { level: string; created_at: string; schema_version: number };
  target_empty: boolean;
  sections: RestoreSection[];
  media: { key: string; label: string; description: string; in_backup: boolean; backup_files: number };
}

// A backup in the store — created by the app or uploaded from elsewhere.
export interface BackupEntry {
  name: string;
  size_bytes: number;
  created_at: string | null;
  origin: "internal" | "uploaded";
  status: "ready" | "building" | "failed";
  error?: string | null;
  valid: boolean;
  level: string | null;
  schema_version: number;
  media_files: number;
  restorable: boolean;  // false if the backup's schema is newer than this app supports
}

// A whole-DB snapshot — a raw shelf.db file copy (pre-op safety + recovery). Restoring swaps the
// file in wholesale (replaces ALL data), distinct from the logical per-section restore of a zip.
export interface DbSnapshot {
  name: string;
  size_bytes: number;
  created_at: string | null;
  kind: "db_snapshot";
  restorable: boolean;  // false if the file doesn't have a SQLite header
}

export const systemApi = {
  health: () => req<{ status: string }>("/health"),

  getRequestStats: (hours = 48) => req<RequestStats>(`/index/request-stats?hours=${hours}`),
  getPipelineStats: () => req<PipelineStats>("/stats/pipeline"),

  getStorage: () => req<StorageState>("/settings/storage"),
  putStorage: (patch: Partial<StoragePatch>) =>
    req<StorageState>("/settings/storage", { method: "PUT", body: JSON.stringify(patch) }),

  getSystemConfig: () => req<SystemConfig>("/settings/system"),
  putSystemConfig: (patch: Record<string, unknown>) =>
    req<SystemConfig>("/settings/system", { method: "PUT", body: JSON.stringify(patch) }),

  // --- Missing-content ledger (titles we couldn't find) ---
  listMissing: (params?: { status?: string; reason?: string; sort?: string }) => {
    const p = new URLSearchParams();
    if (params?.status) p.set("status", params.status);
    if (params?.reason) p.set("reason", params.reason);
    if (params?.sort) p.set("sort", params.sort);
    const qs = p.toString();
    return req<MissingRequest[]>(`/missing${qs ? `?${qs}` : ""}`);
  },
  missingStats: () => req<MissingStats>("/missing/stats"),
  recheckMissing: (id: number) =>
    req<MissingRequest>(`/missing/${id}/recheck`, { method: "POST" }),

  // Global default Index layout (admin-set; applied to users who haven't customized their own).
  getIndexLayout: () => req<IndexLayout>("/settings/index-layout"),
  putIndexLayout: (layout: IndexLayout) =>
    req<IndexLayout>("/settings/index-layout", { method: "PUT", body: JSON.stringify(layout) }),

  getSettings: () => req<AppSettings>("/settings"),
  saveSettings: (patch: Partial<AppSettings>) =>
    req<AppSettings>("/settings", { method: "PUT", body: JSON.stringify(patch) }),
  // Admin: the shared (global) SMTP server everyone sends through.
  getGlobalSmtp: () => req<GlobalSmtp>("/settings/smtp"),
  setGlobalSmtp: (body: {
    smtp_host?: string; smtp_port?: number; smtp_username?: string; smtp_from?: string;
    smtp_security?: string; smtp_password?: string;
  }) => req<GlobalSmtp>("/settings/smtp", { method: "PUT", body: JSON.stringify(body) }),

  // Admin instance backup (zip). The browser downloads it directly (cookie-authed) so even a
  // multi-GB "full" archive streams to disk instead of through a JS blob.
  backupUrl: (level: "settings" | "data" | "full") =>
    `${BASE}/admin/backup?level=${level}`,
  // ---- Backups store: backups are selectable objects (app-created OR uploaded) ----
  listBackups: () =>
    req<{ backups: BackupEntry[]; db_snapshots: DbSnapshot[]; free_bytes: number; schema_version: number }>(
      "/admin/backups"),
  // Whole-DB snapshot restore: stages the file + restarts the service (which swaps it in at boot).
  restoreDbSnapshot: (name: string) =>
    req<{ restoring: string; status: string }>(
      `/admin/backups/db-snapshots/${encodeURIComponent(name)}/restore`, { method: "POST" }),
  deleteDbSnapshot: (name: string) =>
    req<{ deleted: string }>(
      `/admin/backups/db-snapshots/${encodeURIComponent(name)}`, { method: "DELETE" }),
  createBackup: (level: "settings" | "data" | "full") =>
    req<{ name: string; status: string; level: string }>(`/admin/backups?level=${level}`,
      { method: "POST" }),
  uploadBackup: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return req<BackupEntry>("/admin/backups/upload", { method: "POST", body: fd });
  },
  deleteBackup: (name: string) =>
    req<{ deleted: string }>(`/admin/backups/${encodeURIComponent(name)}`, { method: "DELETE" }),
  storedBackupUrl: (name: string) =>
    `${BASE}/admin/backups/${encodeURIComponent(name)}/download`,
  // Restore plan for a STORED backup (per-section counts), then commit by name.
  backupPlan: (name: string) =>
    req<RestorePlan>(`/admin/backups/${encodeURIComponent(name)}/plan`),
  commitRestore: (name: string, sections: Record<string, RestoreMode>) =>
    req<{ restored: boolean; level: string; loaded: Record<string, number>; warnings: string[] }>(
      "/admin/restore/commit",
      { method: "POST", body: JSON.stringify({ name, sections }) },
    ),
};
