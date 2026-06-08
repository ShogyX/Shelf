// Netflix-style discovery rows for the Index page: a "Most Popular" lane plus the marquee genre
// and theme lanes, grouped into a section per MEDIA CATEGORY (Manga / Manhua / Webtoon / Comic /
// Novel / Book). Each lane is a horizontal scroller of compact poster cards (click → the shared
// detail modal to hook), with a "Browse →" link to the full sorted grid. A chip row lets the user
// hide categories they don't care about (persisted per-user).
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, CatalogGroup, CatalogRow, MEDIA_CATEGORIES } from "../../api/client";
import { Badge, Spinner } from "../ui";
import Cover from "../Cover";
import { mediaTone } from "./CatalogCard";
import { useApp } from "../../store";
import { useAuth } from "../../auth";

function browseHref(row: CatalogRow): string {
  const dim = row.kind === "popular" ? "popular" : row.kind;
  const val = row.slug || "all";
  return `/browse/${dim}/${encodeURIComponent(val)}?media=${encodeURIComponent(row.media_category)}`;
}

/** Per-user chip toggles for which media categories appear on the Index. Only the categories the
 *  admin permits this user to view are offered (an admin sees all). */
export function CategoryToggles({ available }: { available?: string[] }) {
  const { prefs, setPrefs } = useApp();
  const allowed = useAuth((s) => s.me?.allowed_categories);
  const hidden = new Set(prefs.indexHiddenCategories ?? []);
  // Show every known category, but mark which actually have content (so empty ones read as muted).
  const present = new Set(available ?? MEDIA_CATEGORIES);
  // Only categories the admin permits (fall back to all while `me` is still loading).
  const offered = MEDIA_CATEGORIES.filter((c) => (allowed ?? MEDIA_CATEGORIES).includes(c));
  const toggle = (cat: string) => {
    const next = new Set(hidden);
    next.has(cat) ? next.delete(cat) : next.add(cat);
    setPrefs({ indexHiddenCategories: [...next] });
  };
  if (offered.length === 0) return null;
  return (
    <div className="mt-3 flex flex-wrap items-center gap-1.5">
      <span className="mr-1 text-[11px] uppercase tracking-wide text-muted">Show:</span>
      {offered.map((cat) => {
        const on = !hidden.has(cat);
        const has = present.has(cat);
        return (
          <button
            key={cat}
            onClick={() => toggle(cat)}
            title={on ? `Hide ${cat} from the index` : `Show ${cat} on the index`}
            className={`rounded-full border px-2.5 py-1 text-xs transition ${
              on
                ? "border-accent bg-accent text-accent-fg"
                : "border-border bg-surface text-muted hover:bg-surface-2"
            } ${!has ? "opacity-50" : ""}`}
          >
            {on ? "✓ " : ""}
            {cat}
          </button>
        );
      })}
    </div>
  );
}

export function CatalogRows({ onOpenDetail }: { onOpenDetail: (g: CatalogGroup) => void }) {
  const { prefs } = useApp();
  const rows = useQuery({ queryKey: ["catalog-rows"], queryFn: () => api.catalogRows() });
  const hidden = new Set(prefs.indexHiddenCategories ?? []);

  if (rows.isLoading) return <div className="mt-4"><Spinner label="Loading discovery…" /></div>;
  const data = rows.data ?? [];
  // Categories that actually have lanes (for the chip "has content" hint).
  const present = Array.from(new Set(data.map((r) => r.media_category)));

  if (data.length === 0) {
    return (
      <>
        <CategoryToggles available={present} />
        <p className="mt-3 text-sm text-muted">
          No works discovered yet — index a fiction site and they'll appear here, sorted by
          popularity and genre, as the crawler finds and enriches them.
        </p>
      </>
    );
  }

  // Section the lanes by media category, in the canonical order, skipping user-hidden ones.
  const visibleCats = MEDIA_CATEGORIES.filter(
    (cat) => !hidden.has(cat) && data.some((r) => r.media_category === cat)
  );

  return (
    <>
      <CategoryToggles available={present} />
      <div className="mt-4 space-y-8">
        {visibleCats.length === 0 && (
          <p className="text-sm text-muted">
            All categories are hidden — use the toggles above to show some.
          </p>
        )}
        {visibleCats.map((cat) => (
          <section key={cat}>
            <h3 className="mb-2 text-sm font-semibold uppercase tracking-wide text-muted">{cat}</h3>
            <div className="space-y-5">
              {data.filter((r) => r.media_category === cat).map((row) => (
                <Lane key={`${cat}:${row.kind}:${row.slug}`} row={row} onOpenDetail={onOpenDetail} />
              ))}
            </div>
          </section>
        ))}
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
            <span className="ml-1.5 text-xs font-normal text-muted">
              {row.count.toLocaleString()}
            </span>
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
            <Badge tone="green">in library</Badge>
          </span>
        )}
      </div>
      <div className="mt-1 line-clamp-2 text-xs font-medium leading-tight text-text group-hover:text-accent">
        {group.title}
      </div>
      <div className="mt-0.5 flex items-center gap-1 text-[10px] text-muted">
        <Badge tone={mediaTone(group.media_label)}>{group.media_label}</Badge>
        {group.chapters != null && <span>{group.chapters.toLocaleString()} ch</span>}
      </div>
    </button>
  );
}
