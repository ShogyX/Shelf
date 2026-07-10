// Netflix-style discovery rows for the Index page: a "Most Popular" lane plus the marquee genre
// and theme lanes, grouped into a section per MEDIA CATEGORY (Manga & Comics / Novel / Book).
// The arrangement comes from the user's EFFECTIVE layout — their personal one if they've customized
// it via "Edit layout" here, else the admin's global default (set in Settings → Index layout).
// The rows the server returns are already filtered to the categories + 18+ content this user may
// see, so editing only ever reorders/hides authorized content.
import { useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import { api, CatalogGroup, CatalogRow } from "../../api/client";
import { qk } from "../../api/queryKeys";
import { Badge, Spinner } from "../ui";
import { RailScroller } from "../Rail";
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
  const { t } = useTranslation();
  const btn = "rounded border border-border px-1.5 py-0.5 text-[11px] leading-none text-muted " +
    "hover:bg-surface-2 disabled:opacity-30 disabled:hover:bg-transparent";
  return (
    <span className="inline-flex items-center gap-1">
      <button className={btn} disabled={upDisabled} onClick={onUp} title={t("catalogRows.moveUp")} aria-label={t("catalogRows.moveUp")}>▲</button>
      <button className={btn} disabled={downDisabled} onClick={onDown} title={t("catalogRows.moveDown")} aria-label={t("catalogRows.moveDown")}>▼</button>
      <button className={btn} onClick={onToggle} title={hidden ? t("catalogRows.show") : t("catalogRows.hide")}>{hidden ? t("catalogRows.show") : t("catalogRows.hide")}</button>
    </span>
  );
}

export function CatalogRows({ onOpenDetail }: { onOpenDetail: (g: CatalogGroup) => void }) {
  const { t } = useTranslation();
  const { prefs, setPrefs } = useApp();
  const allowed = useAuth((s) => s.me?.allowed_categories);
  // staleTime matches the Index page's observers on the SAME keys — one stale-0 observer would
  // re-trigger the fetch on every mount and defeat the shared client cache.
  const rowsQ = useQuery({ queryKey: qk.catalogRows(), queryFn: () => api.catalogRows(), staleTime: 60_000 });
  const globalQ = useQuery({ queryKey: qk.indexLayout(), queryFn: () => api.getIndexLayout(), staleTime: 300_000 });
  const [editing, setEditing] = useState(false);

  if (rowsQ.isLoading) return <div className="mt-4"><Spinner label={t("catalogRows.loadingDiscovery")} /></div>;
  const data = rowsQ.data ?? [];
  if (data.length === 0) {
    return (
      <p className="mt-3 text-sm text-muted">
        {t("catalogRows.emptyDiscovery")}
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
            {t("catalogRows.resetToDefault")}
          </button>
        )}
        {editing && <span className="text-xs text-muted">{t("catalogRows.editHint")}</span>}
        <button
          onClick={() => setEditing((e) => !e)}
          className={`rounded-full border px-3 py-1 text-xs transition ${
            editing ? "border-accent bg-accent text-accent-fg" : "border-border bg-surface text-muted hover:bg-surface-2"
          }`}
        >
          {editing ? t("catalogRows.done") : t("catalogRows.editLayout")}
        </button>
      </div>
      <div className="mt-2 space-y-8">
        {cats.length === 0 && !editing && (
          <p className="text-sm text-muted">
            {t("catalogRows.allHidden")}
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
                <div className="space-y-8">
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
  const { t } = useTranslation();
  return (
    // Same faint, edge-faded hairline cut between rails as the Library (Rail.tsx) — a ::before centered
    // in the gap. Hidden above the FIRST lane of a category, where the uppercase category header already
    // separates it.
    <div className="relative before:pointer-events-none before:absolute before:inset-x-0 before:-top-4 before:h-px before:bg-gradient-to-r before:from-transparent before:via-[var(--hair-strong,var(--border))] before:to-transparent before:content-[''] first:before:hidden">
      <div className="mb-3.5 flex items-baseline justify-between gap-3 px-1">
        {/* Same title treatment as the Library rails (font-display 23px). */}
        <h4 className="flex items-baseline gap-2 font-display text-[23px] font-semibold tracking-tight text-text">
          <span>
            {row.label}
            {row.kind !== "popular" && (
              <span className="ml-2 align-middle text-[13px] font-normal text-muted">{row.count.toLocaleString()}</span>
            )}
          </span>
          {controls}
        </h4>
        <Link to={browseHref(row)} className="shrink-0 text-[13px] font-semibold text-[var(--accent-bright,var(--accent))] opacity-90 hover:opacity-100">
          {t("catalogRows.browseAll")}
        </Link>
      </div>
      {/* Shared rail scroller: hidden scrollbar + hover arrows just outside the rail (same as Library). */}
      <RailScroller gap="gap-3">
        {row.items.map((g) => (
          <PosterCard key={g.id || g.norm_key} group={g} onOpen={() => onOpenDetail(g)} />
        ))}
      </RailScroller>
    </div>
  );
}

function PosterCard({ group, onOpen }: { group: CatalogGroup; onOpen: () => void }) {
  const { t } = useTranslation();
  return (
    <button
      onClick={onOpen}
      className="group w-32 shrink-0 text-left"
      title={t("catalogRows.cardTitle", { title: group.title })}
    >
      <div className="relative aspect-[2/3] w-full overflow-hidden rounded-lg border border-border bg-surface shadow-sm hover-lift group-hover:shadow-lg">
        <Cover title={group.title} author={group.author} coverUrl={group.cover_url} small />
        <div className="pointer-events-none absolute inset-0 transition group-hover:bg-black/5" />
      </div>
      <div className="mt-1 line-clamp-2 text-xs font-medium leading-tight text-text group-hover:text-accent">
        {group.title}
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-1 text-[11px] text-muted">
        <Badge tone={mediaTone(group.media_label)}>{group.media_label}</Badge>
        {/* in-library / in-stock now sits BESIDE the type pill instead of over the cover art. */}
        {group.hooked_work_id && (
          <Badge tone={group.in_library ? "green" : "violet"}>
            {group.in_library ? t("catalogRows.inLibrary") : t("catalogRows.inStock")}
          </Badge>
        )}
        {group.chapters != null && <span>{t("catalogRows.chaptersShort", { count: group.chapters.toLocaleString() })}</span>}
      </div>
    </button>
  );
}
