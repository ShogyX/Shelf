import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, Source } from "../api/client";
import { Badge, Button, Card, Spinner, Toggle } from "../components/ui";
import { useState } from "react";

function SourceRow({ source }: { source: Source }) {
  const qc = useQueryClient();
  const [interval, setInterval] = useState(source.min_request_interval_s);
  const [daily, setDaily] = useState(source.max_daily_requests);
  const [token, setToken] = useState("");
  const [tokenSaved, setTokenSaved] = useState(false);

  const update = useMutation({
    mutationFn: (patch: Partial<Source>) => api.updateSource(source.id, patch),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["sources"] }),
  });

  return (
    <Card className="p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <h3 className="font-semibold">{source.display_name}</h3>
            <Badge tone="violet">{source.license_basis}</Badge>
          </div>
          <p className="text-xs text-muted">{source.base_url ?? "no network (local)"}</p>
        </div>
        <Toggle
          checked={source.tos_permitted}
          label={source.tos_permitted ? "Permitted" : "Disabled"}
          onChange={(v) => {
            if (v && !confirm(`Enable "${source.display_name}"? Only do this for sources you are permitted to read.`)) return;
            update.mutate({ tos_permitted: v });
          }}
        />
      </div>

      <div className="mt-4 grid grid-cols-2 gap-4 sm:grid-cols-4">
        <label className="text-xs text-muted">
          Min interval (s)
          <input
            type="number"
            min={0}
            step={0.5}
            value={interval}
            onChange={(e) => setInterval(parseFloat(e.target.value))}
            onBlur={() => update.mutate({ min_request_interval_s: interval })}
            className="mt-1 w-full rounded-lg border border-border bg-bg px-2 py-1 text-sm text-text"
          />
        </label>
        <label className="text-xs text-muted">
          Max daily requests
          <input
            type="number"
            min={0}
            value={daily}
            onChange={(e) => setDaily(parseInt(e.target.value))}
            onBlur={() => update.mutate({ max_daily_requests: daily })}
            className="mt-1 w-full rounded-lg border border-border bg-bg px-2 py-1 text-sm text-text"
          />
          <span className="mt-1 block text-[11px] text-muted">
            {daily === 0
              ? "0 = unlimited — only the interval throttles"
              : "set 0 for unlimited (interval-only)"}
          </span>
        </label>
        <div className="text-xs text-muted">
          robots.txt
          <div className="mt-1">
            <Toggle
              checked={source.robots_respected}
              label={source.robots_respected ? "respected" : "ignored"}
              onChange={(v) => {
                if (!v && !confirm(
                  `Ignore robots.txt for "${source.display_name}"?\n\nOnly for dev/troubleshooting on sources you are permitted to read.`
                )) return;
                update.mutate({ robots_respected: v });
              }}
            />
          </div>
        </div>
        <div className="text-xs text-muted">
          Headless browser
          <div className="mt-1">
            <Toggle
              checked={source.render_js}
              label={source.render_js ? "render JS" : "plain HTTP"}
              onChange={(v) => {
                if (v && !confirm(
                  `Render "${source.display_name}" with a headless browser?\n\n` +
                  `Slower and heavier — use for JS-heavy sites you are permitted to read.`
                )) return;
                update.mutate({ render_js: v });
              }}
            />
          </div>
        </div>
        <div className="text-xs text-muted">
          Status
          <div className="mt-1">
            <Badge tone={source.tos_permitted ? "green" : "red"}>
              {source.tos_permitted ? "ingest allowed" : "gate closed"}
            </Badge>
          </div>
        </div>
      </div>

      {/* Members-only sources (e.g. J-Novel): provide an access token to fetch content you own. */}
      {source.supports_auth && (
        <div className="mt-4 border-t border-border pt-3">
          <div className="mb-1 flex items-center gap-2 text-xs text-muted">
            Access token (members-only content)
            <Badge tone={source.has_auth ? "green" : "amber"}>
              {source.has_auth ? "saved" : "not set"}
            </Badge>
          </div>
          <div className="flex items-center gap-2">
            <input
              type="password"
              autoComplete="off"
              value={token}
              onChange={(e) => { setToken(e.target.value); setTokenSaved(false); }}
              placeholder={source.has_auth ? "•••••••• (leave blank to keep)" : "paste your account access token"}
              className="w-full rounded-lg border border-border bg-bg px-2 py-1 text-sm text-text"
            />
            <Button
              size="sm"
              variant="primary"
              disabled={!token.trim() || update.isPending}
              onClick={() => update.mutate({ auth_token: token.trim() } as Partial<Source>,
                { onSuccess: () => { setToken(""); setTokenSaved(true); } })}
            >
              Save
            </Button>
            {source.has_auth && (
              <Button
                size="sm"
                variant="ghost"
                title="Remove the stored token"
                disabled={update.isPending}
                onClick={() => update.mutate({ auth_token: "" } as Partial<Source>)}
              >
                Clear
              </Button>
            )}
          </div>
          {tokenSaved && <p className="mt-1 text-[11px] text-green-600">Token saved.</p>}
          <p className="mt-1 text-[11px] text-muted">
            Stored on the server and never returned by the API. Used only to fetch content your
            account is entitled to. For J-Novel, this is your account access token.
          </p>
        </div>
      )}
    </Card>
  );
}

/** Submit a web location for the crawler to auto-index. Moved here from the Index page so only
 *  admins (who can see Sources) can start new crawls; everyone else browses what's discovered. */
function IndexSiteForm() {
  const qc = useQueryClient();
  const [url, setUrl] = useState("");
  const [updateIndexed, setUpdateIndexed] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const addSite = useMutation({
    mutationFn: () => api.addIndexSite({ url: url.trim(), update_indexed: updateIndexed }),
    onSuccess: () => {
      setUrl("");
      setError(null);
      qc.invalidateQueries({ queryKey: ["index-sites"] });
    },
    onError: (e) => setError((e as Error).message),
  });

  return (
    <Card className="mb-6 p-4">
      <div className="mb-2 text-sm font-semibold">Index a site</div>
      <div className="flex flex-col gap-2 sm:flex-row">
        <input
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && url.trim() && addSite.mutate()}
          placeholder="https://example.com/section-to-index"
          className="w-full rounded-lg border border-border bg-bg px-3 py-2 text-sm"
        />
        <Button
          variant="primary"
          disabled={!url.trim() || addSite.isPending}
          onClick={() => addSite.mutate()}
        >
          {addSite.isPending ? "Starting…" : "Index"}
        </Button>
      </div>
      <p className="mt-2 text-xs text-muted">
        Crawls run with no page cap and stop once they stop finding new titles. Watch progress on{" "}
        <span className="text-text">Jobs</span>; discovered titles appear on the{" "}
        <span className="text-text">Index</span> page for everyone.
      </p>
      <label className="mt-2 flex items-center gap-2 text-xs text-muted">
        <input
          type="checkbox"
          checked={updateIndexed}
          onChange={(e) => setUpdateIndexed(e.target.checked)}
        />
        Update already-indexed content (re-fetch pages crawled before). Off by default:
        re-adding a source resumes without repeating what was already indexed.
      </label>
      {error && <p className="mt-2 text-sm text-red-500">{error}</p>}
    </Card>
  );
}

export default function Sources() {
  const sources = useQuery({ queryKey: ["sources"], queryFn: api.listSources });

  return (
    <main className="mx-auto max-w-3xl px-4 py-8">
      <h1 className="mb-1 text-2xl font-semibold">Sources</h1>
      <p className="mb-6 text-sm text-muted">
        Each source carries a compliance declaration. The engine refuses to ingest any source that is
        not explicitly permitted — toggle a source on only for content you have the right to read.
      </p>

      <IndexSiteForm />

      {sources.isLoading && <Spinner label="Loading sources…" />}
      <div className="space-y-3">
        {sources.data?.map((s) => (
          <SourceRow key={s.id} source={s} />
        ))}
      </div>
    </main>
  );
}
