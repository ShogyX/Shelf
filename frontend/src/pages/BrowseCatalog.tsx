// The Browse grid for one discovery category (genre / theme / most-popular). Sorted, paginated
// titles from the precomputed grouping, reusing the shared CatalogCard + detail modal. Reached
// from a row's "Browse →" link; its URL is shareable and the back button works.
import { useEffect, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { keepPreviousData, useInfiniteQuery } from "@tanstack/react-query";
import { api, CatalogGroup } from "../api/client";
import { Button, Spinner } from "../components/ui";
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

  const PAGE = 60;
  const q = useInfiniteQuery({
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

  return (
    <main className="mx-auto max-w-7xl px-4 py-8">
      <div className="mb-1 flex flex-wrap items-baseline justify-between gap-3">
        <h1 className="text-2xl font-semibold capitalize">{heading}</h1>
        <Link to="/index" className="text-sm text-accent hover:underline">
          ← Back to discovery
        </Link>
      </div>
      <p className="mb-5 text-sm text-muted">
        {dimension === "popular" ? "The most popular" : dimension}{" "}
        {dimension !== "popular" && <span className="text-text">{heading}</span>} titles
        {media ? ` · ${media}` : ""}.
      </p>

      <div className="mb-4 flex flex-wrap items-center gap-2">
        <select
          className="rounded-lg border border-border bg-surface px-2 py-1.5 text-xs text-text focus:border-accent focus:outline-none"
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
        <Spinner label="Loading titles…" />
      ) : groups.length === 0 ? (
        <p className="text-sm text-muted">
          No titles here yet — they appear as the crawler enriches discovered works with genres.
        </p>
      ) : (
        <>
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
            {groups.map((g) => (
              <CatalogCard key={g.id || g.norm_key} group={g} onOpenDetail={() => setDetail(g)} />
            ))}
          </div>
          <div ref={sentinel} className="h-8" />
          {q.isFetchingNextPage && <div className="mt-2"><Spinner label="Loading more…" /></div>}
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
    </main>
  );
}
