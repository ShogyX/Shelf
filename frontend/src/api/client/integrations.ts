// Integrations domain: connectable providers (metadata sources, library managers, the acquisition
// pipeline, security scanners) and their stored config. See IntegrationConfig below for the typed
// per-kind config shape (FE-L2).
import { req } from "./http";

export type IntegrationKind =
  | "readarr"
  | "kapowarr"
  | "prowlarr"
  | "sabnzbd"
  | "qbittorrent"
  | "libgen"
  | "virustotal"
  | "ranobedb"
  | "goodreads"
  | "googlebooks"
  | "hardcover"
  | "anilist"
  | "novelupdates";

export type IntegrationCategory = "metadata" | "manager" | "pipeline" | "security";

// A remote→local path translation (used by the SABnzbd / qBittorrent pipelines when the download
// client runs on a different host than Shelf).
export interface IntegrationPathMapping {
  remote: string;
  local: string;
}

// Per-kind integration config (FE-L2). Replaces the former `Record<string, any>`. Every provider's
// config is stored as one JSON bag, so this is modelled as a single permissive interface whose
// fields are all optional — a stored config from the API carries no `kind` discriminant of its own,
// and IntegrationsManager's blankForm()/buildBody() read and write these keys as a flat bag across
// kinds. The per-kind shapes below (IntegrationConfigByKind) document which keys each kind actually
// uses; the union of them all is IntegrationConfig.
//
// Shared limit/timeout overrides (any kind may carry these):
export interface IntegrationLimits {
  requests_per_minute?: number; // per-integration override of the catalog default rpm
  timeout?: number;             // per-request timeout (seconds)
}

// Goodreads (per-user metadata): which shelf to mirror.
export interface GoodreadsConfig extends IntegrationLimits {
  user_id?: string;
  shelf?: string;
}

// NovelUpdates (metadata): optional Cloudflare clearance cookie + the matching User-Agent.
export interface NovelUpdatesConfig extends IntegrationLimits {
  cf_clearance?: string;
  user_agent?: string;
}

// Prowlarr (usenet/torrent search): the release-filtering preferences.
export interface ProwlarrConfig extends IntegrationLimits {
  protocols?: string[];
  categories?: number[];
  preferred_formats?: string[];
  languages?: string[];
  min_size_mb?: number | null;
  max_size_mb?: number | null;
  exclude_terms?: string[];
  required_terms?: string[];
  ignored_terms?: string[];
  preferred_terms?: string[];
  indexer_ids?: number[];
  comic_categories?: number[];
  comic_formats?: string[];
  auto_grab_min_confidence?: number;
}

// SABnzbd (usenet download client): staging category, library path, daily cap, path mappings.
export interface SabnzbdConfig extends IntegrationLimits {
  category?: string;
  library_path?: string | null;
  max_grabs_per_day?: number;
  path_mappings?: IntegrationPathMapping[];
}

// Anna's Archive / LibGen (direct-download pipeline, kind="libgen").
export interface LibgenConfig extends IntegrationLimits {
  providers?: string[];
  formats?: string[];
  min_interval_s?: number;
  max_per_day?: number;
  max_concurrent?: number;
  download_dir?: string | null;
  annas_key?: string;       // write-only secret
  annas_key_set?: boolean;  // read-only: is a secret stored
}

// qBittorrent (torrent download client).
export interface QbittorrentConfig extends IntegrationLimits {
  username?: string;
  category?: string;
  save_path?: string | null;
  library_path?: string | null;
  keep_after_import?: boolean;
  path_mappings?: IntegrationPathMapping[];
}

// VirusTotal (security): hold files VirusTotal has never seen.
export interface VirusTotalConfig extends IntegrationLimits {
  vt_block_unknown?: boolean;
}

// Per-kind config shapes, keyed by integration kind. Kinds that carry no structured config beyond
// the shared limits (readarr, kapowarr, ranobedb, googlebooks, hardcover, anilist) use the base.
export interface IntegrationConfigByKind {
  readarr: IntegrationLimits;
  kapowarr: IntegrationLimits;
  prowlarr: ProwlarrConfig;
  sabnzbd: SabnzbdConfig;
  qbittorrent: QbittorrentConfig;
  libgen: LibgenConfig;
  virustotal: VirusTotalConfig;
  ranobedb: IntegrationLimits;
  goodreads: GoodreadsConfig;
  googlebooks: IntegrationLimits;
  hardcover: IntegrationLimits;
  anilist: IntegrationLimits;
  novelupdates: NovelUpdatesConfig;
}

// The stored config bag. The API returns config without a `kind` discriminant, and the form reads
// keys across kinds, so the public shape is the permissive union of every kind's fields (each
// optional). This removes the former `Record<string, any>` escape hatch while letting blankForm /
// buildBody read and write any provider's keys.
export type IntegrationConfig = GoodreadsConfig &
  NovelUpdatesConfig &
  ProwlarrConfig &
  SabnzbdConfig &
  LibgenConfig &
  QbittorrentConfig &
  VirusTotalConfig;

export interface Integration {
  id: number;
  kind: IntegrationKind;
  name: string;
  base_url: string;
  enabled: boolean;
  root_folder: string | null;
  auto_map_folders: boolean;
  config: IntegrationConfig | null;
  category: IntegrationCategory;
  is_metadata: boolean;
  is_pipeline: boolean;
  has_api_key: boolean;
  requests_per_minute: number;   // effective request cap (override or catalog default)
  timeout: number;               // effective per-request timeout (seconds)
  last_sync_at: string | null;
  last_error: string | null;
  catalog_count: number;
}

// Static descriptor of a connectable integration (from GET /integrations/catalog) — drives the
// provider boxes: what each is, what it provides, how matching works, and its default limits.
export interface ProviderCatalogEntry {
  kind: IntegrationKind;
  category: IntegrationCategory;
  label: string;
  tagline: string;
  provides: string[];
  use: string;
  requests: string;
  matching: string;
  auth: "none" | "optional_key" | "key" | "token" | "cookie";
  per_user: boolean;
  default_rpm: number;
  default_timeout: number;
}

export interface IntegrationTest {
  ok: boolean;
  app: string | null;
  version: string | null;
  detail: string | null;
  root_folders: string[];
  error: string | null;
}

export const integrationsApi = {
  // --- Integrations (Readarr / Kapowarr / Prowlarr / SABnzbd / metadata) ---
  listIntegrations: () => req<Integration[]>("/integrations"),
  getIntegrationCatalog: () => req<ProviderCatalogEntry[]>("/integrations/catalog"),
  addIntegration: (body: {
    kind: IntegrationKind;
    base_url?: string;
    api_key?: string;
    name?: string;
    root_folder?: string;
    auto_map_folders?: boolean;
    config?: IntegrationConfig;
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
      config: IntegrationConfig;
    }>
  ) => req<Integration>(`/integrations/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteIntegration: (id: number) =>
    req<{ deleted: number }>(`/integrations/${id}`, { method: "DELETE" }),
  testIntegration: (id: number) =>
    req<IntegrationTest>(`/integrations/${id}/test`, { method: "POST" }),
  syncIntegration: (id: number) =>
    req<Record<string, unknown>>(`/integrations/${id}/sync`, { method: "POST" }),
};
