// The Browse grid for one discovery category (genre / theme / most-popular). Sorted, paginated
// titles from the precomputed grouping, reusing the shared CatalogCard + detail modal. Reached
// from a row's "Browse →" link; its URL is shareable and the back button works.
import { useEffect, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { keepPreviousData, useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { api, CatalogGroup } from "../api/client";
import { qk } from "../api/queryKeys";
import { useIsAdmin } from "../auth";
import { Button, EmptyState, PosterGridSkeleton, Select } from "../components/ui";
import { useLanguageName } from "../components/LanguageBadge";
import { CatalogCard, CatalogDetail } from "../components/catalog/CatalogCard";

export default function BrowseCatalog() {
  const { t } = useTranslation();
  const languageName = useLanguageName();
  const { dimension = "popular", value = "all" } = useParams<{ dimension: string; value: string }>();
  const [params] = useSearchParams();
  const media = params.get("media") || undefined;
  const [sort, setSort] = useState("popularity");
  const [language, setLanguage] = useState(""); // "" = all languages
  const [detail, setDetail] = useState<CatalogGroup | null>(null);
  const SORTS = [
    { value: "popularity", label: t("discover.sortPopular") },
    { value: "chapters", label: t("discover.sortChapters") },
    { value: "new", label: t("discover.sortNewest") },
    { value: "title", label: t("library.sortTitle") },
  ];
  // One shared stock-summary query for the whole grid (FE-M2).
  const isAdmin = useIsAdmin();
  const stockCfg = useQuery({ queryKey: qk.stockSummary(), queryFn: api.getStockSummary, enabled: isAdmin });
  const canStock = isAdmin && !!stockCfg.data?.configured;
  // Languages present in the catalog (most-common first) → the "All languages" filter dropdown.
  const langsQ = useQuery({ queryKey: qk.catalogLanguages(), queryFn: api.catalogLanguages });
  const languageOptions = [
    { value: "", label: t("library.filter.allLanguages") },
    ...(langsQ.data ?? []).map((l) => ({ value: l.code, label: languageName(l.code) })),
  ];

  const PAGE = 60;
  const q = useInfiniteQuery({
    // Intentionally a literal (not qk.*): param-laden key whose argument order must stay byte-exact
    // and can't be type-checked for divergence (value/media/language may be undefined).
    queryKey: ["catalog-browse", dimension, value, media, language, sort],
    initialPageParam: 0,
    queryFn: ({ pageParam }) =>
      api.catalogBrowse({
        dimension,
        value: dimension === "popular" ? undefined : value,
        media,
        language: language || undefined,
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
      ? t("discover.mostPopular")
      : value.replace(/-/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

  const eyebrow = dimension === "popular" ? t("nav.discover") : dimension;

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
              {t("discover.backToDiscovery")}
            </Link>
          </div>
          <p className="mt-2 max-w-2xl text-sm text-[var(--text-soft,var(--muted))]">
            {dimension === "popular" ? t("discover.blurbPopular") : t("discover.blurbBrowsing")}{" "}
            {dimension !== "popular" && <span className="font-semibold text-text">{heading}</span>} {t("discover.blurbTitles")}
            {media ? ` · ${media}` : ""}.
          </p>
        </div>
      </section>

      <div className="mx-auto max-w-6xl px-4 pb-10 pt-6 sm:px-6">
        <div className="mb-5 flex flex-wrap items-center gap-2">
          <div className="w-[190px]">
            <Select value={sort} onChange={setSort} label={t("library.sort")} options={SORTS} />
          </div>
          {languageOptions.length > 1 && (
            <div className="w-[190px]">
              <Select value={language} onChange={setLanguage} label={t("discover.language")} options={languageOptions} />
            </div>
          )}
        </div>

        {q.isLoading ? (
          <PosterGridSkeleton count={12} />
        ) : groups.length === 0 ? (
          <EmptyState
            title={t("discover.emptyTitle")}
            hint={t("discover.emptyHint")}
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
                  {t("discover.loadMore")}
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
