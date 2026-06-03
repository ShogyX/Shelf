import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  keepPreviousData,
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { api, CatalogGroup, CatalogSource, IndexSearchResult } from "../api/client";
import { Badge, Button, Card, Spinner } from "../components/ui";
import { healthBadge, PageReader, Tone } from "../components/IndexShared";
import { useApp } from "../store";

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
  const toast = useApp((s) => s.toast);
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState<SearchMode>("titles"); // titles & authors | full page text
  const [live, setLive] = useState(false);
  const [detail, setDetail] = useState<CatalogGroup | null>(null);
  const [openPage, setOpenPage] = useState<number | null>(null);
  const [mediaFilter, setMediaFilter] = useState<string>(ALL);
  const [sourceFilter, setSourceFilter] = useState<string>(ALL);
  const [sortBy, setSortBy] = useState<"relevance" | "chapters" | "title">("relevance");
  const debounced = useDebounced(query.trim());
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
    enabled: mode === "titles",
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
          <button
            className="shrink-0 text-xs text-muted underline hover:text-text disabled:opacity-50"
            disabled={purge.isPending}
            title="Remove every broken, un-hooked discovered work and block them from re-adding"
            onClick={() => {
              if (confirm("Remove all broken (incomplete / no-chapters / unreachable) discovered works that aren't in your library, and block them from being re-added?"))
                purge.mutate();
            }}
          >
            {purge.isPending ? "Cleaning…" : "Clean up broken"}
          </button>
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
      </div>

      {mode === "titles" ? (
        <>
          {/* Sort + filter by media type and source. */}
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <select className={selCls} value={mediaFilter} onChange={(e) => setMediaFilter(e.target.value)}>
              <option value={ALL}>All types</option>
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

          {catalog.isLoading ? (
            <div className="mt-3"><Spinner label="Loading catalog…" /></div>
          ) : groups.length === 0 ? (
            <p className="mt-3 text-sm text-muted">
              {debounced || mediaFilter !== ALL || sourceFilter !== ALL
                ? "No discovered titles match your search / filters."
                : "No works discovered yet — index a fiction site above and they'll appear here as the crawler finds them."}
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

function mediaTone(label: string): Tone {
  switch (label) {
    case "Manga":
    case "Webtoon":
    case "Comic":
      return "violet";
    case "Book":
      return "amber";
    default:
      return "default"; // Novel
  }
}

function CatalogCard({ group, onOpenDetail }: { group: CatalogGroup; onOpenDetail: () => void }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [error, setError] = useState<string | null>(null);
  const [pendingId, setPendingId] = useState<number | null>(null);
  // Non-blocking hook: show a processing → done message in place; never yank the user away.
  const [doneWorkId, setDoneWorkId] = useState<number | null>(null);

  const hook = useMutation({
    mutationFn: (catalogId: number) => api.hookCatalog(catalogId),
    onMutate: (catalogId) => {
      setPendingId(catalogId);
      setError(null);
      setDoneWorkId(null);
    },
    onSuccess: (work) => {
      qc.invalidateQueries({ queryKey: ["works"] });
      qc.invalidateQueries({ queryKey: ["catalog"] });
      qc.invalidateQueries({ queryKey: ["catalog-stats"] });
      setDoneWorkId(work.id);
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
      setError(null);
      setDoneWorkId(-1); // sentinel: a grab was queued (message shown below)
    },
    onError: (e) => setError((e as Error).message),
    onSettled: () => setPendingId(null),
  });

  const sources = group.sources;
  const busyAny = hook.isPending || grab.isPending;
  return (
    <Card className="flex gap-4 p-4">
      {group.cover_url ? (
        <button onClick={onOpenDetail} className="shrink-0" title="View details & all sources">
          <img
            src={group.cover_url}
            alt=""
            loading="lazy"
            className="h-44 rounded-md border border-border object-cover"
            style={{ width: "7.5rem" }}
            onError={(e) => (e.currentTarget.style.display = "none")}
          />
        </button>
      ) : null}
      <div className="min-w-0 flex-1">
        <div className="flex items-start justify-between gap-2">
          <button
            onClick={onOpenDetail}
            className="text-left text-base font-semibold leading-tight text-text hover:text-accent hover:underline"
            title="View details & all sources"
          >
            {group.title}
          </button>
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
        <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-muted">
          <Badge tone={mediaTone(group.media_label)}>{group.media_label}</Badge>
          {group.author && <span className="truncate">by {group.author}</span>}
          {group.chapters != null && <span>· {group.chapters.toLocaleString()} ch</span>}
        </div>
        {group.synopsis && (
          <p className="mt-1.5 line-clamp-3 text-sm text-muted">{group.synopsis}</p>
        )}

        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          {sources.length > 1 && (
            <span className="text-[11px] uppercase tracking-wide text-muted">
              {sources.length} sources:
            </span>
          )}
          {sources.map((s) => (
            <SourceButton
              key={s.catalog_id}
              source={s}
              multi={sources.length > 1}
              busy={pendingId === s.catalog_id}
              disabled={busyAny}
              onHook={() => hook.mutate(s.catalog_id)}
              onGrab={() => grab.mutate(s.catalog_id)}
              onOpen={(workId) => navigate(`/read/${workId}`)}
            />
          ))}
        </div>
        {busyAny && <p className="mt-1.5 text-xs text-accent">Adding to your library…</p>}
        {doneWorkId != null && doneWorkId > 0 && (
          <p className="mt-1.5 text-xs text-green-600">
            Added to your library ✓{" "}
            <button className="underline" onClick={() => navigate(`/read/${doneWorkId}`)}>
              Open
            </button>
          </p>
        )}
        {doneWorkId === -1 && (
          <p className="mt-1.5 text-xs text-green-600">
            Queued — it'll appear once downloaded into a watched folder.
          </p>
        )}
        {error && <p className="mt-1 text-xs text-red-500">Couldn't add: {error}</p>}
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
  // Mark each source with what it is (Novel / Book / Manga / Webtoon / Comic) + its domain,
  // so a multi-source card makes clear whether you're hooking the novel or the manga.
  const label = multi ? `${source.media_label} · ${source.domain}` : "Add to library";
  return (
    <Button
      size="sm"
      variant={multi ? "outline" : "primary"}
      disabled={disabled}
      onClick={onHook}
      title={
        `Hook the ${source.media_label} from ${source.domain}` +
        (count ? ` · ${count} chapters` : "") +
        (hb ? ` · ${hb.label}` : "")
      }
    >
      {busy ? "Adding…" : label}
      {multi && count ? <span className="ml-1 text-[11px] text-muted">{count}</span> : null}
    </Button>
  );
}

function srcCount(s: CatalogSource): number {
  return s.chapters_advertised ?? s.chapters_listed ?? 0;
}

/** Detailed card for one discovered work: overview + every matched source/sub-title so the
 *  user can compare and choose where to hook from. */
function CatalogDetail({ group, onClose }: { group: CatalogGroup; onClose: () => void }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [error, setError] = useState<string | null>(null);
  const [pendingId, setPendingId] = useState<number | null>(null);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["works"] });
    qc.invalidateQueries({ queryKey: ["catalog"] });
    qc.invalidateQueries({ queryKey: ["catalog-stats"] });
  };
  const [doneWorkId, setDoneWorkId] = useState<number | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const hook = useMutation({
    mutationFn: (id: number) => api.hookCatalog(id),
    onMutate: (id) => {
      setPendingId(id);
      setError(null);
      setDoneWorkId(null);
      setNotice(null);
    },
    onSuccess: (work) => {
      invalidate();
      setDoneWorkId(work.id);
      setNotice("Added to your library ✓");
    },
    onError: (e) => setError((e as Error).message),
    onSettled: () => setPendingId(null),
  });
  const grab = useMutation({
    mutationFn: (id: number) => api.grabCatalog(id),
    onMutate: (id) => {
      setPendingId(id);
      setError(null);
      setDoneWorkId(null);
      setNotice(null);
    },
    onSuccess: (r) => {
      invalidate();
      setNotice(r.message);
    },
    onError: (e) => setError((e as Error).message),
    onSettled: () => setPendingId(null),
  });
  // Sources removed in this modal session — hidden immediately so the row doesn't linger on
  // the stale `group` prop until a reopen (the catalog list refetches in the background).
  const [removedIds, setRemovedIds] = useState<Set<number>>(new Set());
  const remove = useMutation({
    mutationFn: ({ id, blockDomain }: { id: number; blockDomain: boolean }) =>
      api.removeCatalog(id, { blockDomain }),
    onMutate: () => {
      setError(null);
      setNotice(null);
    },
    onSuccess: (r, vars) => {
      invalidate();
      setNotice(
        `Removed and blocked${r.blocked?.scope === "domain" ? " (whole domain)" : ""}. ` +
          "It won't be re-added by future crawls."
      );
      const next = new Set(removedIds).add(vars.id);
      setRemovedIds(next);
      // Close the detail view once every source has been removed.
      if (group.sources.every((s) => next.has(s.catalog_id))) onClose();
    },
    onError: (e) => setError((e as Error).message),
  });

  // Surface the most complete / healthiest source first; hide ones removed this session.
  const sources = [...group.sources]
    .filter((s) => !removedIds.has(s.catalog_id))
    .sort((a, b) => {
      const hooked = Number(!!b.hooked_work_id) - Number(!!a.hooked_work_id);
      return hooked || srcCount(b) - srcCount(a);
    });

  return (
    <div
      className="fixed inset-0 z-50 flex justify-center overflow-y-auto bg-black/50 p-0 sm:p-6"
      onClick={onClose}
    >
      <div
        className="relative h-full w-full max-w-2xl overflow-y-auto bg-surface sm:h-auto sm:rounded-2xl sm:shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="sticky top-0 z-10 flex items-center justify-between gap-2 border-b border-border bg-surface/95 px-4 py-3 backdrop-blur">
          <div className="truncate font-semibold">{group.title}</div>
          <Button size="sm" variant="ghost" onClick={onClose}>
            ✕
          </Button>
        </div>
        <div className="px-5 py-4">
          <div className="flex gap-4">
            {group.cover_url && (
              <img
                src={group.cover_url}
                alt=""
                className="h-40 w-28 shrink-0 rounded-md border border-border object-cover"
                onError={(e) => (e.currentTarget.style.display = "none")}
              />
            )}
            <div className="min-w-0">
              <div className="text-lg font-semibold leading-tight">{group.title}</div>
              {group.author && <div className="text-sm text-muted">by {group.author}</div>}
              <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted">
                <Badge tone={mediaTone(group.media_label)}>{group.media_label}</Badge>
                {group.chapters != null && <span>{group.chapters.toLocaleString()} chapters</span>}
                <span>
                  · {group.sources.length} source{group.sources.length === 1 ? "" : "s"}
                </span>
              </div>
              {group.hooked_work_id && (
                <button className="mt-2" onClick={() => navigate(`/read/${group.hooked_work_id}`)}>
                  <Badge tone="green">in library — open →</Badge>
                </button>
              )}
            </div>
          </div>
          {group.synopsis && <p className="mt-3 text-sm text-text">{group.synopsis}</p>}
          {(hook.isPending || grab.isPending) && (
            <p className="mt-2 text-sm text-accent">Adding to your library…</p>
          )}
          {notice && (
            <p className="mt-2 text-sm text-green-600">
              {notice}{" "}
              {doneWorkId != null && (
                <button className="underline" onClick={() => navigate(`/read/${doneWorkId}`)}>
                  Open
                </button>
              )}
            </p>
          )}
          {error && <p className="mt-2 text-sm text-red-500">Couldn't add: {error}</p>}

          <h3 className="mb-2 mt-5 text-sm font-semibold uppercase tracking-wide text-muted">
            Sources — choose where to read from
          </h3>
          <div className="space-y-2">
            {sources.map((s) => (
              <SourceDetailRow
                key={s.catalog_id}
                source={s}
                groupTitle={group.title}
                busy={pendingId === s.catalog_id}
                disabled={hook.isPending || grab.isPending}
                removing={remove.isPending && remove.variables?.id === s.catalog_id}
                onHook={() => hook.mutate(s.catalog_id)}
                onGrab={() => grab.mutate(s.catalog_id)}
                onRemove={(blockDomain) => remove.mutate({ id: s.catalog_id, blockDomain })}
                onOpen={(id) => navigate(`/read/${id}`)}
              />
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

function SourceDetailRow({
  source,
  groupTitle,
  busy,
  disabled,
  removing,
  onHook,
  onGrab,
  onRemove,
  onOpen,
}: {
  source: CatalogSource;
  groupTitle: string;
  busy: boolean;
  disabled: boolean;
  removing: boolean;
  onHook: () => void;
  onGrab: () => void;
  onRemove: (blockDomain: boolean) => void;
  onOpen: (workId: number) => void;
}) {
  const hb = healthBadge(source.health);
  const count = source.chapters_advertised ?? source.chapters_listed;
  const [confirming, setConfirming] = useState(false);
  const [blockDomain, setBlockDomain] = useState(false);
  return (
    <div className="rounded-lg border border-border p-3">
     <div className="flex gap-3">
      {source.cover_url && (
        <img
          src={source.cover_url}
          alt=""
          loading="lazy"
          className="h-20 w-14 shrink-0 rounded border border-border object-cover"
          onError={(e) => (e.currentTarget.style.display = "none")}
        />
      )}
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-1.5">
          <Badge tone={mediaTone(source.media_label)}>{source.media_label}</Badge>
          <Badge tone={source.kind === "online" ? "default" : "violet"}>
            {source.kind === "online" ? source.domain : source.kind}
          </Badge>
          {hb && <Badge tone={hb.tone}>{hb.label}</Badge>}
          {source.hooked_work_id && <Badge tone="green">in library</Badge>}
        </div>
        {/* This source's own matched title (the "sub-title") + author. */}
        <div className="mt-1 truncate text-sm font-medium text-text" title={source.title ?? undefined}>
          {source.title || groupTitle}
        </div>
        {source.author && <div className="truncate text-xs text-muted">by {source.author}</div>}
        <div className="mt-0.5 text-xs text-muted">
          {count != null ? `${count.toLocaleString()} chapters` : "chapter count unknown"}
          {source.health_detail ? ` · ${source.health_detail}` : ""}
        </div>
        <a
          href={source.work_url}
          target="_blank"
          rel="noreferrer"
          className="mt-0.5 block truncate text-[11px] text-muted underline"
        >
          {source.work_url}
        </a>
      </div>
      <div className="flex shrink-0 items-center gap-1">
        {source.hooked_work_id ? (
          <Button size="sm" variant="ghost" onClick={() => onOpen(source.hooked_work_id!)}>
            Open →
          </Button>
        ) : source.kind !== "online" ? (
          source.grab_status ? (
            <Badge tone="green">requested</Badge>
          ) : (
            <Button size="sm" variant="outline" disabled={disabled} onClick={onGrab}>
              {busy ? "Grabbing…" : `Grab via ${source.kind}`}
            </Button>
          )
        ) : (
          <Button size="sm" variant="primary" disabled={disabled} onClick={onHook}>
            {busy ? "Adding…" : "Hook"}
          </Button>
        )}
        {/* Remove broken/unwanted content from the index (bars it from being re-added). */}
        <Button
          size="sm"
          variant="ghost"
          title="Remove from index and block from re-adding"
          disabled={removing}
          onClick={() => setConfirming((v) => !v)}
        >
          🗑
        </Button>
      </div>
     </div>
      {confirming && (
        <div className="mt-2 rounded-lg border border-red-500/30 bg-red-500/5 p-2.5 text-sm">
          <div className="mb-2 text-text">
            Remove this source from the index and block it from being re-added by future crawls?
            {source.hooked_work_id && (
              <span className="text-muted"> Your hooked library copy is kept.</span>
            )}
          </div>
          <label className="mb-2 flex items-center gap-2 text-xs text-muted">
            <input
              type="checkbox"
              checked={blockDomain}
              onChange={(e) => setBlockDomain(e.target.checked)}
            />
            Block the whole domain ({source.domain}), not just this URL
          </label>
          <div className="flex gap-2">
            <Button
              size="sm"
              variant="danger"
              disabled={removing}
              onClick={() => onRemove(blockDomain)}
            >
              {removing ? "Removing…" : "Remove & block"}
            </Button>
            <Button size="sm" variant="ghost" disabled={removing} onClick={() => setConfirming(false)}>
              Cancel
            </Button>
          </div>
        </div>
      )}
    </div>
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
