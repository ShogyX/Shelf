import { useEffect, useRef, useState } from "react";
import {
  keepPreviousData,
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { api, CatalogGroup, IndexSearchResult } from "../api/client";
import { Button, Card, Spinner } from "../components/ui";
import { useConfirm } from "../components/confirm";
import { PageReader } from "../components/IndexShared";
import { CatalogCard, CatalogDetail } from "../components/catalog/CatalogCard";
import { CatalogRows } from "../components/catalog/CatalogRows";
import ShelfDestination from "../components/ShelfDestination";
import { useApp } from "../store";
import { useHasPermission } from "../auth";

function useDebounced<T>(value: T, ms = 250): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

export default function IndexPage() {
  return (
    <main className="mx-auto max-w-7xl px-4 py-8">
      <h1 className="mb-1 text-2xl font-semibold">Index</h1>
      <p className="mb-6 text-sm text-muted">
        Browse and search everything the crawler has discovered, and add a title to your library
        with one click. New sites to crawl are added by an admin on the{" "}
        <span className="text-text">Sources</span> page.
      </p>

      {/* Discovered-works catalog — the prominent, searchable library of what crawling found. */}
      <CatalogSection />
    </main>
  );
}

type SearchMode = "titles" | "fulltext";
const ALL = "__all__";

function CatalogSection() {
  const qc = useQueryClient();
  const confirm = useConfirm();
  const toast = useApp((s) => s.toast);
  const canHook = useHasPermission("index.hook");
  const canAcquire = useHasPermission("index.acquire");
  const canRoute = canHook || canAcquire;
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState<SearchMode>("titles"); // titles & authors | full page text
  const [live, setLive] = useState(false);
  const [detail, setDetail] = useState<CatalogGroup | null>(null);
  const [openPage, setOpenPage] = useState<number | null>(null);
  const [mediaFilter, setMediaFilter] = useState<string>(ALL);
  const [sourceFilter, setSourceFilter] = useState<string>(ALL);
  const [sortBy, setSortBy] = useState<"relevance" | "chapters" | "title">("relevance");
  const debounced = useDebounced(query.trim());
  // Idle = no search/filter active → show the curated discovery rows instead of a flat grid.
  const idle = mode === "titles" && !debounced && mediaFilter === ALL && sourceFilter === ALL;
  const stats = useQuery({ queryKey: ["catalog-stats"], queryFn: api.catalogStats });
  // Complete filter options (all media types + source domains) from the whole catalog —
  // NOT just the loaded page, so low-ranked types/sources (e.g. Gutenberg books) appear.
  const facets = useQuery({ queryKey: ["catalog-facets"], queryFn: api.catalogFacets });
  const mediaOptions = facets.data?.media ?? [];
  const sourceOptions = facets.data?.domains ?? [];

  const purge = useMutation({
    mutationFn: () => api.purgeBroken(true),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["catalog"] });
      qc.invalidateQueries({ queryKey: ["catalog-stats"] });
      toast(
        r.removed > 0
          ? `Removed and blocked ${r.removed} broken ${r.removed === 1 ? "entry" : "entries"}.`
          : "No broken entries to clean up.",
        "success"
      );
    },
    onError: (e) => toast((e as Error).message, "error"),
  });

  // Filtering + sorting are applied server-side; results are paged and loaded lazily on scroll
  // (the catalog can hold thousands of titles — fetching/rendering them all at once is slow).
  const PAGE = 60;
  const catalog = useInfiniteQuery({
    queryKey: ["catalog", debounced, live, mediaFilter, sourceFilter, sortBy],
    initialPageParam: 0,
    queryFn: ({ pageParam }) =>
      api.listCatalog(debounced || undefined, {
        limit: PAGE,
        offset: pageParam,
        live: live && !!debounced,
        media: mediaFilter !== ALL ? mediaFilter : undefined,
        domain: sourceFilter !== ALL ? sourceFilter : undefined,
        sort: sortBy,
      }),
    getNextPageParam: (lastPage, allPages) =>
      lastPage.length < PAGE ? undefined : allPages.length * PAGE,
    enabled: mode === "titles" && !idle,
    // Keep showing previous results while a filter change refetches, so the grid doesn't flash.
    placeholderData: keepPreviousData,
    staleTime: 3000,
  });
  // Auto-load the next page when the sentinel scrolls into view.
  const sentinel = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const el = sentinel.current;
    if (!el) return;
    const obs = new IntersectionObserver(
      (entries) => {
        if (entries[0].isIntersecting && catalog.hasNextPage && !catalog.isFetchingNextPage)
          catalog.fetchNextPage();
      },
      { rootMargin: "600px" }
    );
    obs.observe(el);
    return () => obs.disconnect();
  }, [catalog.hasNextPage, catalog.isFetchingNextPage, catalog.fetchNextPage]);
  // Full-text search over the indexed page bodies (the old standalone search, now a mode).
  const search = useQuery({
    queryKey: ["index-search", debounced],
    queryFn: () => api.searchIndex(debounced, undefined, 40),
    enabled: mode === "fulltext" && debounced.length > 0,
  });

  const groups = catalog.data?.pages.flat() ?? [];

  const selCls =
    "rounded-lg border border-border bg-surface px-2 py-1.5 text-xs text-text focus:border-accent focus:outline-none";

  return (
    <section className="mb-8">
      <div className="mb-2 flex items-baseline justify-between gap-3">
        <h2 className="text-lg font-semibold">Discovered works</h2>
        <div className="flex items-baseline gap-3">
          {stats.data && (
            <span className="text-xs text-muted">
              {stats.data.titles.toLocaleString()} titles · {stats.data.sites} source
              {stats.data.sites === 1 ? "" : "s"}
              {stats.data.hooked > 0 && ` · ${stats.data.hooked} in library`}
            </span>
          )}
          <Button
            size="sm"
            variant="ghost"
            className="shrink-0"
            disabled={purge.isPending}
            title="Remove every broken, un-hooked discovered work and block them from re-adding"
            onClick={async () => {
              if (
                await confirm({
                  title: "Clean up broken titles",
                  message:
                    "Remove all broken (incomplete / no-chapters / unreachable) discovered works that aren't in your library, and block them from being re-added?",
                  danger: true,
                  confirmText: "Remove & block",
                })
              )
                purge.mutate();
            }}
          >
            {purge.isPending ? "Cleaning…" : "🧹 Clean up broken"}
          </Button>
        </div>
      </div>

      {/* One search bar; a mode toggle switches between matching titles/authors and the full
          text of indexed pages. */}
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
        <div className="relative flex-1">
          <span className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted">
            {mode === "titles" ? "📚" : "🔍"}
          </span>
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={
              mode === "titles"
                ? "Search discovered titles, authors, synopses…"
                : "Search the full text of indexed pages…"
            }
            className="w-full rounded-xl border border-border bg-surface py-3 pl-10 pr-3 text-base shadow-sm focus:border-accent focus:outline-none"
          />
        </div>
        <div className="inline-flex shrink-0 overflow-hidden rounded-lg border border-border text-sm">
          <button
            className={`px-3 py-2 ${mode === "titles" ? "bg-accent text-white" : "bg-surface text-muted"}`}
            onClick={() => setMode("titles")}
          >
            Titles
          </button>
          <button
            className={`px-3 py-2 ${mode === "fulltext" ? "bg-accent text-white" : "bg-surface text-muted"}`}
            onClick={() => setMode("fulltext")}
            title="Search inside the full text of every indexed page"
          >
            Full text
          </button>
        </div>
        {canRoute && <ShelfDestination className="shrink-0" />}
      </div>

      {mode === "titles" ? (
        <>
          {/* Sort + filter by media type and source — only while searching. The main discovery
              view organizes itself by category (with per-user toggles), so these legacy controls
              would just clutter it; they belong to an active search (or the Browse page). */}
          {!idle && (
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <select className={selCls} value={mediaFilter} onChange={(e) => setMediaFilter(e.target.value)}>
              <option value={ALL}>All categories</option>
              {mediaOptions.map((m) => (
                <option key={m} value={m}>{m}</option>
              ))}
            </select>
            <select className={selCls} value={sourceFilter} onChange={(e) => setSourceFilter(e.target.value)}>
              <option value={ALL}>All sources</option>
              {sourceOptions.map((s) => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
            <select className={selCls} value={sortBy} onChange={(e) => setSortBy(e.target.value as typeof sortBy)}>
              <option value="relevance">Sort: relevance</option>
              <option value="chapters">Sort: most chapters</option>
              <option value="title">Sort: title A–Z</option>
            </select>
            <label className="ml-auto flex items-center gap-2 text-xs text-muted">
              <input type="checkbox" checked={live} onChange={(e) => setLive(e.target.checked)} />
              Also search Readarr / Kapowarr live
            </label>
          </div>
          )}

          {idle ? (
            <CatalogRows onOpenDetail={setDetail} />
          ) : catalog.isLoading ? (
            <div className="mt-3"><Spinner label="Loading catalog…" /></div>
          ) : groups.length === 0 ? (
            <p className="mt-3 text-sm text-muted">
              No discovered titles match your search / filters.
            </p>
          ) : (
            <>
              <div className="mt-3 grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
                {groups.map((g) => (
                  <CatalogCard key={g.id || g.norm_key || g.title} group={g} onOpenDetail={() => setDetail(g)} />
                ))}
              </div>
              {/* Infinite-scroll sentinel + manual fallback. */}
              <div ref={sentinel} className="h-8" />
              {catalog.isFetchingNextPage && (
                <div className="mt-2"><Spinner label="Loading more…" /></div>
              )}
              {catalog.hasNextPage && !catalog.isFetchingNextPage && (
                <div className="mt-3 flex justify-center">
                  <Button variant="outline" onClick={() => catalog.fetchNextPage()}>
                    Load more
                  </Button>
                </div>
              )}
            </>
          )}
        </>
      ) : !debounced ? (
        <p className="mt-3 text-sm text-muted">Type to search the full text of indexed pages.</p>
      ) : (
        <SearchResults
          q={debounced}
          result={search.data}
          loading={search.isFetching}
          onOpen={setOpenPage}
        />
      )}

      {detail && <CatalogDetail group={detail} onClose={() => setDetail(null)} />}
      {openPage != null && <PageReader pageId={openPage} onClose={() => setOpenPage(null)} />}
    </section>
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
