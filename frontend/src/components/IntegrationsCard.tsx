import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, Integration, IntegrationKind, IntegrationTest } from "../api/client";
import { Badge, Button, Card, Spinner, Toggle } from "./ui";

const input = "w-full rounded-lg border border-border bg-bg px-3 py-2 text-sm";

const METADATA_KINDS: IntegrationKind[] = ["ranobedb", "goodreads", "googlebooks"];
const isMetadata = (k: IntegrationKind) => METADATA_KINDS.includes(k);

export default function IntegrationsCard() {
  const qc = useQueryClient();
  const integs = useQuery({ queryKey: ["integrations"], queryFn: api.listIntegrations });

  const [kind, setKind] = useState<IntegrationKind>("readarr");
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [autoMap, setAutoMap] = useState(true);
  const [userId, setUserId] = useState(""); // goodreads numeric user id
  const [shelf, setShelf] = useState("to-read"); // goodreads shelf
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
      if (kind === "ranobedb")
        return api.addIntegration({ kind, base_url: baseUrl.trim() });
      if (kind === "googlebooks")
        return api.addIntegration({ kind, api_key: apiKey.trim() });
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
      setError(null);
      invalidate();
    },
    onError: (e) => setError((e as Error).message),
  });

  const meta = isMetadata(kind);
  const canSubmit =
    kind === "ranobedb" || kind === "googlebooks"
      ? true
      : kind === "goodreads"
        ? !!userId.trim()
        : !!baseUrl.trim() && !!apiKey.trim();

  return (
    <Card className="mb-4 p-4">
      <h2 className="mb-1 font-semibold">Integrations</h2>
      <p className="mb-3 text-sm text-muted">
        Connect <b>download managers</b> (Readarr / Kapowarr) to fill the index with their
        libraries, or <b>metadata providers</b> (RanobeDB / Google Books) that become the source of
        truth for author, synopsis, cover &amp; release count, detect new releases, and surface
        related titles. (Goodreads is per-user — connect your own shelf in{" "}
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
          <optgroup label="Metadata providers">
            <option value="ranobedb">RanobeDB — light-novel metadata</option>
            <option value="googlebooks">Google Books — broad book metadata</option>
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

  const countLabel = integ.is_metadata ? "linked" : "in catalog";
  const metaTarget =
    integ.kind === "goodreads"
      ? `shelf: ${integ.config?.shelf ?? "to-read"}`
      : integ.kind === "googlebooks"
        ? integ.base_url || "googleapis.com/books"
        : integ.base_url || "ranobedb.org";
  const target = integ.is_metadata ? metaTarget : integ.base_url;

  return (
    <div className="rounded-lg border border-border p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="truncate font-medium">{integ.name}</span>
            <Badge tone={integ.is_metadata ? "amber" : "violet"}>{integ.kind}</Badge>
            {integ.is_metadata && <Badge tone="green">metadata</Badge>}
            {!integ.enabled && <Badge>disabled</Badge>}
          </div>
          <div className="truncate text-xs text-muted">{target}</div>
          <div className="text-xs text-muted">
            {integ.catalog_count} {countLabel}
            {integ.root_folder ? ` · ${integ.root_folder}` : ""}
            {integ.last_sync_at
              ? ` · synced ${new Date(integ.last_sync_at).toLocaleString()}`
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
