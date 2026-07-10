// System & settings domain: health, request statistics, storage/system config, app + reader
// settings, the global SMTP server, the Index layout default, and instance backups/restore.
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
  fabSide: "left" | "right" | "top" | "bottom"; // legacy docked edge (unused)
  fabPos: number; // legacy edge position (unused)
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
  // --- Audiobook listening (media_kind === "audio") ---
  audioSpeed: number;          // default playback rate; the player persists changes back here
  audioSkipBack: number;       // seconds the ⏪ button / lock-screen rewind jumps
  audioSkipForward: number;    // seconds the ⏩ button / lock-screen advance jumps
  audioAutoplayNext: boolean;  // auto-advance to the next track/chapter when one finishes
}

// The shape stored for both the global default and (mirrored in the four ReaderPrefs fields) a
// user's personal layout. Categories are media-section names; lanes are "<category>|<kind>|<slug>".
// Cloudflare Access integration (admin). The stored API token is never returned — only api_token_set.
export interface CloudflareAccess {
  account_id: string;
  app_id: string;
  policy_id: string;
  enabled: boolean;
  api_token_set: boolean;
}
export interface CloudflareAccessIn {
  account_id: string;
  app_id: string;
  policy_id: string;
  api_token: string;   // write-only; blank preserves the stored token
  enabled: boolean;
}

export interface IndexLayout {
  categoryOrder: string[];
  hiddenCategories: string[];
  laneOrder: string[];
  hiddenLanes: string[];
}

export interface FeaturedConfig {
  method: "popular" | "random" | "newest";
  categories: string[]; // genre/theme labels to draw from (empty = all)
  media: string[]; // media labels: Book / Novel / Manga / Comic (empty = all)
  rotateHours: number; // 0 = pick fresh each visit; else stable per N-hour window
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
    by_route: { route: string; imported: number; failed: number; active: number; hit_rate: number | null }[];
    totals: { imported: number; failed: number; active: number };
  };
  web_fetch: { hooked: number };
  requests: { resolved: number; unavailable: number; open: number; searching: number };
  failure_reasons: { reason: string; count: number; label: string }[];
  sources?: {
    by_source: { source: string; searched: number; queued: number; in_flight: number }[];
    due_now: number;
  };
  following?: { authors: number; series: number; auto_added: number };
}

// --- Insights time-series (redesign) ---
export interface AcqDay { date: string; imported: number; failed: number; acquire_s: number | null }
export interface AcquisitionsStats { days: AcqDay[] }
export interface GrowthDay { date: string; added: number; total: number }
export interface LibraryGrowth { days: GrowthDay[]; total: number }
export interface StatsOverview {
  downloaded_30d: number;
  success_rate: number | null;
  avg_acquire_s: number | null;
  titles_in_library: number;
  spark: { downloaded: number[]; success: number[]; acquire_s: number[]; titles: number[] };
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

export interface FlaggedWork {
  id: number;
  title: string;
  author: string | null;
  media_kind: string;
  health: string; // missing | corrupt | mismatch
  detail: string | null;
  checked_at: string | null;
}

export interface LibraryHealth {
  total: number;
  scanned: number;
  ok: number;
  missing: number;
  corrupt: number;
  mismatch: number; // file content doesn't match the recorded title (wrong-match watcher)
  flagged: FlaggedWork[];
}

export interface SystemConfig {
  values: Record<string, string | number | boolean>;
  overridden: string[];
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
  // --- Insights time-series (redesign) ---
  statsAcquisitions: (days = 14) => req<AcquisitionsStats>(`/stats/acquisitions?days=${days}`),
  statsLibraryGrowth: (days = 90) => req<LibraryGrowth>(`/stats/library-growth?days=${days}`),
  statsOverview: () => req<StatsOverview>("/stats/overview"),
  statsVtUsage: () => req<Record<string, unknown>>("/stats/vt-usage"),

  getStorage: () => req<StorageState>("/settings/storage"),
  putStorage: (patch: Partial<StoragePatch>) =>
    req<StorageState>("/settings/storage", { method: "PUT", body: JSON.stringify(patch) }),

  // Background media-integrity scan summary (admin): counts + the flagged (missing/corrupt) titles.
  getLibraryHealth: () => req<LibraryHealth>("/settings/library-health"),

  getSystemConfig: () => req<SystemConfig>("/settings/system"),
  putSystemConfig: (patch: Record<string, unknown>) =>
    req<SystemConfig>("/settings/system", { method: "PUT", body: JSON.stringify(patch) }),

  // Content languages Shelf grabs + stocks (visibility for all; admin-set). enabled = canonical codes.
  getContentLanguages: () =>
    req<{ supported: { code: string; name: string }[]; enabled: string[] }>("/settings/content-languages"),
  setContentLanguages: (languages: string[]) =>
    req<{ enabled: string[] }>("/settings/content-languages", {
      method: "PUT",
      body: JSON.stringify({ languages }),
    }),

  // Cloudflare Access (admin): auto-add a new user's email to a Zero Trust Access policy. Token redacted.
  getCloudflareAccess: () =>
    req<CloudflareAccess>("/settings/cloudflare-access"),
  setCloudflareAccess: (patch: Partial<CloudflareAccessIn>) =>
    req<CloudflareAccess>("/settings/cloudflare-access", { method: "PUT", body: JSON.stringify(patch) }),
  testCloudflareAccess: () =>
    req<{ ok: boolean }>("/settings/cloudflare-access/test", { method: "POST" }),

  // Global default Index layout (admin-set; applied to users who haven't customized their own).
  getIndexLayout: () => req<IndexLayout>("/settings/index-layout"),
  putIndexLayout: (layout: IndexLayout) =>
    req<IndexLayout>("/settings/index-layout", { method: "PUT", body: JSON.stringify(layout) }),

  // Discover "Featured this week" selection rules (admin-set; read by all to pick the billboard).
  getFeaturedConfig: () => req<FeaturedConfig>("/settings/featured"),
  putFeaturedConfig: (cfg: FeaturedConfig) =>
    req<FeaturedConfig>("/settings/featured", { method: "PUT", body: JSON.stringify(cfg) }),

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
