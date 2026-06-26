// Netflix-style discovery rows for the Index page: a "Most Popular" lane plus the marquee genre
// and theme lanes, grouped into a section per MEDIA CATEGORY (Manga & Comics / Novel / Book).
// The arrangement comes from the user's EFFECTIVE layout — their personal one if they've customized
// it via "Edit layout" here, else the admin's global default (set in Settings → Index layout).
// The rows the server returns are already filtered to the categories + 18+ content this user may
// see, so editing only ever reorders/hides authorized content.
import { useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, CatalogGroup, CatalogRow } from "../../api/client";
import { qk } from "../../api/queryKeys";
import { Badge, Spinner } from "../ui";
import Cover from "../Cover";
import { mediaTone } from "./CatalogCard";
import { useApp } from "../../store";
import { useAuth } from "../../auth";
import {
  EMPTY_LAYOUT, effectiveLayout, laneKey, lanesForCategory, layoutToPrefs,
  moveCategory, moveLane, orderedCategories, toggleCategory, toggleLaneHidden,
} from "./layout";

function browseHref(row: CatalogRow): string {
  const dim = row.kind === "popular" ? "popular" : row.kind;
  const val = row.slug || "all";
  return `/browse/${dim}/${encodeURIComponent(val)}?media=${encodeURIComponent(row.media_category)}`;
}

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

export function CatalogRows({ onOpenDetail }: { onOpenDetail: (g: CatalogGroup) => void }) {
  const { prefs, setPrefs } = useApp();
  const allowed = useAuth((s) => s.me?.allowed_categories);
  const rowsQ = useQuery({ queryKey: qk.catalogRows(), queryFn: () => api.catalogRows() });
  const globalQ = useQuery({ queryKey: qk.indexLayout(), queryFn: () => api.getIndexLayout() });
  const [editing, setEditing] = useState(false);

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

  // Effective layout: the user's own (when they've customized) else the admin global default.
  const layout = effectiveLayout(prefs, globalQ.data ?? EMPTY_LAYOUT);
  // Any edit writes the PERSONAL layout (sets indexLayoutCustom=true), overriding the global default;
  // the first edit seeds from the current effective layout.
  const update = (next: typeof layout) => setPrefs(layoutToPrefs(next));

  // `allowed` is belt-and-braces: the server already returns only permitted categories.
  const allCats = orderedCategories(data, layout, allowed ?? undefined);
  const cats = editing ? allCats : allCats.filter((c) => !layout.hiddenCategories.includes(c));

  return (
    <>
      <div className="mt-3 flex items-center justify-end gap-2">
        {editing && prefs.indexLayoutCustom && (
          <button
            onClick={() => setPrefs({ indexLayoutCustom: false })}
            className="rounded-full border border-border bg-surface px-3 py-1 text-xs text-muted hover:bg-surface-2"
          >
            Reset to default
          </button>
        )}
        {editing && <span className="text-xs text-muted">Reorder ▲▼ and Hide/Show — saved to your account.</span>}
        <button
          onClick={() => setEditing((e) => !e)}
          className={`rounded-full border px-3 py-1 text-xs transition ${
            editing ? "border-accent bg-accent text-accent-fg" : "border-border bg-surface text-muted hover:bg-surface-2"
          }`}
        >
          {editing ? "✓ Done" : "✎ Edit layout"}
        </button>
      </div>
      <div className="mt-2 space-y-8">
        {cats.length === 0 && !editing && (
          <p className="text-sm text-muted">
            All categories are hidden — click “Edit layout” to show some.
          </p>
        )}
        {cats.map((cat, ci) => {
          const catHidden = layout.hiddenCategories.includes(cat);
          const allLanes = lanesForCategory(data, cat, layout);
          const lanes = editing ? allLanes : allLanes.filter((r) => !layout.hiddenLanes.includes(laneKey(r)));
          if (!editing && lanes.length === 0) return null;
          return (
            <section key={cat} className={catHidden ? "opacity-50" : ""}>
              <div className="mb-2 flex items-center gap-2">
                <h3 className="text-sm font-semibold uppercase tracking-wide text-muted">{cat}</h3>
                {editing && (
                  <EditControls
                    onUp={() => update(moveCategory(data, layout, cat, -1))}
                    onDown={() => update(moveCategory(data, layout, cat, 1))}
                    upDisabled={ci === 0} downDisabled={ci === cats.length - 1}
                    hidden={catHidden} onToggle={() => update(toggleCategory(layout, cat))}
                  />
                )}
              </div>
              {/* A hidden category collapses to its header in edit mode (Show to expand). */}
              {!catHidden && (
                <div className="space-y-5">
                  {lanes.map((row, li) => {
                    const k = laneKey(row);
                    const laneHidden = layout.hiddenLanes.includes(k);
                    const controls = editing ? (
                      <EditControls
                        onUp={() => update(moveLane(data, layout, cat, k, -1))}
                        onDown={() => update(moveLane(data, layout, cat, k, 1))}
                        upDisabled={li === 0} downDisabled={li === lanes.length - 1}
                        hidden={laneHidden} onToggle={() => update(toggleLaneHidden(layout, k))}
                      />
                    ) : null;
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
          Browse all
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
      <div className="relative aspect-[2/3] w-full overflow-hidden rounded-lg border border-border bg-surface shadow-sm hover-lift group-hover:shadow-lg">
        <Cover title={group.title} author={group.author} coverUrl={group.cover_url} small />
        <div className="pointer-events-none absolute inset-0 transition group-hover:bg-black/5" />
        {group.hooked_work_id && (
          <span className="absolute left-1 top-1">
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
