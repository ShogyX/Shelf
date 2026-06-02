import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  CatalogGroup,
  CatalogSource,
  IndexSite,
  IndexedPage,
  IndexSearchResult,
} from "../api/client";

function fmtDuration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="flex flex-col" title={hint}>
      <span className="text-lg font-semibold tabular-nums text-text">{value}</span>
      <span className="text-xs text-muted">{label}</span>
    </div>
  );
}

/** Aggregate crawl observability: titles found, requests, time, and site status mix. */
function CrawlStats() {
  const stats = useQuery({
    queryKey: ["index-stats"],
    queryFn: api.indexStats,
    refetchInterval: (q) => (q.state.data && q.state.data.sites_active > 0 ? 2500 : false),
  });
  const d = stats.data;
  if (!d) return null;
  return (
    <Card className="mb-4 p-4">
      <div className="mb-3 grid grid-cols-2 gap-4 sm:grid-cols-4">
        <Stat label="Titles found" value={(d.titles_found ?? 0).toLocaleString()} />
        <Stat
          label="Requests made"
          value={(d.requests_made ?? 0).toLocaleString()}
          hint="Pages requested (fetched + failed)"
        />
        <Stat
          label="Time spent"
          value={fmtDuration(d.time_spent_seconds ?? 0)}
          hint="Total crawl time, summed across all sites (parallel crawls each count)"
        />
        <Stat label="Words indexed" value={(d.words_indexed ?? 0).toLocaleString()} />
      </div>
      <div className="flex flex-wrap items-center gap-2 border-t border-border pt-3 text-xs">
        <span className="text-muted">Sites:</span>
        {d.sites_active > 0 && <Badge tone="violet">{d.sites_active} in-progress</Badge>}
        {d.sites_done > 0 && <Badge tone="green">{d.sites_done} complete</Badge>}
        {d.sites_paused > 0 && <Badge tone="amber">{d.sites_paused} aborted</Badge>}
        {d.sites_failed > 0 && <Badge tone="red">{d.sites_failed} error</Badge>}
        <span className="ml-auto text-muted">
          {d.pages_fetched.toLocaleString()} fetched · {d.pages_pending.toLocaleString()} queued ·{" "}
          {d.pages_failed.toLocaleString()} failed
        </span>
      </div>
    </Card>
  );
}
import { Badge, Button, Card, EmptyState, Spinner, Toggle } from "../components/ui";

function statusTone(s: string): "green" | "amber" | "violet" | "red" | "default" {
  return s === "active" ? "violet" : s === "done" ? "green" : s === "failed" ? "red" : "amber";
}

type Tone = "green" | "amber" | "violet" | "red" | "default";
export function healthBadge(h: string): { tone: Tone; label: string } | null {
  switch (h) {
    case "ok":
      return { tone: "green", label: "complete" };
    case "incomplete":
      return { tone: "amber", label: "incomplete" };
    case "no_chapters":
      return { tone: "red", label: "no chapters" };
    case "unreachable":
      return { tone: "red", label: "unreachable" };
    default:
      return null; // "unknown" → no badge
  }
}

function useDebounced<T>(value: T, ms = 250): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

export default function IndexPage() {
  const qc = useQueryClient();
  const [url, setUrl] = useState("");
  const [showAdv, setShowAdv] = useState(false);
  const [maxPages, setMaxPages] = useState(200);
  const [maxDepth, setMaxDepth] = useState(3);
  const [sameHost, setSameHost] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [query, setQuery] = useState("");
  const debounced = useDebounced(query.trim());
  const [siteFilter, setSiteFilter] = useState<number | null>(null);
  const [openPage, setOpenPage] = useState<number | null>(null);

  // Poll sites so crawl progress animates.
  const sites = useQuery({
    queryKey: ["index-sites"],
    queryFn: api.listIndexSites,
    refetchInterval: (q) =>
      (q.state.data ?? []).some((s) => s.status === "active") ? 2500 : false,
  });

  const search = useQuery({
    queryKey: ["index-search", debounced, siteFilter],
    queryFn: () => api.searchIndex(debounced, siteFilter ?? undefined, 40),
    enabled: debounced.length > 0,
  });

  const addSite = useMutation({
    mutationFn: () =>
      api.addIndexSite({
        url: url.trim(),
        max_pages: maxPages,
        max_depth: maxDepth,
        same_host_only: sameHost,
      }),
    onSuccess: () => {
      setUrl("");
      setError(null);
      qc.invalidateQueries({ queryKey: ["index-sites"] });
    },
    onError: (e) => setError((e as Error).message),
  });

  return (
    <main className="mx-auto max-w-5xl px-4 py-8">
      <h1 className="mb-1 text-2xl font-semibold">Index</h1>
      <p className="mb-6 text-sm text-muted">
        Point Shelf at a web location. It politely auto-crawls the section (obeying robots.txt and
        rate limits), indexes the readable text for fast search, and lets you read pages in-app or
        add any of them to your library.
      </p>

      {/* Add a site */}
      <Card className="mb-6 p-4">
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
        <button
          className="mt-2 text-xs text-muted underline"
          onClick={() => setShowAdv((s) => !s)}
        >
          {showAdv ? "Hide" : "Crawl options"}
        </button>
        {showAdv && (
          <div className="mt-3 grid gap-3 sm:grid-cols-3">
            <label className="text-sm">
              <span className="text-muted">Max pages</span>
              <input
                type="number"
                min={1}
                value={maxPages}
                onChange={(e) => setMaxPages(Number(e.target.value) || 1)}
                className="mt-1 w-full rounded-lg border border-border bg-bg px-3 py-1.5 text-sm"
              />
            </label>
            <label className="text-sm">
              <span className="text-muted">Max depth</span>
              <input
                type="number"
                min={0}
                value={maxDepth}
                onChange={(e) => setMaxDepth(Number(e.target.value) || 0)}
                className="mt-1 w-full rounded-lg border border-border bg-bg px-3 py-1.5 text-sm"
              />
            </label>
            <div className="flex items-end">
              <Toggle
                checked={sameHost}
                onChange={setSameHost}
                label="Same host only"
              />
            </div>
          </div>
        )}
        {error && <p className="mt-2 text-sm text-red-500">{error}</p>}
      </Card>

      {/* Discovered-works catalog (the searchable library of what crawling found). */}
      <CatalogSection />

      {/* Full-text page search */}
      <div className="mb-6">
        <div className="relative">
          <span className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted">
            🔍
          </span>
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search the full text of indexed pages…"
            className="w-full rounded-xl border border-border bg-surface py-3 pl-10 pr-3 text-base shadow-sm focus:border-accent focus:outline-none"
          />
        </div>
        {siteFilter != null && (
          <div className="mt-2 flex items-center gap-2 text-xs text-muted">
            Filtering to one site
            <button className="underline" onClick={() => setSiteFilter(null)}>
              clear
            </button>
          </div>
        )}
        {debounced && (
          <SearchResults
            q={debounced}
            result={search.data}
            loading={search.isFetching}
            onOpen={setOpenPage}
          />
        )}
      </div>

      {/* Crawl stats */}
      {(sites.data?.length ?? 0) > 0 && <CrawlStats />}

      {/* Sites */}
      {sites.isLoading ? (
        <Spinner label="Loading index…" />
      ) : (sites.data?.length ?? 0) === 0 ? (
        <EmptyState
          title="Nothing indexed yet"
          hint="Paste a URL above to start building a searchable, browsable index."
        />
      ) : (
        <div className="space-y-3">
          {sites.data!.map((s) => (
            <SiteCard
              key={s.id}
              site={s}
              activeFilter={siteFilter === s.id}
              onFilter={() => setSiteFilter(siteFilter === s.id ? null : s.id)}
              onOpenPage={setOpenPage}
            />
          ))}
        </div>
      )}

      {openPage != null && <PageReader pageId={openPage} onClose={() => setOpenPage(null)} />}
    </main>
  );
}

function CatalogSection() {
  const [query, setQuery] = useState("");
  const [live, setLive] = useState(false);
  const debounced = useDebounced(query.trim());
  const stats = useQuery({ queryKey: ["catalog-stats"], queryFn: api.catalogStats });
  const catalog = useQuery({
    queryKey: ["catalog", debounced, live],
    queryFn: () =>
      api.listCatalog(debounced || undefined, { limit: 60, live: live && !!debounced }),
    // While crawling is discovering works, keep the catalog fresh.
    refetchInterval: live ? false : 5000,
  });

  const groups = catalog.data ?? [];
  return (
    <section className="mb-8">
      <div className="mb-2 flex items-baseline justify-between gap-3">
        <h2 className="text-lg font-semibold">Discovered works</h2>
        {stats.data && (
          <span className="text-xs text-muted">
            {stats.data.titles.toLocaleString()} titles · {stats.data.sites} source
            {stats.data.sites === 1 ? "" : "s"}
            {stats.data.hooked > 0 && ` · ${stats.data.hooked} in library`}
          </span>
        )}
      </div>
      <p className="mb-3 text-sm text-muted">
        Books, novels and comics the crawler has recognized across your indexed sites. Search,
        then hook a title from any source to pull it into your library.
      </p>
      <div className="relative">
        <span className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted">
          📚
        </span>
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search discovered titles, authors, synopses…"
          className="w-full rounded-xl border border-border bg-surface py-3 pl-10 pr-3 text-base shadow-sm focus:border-accent focus:outline-none"
        />
      </div>
      <label className="mt-2 flex items-center gap-2 text-xs text-muted">
        <input type="checkbox" checked={live} onChange={(e) => setLive(e.target.checked)} />
        Also search connected libraries (Readarr / Kapowarr) live
      </label>

      {catalog.isLoading ? (
        <div className="mt-3"><Spinner label="Loading catalog…" /></div>
      ) : groups.length === 0 ? (
        <p className="mt-3 text-sm text-muted">
          {debounced
            ? `No discovered titles match “${debounced}”.`
            : "No works discovered yet — index a fiction site above and they'll appear here as the crawler finds them."}
        </p>
      ) : (
        <div className="mt-3 grid gap-3 sm:grid-cols-2">
          {groups.map((g) => (
            <CatalogCard key={g.norm_key || g.title} group={g} />
          ))}
        </div>
      )}
    </section>
  );
}

function CatalogCard({ group }: { group: CatalogGroup }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [error, setError] = useState<string | null>(null);
  const [pendingId, setPendingId] = useState<number | null>(null);

  const hook = useMutation({
    mutationFn: (catalogId: number) => api.hookCatalog(catalogId),
    onMutate: (catalogId) => {
      setPendingId(catalogId);
      setError(null);
    },
    onSuccess: (work) => {
      qc.invalidateQueries({ queryKey: ["works"] });
      qc.invalidateQueries({ queryKey: ["catalog"] });
      qc.invalidateQueries({ queryKey: ["catalog-stats"] });
      navigate(`/read/${work.id}`);
    },
    onError: (e) => setError((e as Error).message),
    onSettled: () => setPendingId(null),
  });

  const grab = useMutation({
    mutationFn: (catalogId: number) => api.grabCatalog(catalogId),
    onMutate: (catalogId) => {
      setPendingId(catalogId);
      setError(null);
    },
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["catalog"] });
      alert(r.message);
    },
    onError: (e) => setError((e as Error).message),
    onSettled: () => setPendingId(null),
  });

  const sources = group.sources;
  return (
    <Card className="flex gap-3 p-3">
      {group.cover_url ? (
        <img
          src={group.cover_url}
          alt=""
          loading="lazy"
          className="h-32 shrink-0 rounded-md border border-border object-cover"
          style={{ width: "5.5rem" }}
          onError={(e) => (e.currentTarget.style.display = "none")}
        />
      ) : null}
      <div className="min-w-0 flex-1">
        <div className="flex items-start justify-between gap-2">
          <div className="font-medium leading-tight text-text">{group.title}</div>
          {group.hooked_work_id && (
            <button
              className="shrink-0"
              onClick={() => navigate(`/read/${group.hooked_work_id}`)}
              title="Open in library"
            >
              <Badge tone="green">in library</Badge>
            </button>
          )}
        </div>
        {group.author && <div className="truncate text-xs text-muted">by {group.author}</div>}
        {group.chapters != null && (
          <div className="text-xs text-muted">{group.chapters.toLocaleString()} chapters</div>
        )}
        {group.synopsis && (
          <p className="mt-1 line-clamp-3 text-xs text-muted">{group.synopsis}</p>
        )}

        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          {sources.length > 1 && (
            <span className="text-[11px] uppercase tracking-wide text-muted">Source:</span>
          )}
          {sources.map((s) => (
            <SourceButton
              key={s.catalog_id}
              source={s}
              multi={sources.length > 1}
              busy={pendingId === s.catalog_id}
              disabled={hook.isPending || grab.isPending}
              onHook={() => hook.mutate(s.catalog_id)}
              onGrab={() => grab.mutate(s.catalog_id)}
              onOpen={(workId) => navigate(`/read/${workId}`)}
            />
          ))}
        </div>
        {error && <p className="mt-1 text-xs text-red-500">{error}</p>}
      </div>
    </Card>
  );
}

function SourceButton({
  source,
  multi,
  busy,
  disabled,
  onHook,
  onGrab,
  onOpen,
}: {
  source: CatalogSource;
  multi: boolean;
  busy: boolean;
  disabled: boolean;
  onHook: () => void;
  onGrab: () => void;
  onOpen: (workId: number) => void;
}) {
  const hb = healthBadge(source.health);
  const count = source.chapters_advertised ?? source.chapters_listed;
  if (source.hooked_work_id) {
    return (
      <Button size="sm" variant="ghost" onClick={() => onOpen(source.hooked_work_id!)}>
        Open ({source.domain})
      </Button>
    );
  }
  // Integration source (Readarr/Kapowarr): grab it there; Shelf imports the file once it
  // downloads into a watched folder.
  if (source.kind !== "online") {
    if (source.grab_status) {
      return (
        <span title={`Requested via ${source.kind}`}>
          <Badge tone="green">requested ({source.kind})</Badge>
        </span>
      );
    }
    return (
      <Button
        size="sm"
        variant="outline"
        disabled={disabled}
        onClick={onGrab}
        title={`Add + download via ${source.kind} (${source.domain})`}
      >
        {busy ? "Grabbing…" : `Grab via ${source.kind}`}
      </Button>
    );
  }
  const label = multi ? source.domain : "Add to library";
  return (
    <Button
      size="sm"
      variant={multi ? "outline" : "primary"}
      disabled={disabled}
      onClick={onHook}
      title={
        `Hook from ${source.domain}` +
        (count ? ` · ${count} chapters` : "") +
        (hb ? ` · ${hb.label}` : "")
      }
    >
      {busy ? "Adding…" : label}
      {multi && count ? <span className="ml-1 text-[11px] text-muted">{count}</span> : null}
    </Button>
  );
}

function SearchResults({
  q,
  result,
  loading,
  onOpen,
}: {
  q: string;
  result: IndexSearchResult[] | undefined;
  loading: boolean;
  onOpen: (id: number) => void;
}) {
  if (loading && !result) return <div className="mt-3"><Spinner label="Searching…" /></div>;
  if (!result) return null;
  if (result.length === 0)
    return <p className="mt-3 text-sm text-muted">No matches for “{q}”.</p>;
  return (
    <Card className="mt-3 divide-y divide-border">
      {result.map((r) => (
        <button
          key={r.page_id}
          onClick={() => onOpen(r.page_id)}
          className="flex w-full gap-3 px-4 py-3 text-left hover:bg-surface-2"
        >
          {r.cover_url && (
            <img
              src={r.cover_url}
              alt=""
              loading="lazy"
              className="h-20 w-14 shrink-0 rounded-md border border-border object-cover"
              onError={(e) => (e.currentTarget.style.display = "none")}
            />
          )}
          <div className="min-w-0 flex-1">
            <div className="font-medium text-text">{r.title || r.url}</div>
            {r.author && <div className="truncate text-xs text-muted">by {r.author}</div>}
            <div className="truncate text-xs text-muted">{r.url}</div>
            <div
              className="mt-1 text-sm text-muted [&_mark]:bg-accent/30 [&_mark]:text-text [&_mark]:rounded"
              dangerouslySetInnerHTML={{ __html: r.snippet }}
            />
          </div>
        </button>
      ))}
    </Card>
  );
}

function SiteCard({
  site,
  activeFilter,
  onFilter,
  onOpenPage,
}: {
  site: IndexSite;
  activeFilter: boolean;
  onFilter: () => void;
  onOpenPage: (id: number) => void;
}) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const pct = site.pages_total
    ? Math.round((site.pages_fetched / site.pages_total) * 100)
    : 0;

  const pages = useQuery({
    queryKey: ["index-pages", site.id],
    queryFn: () => api.listIndexPages(site.id, undefined, 200),
    enabled: open,
  });

  const act = (fn: () => Promise<unknown>) => async () => {
    await fn();
    qc.invalidateQueries({ queryKey: ["index-sites"] });
  };

  const hookAll = useMutation({
    mutationFn: () => api.hookIndexSite(site.id),
    onSuccess: (work) => {
      qc.invalidateQueries({ queryKey: ["works"] });
      qc.invalidateQueries({ queryKey: ["index-pages", site.id] });
      navigate(`/read/${work.id}`);
    },
  });

  return (
    <Card className="p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="truncate font-medium">{site.title || site.domain}</span>
            <Badge tone={statusTone(site.status)}>{site.status}</Badge>
          </div>
          <a
            href={site.root_url}
            target="_blank"
            rel="noreferrer"
            className="block truncate text-xs text-muted underline"
          >
            {site.root_url}
          </a>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-1">
          <Button size="sm" variant={activeFilter ? "primary" : "ghost"} onClick={onFilter}>
            {activeFilter ? "Searching here" : "Search here"}
          </Button>
          {site.status === "active" ? (
            <Button size="sm" variant="ghost" onClick={act(() => api.pauseIndexSite(site.id))}>
              Pause
            </Button>
          ) : (
            <Button size="sm" variant="ghost" onClick={act(() => api.resumeIndexSite(site.id))}>
              Resume
            </Button>
          )}
          <Button size="sm" variant="ghost" onClick={() => setOpen((o) => !o)}>
            {open ? "Hide" : "Browse"}
          </Button>
          <Button
            size="sm"
            variant="outline"
            title="Add every fetched page to your library as one work"
            disabled={site.pages_fetched === 0 || hookAll.isPending}
            onClick={() => hookAll.mutate()}
          >
            {hookAll.isPending ? "Adding…" : "+ Library"}
          </Button>
          <Button size="sm" variant="danger" onClick={act(() => api.deleteIndexSite(site.id))}>
            ✕
          </Button>
        </div>
      </div>

      <div className="mt-3">
        <div className="flex justify-between text-xs text-muted">
          <span>
            {site.pages_fetched} / {site.pages_total} pages
            {site.pages_pending > 0 && ` · ${site.pages_pending} queued`}
            {site.pages_failed > 0 && ` · ${site.pages_failed} failed`}
          </span>
          <span>{site.words.toLocaleString()} words</span>
        </div>
        <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-surface-2">
          <div className="h-full rounded-full bg-accent transition-all" style={{ width: `${pct}%` }} />
        </div>
        <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-0.5 text-xs text-muted">
          <span>{site.titles_found ?? 0} title{(site.titles_found ?? 0) === 1 ? "" : "s"} found</span>
          <span>· {(site.requests ?? 0).toLocaleString()} requests</span>
          <span>
            · {fmtDuration(site.duration_seconds ?? 0)}
            {site.status === "active" ? " (running)" : ""}
          </span>
        </div>
      </div>

      {open && (
        <div className="mt-3 max-h-80 overflow-y-auto rounded-lg border border-border">
          {pages.isLoading ? (
            <div className="p-3"><Spinner label="Loading pages…" /></div>
          ) : (pages.data?.length ?? 0) === 0 ? (
            <p className="p-3 text-sm text-muted">No pages yet.</p>
          ) : (
            <ul className="divide-y divide-border">
              {pages.data!.map((p) => (
                <PageRow key={p.id} page={p} onOpen={() => onOpenPage(p.id)} />
              ))}
            </ul>
          )}
        </div>
      )}
    </Card>
  );
}

function PageRow({ page, onOpen }: { page: IndexedPage; onOpen: () => void }) {
  return (
    <li className="flex items-center justify-between gap-2 px-3 py-2 hover:bg-surface-2">
      <button
        onClick={onOpen}
        className="flex min-w-0 flex-1 gap-3 text-left"
        disabled={page.status !== "fetched"}
      >
        {page.cover_url && (
          <img
            src={page.cover_url}
            alt=""
            loading="lazy"
            className="h-16 w-11 shrink-0 rounded border border-border object-cover"
            onError={(e) => (e.currentTarget.style.display = "none")}
          />
        )}
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-medium text-text">{page.title || page.url}</div>
          {page.author && <div className="truncate text-xs text-muted">by {page.author}</div>}
          {page.description ? (
            <div className="line-clamp-2 text-xs text-muted">{page.description}</div>
          ) : (
            <div className="truncate text-xs text-muted">{page.url}</div>
          )}
        </div>
      </button>
      <div className="flex shrink-0 items-center gap-2">
        {page.status !== "fetched" && (
          <Badge tone={page.status === "failed" ? "red" : "amber"}>{page.status}</Badge>
        )}
        {page.hooked_work_id && <Badge tone="green">in library</Badge>}
      </div>
    </li>
  );
}

function PageReader({ pageId, onClose }: { pageId: number; onClose: () => void }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const page = useQuery({ queryKey: ["index-page", pageId], queryFn: () => api.getIndexPage(pageId) });
  const hook = useMutation({
    mutationFn: () => api.hookIndexPage(pageId),
    onSuccess: (work) => {
      qc.invalidateQueries({ queryKey: ["works"] });
      qc.invalidateQueries({ queryKey: ["index-pages"] });
      navigate(`/read/${work.id}`);
    },
  });

  return (
    <div className="fixed inset-0 z-50 flex justify-center overflow-y-auto bg-black/50 p-0 sm:p-6">
      <div className="relative h-full w-full max-w-3xl overflow-y-auto bg-surface sm:h-auto sm:rounded-2xl sm:shadow-2xl">
        <div className="sticky top-0 z-10 flex items-center justify-between gap-2 border-b border-border bg-surface/95 px-4 py-3 backdrop-blur">
          <div className="min-w-0">
            <div className="truncate font-medium">{page.data?.title || "Reading…"}</div>
            {page.data && (
              <a
                href={page.data.url}
                target="_blank"
                rel="noreferrer"
                className="truncate text-xs text-muted underline"
              >
                {page.data.domain || page.data.url}
              </a>
            )}
          </div>
          <div className="flex shrink-0 items-center gap-1">
            <Button
              size="sm"
              variant="primary"
              disabled={!page.data || hook.isPending || !!page.data.hooked_work_id}
              onClick={() => hook.mutate()}
            >
              {page.data?.hooked_work_id ? "In library" : hook.isPending ? "Adding…" : "Add to library"}
            </Button>
            <Button size="sm" variant="ghost" onClick={onClose}>
              ✕
            </Button>
          </div>
        </div>
        <div className="px-5 py-6">
          {page.isLoading ? (
            <Spinner label="Loading…" />
          ) : (
            <>
              {(page.data?.cover_url || page.data?.description) && (
                <div className="mb-5 flex gap-4 rounded-xl border border-border bg-surface-2/50 p-4">
                  {page.data?.cover_url && (
                    <img
                      src={page.data.cover_url}
                      alt=""
                      className="h-32 w-24 shrink-0 rounded-md border border-border object-cover"
                      onError={(e) => (e.currentTarget.style.display = "none")}
                    />
                  )}
                  <div className="min-w-0">
                    {page.data?.author && (
                      <div className="text-sm text-muted">by {page.data.author}</div>
                    )}
                    {page.data?.site_name && (
                      <div className="text-xs text-muted">{page.data.site_name}</div>
                    )}
                    {page.data?.description && (
                      <p className="mt-1 text-sm text-text">{page.data.description}</p>
                    )}
                  </div>
                </div>
              )}
              <article
                className="reader-prose mx-auto"
                dangerouslySetInnerHTML={{ __html: page.data?.html || "<p>(no content)</p>" }}
              />
            </>
          )}
        </div>
      </div>
    </div>
  );
}
