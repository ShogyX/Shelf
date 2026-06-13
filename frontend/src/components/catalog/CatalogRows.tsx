// Netflix-style discovery rows for the Index page: a "Most Popular" lane plus the marquee genre
// and theme lanes, grouped into a section per MEDIA CATEGORY (Manga / Manhua / Webtoon / Comic /
// Novel / Book). Each lane is a horizontal scroller of compact poster cards (click → the shared
// detail modal to hook), with a "Browse →" link to the full sorted grid. A chip row lets the user
// hide categories they don't care about (persisted per-user).
import { useState, type ReactNode } from "react";
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

// Stable key for a genre/popular lane within its category section.
const laneKey = (r: CatalogRow): string => `${r.media_category}|${r.kind}|${r.slug}`;

/** Order `items` by a saved key order; items not in the order keep their original relative position,
 *  placed after the ordered ones (so a newly-discovered lane/category just appears at the end). */
function applyOrder<T>(items: T[], keyOf: (t: T) => string, order?: string[]): T[] {
  if (!order || order.length === 0) return items;
  const pos = new Map(order.map((k, i) => [k, i] as const));
  return items
    .map((it, i) => ({ it, i, p: pos.has(keyOf(it)) ? pos.get(keyOf(it))! : Infinity }))
    .sort((a, b) => a.p - b.p || a.i - b.i)
    .map((x) => x.it);
}

/** Move/hide controls shown in edit mode next to a category or genre header. */
function EditControls({ onUp, onDown, upDisabled, downDisabled, hidden, onToggle }: {
  onUp: () => void; onDown: () => void; upDisabled: boolean; downDisabled: boolean;
  hidden: boolean; onToggle: () => void;
}) {
  const btn = "rounded border border-border px-1.5 py-0.5 text-[11px] leading-none text-muted " +
    "hover:bg-surface-2 disabled:opacity-30 disabled:hover:bg-transparent";
  return (
    <span className="inline-flex items-center gap-1">
      <button className={btn} disabled={upDisabled} onClick={onUp} title="Move up" aria-label="Move up">▲</button>
      <button className={btn} disabled={downDisabled} onClick={onDown} title="Move down" aria-label="Move down">▼</button>
      <button className={btn} onClick={onToggle} title={hidden ? "Show" : "Hide"}>{hidden ? "Show" : "Hide"}</button>
    </span>
  );
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
  const { prefs, setPrefs } = useApp();
  const allowed = useAuth((s) => s.me?.allowed_categories);
  const rows = useQuery({ queryKey: ["catalog-rows"], queryFn: () => api.catalogRows() });
  const [editing, setEditing] = useState(false);

  if (rows.isLoading) return <div className="mt-4"><Spinner label="Loading discovery…" /></div>;
  const data = rows.data ?? [];
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

  const hiddenCats = new Set(prefs.indexHiddenCategories ?? []);
  const hiddenLanes = new Set(prefs.indexHiddenLanes ?? []);
  // Categories present + admin-permitted, in the user's saved order (then canonical for new ones).
  const orderedCats = applyOrder(
    MEDIA_CATEGORIES.filter((c) => (allowed ?? MEDIA_CATEGORIES).includes(c)
      && data.some((r) => r.media_category === c)),
    (c) => c, prefs.indexCategoryOrder,
  );
  const lanesFor = (cat: string) =>
    applyOrder(data.filter((r) => r.media_category === cat), laneKey, prefs.indexLaneOrder);

  // --- layout mutations (persisted to the per-user reader prefs) ---
  const swapped = <T,>(list: T[], keyOf: (t: T) => string, key: string, dir: number): string[] | null => {
    const keys = list.map(keyOf);
    const i = keys.indexOf(key), j = i + dir;
    if (i < 0 || j < 0 || j >= keys.length) return null;
    [keys[i], keys[j]] = [keys[j], keys[i]];
    return keys;
  };
  const moveCat = (cat: string, dir: number) => {
    const next = swapped(orderedCats, (c) => c, cat, dir);
    if (next) setPrefs({ indexCategoryOrder: next });
  };
  const moveLane = (cat: string, key: string, dir: number) => {
    const within = swapped(lanesFor(cat), laneKey, key, dir);
    if (!within) return;
    // Persist a full flat lane order: each category's lanes in order, this one replaced by `within`.
    const flat: string[] = [];
    for (const c of orderedCats) flat.push(...(c === cat ? within : lanesFor(c).map(laneKey)));
    setPrefs({ indexLaneOrder: flat });
  };
  const toggleCat = (cat: string) => {
    const n = new Set(hiddenCats); n.has(cat) ? n.delete(cat) : n.add(cat);
    setPrefs({ indexHiddenCategories: [...n] });
  };
  const toggleLane = (key: string) => {
    const n = new Set(hiddenLanes); n.has(key) ? n.delete(key) : n.add(key);
    setPrefs({ indexHiddenLanes: [...n] });
  };
  const resetLayout = () => setPrefs({
    indexHiddenCategories: [], indexCategoryOrder: [], indexHiddenLanes: [], indexLaneOrder: [],
  });

  const renderCats = editing ? orderedCats : orderedCats.filter((c) => !hiddenCats.has(c));

  return (
    <>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        {!editing && <CategoryToggles available={present} />}
        <button
          onClick={() => setEditing((e) => !e)}
          className={`rounded-full border px-3 py-1 text-xs transition ${
            editing ? "border-accent bg-accent text-accent-fg" : "border-border bg-surface text-muted hover:bg-surface-2"
          }`}
        >
          {editing ? "✓ Done" : "✎ Edit layout"}
        </button>
        {editing && (
          <>
            <button onClick={resetLayout} className="rounded-full border border-border bg-surface px-3 py-1 text-xs text-muted hover:bg-surface-2">
              Reset
            </button>
            <span className="text-xs text-muted">Reorder with ▲▼ and Hide/Show sections &amp; genres — saved automatically.</span>
          </>
        )}
      </div>
      <div className="mt-4 space-y-8">
        {renderCats.length === 0 && !editing && (
          <p className="text-sm text-muted">All categories are hidden — click “Edit layout” to show some.</p>
        )}
        {renderCats.map((cat, ci) => {
          const catHidden = hiddenCats.has(cat);
          const lanes = lanesFor(cat);
          const visibleLanes = editing ? lanes : lanes.filter((r) => !hiddenLanes.has(laneKey(r)));
          return (
            <section key={cat} className={catHidden ? "opacity-50" : ""}>
              <div className="mb-2 flex items-center gap-2">
                <h3 className="text-sm font-semibold uppercase tracking-wide text-muted">{cat}</h3>
                {editing && (
                  <EditControls
                    onUp={() => moveCat(cat, -1)} onDown={() => moveCat(cat, 1)}
                    upDisabled={ci === 0} downDisabled={ci === renderCats.length - 1}
                    hidden={catHidden} onToggle={() => toggleCat(cat)}
                  />
                )}
              </div>
              {/* A hidden category collapses to just its header in edit mode (Show to expand). */}
              {!catHidden && (
                <div className="space-y-5">
                  {visibleLanes.map((row, li) => {
                    const k = laneKey(row);
                    const laneHidden = hiddenLanes.has(k);
                    const controls = editing ? (
                      <EditControls
                        onUp={() => moveLane(cat, k, -1)} onDown={() => moveLane(cat, k, 1)}
                        upDisabled={li === 0} downDisabled={li === visibleLanes.length - 1}
                        hidden={laneHidden} onToggle={() => toggleLane(k)}
                      />
                    ) : null;
                    // A hidden genre (edit mode only) shows just a compact greyed header + Show.
                    if (laneHidden) {
                      return (
                        <div key={k} className="flex items-center gap-2 opacity-50">
                          <h4 className="text-base font-semibold text-text">{row.label}</h4>
                          {controls}
                        </div>
                      );
                    }
                    return <Lane key={k} row={row} onOpenDetail={onOpenDetail} controls={controls} />;
                  })}
                </div>
              )}
            </section>
          );
        })}
      </div>
    </>
  );
}

function Lane({ row, onOpenDetail, controls }: {
  row: CatalogRow; onOpenDetail: (g: CatalogGroup) => void; controls?: ReactNode;
}) {
  return (
    <div>
      <div className="mb-1.5 flex items-baseline justify-between gap-3">
        <h4 className="flex items-baseline gap-2 text-base font-semibold text-text">
          <span>
            {row.label}
            {row.kind !== "popular" && (
              <span className="ml-1.5 text-xs font-normal text-muted">{row.count.toLocaleString()}</span>
            )}
          </span>
          {controls}
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
