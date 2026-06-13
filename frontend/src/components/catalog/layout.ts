// Shared helpers for the Index discovery layout (category + genre order / hide), used by both the
// Index page (read/apply) and the Settings → Layout matrix (edit). A "lane" is a genre/popular row
// inside a media category, keyed stably as "<category>|<kind>|<slug>".
import type { CatalogRow, IndexLayout, ReaderPrefs } from "../../api/client";

export const EMPTY_LAYOUT: IndexLayout = {
  categoryOrder: [], hiddenCategories: [], laneOrder: [], hiddenLanes: [],
};

export const laneKey = (r: CatalogRow): string => `${r.media_category}|${r.kind}|${r.slug}`;

/** Order `items` by a saved key order; items not in the order keep their original relative position,
 *  placed after the ordered ones (so a newly-discovered lane/category appears at the end). */
export function applyOrder<T>(items: T[], keyOf: (t: T) => string, order?: string[]): T[] {
  if (!order || order.length === 0) return items;
  const pos = new Map(order.map((k, i) => [k, i] as const));
  return items
    .map((it, i) => ({ it, i, p: pos.has(keyOf(it)) ? pos.get(keyOf(it))! : Infinity }))
    .sort((a, b) => a.p - b.p || a.i - b.i)
    .map((x) => x.it);
}

/** Swap `key` with its neighbour in `keys` by `dir` (±1). Returns the new key array, or null at a
 *  boundary. */
export function swapped(keys: string[], key: string, dir: number): string[] | null {
  const next = [...keys];
  const i = next.indexOf(key), j = i + dir;
  if (i < 0 || j < 0 || j >= next.length) return null;
  [next[i], next[j]] = [next[j], next[i]];
  return next;
}

/** A user's EFFECTIVE layout: their personal one when they've customized, else the global default. */
export function effectiveLayout(prefs: ReaderPrefs, globalDefault: IndexLayout | undefined): IndexLayout {
  if (prefs.indexLayoutCustom) {
    return {
      categoryOrder: prefs.indexCategoryOrder ?? [],
      hiddenCategories: prefs.indexHiddenCategories ?? [],
      laneOrder: prefs.indexLaneOrder ?? [],
      hiddenLanes: prefs.indexHiddenLanes ?? [],
    };
  }
  return globalDefault ?? EMPTY_LAYOUT;
}

/** Mirror an IndexLayout into the per-user ReaderPrefs fields (for saving a personal layout). */
export function layoutToPrefs(layout: IndexLayout): Partial<ReaderPrefs> {
  return {
    indexLayoutCustom: true,
    indexCategoryOrder: layout.categoryOrder,
    indexHiddenCategories: layout.hiddenCategories,
    indexLaneOrder: layout.laneOrder,
    indexHiddenLanes: layout.hiddenLanes,
  };
}

// Categories present in `rows`, optionally restricted to an allow-list, in the layout's order.
export function orderedCategories(rows: CatalogRow[], layout: IndexLayout, allowed?: string[]): string[] {
  const present = Array.from(new Set(rows.map((r) => r.media_category)))
    .filter((c) => !allowed || allowed.includes(c));
  return applyOrder(present, (c) => c, layout.categoryOrder);
}

// Lanes of one category, in the layout's order.
export function lanesForCategory(rows: CatalogRow[], cat: string, layout: IndexLayout): CatalogRow[] {
  return applyOrder(rows.filter((r) => r.media_category === cat), laneKey, layout.laneOrder);
}

// --- pure mutations returning a NEW layout (used by the editor matrix) ---
export function moveCategory(rows: CatalogRow[], layout: IndexLayout, cat: string, dir: number): IndexLayout {
  const cats = orderedCategories(rows, layout);
  const next = swapped(cats, cat, dir);
  return next ? { ...layout, categoryOrder: next } : layout;
}

export function moveLane(rows: CatalogRow[], layout: IndexLayout, cat: string, key: string, dir: number): IndexLayout {
  const within = swapped(lanesForCategory(rows, cat, layout).map(laneKey), key, dir);
  if (!within) return layout;
  // Rebuild a flat lane order across every category, this one replaced by `within`.
  const flat: string[] = [];
  for (const c of orderedCategories(rows, layout)) {
    flat.push(...(c === cat ? within : lanesForCategory(rows, c, layout).map(laneKey)));
  }
  return { ...layout, laneOrder: flat };
}

const toggle = (list: string[], key: string): string[] =>
  list.includes(key) ? list.filter((x) => x !== key) : [...list, key];

export const toggleCategory = (layout: IndexLayout, cat: string): IndexLayout =>
  ({ ...layout, hiddenCategories: toggle(layout.hiddenCategories, cat) });

export const toggleLaneHidden = (layout: IndexLayout, key: string): IndexLayout =>
  ({ ...layout, hiddenLanes: toggle(layout.hiddenLanes, key) });
