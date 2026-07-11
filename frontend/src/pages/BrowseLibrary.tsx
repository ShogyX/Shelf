// Browse library — the full poster grid + multi-select, filtered by shelf and the nav ?q= search.
// This is the dense, manage-everything surface (the cinematic home stays on "/"). Revamped: a stats
// strip in the hero, and a STICKY filter bar (format incl. comics, language pills from the EN/NO
// variant work, a needs-attention toggle fed by the integrity/match watcher, sort) — all URL-backed
// so a filtered view is shareable and survives refresh.
import { Link, useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { api, Work } from "../api/client";
import { qk } from "../api/queryKeys";
import { Badge, Button, EmptyState, OverflowMenu, PosterGridSkeleton, Select } from "../components/ui";
import { useApp } from "../store";
import { useIsAdmin } from "../auth";
import LibraryGrid from "../components/LibraryGrid";
import { BookOpen, Headphones, LibraryBig, Search, TriangleAlert, Zap } from "lucide-react";

// Language bucket for filtering — mirrors the backend's language.bucket: Norwegian variants fold to
// "no"; unknown/empty defaults to English; anything else keeps its 2-letter code.
function langBucket(lang: string | null | undefined): string {
  const c = (lang ?? "").trim().toLowerCase().slice(0, 3);
  if (["no", "nb", "nn", "nob", "nno", "nor"].some((p) => c === p || c.startsWith(p))) return "no";
  if (!c || c.startsWith("en")) return "en";
  return c.slice(0, 2);
}

// File/content problems the background watchers flag (integrity scan + wrong-match audit).
const ATTENTION = new Set(["missing", "corrupt", "mismatch"]);

const isAudio = (w: Work) => !!w.audiobook_work_id || w.media_kind === "audio";

export default function BrowseLibrary() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useApp((s) => s.toast);
  const isAdmin = useIsAdmin();
  const [sp, setSp] = useSearchParams();
  const q = (sp.get("q") ?? "").trim();
  // ?shelf=<id> filters to that shelf; ?shelf=all (or absent) = the whole library.
  const shelfParam = sp.get("shelf");
  const activeShelf = shelfParam && shelfParam !== "all" ? Number(shelfParam) : null;
  // Filters are URL-backed (replace, not push — filter twiddling must not pile up history).
  const format = sp.get("format") ?? "all";        // all | books | comics | audio
  const lang = sp.get("lang") ?? "all";            // all | en | no | <code>
  const attention = sp.get("attn") === "1";        // only flagged (missing/corrupt/mismatch)
  const sort = sp.get("sort") ?? "added";
  const setParam = (key: string, value: string | null, def: string | null = null) =>
    setSp((prev) => {
      const next = new URLSearchParams(prev);
      if (value == null || value === def) next.delete(key);
      else next.set(key, value);
      return next;
    }, { replace: true });

  const [selecting, setSelecting] = useState(false);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [downloading, setDownloading] = useState(false);
  const toggleSelected = (id: number) =>
    setSelected((s) => {
      const n = new Set(s);
      n.has(id) ? n.delete(id) : n.add(id);
      return n;
    });
  async function downloadSelected() {
    setDownloading(true);
    try {
      await api.downloadLibrary({ work_ids: [...selected] });
      setSelecting(false);
      setSelected(new Set());
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setDownloading(false);
    }
  }

  const { data: shelves = [] } = useQuery({ queryKey: qk.bookshelves(), queryFn: api.listBookshelves });
  const { data: works, isLoading, isError, refetch } = useQuery({
    queryKey: qk.works(q, activeShelf),
    queryFn: () => api.listWorks(q, { shelfId: activeShelf ?? undefined }),
    // Hold the previous grid while a new shelf/search loads, so it never flashes empty.
    placeholderData: keepPreviousData,
  });

  const checkAll = useMutation({
    mutationFn: () => api.checkAllUpdates(),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: qk.works() });
      toast(
        t("library.checkUpdatesResult", {
          titles: t("library.checkedTitles", { count: r.works_checked }),
          updated: r.works_updated,
          chapters: t("library.newChapters", { count: r.new_chapters }),
        }),
        "success"
      );
    },
    onError: (e) => toast((e as Error).message, "error"),
  });

  // Facet counts over the (shelf/search-scoped) library. A text title WITH a paired audiobook
  // counts under Books AND Audiobooks — that matches how people look for it.
  const all = works ?? [];
  const counts = useMemo(() => {
    const langs = new Map<string, number>();
    let books = 0, comics = 0, audio = 0, attn = 0;
    for (const w of all) {
      if (w.media_kind === "comic") comics++;
      else if (w.media_kind !== "audio") books++;
      if (isAudio(w)) audio++;
      if (ATTENTION.has(w.health)) attn++;
      const b = langBucket(w.language);
      langs.set(b, (langs.get(b) ?? 0) + 1);
    }
    return { books, comics, audio, attn, langs };
  }, [all]);
  // Language pills: en/no pinned first (the configured content languages), the long tail after.
  const langOptions = useMemo(() => {
    const keys = [...counts.langs.keys()].sort(
      (a, b) => (a === "en" ? -1 : b === "en" ? 1 : a === "no" ? -1 : b === "no" ? 1 : a.localeCompare(b)));
    return keys;
  }, [counts.langs]);

  const shown = useMemo(() => {
    const arr = all.filter((w) => {
      if (format === "books" && (w.media_kind !== "text")) return false;
      if (format === "comics" && w.media_kind !== "comic") return false;
      if (format === "audio" && !isAudio(w)) return false;
      if (lang !== "all" && langBucket(w.language) !== lang) return false;
      if (attention && !ATTENTION.has(w.health)) return false;
      return true;
    });
    const byTitle = (a: Work, b: Work) => a.title.localeCompare(b.title);
    switch (sort) {
      case "title": arr.sort(byTitle); break;
      case "author": arr.sort((a, b) => (a.author ?? "").localeCompare(b.author ?? "") || byTitle(a, b)); break;
      case "updated": arr.sort((a, b) => (b.last_update_at ?? "").localeCompare(a.last_update_at ?? "")); break;
      case "added":
      default: arr.sort((a, b) => b.id - a.id); break; // higher id ⇒ more recently added
    }
    return arr;
  }, [all, format, lang, attention, sort]);
  const filtered = format !== "all" || lang !== "all" || attention;
  const SORTS = [
    { value: "added", label: t("library.sortAdded") },
    { value: "title", label: t("library.sortTitle") },
    { value: "author", label: t("library.sortAuthor") },
    { value: "updated", label: t("library.sortUpdated") },
  ];

  const shelfChip = (id: number | null, label: string, count?: number) => {
    const on = activeShelf === id;
    return (
      <button
        key={id ?? "all"}
        onClick={() => setParam("shelf", id == null ? null : String(id))}
        className={`group inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full px-3.5 py-1.5 text-sm transition ${
          on
            ? "bg-accent font-semibold text-accent-fg shadow-sm"
            : "border border-[var(--hair,var(--border))] bg-bg text-muted hover:bg-surface-2 hover:text-text"
        }`}
      >
        <span className="max-w-[11rem] truncate">{label}</span>
        {count != null && count > 0 && (
          <span
            className={`rounded-full px-1.5 py-px text-[11px] font-medium tabular-nums ${
              on ? "bg-accent-fg/20 text-accent-fg" : "bg-surface-2 text-muted group-hover:text-text"
            }`}
          >
            {count}
          </span>
        )}
      </button>
    );
  };

  // One segmented-control button (format bar) / one pill (language bar).
  const seg = (on: boolean, onClick: () => void, label: React.ReactNode, extra = "") => (
    <button
      aria-pressed={on}
      onClick={onClick}
      className={`whitespace-nowrap px-3 py-1.5 text-sm font-medium transition ${extra} ${
        on ? "bg-accent text-accent-fg" : "bg-transparent text-muted hover:bg-surface-2 hover:text-text"
      }`}
    >
      {label}
    </button>
  );

  const actions = (
    <div className="flex flex-wrap items-center gap-2">
      {/* Phones: one compact menu instead of a two-row button stack above the stats. */}
      {!selecting && (
        <div className="sm:hidden">
          <OverflowMenu
            label={t("library.browseTitle")}
            items={[
              { label: t("library.select"), onClick: () => setSelecting(true) },
              isAdmin && { label: t("library.checkUpdates"), onClick: () => checkAll.mutate(), disabled: checkAll.isPending },
              { label: t("library.addAWork"), onClick: () => { window.location.href = "/discover"; } },
            ]}
          />
        </div>
      )}
      <div className={selecting ? "contents" : "hidden sm:contents"}>
      {selecting ? (
        <>
          <Button
            variant="primary"
            disabled={selected.size === 0 || downloading}
            onClick={downloadSelected}
            title={t("library.downloadSelectedTitle")}
          >
            {downloading ? t("library.preparing") : t("library.downloadSelected", { count: selected.size })}
          </Button>
          <Button variant="ghost" onClick={() => { setSelecting(false); setSelected(new Set()); }}>
            {t("common.cancel")}
          </Button>
        </>
      ) : (
        <Button variant="outline" title={t("library.selectTitle")} onClick={() => setSelecting(true)}>
          {t("library.select")}
        </Button>
      )}
      {isAdmin && (
        <Button
          variant="outline"
          title={t("library.checkUpdatesTitle")}
          disabled={checkAll.isPending}
          onClick={() => checkAll.mutate()}
        >
          {checkAll.isPending ? t("library.checking") : t("library.checkUpdates")}
        </Button>
      )}
      <Link to="/discover">
        <Button variant="outline">{t("library.addAWork")}</Button>
      </Link>
      </div>
    </div>
  );

  return (
    <main className="page-in">
      {/* Premium header band — matches the home/Discover/BrowseCatalog chrome. */}
      <section className="relative overflow-hidden border-b border-[var(--hair,var(--border))]">
        <div className="absolute inset-0" style={{
          background:
            "radial-gradient(120% 140% at 0% 0%, color-mix(in srgb, var(--accent) 16%, transparent), transparent 60%)," +
            "linear-gradient(0deg, var(--bg), transparent 70%)",
        }} />
        <div className="relative mx-auto max-w-6xl px-4 pb-6 pt-10 sm:px-6">
          <div className="mb-1.5 text-xs font-semibold uppercase tracking-widest text-[var(--accent-bright,var(--accent))]">
            {t("library.eyebrow")}
          </div>
          <div className="flex flex-wrap items-end justify-between gap-3">
            <h1 className="font-display text-[34px] font-semibold leading-[1.05] tracking-tight text-text sm:text-[44px]">
              {t("library.browseTitle")}
            </h1>
            {actions}
          </div>

          {/* Inventory at a glance: totals per format + per configured language. */}
          {all.length > 0 && (
            <p className="mt-2.5 text-sm text-[var(--text-soft,var(--muted))]">
              {t("library.statsTitles", { count: all.length })}
              {counts.books > 0 && <> · <BookOpen className="inline h-3.5 w-3.5 -mt-px" /> {t("library.statsBooks", { count: counts.books })}</>}
              {counts.comics > 0 && <> · <Zap className="inline h-3.5 w-3.5 -mt-px" /> {t("library.statsComics", { count: counts.comics })}</>}
              {counts.audio > 0 && <> · <Headphones className="inline h-3.5 w-3.5 -mt-px" /> {t("library.statsAudio", { count: counts.audio })}</>}
              {(counts.langs.get("no") ?? 0) > 0 && <> · 🇳🇴 {counts.langs.get("no")}</>}
            </p>
          )}

          {/* Shelf filter (All + each bookshelf). Create/manage shelves lives in Settings → Bookshelves. */}
          <div className="mt-5 flex items-center gap-2">
            <div className="flex min-w-0 flex-1 items-center gap-2 overflow-x-auto pb-1 scrollbar-none">
              {shelfChip(null, t("library.shelfAll"))}
              {shelves.map((s) => shelfChip(s.id, s.name, s.count))}
            </div>
            {shelves.length > 0 && (
              <Link to="/settings#bookshelves" className="shrink-0 px-2 text-xs text-muted underline hover:text-text">
                {t("library.manageShelves")}
              </Link>
            )}
          </div>
        </div>
      </section>

      {/* Sticky filter bar: pinned right below the nav (h-16) so filters stay reachable while
          scrolling a large grid. Translucent + blurred, matching the nav's chrome. */}
      {!isLoading && !isError && all.length > 0 && (
        <div className="sticky top-16 z-20 border-b border-[var(--hair,var(--border))] bg-[color-mix(in_srgb,var(--bg)_78%,transparent)] [backdrop-filter:blur(14px)_saturate(1.3)]">
          <div className="mx-auto flex max-w-6xl items-center gap-x-3 gap-y-2 overflow-x-auto px-4 py-2.5 scrollbar-none sm:flex-wrap sm:overflow-visible sm:px-6">
            {/* Format — segmented. Books/Comics/Audiobooks with live counts. */}
            <div role="group" aria-label={t("library.filterByFormat")}
                 className="inline-flex shrink-0 overflow-hidden rounded-lg border border-[var(--hair,var(--border))]">
              {seg(format === "all", () => setParam("format", null, null), t("library.filterAll", { n: all.length }))}
              {counts.books > 0 && seg(format === "books", () => setParam("format", "books"), <><BookOpen className="mr-1 inline h-3.5 w-3.5 -mt-px" />{t("library.statsBooks", { count: counts.books })}</>)}
              {counts.comics > 0 && seg(format === "comics", () => setParam("format", "comics"), <><Zap className="mr-1 inline h-3.5 w-3.5 -mt-px" />{t("library.statsComics", { count: counts.comics })}</>)}
              {counts.audio > 0 && seg(format === "audio", () => setParam("format", "audio"), <><Headphones className="mr-1 inline h-3.5 w-3.5 -mt-px" />{t("library.statsAudio", { count: counts.audio })}</>)}
            </div>

            {/* Language — only when the library actually spans >1 language. */}
            {langOptions.length > 1 && (
              <div role="group" aria-label={t("library.filterByLanguage")}
                   className="inline-flex shrink-0 overflow-hidden rounded-lg border border-[var(--hair,var(--border))]">
                {seg(lang === "all", () => setParam("lang", null, null), t("library.langAll"))}
                {langOptions.map((code) =>
                  seg(lang === code, () => setParam("lang", code),
                      `${code === "un" ? "?" : code.toUpperCase()} ${counts.langs.get(code) ?? 0}`))}
              </div>
            )}

            {/* Needs attention — the integrity/match watcher's flagged titles, one tap away. */}
            {counts.attn > 0 && (
              <button
                aria-pressed={attention}
                onClick={() => setParam("attn", attention ? null : "1")}
                className={`inline-flex shrink-0 items-center gap-1.5 rounded-lg border px-3 py-1.5 text-sm font-medium transition ${
                  attention
                    ? "border-transparent bg-[color-mix(in_srgb,#ef4444_18%,var(--surface))] text-text"
                    : "border-[var(--hair,var(--border))] text-muted hover:bg-surface-2 hover:text-text"
                }`}
                title={t("library.attentionHint")}
              >
                <TriangleAlert className="mr-1 inline h-3.5 w-3.5 -mt-px" />{t("library.attention")} <Badge tone="red">{counts.attn}</Badge>
              </button>
            )}

            <div className="ml-auto flex shrink-0 items-center gap-2 text-sm text-muted">
              {filtered && (
                <button className="underline hover:text-text"
                        onClick={() => { setParam("format", null); setParam("lang", null); setParam("attn", null); }}>
                  {t("library.clearFilters")}
                </button>
              )}
              <span className="hidden sm:inline">{t("library.sort")}</span>
              <div className="w-[160px]">
                <Select value={sort} onChange={(v) => setParam("sort", v, "added")} options={SORTS} />
              </div>
            </div>
          </div>
        </div>
      )}

      <div className="mx-auto max-w-6xl px-4 pb-10 pt-6 sm:px-6">
      {isLoading && <PosterGridSkeleton count={12} />}

      {!isLoading && isError && (
        <EmptyState
          title={t("library.loadErrorTitle")}
          hint={t("library.loadErrorHint")}
          action={<Button variant="primary" onClick={() => refetch()}>{t("common.retry")}</Button>}
        />
      )}

      {!isLoading && !isError && all.length === 0 && (
        q ? (
          <EmptyState
            title={t("library.noMatchTitle", { query: q })}
            hint={t("library.noMatchHint")}
          />
        ) : (
          <EmptyState
            icon={<LibraryBig className="h-7 w-7" />}
            title={t("library.emptyTitle")}
            hint={t("library.emptyHint")}
            action={
              <Link to="/discover">
                <Button variant="primary">{t("library.addFirstWork")}</Button>
              </Link>
            }
          />
        )
      )}

      {!isLoading && !isError && all.length > 0 && (q || filtered) && (
        <p className="mb-3 text-sm text-muted">
          {q
            ? t("library.resultsFor", { count: shown.length, query: q })
            : t("library.filteredCount", { count: shown.length, total: all.length })}
        </p>
      )}

      {!isLoading && !isError && all.length > 0 && shown.length === 0 && (
        <EmptyState
          icon={<Search className="h-7 w-7" />}
          title={t("library.noFilterMatchTitle")}
          hint={t("library.noFilterMatchHint")}
          action={
            <Button variant="outline"
                    onClick={() => { setParam("format", null); setParam("lang", null); setParam("attn", null); }}>
              {t("library.clearFilters")}
            </Button>
          }
        />
      )}

      {!isLoading && !isError && shown.length > 0 && (
        <LibraryGrid
          works={shown}
          shelves={shelves}
          selecting={selecting}
          selected={selected}
          onToggleSelect={toggleSelected}
        />
      )}
      </div>
    </main>
  );
}
