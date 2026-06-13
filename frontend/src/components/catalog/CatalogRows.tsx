// Netflix-style discovery rows for the Index page: a "Most Popular" lane plus the marquee genre
// and theme lanes, grouped into a section per MEDIA CATEGORY (Manga & Comics / Novel / Book). The
// arrangement (category + genre order, and what's hidden) comes from the user's EFFECTIVE layout —
// their personal one if they've customized it, else the admin's global default. Editing happens in
// Settings → Index layout. The rows the server returns are already filtered to the categories +
// 18+ content this user may see, so the layout only ever reorders/hides authorized content.
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, CatalogGroup, CatalogRow } from "../../api/client";
import { Badge, Spinner } from "../ui";
import Cover from "../Cover";
import { mediaTone } from "./CatalogCard";
import { useApp } from "../../store";
import { useAuth } from "../../auth";
import {
  EMPTY_LAYOUT, effectiveLayout, laneKey, lanesForCategory, orderedCategories,
} from "./layout";

function browseHref(row: CatalogRow): string {
  const dim = row.kind === "popular" ? "popular" : row.kind;
  const val = row.slug || "all";
  return `/browse/${dim}/${encodeURIComponent(val)}?media=${encodeURIComponent(row.media_category)}`;
}

export function CatalogRows({ onOpenDetail }: { onOpenDetail: (g: CatalogGroup) => void }) {
  const { prefs } = useApp();
  const allowed = useAuth((s) => s.me?.allowed_categories);
  const rowsQ = useQuery({ queryKey: ["catalog-rows"], queryFn: () => api.catalogRows() });
  // The global default layout (cheap; applied when the user hasn't customized their own).
  const globalQ = useQuery({ queryKey: ["index-layout"], queryFn: () => api.getIndexLayout() });

  if (rowsQ.isLoading) return <div className="mt-4"><Spinner label="Loading discovery…" /></div>;
  const data = rowsQ.data ?? [];

  if (data.length === 0) {
    return (
      <p className="mt-3 text-sm text-muted">
        No works discovered yet — index a fiction site and they'll appear here, sorted by
        popularity and genre, as the crawler finds and enriches them.
      </p>
    );
  }

  const layout = effectiveLayout(prefs, globalQ.data ?? EMPTY_LAYOUT);
  // `allowed` is belt-and-braces: the server already returns only permitted categories, so
  // orderedCategories(data) can't contain a disallowed one — but we double-filter just in case.
  const cats = orderedCategories(data, layout, allowed ?? undefined)
    .filter((c) => !layout.hiddenCategories.includes(c));

  return (
    <>
      <div className="mt-3 flex items-center justify-end">
        <Link to="/settings#layout" className="text-xs text-accent hover:underline">
          ✎ Customize layout
        </Link>
      </div>
      <div className="mt-2 space-y-8">
        {cats.length === 0 && (
          <p className="text-sm text-muted">
            All categories are hidden — adjust your{" "}
            <Link to="/settings#layout" className="text-accent hover:underline">index layout</Link>.
          </p>
        )}
        {cats.map((cat) => {
          const lanes = lanesForCategory(data, cat, layout)
            .filter((r) => !layout.hiddenLanes.includes(laneKey(r)));
          if (lanes.length === 0) return null;
          return (
            <section key={cat}>
              <h3 className="mb-2 text-sm font-semibold uppercase tracking-wide text-muted">{cat}</h3>
              <div className="space-y-5">
                {lanes.map((row) => (
                  <Lane key={laneKey(row)} row={row} onOpenDetail={onOpenDetail} />
                ))}
              </div>
            </section>
          );
        })}
      </div>
    </>
  );
}

function Lane({ row, onOpenDetail }: { row: CatalogRow; onOpenDetail: (g: CatalogGroup) => void }) {
  return (
    <div>
      <div className="mb-1.5 flex items-baseline justify-between gap-3">
        <h4 className="text-base font-semibold text-text">
          {row.label}
          {row.kind !== "popular" && (
            <span className="ml-1.5 text-xs font-normal text-muted">{row.count.toLocaleString()}</span>
          )}
        </h4>
        <Link to={browseHref(row)} className="shrink-0 text-xs text-accent hover:underline">
          Browse →
        </Link>
      </div>
      <div className="flex gap-3 overflow-x-auto pb-2 [scrollbar-width:thin]">
        {row.items.map((g) => (
          <PosterCard key={g.id || g.norm_key} group={g} onOpen={() => onOpenDetail(g)} />
        ))}
      </div>
    </div>
  );
}

function PosterCard({ group, onOpen }: { group: CatalogGroup; onOpen: () => void }) {
  return (
    <button
      onClick={onOpen}
      className="group w-32 shrink-0 text-left"
      title={`${group.title} — view details & add`}
    >
      <div className="relative aspect-[2/3] w-full overflow-hidden rounded-lg border border-border bg-surface">
        <Cover title={group.title} author={group.author} coverUrl={group.cover_url} small />
        <div className="pointer-events-none absolute inset-0 transition group-hover:bg-black/5" />
        {group.hooked_work_id && (
          <span className="absolute left-1 top-1">
            {/* in_library = the viewer added it (green); a hooked title NOT in their library is
                operator-stocked → "in stock" (violet), not "in library". */}
            <Badge tone={group.in_library ? "green" : "violet"}>
              {group.in_library ? "in library" : "in stock"}
            </Badge>
          </span>
        )}
      </div>
      <div className="mt-1 line-clamp-2 text-xs font-medium leading-tight text-text group-hover:text-accent">
        {group.title}
      </div>
      <div className="mt-0.5 flex items-center gap-1 text-[11px] text-muted">
        <Badge tone={mediaTone(group.media_label)}>{group.media_label}</Badge>
        {group.chapters != null && <span>{group.chapters.toLocaleString()} ch</span>}
      </div>
    </button>
  );
}
