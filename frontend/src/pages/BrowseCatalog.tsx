// The Browse grid for one discovery category (genre / theme / most-popular). Sorted, paginated
// titles from the precomputed grouping, reusing the shared CatalogCard + detail modal. Reached
// from a row's "Browse →" link; its URL is shareable and the back button works.
import { useEffect, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { keepPreviousData, useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { api, CatalogGroup } from "../api/client";
import { qk } from "../api/queryKeys";
import { useIsAdmin } from "../auth";
import { Button, EmptyState, inputCls, PosterGridSkeleton } from "../components/ui";
import { CatalogCard, CatalogDetail } from "../components/catalog/CatalogCard";

const SORTS: { value: string; label: string }[] = [
  { value: "popularity", label: "Most popular" },
  { value: "chapters", label: "Most chapters" },
  { value: "new", label: "Newest" },
  { value: "title", label: "Title A–Z" },
];

export default function BrowseCatalog() {
  const { dimension = "popular", value = "all" } = useParams<{ dimension: string; value: string }>();
  const [params] = useSearchParams();
  const media = params.get("media") || undefined;
  const [sort, setSort] = useState("popularity");
  const [detail, setDetail] = useState<CatalogGroup | null>(null);
  // One shared stock-summary query for the whole grid (FE-M2).
  const isAdmin = useIsAdmin();
  const stockCfg = useQuery({ queryKey: qk.stockSummary(), queryFn: api.getStockSummary, enabled: isAdmin });
  const canStock = isAdmin && !!stockCfg.data?.configured;

  const PAGE = 60;
  const q = useInfiniteQuery({
    // Intentionally a literal (not qk.*): param-laden key whose argument order must stay byte-exact
    // and can't be type-checked for divergence (value/media may be undefined).
    queryKey: ["catalog-browse", dimension, value, media, sort],
    initialPageParam: 0,
    queryFn: ({ pageParam }) =>
      api.catalogBrowse({
        dimension,
        value: dimension === "popular" ? undefined : value,
        media,
        sort,
        limit: PAGE,
        offset: pageParam,
      }),
    getNextPageParam: (last, all) => (last.length < PAGE ? undefined : all.length * PAGE),
    placeholderData: keepPreviousData,
  });
  const groups = q.data?.pages.flat() ?? [];

  const sentinel = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const el = sentinel.current;
    if (!el) return;
    const obs = new IntersectionObserver(
      (e) => {
        if (e[0].isIntersecting && q.hasNextPage && !q.isFetchingNextPage) q.fetchNextPage();
      },
      { rootMargin: "600px" }
    );
    obs.observe(el);
    return () => obs.disconnect();
  }, [q.hasNextPage, q.isFetchingNextPage, q.fetchNextPage]);

  const heading =
    dimension === "popular"
      ? "Most Popular"
      : value.replace(/-/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

  const eyebrow = dimension === "popular" ? "Discover" : dimension;

  return (
    <main className="page-in">
      {/* Premium header band — a slim full-bleed, accent-tinted hero mirroring the home/Discover
          chrome (eyebrow + Newsreader title), so Browse reads as part of the redesign rather than
          the old dense admin grid. */}
      <section className="relative overflow-hidden border-b border-[var(--hair,var(--border))]">
        <div className="absolute inset-0" style={{
          background:
            "radial-gradient(120% 140% at 0% 0%, color-mix(in srgb, var(--accent) 16%, transparent), transparent 60%)," +
            "linear-gradient(0deg, var(--bg), transparent 70%)",
        }} />
        <div className="relative mx-auto max-w-6xl px-4 pb-7 pt-10 sm:px-6">
          <div className="mb-1.5 text-xs font-semibold uppercase tracking-widest text-[var(--accent-bright,var(--accent))]">
            {eyebrow}
          </div>
          <div className="flex flex-wrap items-end justify-between gap-3">
            <h1 className="font-display text-[34px] font-semibold capitalize leading-[1.05] tracking-tight text-text sm:text-[44px]">
              {heading}
            </h1>
            <Link to="/discover" className="shrink-0 text-[13px] font-semibold text-[var(--accent-bright,var(--accent))] opacity-90 hover:opacity-100">
              ← Back to discovery
            </Link>
          </div>
          <p className="mt-2 max-w-2xl text-sm text-[var(--text-soft,var(--muted))]">
            {dimension === "popular" ? "The most popular" : "Browsing"}{" "}
            {dimension !== "popular" && <span className="font-semibold text-text">{heading}</span>} titles
            {media ? ` · ${media}` : ""}.
          </p>
        </div>
      </section>

      <div className="mx-auto max-w-6xl px-4 pb-10 pt-6 sm:px-6">
        <div className="mb-5 flex flex-wrap items-center gap-2">
          <select
            className={`${inputCls} w-auto!`}
            value={sort}
            onChange={(e) => setSort(e.target.value)}
          >
            {SORTS.map((s) => (
              <option key={s.value} value={s.value}>
                Sort: {s.label}
              </option>
            ))}
          </select>
        </div>

        {q.isLoading ? (
          <PosterGridSkeleton count={12} />
        ) : groups.length === 0 ? (
          <EmptyState
            title="No titles here yet"
            hint="They appear as the crawler enriches discovered works with genres."
          />
        ) : (
          <>
            <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
              {groups.map((g) => (
                <CatalogCard key={g.id ?? g.norm_key} group={g} canStock={canStock} onOpenDetail={() => setDetail(g)} />
              ))}
            </div>
            <div ref={sentinel} className="h-8" />
            {q.isFetchingNextPage && <div className="mt-2"><PosterGridSkeleton count={3} /></div>}
            {q.hasNextPage && !q.isFetchingNextPage && (
              <div className="mt-3 flex justify-center">
                <Button variant="outline" onClick={() => q.fetchNextPage()}>
                  Load more
                </Button>
              </div>
            )}
          </>
        )}

        {detail && <CatalogDetail group={detail} onClose={() => setDetail(null)} />}
      </div>
    </main>
  );
}
