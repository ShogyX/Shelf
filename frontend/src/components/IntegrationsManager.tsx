// Settings → Integrations, rebuilt as two tidy cards of provider BOXES:
//   • MetadataProvidersCard — the metadata providers (covers/synopsis/chapter counts/matching)
//   • AcquisitionCard       — download managers + the usenet acquisition pipeline
// Every connectable provider shows as a box: a "＋" to add one that isn't connected, and a connected
// box with an ✎ edit panel, an ✕ to remove, plus test/sync/enable. Each box explains the provider's
// use, requests and matching (from the backend catalog) and exposes its request-limit + timeout.
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  Integration,
  IntegrationCategory,
  IntegrationKind,
  IntegrationTest,
  ProviderCatalogEntry,
} from "../api/client";
import { Badge, Button, Card, Spinner, Toggle } from "./ui";

const input = "w-full rounded-lg border border-border bg-bg px-3 py-2 text-sm";
const field = "w-full rounded-md border border-border bg-bg px-2 py-1 text-sm";

const toList = (s: string): string[] => s.split(/[,\n]/).map((x) => x.trim()).filter(Boolean);
const numOrNull = (s: string): number | null => {
  const n = parseFloat(s);
  return Number.isFinite(n) ? n : null;
};
const CATEGORY_TONE: Record<IntegrationCategory, "amber" | "violet" | "green"> = {
  metadata: "amber",
  manager: "violet",
  pipeline: "green",
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
  sabCategory: string;
  libraryPath: string;
  maxGrabs: string;
  pathFrom: string;
  pathTo: string;
  rpm: string;
  timeout: string;
}

function blankForm(integ?: Integration): FormState {
  const c = integ?.config ?? {};
  const cats: number[] = Array.isArray(c.categories) ? c.categories : [];
  const map0 = Array.isArray(c.path_mappings) ? c.path_mappings[0] ?? {} : {};
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
    sabCategory: c.category ?? "shelf",
    libraryPath: c.library_path ?? "",
    maxGrabs: c.max_grabs_per_day != null ? String(c.max_grabs_per_day) : "",
    pathFrom: map0.remote ?? "",
    pathTo: map0.local ?? "",
    // Show an existing per-integration override; blank means "use the catalog default".
    rpm: c.requests_per_minute != null ? String(c.requests_per_minute) : "",
    timeout: c.timeout != null ? String(c.timeout) : "",
  };
}

// form → POST/PATCH body for a given kind. ``passthrough`` carries any existing config keys the
// form doesn't manage (e.g. a manually-set prowlarr indexer_ids) so an edit never silently drops them.
function buildBody(kind: IntegrationKind, f: FormState, passthrough: Record<string, unknown> = {}) {
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
  // readarr / kapowarr
  return {
    ...base,
    base_url: f.baseUrl.trim(),
    api_key: f.apiKey.trim(),
    auto_map_folders: f.autoMap,
    config: withKey(),
  };
}

function canSubmit(kind: IntegrationKind, f: FormState): boolean {
  if (kind === "hardcover") return !!f.apiKey.trim();
  if (kind === "goodreads") return !!f.userId.trim();
  if (["ranobedb", "googlebooks", "anilist", "novelupdates"].includes(kind)) return true;
  return !!f.baseUrl.trim() && !!f.apiKey.trim();
}

// --------------------------------------------------------------- kind fields
function KindFields({
  entry,
  f,
  set,
}: {
  entry: ProviderCatalogEntry;
  f: FormState;
  set: <K extends keyof FormState>(k: K, v: FormState[K]) => void;
}) {
  const k = entry.kind;
  if (k === "anilist") return null;
  return (
    <div className="grid gap-2">
      {k === "goodreads" && (
        <>
          <input className={input} value={f.userId} onChange={(e) => set("userId", e.target.value)}
            placeholder="Goodreads numeric user ID (or profile URL)" />
          <input className={input} value={f.shelf} onChange={(e) => set("shelf", e.target.value)}
            placeholder="Shelf (default: to-read)" />
        </>
      )}
      {k === "ranobedb" && (
        <input className={input} value={f.baseUrl} onChange={(e) => set("baseUrl", e.target.value)}
          placeholder="API base (optional — defaults to ranobedb.org)" />
      )}
      {(k === "googlebooks" || k === "hardcover") && (
        <input className={input} type="password" value={f.apiKey} onChange={(e) => set("apiKey", e.target.value)}
          placeholder={k === "hardcover" ? "Bearer token (required)" : "API key (optional — raises quota)"} />
      )}
      {k === "novelupdates" && (
        <>
          <input className={input} value={f.cfClearance} onChange={(e) => set("cfClearance", e.target.value)}
            placeholder="cf_clearance cookie (optional)" />
          <input className={input} value={f.userAgent} onChange={(e) => set("userAgent", e.target.value)}
            placeholder="matching User-Agent (from the same browser)" />
        </>
      )}
      {(k === "readarr" || k === "kapowarr") && (
        <>
          <input className={input} value={f.baseUrl} onChange={(e) => set("baseUrl", e.target.value)}
            placeholder={k === "readarr" ? "http://host:8787" : "http://host:5656"} />
          <input className={input} type="password" value={f.apiKey} onChange={(e) => set("apiKey", e.target.value)}
            placeholder="API key" />
          <Toggle checked={f.autoMap} onChange={(v) => set("autoMap", v)} label="Auto-map download folders" />
        </>
      )}
      {k === "prowlarr" && (
        <>
          <input className={input} value={f.baseUrl} onChange={(e) => set("baseUrl", e.target.value)}
            placeholder="http://host:9696" />
          <input className={input} type="password" value={f.apiKey} onChange={(e) => set("apiKey", e.target.value)}
            placeholder="API key" />
          <div className="grid gap-2 rounded-lg border border-border p-2">
            <div className="text-xs font-medium text-muted">Search preferences (content filtering)</div>
            <div className="flex flex-wrap gap-4">
              <Toggle checked={f.wantEbooks} onChange={(v) => set("wantEbooks", v)} label="Ebooks" />
              <Toggle checked={f.wantAudiobooks} onChange={(v) => set("wantAudiobooks", v)} label="Audiobooks" />
            </div>
            <input className={input} value={f.formats} onChange={(e) => set("formats", e.target.value)}
              placeholder="Preferred formats, best first (epub, azw3, mobi, pdf)" />
            <input className={input} value={f.languages} onChange={(e) => set("languages", e.target.value)}
              placeholder="Languages (e.g. en)" />
            <div className="flex gap-2">
              <input className={input} value={f.minSize} inputMode="decimal"
                onChange={(e) => set("minSize", e.target.value)} placeholder="Min MB" />
              <input className={input} value={f.maxSize} inputMode="decimal"
                onChange={(e) => set("maxSize", e.target.value)} placeholder="Max MB" />
            </div>
            <input className={input} value={f.excludeTerms} onChange={(e) => set("excludeTerms", e.target.value)}
              placeholder="Exclude terms (comma separated)" />
            <input className={input} value={f.requiredTerms} onChange={(e) => set("requiredTerms", e.target.value)}
              placeholder="Required terms — release must contain ≥1" />
            <input className={input} value={f.ignoredTerms} onChange={(e) => set("ignoredTerms", e.target.value)}
              placeholder="Ignored terms — reject if present" />
            <input className={input} value={f.preferredTerms} onChange={(e) => set("preferredTerms", e.target.value)}
              placeholder="Preferred terms — rank higher" />
          </div>
        </>
      )}
      {k === "sabnzbd" && (
        <>
          <input className={input} value={f.baseUrl} onChange={(e) => set("baseUrl", e.target.value)}
            placeholder="http://host:8080" />
          <input className={input} type="password" value={f.apiKey} onChange={(e) => set("apiKey", e.target.value)}
            placeholder="API key" />
          <input className={input} value={f.sabCategory} onChange={(e) => set("sabCategory", e.target.value)}
            placeholder="Staging category (default: shelf)" />
          <input className={input} value={f.libraryPath} onChange={(e) => set("libraryPath", e.target.value)}
            placeholder="Library path (e.g. /mnt/NAS-Pool/media/Books)" />
          <input className={input} type="number" min={1} value={f.maxGrabs}
            onChange={(e) => set("maxGrabs", e.target.value)} placeholder="Max downloads/day per release (default: 2)" />
          <div className="grid gap-2 rounded-lg border border-border p-2">
            <div className="text-xs font-medium text-muted">Remote path mapping (only if SABnzbd is on another host)</div>
            <div className="flex gap-2">
              <input className={input} value={f.pathFrom} onChange={(e) => set("pathFrom", e.target.value)}
                placeholder="SABnzbd path" />
              <input className={input} value={f.pathTo} onChange={(e) => set("pathTo", e.target.value)}
                placeholder="Shelf path" />
            </div>
          </div>
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
      return api.addIntegration(buildBody(entry.kind, f) as any);
    },
    onSuccess: onDone,
    onError: (e) => setErr((e as Error).message),
  });

  return (
    <div className="mt-2 grid gap-2 rounded-lg border border-border bg-bg/40 p-2">
      {editing && (
        <input className={field} value={f.name} onChange={(e) => set("name", e.target.value)} placeholder="Name" />
      )}
      <KindFields entry={entry} f={f} set={set} />
      {editing && entry.auth !== "none" && (
        <input className={field} type="password" value={f.apiKey} onChange={(e) => set("apiKey", e.target.value)}
          placeholder={integ!.has_api_key ? "•••••••• (leave blank to keep current key)" : "API key / token"} />
      )}
      {/* Request limiting — defaults avoid provider rate-blocks / timeouts; blank = catalog default. */}
      <div className="grid gap-2 rounded-lg border border-border p-2 sm:grid-cols-2">
        <div className="text-xs font-medium text-muted sm:col-span-2">Request limiting</div>
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
      {err && <p className="text-xs text-red-500">{err}</p>}
      <div className="flex justify-end gap-2">
        <Button size="sm" variant="ghost" onClick={onDone}>Cancel</Button>
        <Button size="sm" variant="primary" disabled={save.isPending || !canSubmit(entry.kind, f)}
          onClick={() => save.mutate()}>
          {save.isPending ? "Saving…" : editing ? "Save changes" : "Connect"}
        </Button>
      </div>
    </div>
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
  const [mode, setMode] = useState<"view" | "form">("view");
  const [info, setInfo] = useState(false);
  const [test, setTest] = useState<IntegrationTest | null>(null);
  const tone = CATEGORY_TONE[entry.category];

  const testM = useMutation({ mutationFn: () => api.testIntegration(integ!.id), onSuccess: (r) => { setTest(r); onChanged(); } });
  const syncM = useMutation({ mutationFn: () => api.syncIntegration(integ!.id), onSuccess: onChanged });
  const toggle = useMutation({ mutationFn: (en: boolean) => api.updateIntegration(integ!.id, { enabled: en }), onSuccess: onChanged });
  const del = useMutation({ mutationFn: () => api.deleteIntegration(integ!.id), onSuccess: onChanged });

  const connected = !!integ;
  const countLabel = entry.category === "metadata" ? "linked" : entry.category === "pipeline" ? "" : "in catalog";

  return (
    <div className={`rounded-xl border p-3 ${connected ? "border-border bg-surface" : "border-dashed border-border"}`}>
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="font-medium">{entry.label}</span>
            <Badge tone={tone}>{entry.category}</Badge>
            {connected && !integ!.enabled && <Badge>disabled</Badge>}
            {connected && integ!.enabled && <span className="text-xs text-green-600">● connected</span>}
            <button className="text-xs text-muted hover:text-text" title="What is this?" onClick={() => setInfo((v) => !v)}>ⓘ</button>
          </div>
          <div className="text-xs text-muted">{entry.tagline}</div>
        </div>
        <div className="flex shrink-0 items-center gap-1">
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
                onClick={() => confirm(`Disconnect ${integ!.name}?`) && del.mutate()}>✕</button>
            </>
          )}
        </div>
      </div>

      {/* provides chips */}
      <div className="mt-2 flex flex-wrap gap-1">
        {entry.provides.map((p) => (
          <span key={p} className="rounded-md bg-surface-2 px-1.5 py-0.5 text-[11px] text-muted">{p}</span>
        ))}
      </div>

      {info && (
        <div className="mt-2 space-y-1 rounded-lg bg-surface-2 p-2 text-xs text-muted">
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
          <div className="mt-1 flex gap-1">
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
}: {
  title: string;
  blurb: React.ReactNode;
  categories: IntegrationCategory[];
  withStats?: boolean;
}) {
  const qc = useQueryClient();
  const catalog = useQuery({ queryKey: ["integration-catalog"], queryFn: api.getIntegrationCatalog });
  const integs = useQuery({ queryKey: ["integrations"], queryFn: api.listIntegrations });
  const stats = useQuery({ queryKey: ["metadata-stats"], queryFn: api.getMetadataStats, enabled: !!withStats });

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["integrations"] });
    qc.invalidateQueries({ queryKey: ["metadata-stats"] });
    qc.invalidateQueries({ queryKey: ["catalog"] });
    qc.invalidateQueries({ queryKey: ["catalog-stats"] });
    qc.invalidateQueries({ queryKey: ["queued-hooks"] });
  };

  const entries = (catalog.data ?? []).filter((e) => categories.includes(e.category));
  const byKind = new Map((integs.data ?? []).map((i) => [i.kind, i]));
  const ratio = (kind: string) =>
    stats.data?.providers.find((p) => p.provider === kind)?.match_ratio;

  return (
    <Card className="mb-4 p-4">
      <h2 className="mb-1 font-semibold">{title}</h2>
      <p className="mb-3 text-sm text-muted">{blurb}</p>
      {(catalog.isLoading || integs.isLoading) && <Spinner label="Loading…" />}
      <div className="grid gap-3 sm:grid-cols-2">
        {entries.map((e) => (
          <ProviderBox
            key={e.kind}
            entry={e}
            integ={byKind.get(e.kind)}
            matchRatio={withStats ? ratio(e.kind) : undefined}
            onChanged={invalidate}
          />
        ))}
      </div>
    </Card>
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

export function AcquisitionCard() {
  return (
    <IntegrationGrid
      title="Acquisition & downloads"
      categories={["manager", "pipeline"]}
      blurb={
        <>
          Library managers (Readarr / Kapowarr) fill the catalog from their libraries; the usenet
          pipeline (Prowlarr search → SABnzbd downloader) fetches books on demand. Connect a service,
          test it, and tune its request limit and timeout.
        </>
      }
    />
  );
}
