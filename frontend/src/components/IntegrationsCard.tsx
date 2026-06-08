import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, Integration, IntegrationKind, IntegrationTest } from "../api/client";
import { Badge, Button, Card, Spinner, Toggle } from "./ui";

const input = "w-full rounded-lg border border-border bg-bg px-3 py-2 text-sm";

const METADATA_KINDS: IntegrationKind[] = [
  "ranobedb",
  "goodreads",
  "googlebooks",
  "hardcover",
  "anilist",
  "novelupdates",
];
const isMetadata = (k: IntegrationKind) => METADATA_KINDS.includes(k);

const PIPELINE_KINDS: IntegrationKind[] = ["prowlarr", "sabnzbd"];
const isPipeline = (k: IntegrationKind) => PIPELINE_KINDS.includes(k);

// Comma/space separated string -> trimmed list (and back), for the search-preference fields.
const toList = (s: string): string[] =>
  s.split(/[,\n]/).map((x) => x.trim()).filter(Boolean);
const numOrNull = (s: string): number | null => {
  const n = parseFloat(s);
  return Number.isFinite(n) ? n : null;
};

export default function IntegrationsCard() {
  const qc = useQueryClient();
  const integs = useQuery({ queryKey: ["integrations"], queryFn: api.listIntegrations });

  const [kind, setKind] = useState<IntegrationKind>("readarr");
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [autoMap, setAutoMap] = useState(true);
  const [userId, setUserId] = useState(""); // goodreads numeric user id
  const [shelf, setShelf] = useState("to-read"); // goodreads shelf
  const [cfClearance, setCfClearance] = useState(""); // novelupdates Cloudflare cookie
  const [userAgent, setUserAgent] = useState(""); // novelupdates UA paired with the cookie
  // Prowlarr search preferences (content filtering).
  const [wantEbooks, setWantEbooks] = useState(true);
  const [wantAudiobooks, setWantAudiobooks] = useState(false);
  const [formats, setFormats] = useState("epub, azw3, mobi, pdf");
  const [languages, setLanguages] = useState("en");
  const [minSize, setMinSize] = useState("");
  const [maxSize, setMaxSize] = useState("");
  const [excludeTerms, setExcludeTerms] = useState("");
  const [requiredTerms, setRequiredTerms] = useState("");
  const [ignoredTerms, setIgnoredTerms] = useState("");
  const [preferredTerms, setPreferredTerms] = useState("");
  // SABnzbd downloader settings.
  const [sabCategory, setSabCategory] = useState("shelf");
  const [libraryPath, setLibraryPath] = useState(""); // where verified downloads are promoted
  const [maxGrabs, setMaxGrabs] = useState(""); // max downloads/day per release (blank = default 2)
  const [pathFrom, setPathFrom] = useState(""); // path as SABnzbd reports it
  const [pathTo, setPathTo] = useState(""); // path as Shelf reads it
  const [error, setError] = useState<string | null>(null);

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["integrations"] });
    qc.invalidateQueries({ queryKey: ["catalog"] });
    qc.invalidateQueries({ queryKey: ["catalog-stats"] });
    qc.invalidateQueries({ queryKey: ["queued-hooks"] });
  };

  const add = useMutation({
    mutationFn: () => {
      if (kind === "goodreads")
        return api.addIntegration({
          kind,
          config: { user_id: userId.trim(), shelf: shelf.trim() || "to-read" },
        });
      if (kind === "anilist")
        return api.addIntegration({ kind }); // no config; uses the AniList GraphQL endpoint
      if (kind === "ranobedb")
        return api.addIntegration({ kind, base_url: baseUrl.trim() });
      if (kind === "googlebooks")
        return api.addIntegration({ kind, api_key: apiKey.trim() });
      if (kind === "hardcover")
        return api.addIntegration({ kind, api_key: apiKey.trim() });
      if (kind === "novelupdates")
        return api.addIntegration({
          kind,
          config: cfClearance.trim()
            ? { cf_clearance: cfClearance.trim(), user_agent: userAgent.trim() }
            : {},
        });
      if (kind === "prowlarr") {
        const categories = [
          ...(wantEbooks ? [7000, 7020] : []),
          ...(wantAudiobooks ? [3030] : []),
        ];
        return api.addIntegration({
          kind,
          base_url: baseUrl.trim(),
          api_key: apiKey.trim(),
          config: {
            protocols: ["usenet"],
            categories,
            preferred_formats: toList(formats).map((f) => f.toLowerCase()),
            languages: toList(languages).map((l) => l.toLowerCase()),
            min_size_mb: numOrNull(minSize),
            max_size_mb: numOrNull(maxSize),
            exclude_terms: toList(excludeTerms),
            required_terms: toList(requiredTerms),
            ignored_terms: toList(ignoredTerms),
            preferred_terms: toList(preferredTerms),
          },
        });
      }
      if (kind === "sabnzbd")
        return api.addIntegration({
          kind,
          base_url: baseUrl.trim(),
          api_key: apiKey.trim(),
          config: {
            category: sabCategory.trim() || "shelf",
            library_path: libraryPath.trim() || null,
            max_grabs_per_day: numOrNull(maxGrabs) ?? 2,
            path_mappings:
              pathFrom.trim() && pathTo.trim()
                ? [{ remote: pathFrom.trim(), local: pathTo.trim() }]
                : [],
          },
        });
      return api.addIntegration({
        kind,
        base_url: baseUrl.trim(),
        api_key: apiKey.trim(),
        auto_map_folders: autoMap,
      });
    },
    onSuccess: () => {
      setBaseUrl("");
      setApiKey("");
      setUserId("");
      setCfClearance("");
      setUserAgent("");
      // Pipeline filter/path fields too, so a second add doesn't inherit stale values.
      setMinSize("");
      setMaxSize("");
      setExcludeTerms("");
      setRequiredTerms("");
      setIgnoredTerms("");
      setPreferredTerms("");
      setPathFrom("");
      setPathTo("");
      setLibraryPath("");
      setError(null);
      invalidate();
    },
    onError: (e) => setError((e as Error).message),
  });

  const meta = isMetadata(kind);
  const canSubmit =
    kind === "ranobedb" ||
    kind === "googlebooks" ||
    kind === "anilist" ||
    kind === "novelupdates"
      ? true
      : kind === "hardcover"
        ? !!apiKey.trim()
        : kind === "goodreads"
          ? !!userId.trim()
          : !!baseUrl.trim() && !!apiKey.trim();

  return (
    <Card className="mb-4 p-4">
      <h2 className="mb-1 font-semibold">Integrations</h2>
      <p className="mb-3 text-sm text-muted">
        Connect <b>download managers</b> (Readarr / Kapowarr) to fill the index with their
        libraries, the <b>acquisition pipeline</b> (Prowlarr search + SABnzbd downloader) to fetch
        books from usenet, or <b>metadata providers</b> (RanobeDB / Google Books) that become the
        source of truth for author, synopsis, cover &amp; release count, detect new releases, and
        surface related titles. (Goodreads is per-user — connect your own shelf in{" "}
        <span className="text-text">Settings → Goodreads</span>.)
      </p>

      <div className="grid gap-2 sm:grid-cols-2">
        <select
          value={kind}
          onChange={(e) => setKind(e.target.value as IntegrationKind)}
          className={input}
        >
          <optgroup label="Download managers">
            <option value="readarr">Readarr — books / novels</option>
            <option value="kapowarr">Kapowarr — comics</option>
          </optgroup>
          <optgroup label="Acquisition pipeline">
            <option value="prowlarr">Prowlarr — indexer search (usenet)</option>
            <option value="sabnzbd">SABnzbd — usenet downloader</option>
          </optgroup>
          <optgroup label="Metadata providers">
            <option value="ranobedb">RanobeDB — light-novel metadata (volumes)</option>
            <option value="googlebooks">Google Books — broad book metadata (pages)</option>
            <option value="hardcover">Hardcover — community book database (extra titles)</option>
            <option value="anilist">AniList — manga/manhua chapter counts</option>
            <option value="novelupdates">NovelUpdates — web-novel chapter counts</option>
            {/* Goodreads is per-user — connected from Settings → Goodreads, not here. */}
          </optgroup>
        </select>

        {kind === "goodreads" ? (
          <>
            <input
              value={userId}
              onChange={(e) => setUserId(e.target.value)}
              placeholder="Goodreads numeric user ID (or profile URL)"
              className={input}
            />
            <input
              value={shelf}
              onChange={(e) => setShelf(e.target.value)}
              placeholder="Shelf (default: to-read)"
              className={input}
            />
            <p className="text-xs text-muted sm:col-span-2">
              Find your ID in your profile URL, e.g.{" "}
              <code>goodreads.com/user/show/12345-name</code>. The shelf must be public.
            </p>
          </>
        ) : kind === "ranobedb" ? (
          <>
            <input
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder="API base (optional — defaults to ranobedb.org)"
              className={input}
            />
            <p className="text-xs text-muted sm:col-span-2">
              No credentials needed. Shelf matches your hooked light novels to RanobeDB by title +
              author and pulls canonical metadata + release signals.
            </p>
          </>
        ) : kind === "googlebooks" ? (
          <>
            <input
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="API key (optional — only raises the rate limit)"
              type="password"
              className={input}
            />
            <p className="text-xs text-muted sm:col-span-2">
              No key required. Matches your hooked works to Google Books by title + author for
              broad coverage of prose fiction (and many comics) — a great fallback beyond
              light novels.
            </p>
          </>
        ) : kind === "hardcover" ? (
          <>
            <input
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="API token (required)"
              type="password"
              className={input}
            />
            <p className="text-xs text-muted sm:col-span-2">
              A community books database with strong coverage of titles Google Books / Open Library
              miss — used for catalog discovery &amp; resolution. Get a personal API token from your
              Hardcover account → <span className="text-text">Settings → Hardcover API</span> (paste
              the token, with or without the “Bearer ” prefix).
            </p>
          </>
        ) : kind === "anilist" ? (
          <p className="text-xs text-muted sm:col-span-2">
            No credentials needed. AniList is the source of truth for <b>chapter counts</b> of
            manga / manhua / manhwa — Shelf compares it against what you've downloaded and pulls
            the missing chapters when more exist. (Prose novels are matched by their own medium, so
            a comic adaptation never overrides a novel's count.)
          </p>
        ) : kind === "novelupdates" ? (
          <>
            <input
              value={cfClearance}
              onChange={(e) => setCfClearance(e.target.value)}
              placeholder="cf_clearance cookie (optional — see note)"
              className={input}
            />
            <input
              value={userAgent}
              onChange={(e) => setUserAgent(e.target.value)}
              placeholder="matching User-Agent (paste from the same browser)"
              className={input}
            />
            <p className="text-xs text-muted sm:col-span-2">
              The authoritative <b>chapter count</b> for translated web novels (Chinese / Korean /
              Japanese). NovelUpdates sits behind a Cloudflare challenge, so paste a{" "}
              <code>cf_clearance</code> cookie and the matching <code>User-Agent</code> from a
              browser session where you've passed it (DevTools → Application → Cookies). Without a
              cookie Shelf will try a headless render and report a clear error if it's blocked.
            </p>
          </>
        ) : kind === "prowlarr" ? (
          <>
            <input
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder="http://host:9696"
              className={input}
            />
            <input
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="API key"
              type="password"
              className={input}
            />
            <div className="grid gap-2 rounded-lg border border-border p-2 sm:col-span-2">
              <div className="text-xs font-medium text-muted">
                Search preferences (content filtering)
              </div>
              <div className="flex flex-wrap gap-4">
                <Toggle checked={wantEbooks} onChange={setWantEbooks} label="Ebooks" />
                <Toggle checked={wantAudiobooks} onChange={setWantAudiobooks} label="Audiobooks" />
              </div>
              <input
                value={formats}
                onChange={(e) => setFormats(e.target.value)}
                placeholder="Preferred formats, best first (epub, azw3, mobi, pdf)"
                className={input}
              />
              <input
                value={languages}
                onChange={(e) => setLanguages(e.target.value)}
                placeholder="Languages (e.g. en)"
                className={input}
              />
              <div className="flex gap-2">
                <input
                  value={minSize}
                  onChange={(e) => setMinSize(e.target.value)}
                  placeholder="Min MB"
                  inputMode="decimal"
                  className={input}
                />
                <input
                  value={maxSize}
                  onChange={(e) => setMaxSize(e.target.value)}
                  placeholder="Max MB"
                  inputMode="decimal"
                  className={input}
                />
              </div>
              <input
                value={excludeTerms}
                onChange={(e) => setExcludeTerms(e.target.value)}
                placeholder="Exclude terms (comma separated, e.g. sample, drm)"
                className={input}
              />
              <input
                value={requiredTerms}
                onChange={(e) => setRequiredTerms(e.target.value)}
                placeholder="Required terms — release must contain ≥1 (e.g. retail, /epub/i)"
                className={input}
              />
              <input
                value={ignoredTerms}
                onChange={(e) => setIgnoredTerms(e.target.value)}
                placeholder="Ignored terms — reject if present (e.g. drm, scan)"
                className={input}
              />
              <input
                value={preferredTerms}
                onChange={(e) => setPreferredTerms(e.target.value)}
                placeholder="Preferred terms — rank higher (e.g. retail, /v\d+/)"
                className={input}
              />
            </div>
            <p className="text-xs text-muted sm:col-span-2">
              Prowlarr searches your enabled <b>usenet</b> indexers. The matching engine ranks
              releases by these preferences plus the book's title / author / language / edition.
              Required/ignored/preferred terms accept a plain word (case-insensitive) or a
              <code> /regex/flags </code> pattern.
            </p>
          </>
        ) : kind === "sabnzbd" ? (
          <>
            <input
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder="http://host:8080"
              className={input}
            />
            <input
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="API key"
              type="password"
              className={input}
            />
            <input
              value={sabCategory}
              onChange={(e) => setSabCategory(e.target.value)}
              placeholder="Staging category (default: shelf)"
              className={input}
            />
            <input
              value={libraryPath}
              onChange={(e) => setLibraryPath(e.target.value)}
              placeholder="Library path (e.g. /mnt/NAS-Pool/media/Books)"
              className={input}
            />
            <input
              value={maxGrabs}
              onChange={(e) => setMaxGrabs(e.target.value)}
              type="number"
              min={1}
              placeholder="Max downloads/day per release (default: 2)"
              className={input}
            />
            <div className="grid gap-2 rounded-lg border border-border p-2 sm:col-span-2">
              <div className="text-xs font-medium text-muted">
                Remote path mapping (only if SABnzbd runs on another host)
              </div>
              <div className="flex gap-2">
                <input
                  value={pathFrom}
                  onChange={(e) => setPathFrom(e.target.value)}
                  placeholder="SABnzbd path (e.g. /media/NAS-Pool)"
                  className={input}
                />
                <input
                  value={pathTo}
                  onChange={(e) => setPathTo(e.target.value)}
                  placeholder="Shelf path (e.g. /mnt/NAS-Pool)"
                  className={input}
                />
              </div>
            </div>
            <p className="text-xs text-muted sm:col-span-2">
              Downloads land in the <b>staging category</b>'s folder, where Shelf verifies the
              file's content matches the requested book before <b>promoting</b> it into the{" "}
              <b>library path</b> (the only folder other automation watches). Both folders must be on
              storage Shelf can also read; set the path mapping if SABnzbd runs on another host. If
              no library path is set, books are imported in place (no staging isolation).
            </p>
          </>
        ) : (
          <>
            <input
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder={kind === "readarr" ? "http://host:8787" : "http://host:5656"}
              className={input}
            />
            <input
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="API key"
              type="password"
              className={input}
            />
            <div className="flex items-center">
              <Toggle checked={autoMap} onChange={setAutoMap} label="Auto-map download folders" />
            </div>
          </>
        )}
      </div>
      <div className="mt-2 flex justify-end">
        <Button variant="primary" disabled={!canSubmit || add.isPending} onClick={() => add.mutate()}>
          {add.isPending ? "Connecting…" : "Connect"}
        </Button>
      </div>
      {error && <p className="mt-2 text-sm text-red-500">{error}</p>}

      {integs.isLoading && (
        <div className="mt-3">
          <Spinner label="Loading integrations…" />
        </div>
      )}
      {(() => {
        const list = integs.data || [];
        const on = (k: string) => list.some((i) => i.kind === k && i.enabled);
        const Src = ({ name, active, note }: { name: string; active: boolean; note: string }) => (
          <span className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-0.5 text-xs">
            <span className={active ? "text-green-600" : "text-muted"}>{active ? "●" : "○"}</span>
            <b>{name}</b>
            <span className="text-muted">· {note}</span>
          </span>
        );
        return (
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <span className="text-xs font-medium text-muted">Book catalog sources:</span>
            <Src name="Open Library" active note="built-in · keyless" />
            <Src
              name="Google Books"
              active={on("googlebooks")}
              note={on("googlebooks") ? "key set" : "add a key to raise quota"}
            />
            <Src
              name="Hardcover"
              active={on("hardcover")}
              note={on("hardcover") ? "token set" : "add a token for extra titles"}
            />
          </div>
        );
      })()}
      <div className="mt-4 space-y-2">
        {integs.data?.map((i) => (
          <IntegrationRow key={i.id} integ={i} onChanged={invalidate} />
        ))}
      </div>
    </Card>
  );
}

function IntegrationRow({ integ, onChanged }: { integ: Integration; onChanged: () => void }) {
  const qc = useQueryClient();
  const [test, setTest] = useState<IntegrationTest | null>(null);

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["integrations"] });
    onChanged();
  };

  const testM = useMutation({
    mutationFn: () => api.testIntegration(integ.id),
    onSuccess: (r) => {
      setTest(r);
      refresh();
    },
  });
  const syncM = useMutation({
    mutationFn: () => api.syncIntegration(integ.id),
    onSuccess: () => refresh(),
  });
  const toggle = useMutation({
    mutationFn: (enabled: boolean) => api.updateIntegration(integ.id, { enabled }),
    onSuccess: () => refresh(),
  });
  const del = useMutation({
    mutationFn: () => api.deleteIntegration(integ.id),
    onSuccess: () => refresh(),
  });

  // --- edit (post-creation) ---
  const [editing, setEditing] = useState(false);
  const [eName, setEName] = useState(integ.name);
  const [eBase, setEBase] = useState(integ.base_url || "");
  const [eKey, setEKey] = useState("");
  const [eConfig, setEConfig] = useState(JSON.stringify(integ.config ?? {}, null, 2));
  const [eErr, setEErr] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: () => {
      let cfg: Record<string, unknown> | undefined;
      const raw = eConfig.trim();
      if (raw) {
        try {
          cfg = JSON.parse(raw);
        } catch {
          throw new Error("Config must be valid JSON");
        }
      } else {
        cfg = {};
      }
      const body: Record<string, unknown> = {
        name: eName.trim() || undefined,
        base_url: eBase.trim(),
        config: cfg,
      };
      if (eKey.trim()) body.api_key = eKey.trim();
      return api.updateIntegration(integ.id, body);
    },
    onSuccess: () => {
      setEditing(false);
      setEKey("");
      setEErr(null);
      refresh();
    },
    onError: (e) => setEErr((e as Error).message),
  });
  const startEdit = () => {
    setEName(integ.name);
    setEBase(integ.base_url || "");
    setEKey("");
    setEConfig(JSON.stringify(integ.config ?? {}, null, 2));
    setEErr(null);
    setEditing(true);
  };
  const efield =
    "w-full rounded-md border border-border bg-bg px-2 py-1 text-sm";

  const countLabel = integ.is_metadata ? "linked" : "in catalog";
  const metaTarget =
    integ.kind === "goodreads"
      ? `shelf: ${integ.config?.shelf ?? "to-read"}`
      : integ.kind === "googlebooks"
        ? integ.base_url || "googleapis.com/books"
        : integ.base_url || "ranobedb.org";
  const target = integ.is_metadata ? metaTarget : integ.base_url;
  const pipelineRole =
    integ.kind === "prowlarr"
      ? "search source · usenet"
      : integ.kind === "sabnzbd"
        ? `downloader → category: ${integ.config?.category ?? "shelf"}`
        : "";

  return (
    <div className="rounded-lg border border-border p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="truncate font-medium">{integ.name}</span>
            <Badge tone={integ.is_metadata ? "amber" : integ.is_pipeline ? "green" : "violet"}>
              {integ.kind}
            </Badge>
            {integ.is_metadata && <Badge tone="green">metadata</Badge>}
            {integ.is_pipeline && <Badge tone="violet">pipeline</Badge>}
            {!integ.enabled && <Badge>disabled</Badge>}
          </div>
          <div className="truncate text-xs text-muted">{target}</div>
          <div className="text-xs text-muted">
            {integ.is_pipeline ? pipelineRole : `${integ.catalog_count} ${countLabel}`}
            {!integ.is_pipeline && integ.root_folder ? ` · ${integ.root_folder}` : ""}
            {integ.last_sync_at
              ? ` · ${integ.is_pipeline ? "checked" : "synced"} ${new Date(
                  integ.last_sync_at
                ).toLocaleString()}`
              : ""}
          </div>
          {integ.last_error && <div className="text-xs text-red-500">⚠ {integ.last_error}</div>}
        </div>
        <div className="flex shrink-0 items-center gap-1">
          <Button size="sm" variant="ghost" disabled={testM.isPending} onClick={() => testM.mutate()}>
            {testM.isPending ? "Testing…" : "Test"}
          </Button>
          <Button size="sm" variant="ghost" disabled={syncM.isPending} onClick={() => syncM.mutate()}>
            {syncM.isPending ? "Syncing…" : "Sync now"}
          </Button>
          <Button size="sm" variant="ghost" onClick={() => (editing ? setEditing(false) : startEdit())}>
            {editing ? "Close" : "Edit"}
          </Button>
          <Toggle checked={integ.enabled} onChange={(v) => toggle.mutate(v)} label="" />
          <Button
            size="sm"
            variant="danger"
            onClick={() => confirm(`Disconnect ${integ.name}?`) && del.mutate()}
          >
            ✕
          </Button>
        </div>
      </div>
      {editing && (
        <div className="mt-3 grid gap-2 rounded-lg border border-border bg-bg/40 p-2">
          <label className="text-xs text-muted">Name</label>
          <input className={efield} value={eName} onChange={(e) => setEName(e.target.value)} />
          {!integ.is_metadata || integ.kind === "ranobedb" ? (
            <>
              <label className="text-xs text-muted">Base URL</label>
              <input className={efield} value={eBase} onChange={(e) => setEBase(e.target.value)} />
            </>
          ) : null}
          <label className="text-xs text-muted">API key / token (leave blank to keep current)</label>
          <input
            className={efield}
            type="password"
            value={eKey}
            placeholder="•••••••• (unchanged)"
            onChange={(e) => setEKey(e.target.value)}
          />
          <label className="text-xs text-muted">
            Config (JSON — e.g. categories, library_path, required/ignored/preferred terms)
          </label>
          <textarea
            className={`${efield} font-mono`}
            rows={6}
            value={eConfig}
            onChange={(e) => setEConfig(e.target.value)}
          />
          {eErr && <p className="text-xs text-red-500">{eErr}</p>}
          <div className="flex gap-2">
            <Button size="sm" disabled={save.isPending} onClick={() => save.mutate()}>
              {save.isPending ? "Saving…" : "Save changes"}
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>
              Cancel
            </Button>
          </div>
        </div>
      )}
      {test && (
        <div className={`mt-2 text-xs ${test.ok ? "text-green-600" : "text-red-500"}`}>
          {test.ok
            ? `✓ ${test.app ?? "Connected"}${test.version ? ` v${test.version}` : ""}` +
              (test.detail ? ` · ${test.detail}` : "") +
              (test.root_folders.length ? ` · folders: ${test.root_folders.join(", ")}` : "")
            : `✗ ${test.error}`}
        </div>
      )}
    </div>
  );
}
