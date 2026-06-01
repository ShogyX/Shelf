import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  IndexSite,
  IndexedPage,
  IndexSearchResult,
} from "../api/client";
import { Badge, Button, Card, EmptyState, Spinner, Toggle } from "../components/ui";

function statusTone(s: string): "green" | "amber" | "violet" | "red" | "default" {
  return s === "active" ? "violet" : s === "done" ? "green" : s === "failed" ? "red" : "amber";
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

      {/* Search */}
      <div className="mb-6">
        <div className="relative">
          <span className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted">
            🔍
          </span>
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search indexed content…"
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
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="truncate font-medium">{site.title || site.domain}</span>
            <Badge tone={statusTone(site.status)}>{site.status}</Badge>
          </div>
          <a
            href={site.root_url}
            target="_blank"
            rel="noreferrer"
            className="truncate text-xs text-muted underline"
          >
            {site.root_url}
          </a>
        </div>
        <div className="flex shrink-0 items-center gap-1">
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
