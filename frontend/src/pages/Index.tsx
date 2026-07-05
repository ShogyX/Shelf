import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate, useSearchParams } from "react-router-dom";
import { coverSrc } from "../components/Cover";
import { FeaturedHero, Dot } from "../components/FeaturedHero";
import { useCoverBackdrop } from "../lib/coverBackdrop";
import type { CatalogRow } from "../api/client";
import {
  keepPreviousData,
  useInfiniteQuery,
  useQuery,
} from "@tanstack/react-query";
import { api, CatalogCategory, CatalogGroup, IndexSearchResult } from "../api/client";
import { qk } from "../api/queryKeys";
import { useIsAdmin } from "../auth";
import { useApp } from "../store";
import { Button, Card, Chip, EmptyState, Spinner, inputCls } from "../components/ui";
import { PageReader } from "../components/IndexShared";
import { CatalogCard, CatalogDetail } from "../components/catalog/CatalogCard";
import { CatalogRows } from "../components/catalog/CatalogRows";
import { EMPTY_LAYOUT, effectiveLayout } from "../components/catalog/layout";
import { CoverCard } from "../components/CoverCard";
import { Rail } from "../components/Rail";

export default function IndexPage() {
  // Full-bleed: the billboard hero spans to the page ends (like the Library home); the rails + grid
  // are width-capped inside CatalogSection. No PageHeader — the hero is the header.
  return (
    <main className="page-in">
      <CatalogSection />
    </main>
  );
}

type SearchMode = "titles" | "fulltext";
const ALL = "__all__";

// Tidy a genre/theme pill label: drop a raw "Category: " prefix (some taxonomy labels carry it) and
// capitalise a lowercase first letter, so the pills read consistently (e.g. "adventure" → "Adventure").
function prettyCat(label: string): string {
  const s = label.replace(/^category:\s*/i, "").trim();
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
}

function CatalogSection() {
  const { t } = useTranslation();
  // Search + filters are URL-backed so a search is shareable and a refresh (incl. ?detail=) restores
  // the grid that produced it. The open detail view is likewise URL-driven (?detail=<group.id>) so
  // browser Back closes it, refresh restores it, and the link is shareable.
  const [searchParams, setSearchParams] = useSearchParams();
  // The search text comes from ?q=, written (already debounced) by the nav's single search box.
  const debounced = (searchParams.get("q") ?? "").trim();
  const [live, setLive] = useState(false);
  const [openPage, setOpenPage] = useState<number | null>(null);
  // mode/media/source/sort are DERIVED from the URL each render (no separate state → no sync loop);
  // their setters write the param with `replace` so filter changes don't pile up history entries.
  const mode: SearchMode = searchParams.get("mode") === "fulltext" ? "fulltext" : "titles";
  const mediaFilter = searchParams.get("media") ?? ALL;
  const sourceFilter = searchParams.get("source") ?? ALL;
  const sortByParam = searchParams.get("sort");
  const sortBy: "relevance" | "chapters" | "title" =
    sortByParam === "chapters" || sortByParam === "title" ? sortByParam : "relevance";
  // Set/clear a single param (preserving every other, incl. ?detail) with replace — user actions only.
  const setParam = (key: string, value: string | null, defaultValue: string) =>
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (value == null || value === defaultValue) next.delete(key);
        else next.set(key, value);
        return next;
      },
      { replace: true },
    );
  const setMode = (m: SearchMode) => setParam("mode", m, "titles");
  const setMediaFilter = (m: string) => setParam("media", m, ALL);
  const setSourceFilter = (s: string) => setParam("source", s, ALL);
  const setSortBy = (s: "relevance" | "chapters" | "title") => setParam("sort", s, "relevance");
  const navigate = useNavigate();
  // Idle = no search/filter active → show the curated discovery rows instead of a flat grid.
  const idle = mode === "titles" && !debounced && mediaFilter === ALL && sourceFilter === ALL;
  // Featured billboard title + genre chips for the idle "Discover" wall (only fetched when idle).
  const rows = useQuery({ queryKey: qk.catalogRows(), queryFn: () => api.catalogRows(), enabled: idle });
  const cats = useQuery({ queryKey: qk.catalogCategories(), queryFn: () => api.catalogCategories(), enabled: idle });
  // The genre/theme chip nav. catalogCategories returns one row per (kind, slug, media_category), so a
  // genre spanning several sections (e.g. "Romance" in Novel + Comics) arrives multiple times — DEDUPE
  // by (kind, slug) so each genre is one pill. Also respect the user's effective layout (same as the
  // rows below): drop chips whose section or lane the user has hidden, so only VISIBLE categories show.
  const { prefs } = useApp();
  const layoutQ = useQuery({ queryKey: qk.indexLayout(), queryFn: () => api.getIndexLayout(), enabled: idle });
  const catChips = useMemo(() => {
    const layout = effectiveLayout(prefs, layoutQ.data ?? EMPTY_LAYOUT);
    const seen = new Set<string>();
    const out: CatalogCategory[] = [];
    for (const c of cats.data?.categories ?? []) {
      if (c.kind !== "genre" && c.kind !== "theme") continue;
      if (layout.hiddenCategories.includes(c.media_category)) continue;              // hidden section
      if (layout.hiddenLanes.includes(`${c.media_category}|${c.kind}|${c.slug}`)) continue;  // hidden lane
      const key = `${c.kind}:${c.slug}`;
      if (seen.has(key)) continue;                                                   // one pill per genre/theme
      seen.add(key);
      out.push(c);
    }
    return out.slice(0, 14);
  }, [cats.data, layoutQ.data, prefs]);
  // Downloaded audiobooks (shared pool) → the idle "Audiobooks" lane; the Rail self-hides when empty.
  const audiobooks = useQuery({ queryKey: ["catalog-audiobooks"], queryFn: api.catalogAudiobooks, enabled: idle });
  const featured = useFeaturedHero(rows.data);
  // Tint the whole-page aurora with the featured cover's colours while browsing (the billboard
  // rotates, so the backdrop blooms between titles); revert to accent when searching/filtering.
  useCoverBackdrop(idle ? coverSrc(featured?.cover_url) : null);
  const stats = useQuery({ queryKey: qk.catalogStats(), queryFn: api.catalogStats });
  // Complete filter options (all media types + source domains) from the whole catalog —
  // NOT just the loaded page, so low-ranked types/sources (e.g. Gutenberg books) appear.
  const facets = useQuery({ queryKey: qk.catalogFacets(), queryFn: api.catalogFacets });
  // One shared stock-summary query for the whole grid (FE-M2) — drives the per-card "save to stock" option.
  const isAdmin = useIsAdmin();
  const stockCfg = useQuery({ queryKey: qk.stockSummary(), queryFn: api.getStockSummary, enabled: isAdmin });
  const canStock = isAdmin && !!stockCfg.data?.configured;
  const mediaOptions = facets.data?.media ?? [];
  const sourceOptions = facets.data?.domains ?? [];

  // Filtering + sorting are applied server-side; results are paged and loaded lazily on scroll
  // (the catalog can hold thousands of titles — fetching/rendering them all at once is slow).
  const PAGE = 60;
  const catalog = useInfiniteQuery({
    // Intentionally a literal (not qk.*): this param-laden key's argument order must stay byte-exact,
    // and a divergence can't be type-checked. qk.catalog() (= ["catalog"]) prefix-matches it for
    // invalidation.
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
  // Auto-load the next page when the sentinel scrolls into view. Keep the latest fetch logic in a
  // ref and bind the observer ONCE — depending on catalog.fetchNextPage (a fresh identity most
  // renders) would tear the observer down and recreate it constantly, dropping the intersection
  // event mid-rebind during fast scrolling.
  const sentinel = useRef<HTMLDivElement | null>(null);
  const loadMore = useRef(() => {});
  loadMore.current = () => {
    if (catalog.hasNextPage && !catalog.isFetchingNextPage) catalog.fetchNextPage();
  };
  useEffect(() => {
    const el = sentinel.current;
    if (!el) return;
    const obs = new IntersectionObserver(
      (entries) => { if (entries[0].isIntersecting) loadMore.current(); },
      { rootMargin: "600px" }
    );
    obs.observe(el);
    return () => obs.disconnect();
  }, []);
  // Full-text search over the indexed page bodies (the old standalone search, now a mode).
  const search = useQuery({
    queryKey: qk.indexSearch(debounced),
    queryFn: () => api.searchIndex(debounced, undefined, 40),
    enabled: mode === "fulltext" && debounced.length > 0,
  });

  const groups = catalog.data?.pages.flat() ?? [];

  // Resolve the open detail from the URL. Prefer the clicked group object (works for BOTH the
  // search-results grid AND the idle discovery rows, which come from a SEPARATE query and are
  // never in `groups`); fall back to the loaded `groups` so a same-session Back/Forward still
  // re-resolves. A cold deep-link to a group in neither set resolves to null and renders nothing —
  // graceful degradation, by design (full restore is a Wave-5 "search/filters in URL" item).
  const [lastOpened, setLastOpened] = useState<CatalogGroup | null>(null);
  const detailParam = searchParams.get("detail");
  const detail = detailParam
    ? (String(lastOpened?.id) === detailParam ? lastOpened : null) ??
      groups.find((g) => String(g.id) === detailParam) ??
      null
    : null;

  // Push ?detail=<id> as a NEW history entry (preserving any other params) so Back closes it.
  // Stash the group object too, so resolution never depends on it being in the `groups` grid.
  const openDetail = (g: CatalogGroup) => {
    setLastOpened(g);
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      next.set("detail", String(g.id));
      return next;
    });
  };
  const closeDetail = () => {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      next.delete("detail");
      return next;
    });
  };

  const selCls = `${inputCls} w-auto!`;

  return (
    <>
      {/* Featured this week — idle discovery only. The recommended title as a full poster + details. */}
      {idle && featured && (
        <FeaturedHero
          eyebrow={t("discover.featuredThisWeek")}
          title={featured.title}
          author={featured.author ?? t("discover.unknownAuthor")}
          meta={featured.media_label ? <><Dot /><span>{featured.media_label}</span></> : undefined}
          description={featured.synopsis}
          coverUrl={featured.cover_url}
          actions={
            <>
              <button
                onClick={() => openDetail(featured)}
                className="flex items-center gap-2 rounded-xl bg-accent px-6 py-3 text-[15px] font-bold text-accent-fg shadow-[0_8px_24px_color-mix(in_srgb,var(--accent)_40%,transparent)] transition hover:-translate-y-0.5"
              >{t("discover.addToLibraryHero")}</button>
              <button
                onClick={() => openDetail(featured)}
                className="flex items-center gap-2 rounded-xl border border-[var(--hair-strong,var(--border))] bg-[color-mix(in_srgb,var(--surface)_70%,transparent)] px-5 py-3 text-[15px] font-semibold text-text backdrop-blur transition hover:bg-surface"
              >{t("discover.moreInfo")}</button>
            </>
          }
        />
      )}

      <div className="mx-auto max-w-6xl px-4 pb-8 pt-6 sm:px-6">
        {/* Search chrome — only when NOT idle (a search/filter is active). The idle wall is clean:
            hero → genre chips → rails, per the handoff. */}
        {!idle && (
          <>
            <div className="mb-2 flex items-baseline justify-between gap-3">
              <h2 className="text-lg font-semibold">{t("discover.discoveredWorks")}</h2>
              <div className="flex items-baseline gap-3">
                {stats.data && (
                  <span className="text-xs text-muted">
                    {t("discover.statTitles", { count: stats.data.titles.toLocaleString() })} · {t("discover.statSources", { count: stats.data.sites })}
                    {stats.data.hooked > 0 && ` · ${t("discover.statInLibrary", { count: stats.data.hooked })}`}
                  </span>
                )}
              </div>
            </div>

            {/* The search box now lives in the top nav (drives ?q=). Here we keep just the mode toggle,
                which switches between matching titles/authors and the full text of indexed pages. */}
            <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
              <div className="flex min-w-0 flex-1 items-center gap-2 text-sm text-muted">
                <span aria-hidden>{mode === "titles" ? "📚" : "🔍"}</span>
                <span className="truncate">
                  {mode === "titles"
                    ? t("discover.searchingTitles")
                    : t("discover.searchingFulltext")}
                </span>
              </div>
              <div className="inline-flex shrink-0 overflow-hidden rounded-lg border border-border text-sm">
                <button
                  className={`px-3 py-2 ${mode === "titles" ? "bg-accent text-accent-fg" : "bg-surface text-muted"}`}
                  onClick={() => setMode("titles")}
                >
                  {t("discover.modeTitles")}
                </button>
                <button
                  className={`px-3 py-2 ${mode === "fulltext" ? "bg-accent text-accent-fg" : "bg-surface text-muted"}`}
                  onClick={() => setMode("fulltext")}
                  title={t("discover.modeFulltextHint")}
                >
                  {t("discover.modeFulltext")}
                </button>
              </div>
            </div>

            {/* Sort + filter by media type and source — only while searching. The main discovery
                view organizes itself by category (with per-user toggles), so these legacy controls
                would just clutter it; they belong to an active search (or the Browse page). */}
            {mode === "titles" && (
              <div className="mt-2 flex flex-wrap items-center gap-2">
                <select className={selCls} value={mediaFilter} onChange={(e) => setMediaFilter(e.target.value)}>
                  <option value={ALL}>{t("discover.allCategories")}</option>
                  {mediaOptions.map((m) => (
                    <option key={m} value={m}>{m}</option>
                  ))}
                </select>
                <select className={selCls} value={sourceFilter} onChange={(e) => setSourceFilter(e.target.value)}>
                  <option value={ALL}>{t("discover.allSources")}</option>
                  {sourceOptions.map((s) => (
                    <option key={s} value={s}>{s}</option>
                  ))}
                </select>
                <select className={selCls} value={sortBy} onChange={(e) => setSortBy(e.target.value as typeof sortBy)}>
                  <option value="relevance">{t("discover.sortRelevance")}</option>
                  <option value="chapters">{t("discover.sortMostChapters")}</option>
                  <option value="title">{t("discover.sortTitleAz")}</option>
                </select>
                <label className="ml-auto flex items-center gap-2 text-xs text-muted">
                  <input type="checkbox" checked={live} onChange={(e) => setLive(e.target.checked)} />
                  {t("discover.alsoSearchLive")}
                </label>
              </div>
            )}
          </>
        )}

        {mode === "titles" ? (
          idle ? (
            <>
              {/* Genre chips → category browse. Deduped + layout-filtered (see catChips). WRAP within
                  the page width (never overflow sideways) — spilling onto a second line when needed. */}
              {catChips.length > 0 && (
                <div className="mt-5 flex flex-wrap gap-2.5">
                  {catChips.map((c) => (
                    <Chip key={`${c.kind}:${c.slug}`} onClick={() => navigate(`/browse/${c.kind}/${c.slug}`)}>{prettyCat(c.label)}</Chip>
                  ))}
                </div>
              )}
              <CatalogRows onOpenDetail={openDetail} />
              {/* Audiobooks lane — the downloaded shared-pool audiobooks. Self-hides when there are none. */}
              {(audiobooks.data?.length ?? 0) > 0 && (
                <Rail title={t("discover.audiobooks")} moreLabel={t("audiobooks.seeAll")} moreTo="/audiobooks">
                  {audiobooks.data!.map((a) => (
                    <CoverCard key={a.work_id} title={a.title} author={a.author} coverUrl={a.cover_url}
                      kind="audio" subtitle={a.author ?? undefined}
                      onClick={() => navigate(`/discover?q=${encodeURIComponent(a.title)}`)} />
                  ))}
                </Rail>
              )}
            </>
          ) : catalog.isLoading ? (
            <div className="mt-3"><Spinner label={t("discover.loadingCatalog")} /></div>
          ) : groups.length === 0 ? (
            <div className="mt-3">
              <EmptyState title={t("discover.noMatchingTitles")} hint={t("discover.noMatchingTitlesHint")} />
            </div>
          ) : (
            <>
              <div className="mt-3 grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
                {groups.map((g) => (
                  <CatalogCard key={g.id ?? g.norm_key ?? g.title} group={g} canStock={canStock} onOpenDetail={() => openDetail(g)} />
                ))}
              </div>
              {/* Infinite-scroll sentinel + manual fallback. */}
              <div ref={sentinel} className="h-8" />
              {catalog.isFetchingNextPage && (
                <div className="mt-2"><Spinner label={t("discover.loadingMore")} /></div>
              )}
              {catalog.hasNextPage && !catalog.isFetchingNextPage && (
                <div className="mt-3 flex justify-center">
                  <Button variant="outline" onClick={() => catalog.fetchNextPage()}>
                    {t("discover.loadMore")}
                  </Button>
                </div>
              )}
            </>
          )
        ) : !debounced ? (
          <p className="mt-3 text-sm text-muted">{t("discover.fulltextPrompt")}</p>
        ) : (
          <SearchResults
            q={debounced}
            result={search.data}
            loading={search.isFetching}
            onOpen={setOpenPage}
          />
        )}

        {detail && <CatalogDetail group={detail} onClose={closeDetail} />}
        {openPage != null && <PageReader pageId={openPage} onClose={() => setOpenPage(null)} />}
      </div>
    </>
  );
}

// Pick the billboard featured title: a randomly-chosen BOOK (text/book/novel — never comic/manga/
// webtoon) that has cover art, auto-rotating every ~9s with a cross-fade. The pool is built from the
// loaded discovery rows (no extra fetch); the comic/manga exclusion + cover requirement keep the hero
// looking like a premium book billboard rather than "whatever the first row's first item is".
const BOOK_KINDS = new Set(["text", "book", "novel"]);
function isBookCandidate(g: { media_kind?: string; media_category?: string; cover_url?: string | null }): boolean {
  if (!coverSrc(g.cover_url)) return false; // needs real art for a full-bleed billboard
  const k = (g.media_kind ?? "").toLowerCase();
  const cat = (g.media_category ?? "").toLowerCase();
  if (k === "comic" || cat.includes("comic") || cat.includes("manga")) return false;
  return BOOK_KINDS.has(k) || cat.includes("book") || cat.includes("novel");
}

function useFeaturedHero(rows: CatalogRow[] | undefined) {
  // Admin rules for the billboard: which method/categories/media to draw from + how often it rotates.
  const cfgQ = useQuery({ queryKey: qk.featuredConfig(), queryFn: api.getFeaturedConfig, staleTime: 300_000 });
  const cfg = cfgQ.data;

  // Build the candidate pool honoring the admin config (falls back to the default book/novel pick
  // until/unless the admin narrows it). The catalog the client receives is ALREADY permission- and
  // 18+-filtered, so these rules can only narrow what this user is already allowed to see.
  const pool = useMemo(() => {
    const media = new Set((cfg?.media ?? []).map((s) => s.toLowerCase()));
    const cats = new Set((cfg?.categories ?? []).map((s) => s.toLowerCase()));
    const seen = new Set<number | string>();
    const out: CatalogGroup[] = [];
    for (const row of rows ?? []) {
      // Category filter: when set, only draw from lanes whose label is selected.
      if (cats.size && !cats.has((row.label ?? "").toLowerCase())) continue;
      for (const it of row.items ?? []) {
        const key = it.id ?? it.norm_key ?? it.title;
        if (seen.has(key)) continue;
        if (!coverSrc(it.cover_url)) continue; // the billboard poster needs real art
        if (media.size) {
          const label = (it.media_label ?? "").toLowerCase();
          const kind = (it.media_kind ?? "").toLowerCase();
          if (!media.has(label) && !media.has(kind)) continue;
        } else if (!isBookCandidate(it)) {
          continue; // default (no media set): books/novels, as before
        }
        seen.add(key);
        out.push(it);
      }
    }
    // `method` only sets the pool ORDER; `rotateHours` (below) decides how the index advances.
    if (cfg?.method === "newest") out.sort((a, b) => (b.id ?? 0) - (a.id ?? 0));
    else if (cfg?.method === "random") {
      // ponytail: Fisher-Yates with Math.random — re-shuffles per page load, so "random" isn't
      // cross-reload-stable even with a rotation window (admins wanting a fixed pick use popular/newest).
      for (let i = out.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [out[i], out[j]] = [out[j], out[i]];
      }
    }
    return out;
  }, [rows, cfg]);

  const rotateHours = cfg?.rotateHours ?? 0;
  const [idx, setIdx] = useState(0);
  useEffect(() => {
    if (pool.length === 0) return;
    if (rotateHours > 0) {
      // Deterministic per-time-window pick: stable for everyone within the window, advances each
      // window (e.g. 168h = a steady "featured this week"). No in-page churn.
      const win = Math.floor(Date.now() / 3_600_000 / rotateHours);
      setIdx(((win % pool.length) + pool.length) % pool.length);
      return;
    }
    // Carousel: a random start, then rotate through the pool (the lively default).
    setIdx(Math.floor(Math.random() * pool.length));
    if (pool.length < 2) return;
    const id = setInterval(() => setIdx((i) => (i + 1) % pool.length), 9000);
    return () => clearInterval(id);
  }, [pool, rotateHours]);

  return pool.length ? pool[idx % pool.length] : undefined;
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
  const { t } = useTranslation();
  if (loading && !result) return <div className="mt-3"><Spinner label={t("common.searching")} /></div>;
  if (!result) return null;
  if (result.length === 0)
    return (
      <div className="mt-3">
        <EmptyState title={t("discover.noMatches")} hint={t("discover.noMatchesHint", { query: q })} />
      </div>
    );
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
              src={coverSrc(r.cover_url) ?? ""}
              alt=""
              loading="lazy"
              className="h-20 w-14 shrink-0 rounded-md border border-border object-cover"
              onError={(e) => (e.currentTarget.style.display = "none")}
            />
          )}
          <div className="min-w-0 flex-1">
            <div className="font-medium text-text">{r.title || r.url}</div>
            {r.author && <div className="truncate text-xs text-muted">{t("common.byAuthor", { author: r.author })}</div>}
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
