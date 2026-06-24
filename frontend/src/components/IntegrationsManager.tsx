// Settings → Integrations, rebuilt as two tidy cards of provider BOXES:
//   • MetadataProvidersCard — the metadata providers (covers/synopsis/chapter counts/matching)
//   • AcquisitionCard       — download managers + the usenet acquisition pipeline
// Every connectable provider shows as a box: a "＋" to add one that isn't connected, and a connected
// box with an ✎ edit panel, an ✕ to remove, plus test/sync/enable. Each box explains the provider's
// use, requests and matching (from the backend catalog) and exposes its request-limit + timeout.
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  Integration,
  IntegrationCategory,
  IntegrationConfig,
  IntegrationKind,
  IntegrationTest,
  ProviderCatalogEntry,
  SystemConfig,
} from "../api/client";
import { qk } from "../api/queryKeys";
import {
  Badge, Button, Card, CardHeader, FormField, inputCls, Modal, ProviderCard,
  Spinner, StatusChip, type StatusTone, Toggle,
} from "./ui";
import { useConfirm } from "./confirm";

const field = inputCls;

const toList = (s: string): string[] => s.split(/[,\n]/).map((x) => x.trim()).filter(Boolean);
const numOrNull = (s: string): number | null => {
  const n = parseFloat(s);
  return Number.isFinite(n) ? n : null;
};
const AUTH_LABEL: Record<ProviderCatalogEntry["auth"], string> = {
  none: "No credentials",
  optional_key: "API key optional",
  key: "API key required",
  token: "Bearer token required",
  cookie: "Cloudflare cookie (optional)",
};

// ----------------------------------------------------------------- form state
interface FormState {
  name: string;
  baseUrl: string;
  apiKey: string;
  autoMap: boolean;
  userId: string;
  shelf: string;
  cfClearance: string;
  userAgent: string;
  wantEbooks: boolean;
  wantAudiobooks: boolean;
  formats: string;
  languages: string;
  minSize: string;
  maxSize: string;
  excludeTerms: string;
  requiredTerms: string;
  ignoredTerms: string;
  preferredTerms: string;
  indexerIds: string;
  autoGrabMin: string;
  comicCategories: string;
  comicFormats: string;
  sabCategory: string;
  libraryPath: string;
  maxGrabs: string;
  pathFrom: string;
  pathTo: string;
  rpm: string;
  timeout: string;
  // Anna's Archive (kind="libgen") pipeline
  lgMinInterval: string;
  lgMaxDay: string;
  lgMaxConc: string;
  lgFormats: string;
  lgDownloadDir: string;
  lgAnnasKey: string;
  lgAnnasKeySet: boolean;
  // qBittorrent (torrent pipeline)
  qbUsername: string;
  qbCategory: string;
  qbSavePath: string;
  qbKeepAfterImport: boolean;
  // VirusTotal (security)
  vtBlockUnknown: boolean;
  // Storyteller (companion) — username for the token mint; password rides in apiKey.
  stUsername: string;
  stImportPath: string;
  // Companion: pull the app's missing-format ("wanted") items for Shelf to fetch.
  pullWanted: boolean;
}

function blankForm(integ?: Integration): FormState {
  const c = integ?.config ?? {};
  const cats: number[] = Array.isArray(c.categories) ? c.categories : [];
  const map0: { remote?: string; local?: string } =
    Array.isArray(c.path_mappings) ? c.path_mappings[0] ?? {} : {};
  return {
    name: integ?.name ?? "",
    baseUrl: integ?.base_url ?? "",
    apiKey: "",
    autoMap: integ?.auto_map_folders ?? true,
    userId: c.user_id ?? "",
    shelf: c.shelf ?? "to-read",
    cfClearance: c.cf_clearance ?? "",
    userAgent: c.user_agent ?? "",
    wantEbooks: cats.length ? cats.includes(7000) || cats.includes(7020) : true,
    wantAudiobooks: cats.includes(3030),
    formats: (c.preferred_formats ?? ["epub", "azw3", "mobi", "pdf"]).join(", "),
    languages: (c.languages ?? ["en"]).join(", "),
    minSize: c.min_size_mb != null ? String(c.min_size_mb) : "",
    maxSize: c.max_size_mb != null ? String(c.max_size_mb) : "",
    excludeTerms: (c.exclude_terms ?? []).join(", "),
    requiredTerms: (c.required_terms ?? []).join(", "),
    ignoredTerms: (c.ignored_terms ?? []).join(", "),
    preferredTerms: (c.preferred_terms ?? []).join(", "),
    indexerIds: (c.indexer_ids ?? []).join(", "),
    autoGrabMin: c.auto_grab_min_confidence != null ? String(c.auto_grab_min_confidence) : "",
    comicCategories: (c.comic_categories ?? [7030]).join(", "),
    comicFormats: (c.comic_formats ?? ["cbz", "cbr"]).join(", "),
    sabCategory: c.category ?? "shelf",
    libraryPath: c.library_path ?? "",
    maxGrabs: c.max_grabs_per_day != null ? String(c.max_grabs_per_day) : "",
    pathFrom: map0.remote ?? "",
    pathTo: map0.local ?? "",
    // Show an existing per-integration override; blank means "use the catalog default".
    rpm: c.requests_per_minute != null ? String(c.requests_per_minute) : "",
    timeout: c.timeout != null ? String(c.timeout) : "",
    lgMinInterval: c.min_interval_s != null ? String(c.min_interval_s) : "",
    lgMaxDay: c.max_per_day != null ? String(c.max_per_day) : "",
    lgMaxConc: c.max_concurrent != null ? String(c.max_concurrent) : "",
    lgFormats: (c.formats ?? ["epub", "pdf"]).join(", "),
    lgDownloadDir: c.download_dir ?? "",
    lgAnnasKey: "",
    lgAnnasKeySet: !!c.annas_key_set,
    qbUsername: c.username ?? "",
    qbCategory: c.category ?? "shelf",
    qbSavePath: c.save_path ?? "",
    qbKeepAfterImport: !!c.keep_after_import,
    vtBlockUnknown: !!c.vt_block_unknown,
    stUsername: c.username ?? "",
    stImportPath: c.import_path ?? "",
    pullWanted: !!c.pull_wanted,
  };
}

// form → POST/PATCH body for a given kind. ``passthrough`` carries any existing config keys the
// form doesn't manage (e.g. a manually-set prowlarr indexer_ids) so an edit never silently drops them.
function buildBody(kind: IntegrationKind, f: FormState, passthrough: Partial<IntegrationConfig> = {}) {
  const limits: Record<string, number> = {};
  const rpm = numOrNull(f.rpm);
  const timeout = numOrNull(f.timeout);
  if (rpm != null && rpm > 0) limits.requests_per_minute = rpm;   // 0/blank → keep the catalog default
  if (timeout != null && timeout > 0) limits.timeout = timeout;

  const base: any = { kind, name: f.name.trim() || undefined };
  // The limit inputs fully own these two keys (blanking an input clears the override), so drop them
  // from passthrough; everything else unmanaged (e.g. indexer_ids) survives the edit.
  const { requests_per_minute: _r, timeout: _t, ...rest } = passthrough as Record<string, unknown>;
  // passthrough first (unmanaged keys), then the form's managed keys, then the limit override.
  const withKey = (cfg: Record<string, unknown> = {}) => ({ ...rest, ...cfg, ...limits });

  if (kind === "goodreads")
    return { ...base, config: withKey({ user_id: f.userId.trim(), shelf: f.shelf.trim() || "to-read" }) };
  if (kind === "anilist") return { ...base, config: withKey() };
  if (kind === "ranobedb") return { ...base, base_url: f.baseUrl.trim(), config: withKey() };
  if (kind === "googlebooks" || kind === "hardcover")
    return { ...base, api_key: f.apiKey.trim(), config: withKey() };
  if (kind === "novelupdates")
    return {
      ...base,
      config: withKey(
        f.cfClearance.trim() ? { cf_clearance: f.cfClearance.trim(), user_agent: f.userAgent.trim() } : {}
      ),
    };
  if (kind === "prowlarr")
    return {
      ...base,
      base_url: f.baseUrl.trim(),
      api_key: f.apiKey.trim(),
      config: withKey({
        protocols: ["usenet"],
        categories: [...(f.wantEbooks ? [7000, 7020] : []), ...(f.wantAudiobooks ? [3030] : [])],
        preferred_formats: toList(f.formats).map((x) => x.toLowerCase()),
        languages: toList(f.languages).map((x) => x.toLowerCase()),
        min_size_mb: numOrNull(f.minSize),
        max_size_mb: numOrNull(f.maxSize),
        exclude_terms: toList(f.excludeTerms),
        required_terms: toList(f.requiredTerms),
        ignored_terms: toList(f.ignoredTerms),
        preferred_terms: toList(f.preferredTerms),
        indexer_ids: toList(f.indexerIds).map(Number).filter(Number.isFinite),
        // Comic/manga search: usenet files comics under category 7030 as CBZ/CBR, distinct from ebooks.
        comic_categories: toList(f.comicCategories).map(Number).filter(Number.isFinite),
        comic_formats: toList(f.comicFormats).map((x) => x.toLowerCase()),
        ...(numOrNull(f.autoGrabMin) != null
          ? { auto_grab_min_confidence: numOrNull(f.autoGrabMin) }
          : {}),
      }),
    };
  if (kind === "sabnzbd")
    return {
      ...base,
      base_url: f.baseUrl.trim(),
      api_key: f.apiKey.trim(),
      config: withKey({
        category: f.sabCategory.trim() || "shelf",
        library_path: f.libraryPath.trim() || null,
        max_grabs_per_day: numOrNull(f.maxGrabs) ?? 2,
        path_mappings:
          f.pathFrom.trim() && f.pathTo.trim()
            ? [{ remote: f.pathFrom.trim(), local: f.pathTo.trim() }]
            : [],
      }),
    };
  if (kind === "libgen")
    return {
      ...base,
      config: withKey({
        providers: ["annas"],
        formats: toList(f.lgFormats).map((x) => x.toLowerCase()),
        ...(numOrNull(f.lgMinInterval) != null ? { min_interval_s: numOrNull(f.lgMinInterval) } : {}),
        ...(numOrNull(f.lgMaxDay) != null ? { max_per_day: numOrNull(f.lgMaxDay) } : {}),
        ...(numOrNull(f.lgMaxConc) != null ? { max_concurrent: numOrNull(f.lgMaxConc) } : {}),
        download_dir: f.lgDownloadDir.trim() || null,
        ...(f.lgAnnasKey.trim() ? { annas_key: f.lgAnnasKey.trim() } : {}),
      }),
    };
  if (kind === "qbittorrent")
    return {
      ...base,
      base_url: f.baseUrl.trim(),
      api_key: f.apiKey.trim(),                 // qBit password (blank = keep current)
      config: withKey({
        username: f.qbUsername.trim(),
        category: f.qbCategory.trim() || "shelf",
        save_path: f.qbSavePath.trim() || null,
        library_path: f.libraryPath.trim() || null,
        keep_after_import: f.qbKeepAfterImport,
        path_mappings:
          f.pathFrom.trim() && f.pathTo.trim()
            ? [{ remote: f.pathFrom.trim(), local: f.pathTo.trim() }]
            : [],
      }),
    };
  if (kind === "virustotal")
    return {
      ...base,
      api_key: f.apiKey.trim(),                 // VirusTotal API key (blank = keep current)
      config: withKey({ vt_block_unknown: f.vtBlockUnknown }),
    };
  if (kind === "audiobookshelf")
    return {
      ...base, base_url: f.baseUrl.trim(), api_key: f.apiKey.trim(),
      config: withKey({ pull_wanted: f.pullWanted }),
    };
  if (kind === "storyteller")
    return {
      ...base,
      base_url: f.baseUrl.trim(),
      api_key: f.apiKey.trim(),                 // Storyteller password (blank = keep current)
      config: withKey({
        username: f.stUsername.trim(), import_path: f.stImportPath.trim() || null,
        pull_wanted: f.pullWanted,
      }),
    };
  // readarr / kapowarr
  return {
    ...base,
    base_url: f.baseUrl.trim(),
    api_key: f.apiKey.trim(),
    auto_map_folders: f.autoMap,
    config: withKey(),
  };
}

function canSubmit(kind: IntegrationKind, f: FormState, editing: boolean): boolean {
  if (kind === "hardcover") return !!f.apiKey.trim();
  if (kind === "goodreads") return !!f.userId.trim();
  if (["ranobedb", "googlebooks", "anilist", "novelupdates"].includes(kind)) return true;
  if (kind === "virustotal") return editing || !!f.apiKey.trim();   // no base URL; key kept on edit
  if (kind === "libgen") return true;                              // config-only; Anna's key optional (free MD5 mirrors work without it)
  if (kind === "storyteller") return !!f.baseUrl.trim() && !!f.stUsername.trim() && (editing || !!f.apiKey.trim());
  if (kind === "qbittorrent") return !!f.baseUrl.trim();            // password optional (whitelisted hosts)
  return !!f.baseUrl.trim() && !!f.apiKey.trim();
}

// --------------------------------------------------------------- kind fields
function KindFields({
  entry,
  f,
  set,
  editing,
  hasKey,
}: {
  entry: ProviderCatalogEntry;
  f: FormState;
  set: <K extends keyof FormState>(k: K, v: FormState[K]) => void;
  editing: boolean;
  hasKey: boolean;
}) {
  const k = entry.kind;
  // Editing an integration whose secret is already stored: blank keeps it (the save drops api_key when
  // blank). Used by the per-kind secret inputs so there's ONE secret field per form, not a duplicate.
  const keyPh = editing && hasKey ? "•••••••• (leave blank to keep current key)" : "API key";
  if (k === "anilist") return null;
  return (
    <div>
      {k === "goodreads" && (
        <>
          <FormField label="Goodreads user">
            <input className={inputCls} value={f.userId} onChange={(e) => set("userId", e.target.value)}
              placeholder="Goodreads numeric user ID (or profile URL)" />
          </FormField>
          <FormField label="Shelf" hint="default: to-read">
            <input className={inputCls} value={f.shelf} onChange={(e) => set("shelf", e.target.value)}
              placeholder="Shelf (default: to-read)" />
          </FormField>
        </>
      )}
      {k === "ranobedb" && (
        <FormField label="API base" hint="optional — defaults to ranobedb.org">
          <input className={inputCls} value={f.baseUrl} onChange={(e) => set("baseUrl", e.target.value)}
            placeholder="API base (optional — defaults to ranobedb.org)" />
        </FormField>
      )}
      {(k === "googlebooks" || k === "hardcover") && (
        <FormField label={k === "hardcover" ? "Bearer token" : "API key"}>
          <input className={inputCls} type="password" value={f.apiKey} onChange={(e) => set("apiKey", e.target.value)}
            placeholder={k === "hardcover" ? "Bearer token (required)" : "API key (optional — raises quota)"} />
        </FormField>
      )}
      {k === "novelupdates" && (
        <>
          <FormField label="Cloudflare cookie" hint="optional">
            <input className={inputCls} value={f.cfClearance} onChange={(e) => set("cfClearance", e.target.value)}
              placeholder="cf_clearance cookie (optional)" />
          </FormField>
          <FormField label="User-Agent" hint="from the same browser">
            <input className={inputCls} value={f.userAgent} onChange={(e) => set("userAgent", e.target.value)}
              placeholder="matching User-Agent (from the same browser)" />
          </FormField>
        </>
      )}
      {(k === "readarr" || k === "kapowarr") && (
        <>
          <FormField label="Base URL">
            <input className={inputCls} value={f.baseUrl} onChange={(e) => set("baseUrl", e.target.value)}
              placeholder={k === "readarr" ? "http://host:8787" : "http://host:5656"} />
          </FormField>
          <FormField label="API key">
            <input className={inputCls} type="password" value={f.apiKey} onChange={(e) => set("apiKey", e.target.value)}
              placeholder={keyPh} />
          </FormField>
          <FormField>
            <Toggle checked={f.autoMap} onChange={(v) => set("autoMap", v)} label="Auto-map download folders" />
          </FormField>
        </>
      )}
      {k === "prowlarr" && (
        <>
          <FormField label="Base URL">
            <input className={inputCls} value={f.baseUrl} onChange={(e) => set("baseUrl", e.target.value)}
              placeholder="http://host:9696" />
          </FormField>
          <FormField label="API key">
            <input className={inputCls} type="password" value={f.apiKey} onChange={(e) => set("apiKey", e.target.value)}
              placeholder={keyPh} />
          </FormField>
          <div className="grid gap-2 rounded-xl border border-[var(--hair,var(--border))] bg-surface-2/40 p-3">
            <div className="text-xs font-semibold uppercase tracking-wide text-muted">Search preferences (content filtering)</div>
            <div className="flex flex-wrap gap-4">
              <Toggle checked={f.wantEbooks} onChange={(v) => set("wantEbooks", v)} label="Ebooks" />
              <Toggle checked={f.wantAudiobooks} onChange={(v) => set("wantAudiobooks", v)} label="Audiobooks" />
            </div>
            <input className={inputCls} value={f.formats} onChange={(e) => set("formats", e.target.value)}
              placeholder="Preferred formats, best first (epub, azw3, mobi, pdf)" />
            <input className={inputCls} value={f.languages} onChange={(e) => set("languages", e.target.value)}
              placeholder="Languages (e.g. en)" />
            <div className="flex gap-2">
              <input className={inputCls} value={f.minSize} inputMode="decimal"
                onChange={(e) => set("minSize", e.target.value)} placeholder="Min MB" />
              <input className={inputCls} value={f.maxSize} inputMode="decimal"
                onChange={(e) => set("maxSize", e.target.value)} placeholder="Max MB" />
            </div>
            <input className={inputCls} value={f.excludeTerms} onChange={(e) => set("excludeTerms", e.target.value)}
              placeholder="Exclude terms (comma separated)" />
            <input className={inputCls} value={f.requiredTerms} onChange={(e) => set("requiredTerms", e.target.value)}
              placeholder="Required terms — release must contain ≥1" />
            <input className={inputCls} value={f.ignoredTerms} onChange={(e) => set("ignoredTerms", e.target.value)}
              placeholder="Ignored terms — reject if present" />
            <input className={inputCls} value={f.preferredTerms} onChange={(e) => set("preferredTerms", e.target.value)}
              placeholder="Preferred terms — rank higher" />
            <input className={inputCls} value={f.indexerIds} onChange={(e) => set("indexerIds", e.target.value)}
              placeholder="Restrict to indexer IDs (comma separated — blank = all)" />
            <input className={inputCls} type="number" min={0} max={1} step={0.05} value={f.autoGrabMin}
              onChange={(e) => set("autoGrabMin", e.target.value)}
              placeholder="Auto-grab min confidence 0–1 (default 0.8)" />
            <div className="mt-1 text-xs font-semibold uppercase tracking-wide text-muted">Comics / manga (CBZ/CBR)</div>
            <input className={inputCls} value={f.comicCategories} onChange={(e) => set("comicCategories", e.target.value)}
              placeholder="Comic categories (Newznab, default 7030)" />
            <input className={inputCls} value={f.comicFormats} onChange={(e) => set("comicFormats", e.target.value)}
              placeholder="Comic formats (default cbz, cbr)" />
          </div>
        </>
      )}
      {k === "sabnzbd" && (
        <>
          <FormField label="Base URL">
            <input className={inputCls} value={f.baseUrl} onChange={(e) => set("baseUrl", e.target.value)}
              placeholder="http://host:8080" />
          </FormField>
          <FormField label="API key">
            <input className={inputCls} type="password" value={f.apiKey} onChange={(e) => set("apiKey", e.target.value)}
              placeholder={keyPh} />
          </FormField>
          <FormField label="Staging category" hint="default: shelf">
            <input className={inputCls} value={f.sabCategory} onChange={(e) => set("sabCategory", e.target.value)}
              placeholder="Staging category (default: shelf)" />
          </FormField>
          <FormField label="Library path">
            <input className={inputCls} value={f.libraryPath} onChange={(e) => set("libraryPath", e.target.value)}
              placeholder="Library path (e.g. /mnt/NAS-Pool/media/Books)" />
          </FormField>
          <FormField label="Max downloads/day per release" hint="default: 2">
            <input className={inputCls} type="number" min={1} value={f.maxGrabs}
              onChange={(e) => set("maxGrabs", e.target.value)} placeholder="Max downloads/day per release (default: 2)" />
          </FormField>
          <div className="grid gap-2 rounded-xl border border-[var(--hair,var(--border))] bg-surface-2/40 p-3">
            <div className="text-xs font-semibold uppercase tracking-wide text-muted">Remote path mapping (only if SABnzbd is on another host)</div>
            <div className="flex gap-2">
              <input className={inputCls} value={f.pathFrom} onChange={(e) => set("pathFrom", e.target.value)}
                placeholder="SABnzbd path" />
              <input className={inputCls} value={f.pathTo} onChange={(e) => set("pathTo", e.target.value)}
                placeholder="Shelf path" />
            </div>
          </div>
        </>
      )}
      {k === "libgen" && (
        <>
          <FormField
            label={
              <span className="inline-flex items-center gap-2">
                Anna's Archive membership key
                <StatusChip tone={f.lgAnnasKeySet ? "success" : "warning"}>{f.lgAnnasKeySet ? "set" : "not set"}</StatusChip>
              </span>
            }
            hint={
              <>
                The <em>apijson</em> secret key from your Anna's Archive membership (FAQ → API). It enables
                the fast-download API — the only route that reliably bypasses the overloaded free mirrors.
                Stored server-side and never returned. Free MD5 downloads via the LibGen mirrors still work
                without it.
              </>
            }
          >
            <input className={inputCls} type="password" autoComplete="off" value={f.lgAnnasKey}
              onChange={(e) => set("lgAnnasKey", e.target.value)}
              placeholder={f.lgAnnasKeySet ? "•••••••• (leave blank to keep current)" : "paste your Anna's Archive secret key"} />
          </FormField>
          <FormField label="Formats" hint="default epub, pdf">
            <input className={inputCls} value={f.lgFormats} onChange={(e) => set("lgFormats", e.target.value)}
              placeholder="Formats (default epub, pdf)" />
          </FormField>
          <FormField label="Download dir" hint="blank = use the SABnzbd library path">
            <input className={inputCls} value={f.lgDownloadDir} onChange={(e) => set("lgDownloadDir", e.target.value)}
              placeholder="Download dir (blank = use the SABnzbd library path)" />
          </FormField>
          <FormField label="Pacing">
            <div className="grid grid-cols-3 gap-2">
              <input className={inputCls} type="number" min={0} step={0.5} value={f.lgMinInterval}
                onChange={(e) => set("lgMinInterval", e.target.value)} placeholder="Min interval s (2)" />
              <input className={inputCls} type="number" min={1} value={f.lgMaxDay}
                onChange={(e) => set("lgMaxDay", e.target.value)} placeholder="Max/day per host (300)" />
              <input className={inputCls} type="number" min={1} value={f.lgMaxConc}
                onChange={(e) => set("lgMaxConc", e.target.value)} placeholder="Concurrency (2)" />
            </div>
          </FormField>
        </>
      )}
      {k === "qbittorrent" && (
        <>
          <FormField label="Base URL">
            <input className={inputCls} value={f.baseUrl} onChange={(e) => set("baseUrl", e.target.value)}
              placeholder="http://host:8090" />
          </FormField>
          <FormField label="Username">
            <input className={inputCls} value={f.qbUsername} onChange={(e) => set("qbUsername", e.target.value)}
              placeholder="Username" />
          </FormField>
          <FormField label="Password">
            <input className={inputCls} type="password" value={f.apiKey} onChange={(e) => set("apiKey", e.target.value)}
              placeholder={editing && hasKey ? "•••••••• (leave blank to keep current)" : "Password"} />
          </FormField>
          <FormField label="Category" hint="default: shelf">
            <input className={inputCls} value={f.qbCategory} onChange={(e) => set("qbCategory", e.target.value)}
              placeholder="Category (default: shelf)" />
          </FormField>
          <FormField
            label="qBittorrent download path"
            hint={
              <>
                Where qBittorrent saves grabs — its OWN path. It must land on storage Shelf can also read; if
                qBittorrent is on another host with a different mount point, set the mapping below so Shelf
                can find the files.
              </>
            }
          >
            <input className={inputCls} value={f.qbSavePath} onChange={(e) => set("qbSavePath", e.target.value)}
              placeholder="qBittorrent download path (its own view, e.g. /media/NAS-Pool/media/Downloads/shelf)" />
          </FormField>
          <FormField label="Library path" hint="Shelf's view (e.g. /mnt/NAS-Pool/media/Books)">
            <input className={inputCls} value={f.libraryPath} onChange={(e) => set("libraryPath", e.target.value)}
              placeholder="Library path — Shelf's view (e.g. /mnt/NAS-Pool/media/Books)" />
          </FormField>
          <div className="grid gap-2 rounded-xl border border-[var(--hair,var(--border))] bg-surface-2/40 p-3">
            <div className="text-xs font-semibold uppercase tracking-wide text-muted">Remote path mapping (qBittorrent path → Shelf path; only if on another host)</div>
            <div className="flex gap-2">
              <input className={inputCls} value={f.pathFrom} onChange={(e) => set("pathFrom", e.target.value)}
                placeholder="qBittorrent path" />
              <input className={inputCls} value={f.pathTo} onChange={(e) => set("pathTo", e.target.value)}
                placeholder="Shelf path" />
            </div>
          </div>
          <FormField>
            <Toggle checked={f.qbKeepAfterImport} onChange={(v) => set("qbKeepAfterImport", v)}
              label="Keep the torrent after import (seed manually; default deletes it)" />
          </FormField>
        </>
      )}
      {k === "audiobookshelf" && (
        <>
          <FormField label="Base URL">
            <input className={inputCls} value={f.baseUrl} onChange={(e) => set("baseUrl", e.target.value)}
              placeholder="http://host:13378" />
          </FormField>
          <FormField
            label="API key"
            hint={
              <>
                API key from Audiobookshelf → Settings → Users → API Keys. Point an ABS library at Shelf's
                stock / audiobook folders so it auto-scans them.
              </>
            }
          >
            <input className={inputCls} type="password" value={f.apiKey} onChange={(e) => set("apiKey", e.target.value)}
              placeholder={keyPh} />
          </FormField>
          <FormField>
            <Toggle checked={f.pullWanted} onChange={(v) => set("pullWanted", v)}
              label="Fetch the missing format of single-format items (wanted)" />
          </FormField>
        </>
      )}
      {k === "storyteller" && (
        <>
          <FormField label="Base URL">
            <input className={inputCls} value={f.baseUrl} onChange={(e) => set("baseUrl", e.target.value)}
              placeholder="http://host:8001" />
          </FormField>
          <FormField label="Username">
            <input className={inputCls} value={f.stUsername} onChange={(e) => set("stUsername", e.target.value)}
              placeholder="Storyteller username" />
          </FormField>
          <FormField label="Password">
            <input className={inputCls} type="password" value={f.apiKey} onChange={(e) => set("apiKey", e.target.value)}
              placeholder={editing && hasKey ? "•••••••• (leave blank to keep current)" : "Password"} />
          </FormField>
          <FormField
            label="Import path"
            hint={
              <>
                Shelf copies EPUB + audiobook into the import path (converting to EPUB on demand) and
                triggers alignment. Copies — Storyteller never edits your originals.
              </>
            }
          >
            <input className={inputCls} value={f.stImportPath} onChange={(e) => set("stImportPath", e.target.value)}
              placeholder="Import path — a shared folder Storyteller reads (e.g. /mnt/NAS-Pool/media/Storyteller)" />
          </FormField>
          <FormField>
            <Toggle checked={f.pullWanted} onChange={(v) => set("pullWanted", v)}
              label="Fetch the missing half of read-alongs that have only one format (wanted)" />
          </FormField>
        </>
      )}
      {k === "virustotal" && (
        <>
          <FormField
            label="VirusTotal API key"
            hint={
              <>
                Every torrent-grabbed file is SHA-256 hashed and checked against VirusTotal before import;
                flagged files are deleted and an alert is raised. Files are never uploaded (lookup only).
              </>
            }
          >
            <input className={inputCls} type="password" value={f.apiKey} onChange={(e) => set("apiKey", e.target.value)}
              placeholder={editing && hasKey ? "•••••••• (leave blank to keep current)" : "VirusTotal API key"} />
          </FormField>
          <FormField>
            <Toggle checked={f.vtBlockUnknown} onChange={(v) => set("vtBlockUnknown", v)}
              label="Hold files VirusTotal has never seen (default: allow unknown)" />
          </FormField>
        </>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ the form
function IntegrationForm({
  entry,
  integ,
  onDone,
}: {
  entry: ProviderCatalogEntry;
  integ?: Integration;
  onDone: () => void;
}) {
  const editing = !!integ;
  const [f, setF] = useState<FormState>(() => blankForm(integ));
  const [err, setErr] = useState<string | null>(null);
  const set = <K extends keyof FormState>(k: K, v: FormState[K]) => setF((p) => ({ ...p, [k]: v }));

  const save = useMutation({
    mutationFn: () => {
      if (editing) {
        // Carry forward existing config keys the form doesn't manage so an edit can't drop them.
        const body = buildBody(entry.kind, f, integ!.config ?? {});
        const patch: any = { ...body };
        if (!f.apiKey.trim()) delete patch.api_key;  // blank = keep the stored secret
        delete patch.kind;
        return api.updateIntegration(integ!.id, patch);
      }
      return api.addIntegration(buildBody(entry.kind, f));
    },
    onSuccess: onDone,
    onError: (e) => setErr((e as Error).message),
  });

  return (
    <Modal
      title={editing ? `Configure ${integ!.name}` : `Connect ${entry.label}`}
      onClose={onDone}
      width="w-[30rem]"
      footer={
        <>
          <Button size="sm" variant="ghost" onClick={onDone}>Cancel</Button>
          <Button size="sm" variant="primary" disabled={save.isPending || !canSubmit(entry.kind, f, editing)}
            onClick={() => save.mutate()}>
            {save.isPending ? "Saving…" : editing ? "Save changes" : "Connect"}
          </Button>
        </>
      }
    >
      <p className="mb-4 text-xs text-muted">{entry.tagline}</p>
      {editing && (
        <FormField label="Name">
          <input className={field} value={f.name} onChange={(e) => set("name", e.target.value)} placeholder="Name" />
        </FormField>
      )}
      <KindFields entry={entry} f={f} set={set} editing={editing} hasKey={!!integ?.has_api_key} />
      {/* Request limiting — defaults avoid provider rate-blocks / timeouts; blank = catalog default. */}
      <div className="grid gap-2 rounded-xl border border-[var(--hair,var(--border))] bg-surface-2/40 p-3 sm:grid-cols-2">
        <div className="text-xs font-semibold uppercase tracking-wide text-muted sm:col-span-2">Request limiting</div>
        <label className="text-xs text-muted">
          Max requests / min
          <input className={`${field} mt-1`} type="number" min={1} value={f.rpm}
            onChange={(e) => set("rpm", e.target.value)} placeholder={`default ${entry.default_rpm}`} />
        </label>
        <label className="text-xs text-muted">
          Timeout (seconds)
          <input className={`${field} mt-1`} type="number" min={3} value={f.timeout}
            onChange={(e) => set("timeout", e.target.value)} placeholder={`default ${entry.default_timeout}`} />
        </label>
      </div>
      {err && <p className="mt-3 text-xs text-red-500">{err}</p>}
    </Modal>
  );
}

// ----------------------------------------------------------------- provider box
function ProviderBox({
  entry,
  integ,
  matchRatio,
  onChanged,
}: {
  entry: ProviderCatalogEntry;
  integ?: Integration;
  matchRatio?: number;
  onChanged: () => void;
}) {
  const confirm = useConfirm();
  const [mode, setMode] = useState<"view" | "form">("view");
  const [info, setInfo] = useState(false);
  const [test, setTest] = useState<IntegrationTest | null>(null);

  const vtUsage = useQuery({
    queryKey: ["vt-usage"],
    queryFn: api.getVirusTotalUsage,
    enabled: entry.kind === "virustotal" && !!integ,
  });
  const testM = useMutation({ mutationFn: () => api.testIntegration(integ!.id), onSuccess: (r) => { setTest(r); vtUsage.refetch(); onChanged(); } });
  const syncM = useMutation({ mutationFn: () => api.syncIntegration(integ!.id), onSuccess: onChanged });
  const toggle = useMutation({ mutationFn: (en: boolean) => api.updateIntegration(integ!.id, { enabled: en }), onSuccess: onChanged });
  const del = useMutation({ mutationFn: () => api.deleteIntegration(integ!.id), onSuccess: onChanged });

  const connected = !!integ;
  const countLabel = entry.category === "metadata" ? "linked"
    : (entry.category === "pipeline" || entry.category === "security") ? "" : "in catalog";

  // Status chip on the card: error → danger; enabled → success; disabled → neutral.
  const statusTone: StatusTone = connected
    ? integ!.last_error ? "danger" : integ!.enabled ? "success" : "neutral"
    : "neutral";
  const statusLabel = connected
    ? integ!.last_error ? "Error" : integ!.enabled ? "Connected" : "Disabled"
    : undefined;

  return (
    <div className={`overflow-hidden rounded-2xl border ${connected ? "border-[var(--hair,var(--border))]" : "border-dashed border-[var(--hair,var(--border))]"}`}>
      <ProviderCard
        name={entry.label}
        desc={entry.tagline}
        statusTone={statusTone}
        statusLabel={statusLabel}
        actions={
          // All interactive controls live together on the right (identity stays on the left), so the
          // info toggle never wraps to its own line on longer provider names — consistent across cards.
          <div className="flex shrink-0 items-center gap-1">
            <button className="px-1 text-base leading-none text-muted hover:text-text"
              aria-label="What is this?" title="What is this?" onClick={() => setInfo((v) => !v)}>ⓘ</button>
            {!connected && !entry.per_user && (
              <Button size="sm" variant="outline" onClick={() => setMode(mode === "form" ? "view" : "form")} title="Add">
                {mode === "form" ? "Close" : "＋ Add"}
              </Button>
            )}
            {connected && (
              <>
                <button className="px-1 text-muted hover:text-text" title="Edit" onClick={() => setMode(mode === "form" ? "view" : "form")}>✎</button>
                <Toggle checked={integ!.enabled} onChange={(v) => toggle.mutate(v)} label="" />
                <button className="px-1 text-red-500 hover:text-red-400" title="Remove"
                  onClick={async () => {
                    if (await confirm({ message: `Disconnect ${integ!.name}?`, danger: true, confirmText: "Disconnect" }))
                      del.mutate();
                  }}>✕</button>
              </>
            )}
          </div>
        }
      />

      <div className="border-t border-[var(--hair,var(--border))] px-4 py-3">
        {/* category + provides — quiet facts as neutral badges (chip discipline) */}
        <div className="flex flex-wrap gap-1.5">
          <Badge>{entry.category}</Badge>
          {entry.provides.map((p) => (
            <Badge key={p}>{p}</Badge>
          ))}
        </div>

        {info && (
          <div className="mt-2 space-y-1 rounded-xl bg-surface-2/60 p-3 text-xs text-muted">
            <p><b className="text-text">Use.</b> {entry.use}</p>
            <p><b className="text-text">Requests.</b> {entry.requests}</p>
            <p><b className="text-text">Matching.</b> {entry.matching}</p>
            <p className="text-[11px]">{AUTH_LABEL[entry.auth]}</p>
          </div>
        )}

        {connected && (
          <div className="mt-2 text-xs text-muted">
            <div className="truncate">
              {[
                countLabel ? `${integ!.catalog_count} ${countLabel}` : "",
                matchRatio != null ? `${Math.round(matchRatio * 100)}% matched` : "",
                `≤ ${integ!.requests_per_minute}/min · ${integ!.timeout}s`,
                integ!.last_sync_at ? `synced ${new Date(integ!.last_sync_at).toLocaleDateString()}` : "",
              ].filter(Boolean).join(" · ")}
            </div>
            {integ!.last_error && <div className="text-red-500">⚠ {integ!.last_error}</div>}
            {entry.kind === "virustotal" && vtUsage.data && (
              <div className="mt-1 text-[11px]">
                <span className="text-muted">API usage (30d): </span>
                <span className="text-text">{vtUsage.data.total.toLocaleString()} lookups</span>
                {" · "}{vtUsage.data.last_24h.toLocaleString()} in 24h
                {(vtUsage.data.by_outcome.blocked || 0) > 0 && (
                  <span className="text-amber-600"> · {vtUsage.data.by_outcome.blocked} rate-limited</span>
                )}
                {(vtUsage.data.by_outcome.error || 0) > 0 && (
                  <span className="text-red-500"> · {vtUsage.data.by_outcome.error} errors</span>
                )}
                {vtUsage.data.total === 0 && <span className="text-muted"> — no lookups yet; run Test to verify</span>}
              </div>
            )}
            <div className="mt-2 flex gap-1.5">
              <Button size="sm" variant="ghost" disabled={testM.isPending} onClick={() => testM.mutate()}>
                {testM.isPending ? "Testing…" : "Test"}
              </Button>
              <Button size="sm" variant="ghost" disabled={syncM.isPending} onClick={() => syncM.mutate()}>
                {syncM.isPending ? "Syncing…" : "Sync"}
              </Button>
            </div>
            {test && (
              <div className={`mt-1 ${test.ok ? "text-green-600" : "text-red-500"}`}>
                {test.ok
                  ? `✓ ${test.app ?? "Connected"}${test.version ? ` v${test.version}` : ""}${test.detail ? ` · ${test.detail}` : ""}${test.root_folders.length ? ` · folders: ${test.root_folders.join(", ")}` : ""}`
                  : `✗ ${test.error}`}
              </div>
            )}
          </div>
        )}

        {entry.per_user && !connected && (
          <p className="mt-2 text-[11px] text-muted">Per-user — connect your own shelf under Settings → Goodreads.</p>
        )}
      </div>

      {mode === "form" && (
        <IntegrationForm entry={entry} integ={integ} onDone={() => { setMode("view"); onChanged(); }} />
      )}
    </div>
  );
}

// -------------------------------------------------------------------- the cards
function IntegrationGrid({
  title,
  blurb,
  categories,
  withStats,
  extra,
}: {
  title: string;
  blurb: React.ReactNode;
  categories: IntegrationCategory[];
  withStats?: boolean;
  extra?: React.ReactNode;   // an extra provider-style box appended to the grid (e.g. Cloudflare solver)
}) {
  const qc = useQueryClient();
  const catalog = useQuery({ queryKey: qk.integrationCatalog(), queryFn: api.getIntegrationCatalog });
  const integs = useQuery({ queryKey: qk.integrations(), queryFn: api.listIntegrations });
  const stats = useQuery({ queryKey: qk.metadataStats(), queryFn: api.getMetadataStats, enabled: !!withStats });

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: qk.integrations() });
    qc.invalidateQueries({ queryKey: qk.metadataStats() });
    qc.invalidateQueries({ queryKey: qk.catalog() });
    qc.invalidateQueries({ queryKey: qk.catalogStats() });
    qc.invalidateQueries({ queryKey: qk.queuedHooks() });
  };

  const entries = (catalog.data ?? []).filter((e) => categories.includes(e.category));
  const byKind = new Map((integs.data ?? []).map((i) => [i.kind, i]));
  const ratio = (kind: string) =>
    stats.data?.providers.find((p) => p.provider === kind)?.match_ratio;

  return (
    <Card className="mb-4 p-5">
      <CardHeader title={title} hint={blurb} />
      {(catalog.isLoading || integs.isLoading) && <Spinner label="Loading…" />}
      {/* items-start so an unconnected (short) box never stretches to match a tall connected one. */}
      <div className="grid items-start gap-3 sm:grid-cols-2">
        {entries.map((e) => (
          <ProviderBox
            key={e.kind}
            entry={e}
            integ={byKind.get(e.kind)}
            matchRatio={withStats ? ratio(e.kind) : undefined}
            onChanged={invalidate}
          />
        ))}
        {extra}
      </div>
    </Card>
  );
}

/** Cloudflare solver presented as a provider box (matching the integration cards), but wired to the
 *  shared system-config (flaresolverr_*) rather than the Integration model — so it sits right next to
 *  VirusTotal without needing a backend integration kind. Blank URL = disabled. */
export function CloudflareSolverBox() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: qk.systemConfig(), queryFn: api.getSystemConfig });
  const [mode, setMode] = useState<"view" | "form">("view");
  const [info, setInfo] = useState(false);
  const [form, setForm] = useState<{ url: string; timeout: string; ttl: string } | null>(null);
  const derive = (vals: SystemConfig["values"]) => ({
    url: String(vals.flaresolverr_url ?? ""),
    timeout: String(vals.flaresolverr_timeout_s ?? ""),
    ttl: String(vals.flaresolverr_clearance_ttl_s ?? ""),
  });

  // Reseed from current config when the editor opens (like IntegrationForm rebuilds from its data on
  // mount), so edits never show stale values after a cancel or an external change. Keyed off `mode`
  // (the open transition) rather than `q.data`, so a background refetch can't wipe in-progress edits.
  useEffect(() => {
    if (q.data && (form === null || mode === "form")) setForm(derive(q.data.values));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, q.isSuccess]);

  const save = useMutation({
    mutationFn: () =>
      api.putSystemConfig({
        flaresolverr_url: form!.url.trim(),
        flaresolverr_timeout_s: Number(form!.timeout) || 0,
        flaresolverr_clearance_ttl_s: Number(form!.ttl) || 0,
      }),
    onSuccess: (d) => { qc.setQueryData(qk.systemConfig(), d); setForm(derive(d.values)); setMode("view"); },
  });

  const v = q.data?.values ?? {};
  const url = String(v.flaresolverr_url ?? "").trim();
  const configured = !!url;

  return (
    <div className={`overflow-hidden rounded-2xl border ${configured ? "border-[var(--hair,var(--border))]" : "border-dashed border-[var(--hair,var(--border))]"}`}>
      <ProviderCard
        name="Cloudflare solver"
        desc="Passes Cloudflare / Turnstile challenges via a FlareSolverr proxy."
        statusTone={configured ? "success" : "neutral"}
        statusLabel={configured ? "Configured" : undefined}
        actions={
          <div className="flex shrink-0 items-center gap-1">
            <button className="px-1 text-base leading-none text-muted hover:text-text"
              aria-label="What is this?" title="What is this?" onClick={() => setInfo((s) => !s)}>ⓘ</button>
            {configured ? (
              <button className="px-1 text-muted hover:text-text" title="Edit"
                onClick={() => setMode(mode === "form" ? "view" : "form")}>✎</button>
            ) : (
              <Button size="sm" variant="outline" title="Configure"
                onClick={() => setMode(mode === "form" ? "view" : "form")}>
                {mode === "form" ? "Close" : "＋ Configure"}
              </Button>
            )}
          </div>
        }
      />

      <div className="border-t border-[var(--hair,var(--border))] px-4 py-3">
        <div className="flex flex-wrap gap-1.5">
          <Badge>solver</Badge>
          {["cf_clearance", "Turnstile", "challenge solving"].map((p) => (
            <Badge key={p}>{p}</Badge>
          ))}
        </div>

        {info && (
          <div className="mt-2 space-y-1 rounded-xl bg-surface-2/60 p-3 text-xs text-muted">
            <p><b className="text-text">Use.</b> A FlareSolverr-compatible proxy that solves Cloudflare /
              Turnstile challenges so protected sources can be read. Blank URL disables it (falls back to
              the in-app browser). Changes are honored immediately — no restart.</p>
            <p className="text-[11px]">Cloudflare cookie (optional)</p>
          </div>
        )}

        {configured && mode === "view" && (
          <div className="mt-2 truncate text-xs text-muted">
            {url} · {String(v.flaresolverr_timeout_s ?? "?")}s solve · {String(v.flaresolverr_clearance_ttl_s ?? "?")}s reuse
          </div>
        )}
      </div>

      {mode === "form" && form && (
        <Modal
          title="Configure Cloudflare solver"
          onClose={() => setMode("view")}
          width="w-[30rem]"
          footer={
            <>
              <Button size="sm" variant="ghost" onClick={() => setMode("view")}>Cancel</Button>
              <Button size="sm" variant="primary" disabled={save.isPending} onClick={() => save.mutate()}>
                {save.isPending ? "Saving…" : "Save"}
              </Button>
            </>
          }
        >
          <FormField label="FlareSolverr URL" hint="blank = disabled (falls back to the in-app browser)">
            <input className={inputCls} value={form.url} placeholder="http://host:8191 (blank = disabled)"
              spellCheck={false} onChange={(e) => setForm({ ...form, url: e.target.value })} />
          </FormField>
          <div className="grid grid-cols-2 gap-2">
            <FormField label="Solve timeout (s)">
              <input className={field} type="number" min={1} value={form.timeout}
                onChange={(e) => setForm({ ...form, timeout: e.target.value })} />
            </FormField>
            <FormField label="Clearance reuse (s)">
              <input className={field} type="number" min={1} value={form.ttl}
                onChange={(e) => setForm({ ...form, ttl: e.target.value })} />
            </FormField>
          </div>
          {save.isError && <p className="mt-2 text-xs text-red-500">{(save.error as Error).message}</p>}
        </Modal>
      )}
    </div>
  );
}

export function MetadataProvidersCard() {
  return (
    <IntegrationGrid
      title="Metadata providers"
      withStats
      categories={["metadata"]}
      blurb={
        <>
          The sources of truth for author, synopsis, cover, chapter / volume counts, and matching.
          Connect the ones that fit your library; each box explains what it provides and how it's
          matched. Defaults keep request rates polite — tune them per provider if you hit limits.
        </>
      }
    />
  );
}

export function ReadingAppsCard() {
  return (
    <IntegrationGrid
      title="Reading apps (push + wanted)"
      categories={["companion"]}
      blurb={
        <>
          Connect Audiobookshelf and Storyteller. Shelf makes your stocked ebooks + audiobooks
          available to them (Audiobookshelf auto-scans the shared folders; Storyteller gets EPUB +
          audio copies, converted on demand). Any title they have in only one format — ebook without
          its audiobook, or vice-versa — is fetched by Shelf and delivered back.
        </>
      }
    />
  );
}

export function AcquisitionCard() {
  return (
    <IntegrationGrid
      title="Acquisition & downloads"
      categories={["manager", "pipeline", "security"]}
      extra={<CloudflareSolverBox />}
      blurb={
        <>
          Library managers (Readarr / Kapowarr) fill the catalog; the usenet pipeline (Prowlarr →
          SABnzbd) and the torrent pipeline (Prowlarr → qBittorrent) fetch books on demand. VirusTotal
          scans torrent-grabbed files before import; the Cloudflare solver lets protected sources be
          read. Connect a service, test it, tune its limits.
        </>
      }
    />
  );
}
