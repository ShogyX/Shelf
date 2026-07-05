// Browse library — the full poster grid + multi-select, filtered by shelf and the nav ?q= search.
// This is where the old Library "management" grid moved: shelf filter (All + each shelf), the
// Select / Download(N) / Cancel multi-select toolbar, and the shared <LibraryGrid>. The cinematic
// home (hero + rails) stays on "/"; this is the dense, manage-everything surface.
import { Link, useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { api } from "../api/client";
import { qk } from "../api/queryKeys";
import { Button, EmptyState, PosterGridSkeleton, Select } from "../components/ui";
import { useApp } from "../store";
import { useIsAdmin } from "../auth";
import LibraryGrid from "../components/LibraryGrid";

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

  const [media, setMedia] = useState<"all" | "books" | "audio">("all"); // reading vs listening filter
  const [sort, setSort] = useState("added"); // grid ordering
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

  const setShelf = (id: number | null) =>
    setSp((prev) => {
      const next = new URLSearchParams(prev);
      if (id == null) next.delete("shelf");
      else next.set("shelf", String(id));
      return next;
    }, { replace: true });

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

  // Reading vs listening: a title is an "audiobook" if it has a paired audiobook (the "listen"
  // format) — books are the read-only rest. The filter narrows the grid; counts label the tabs.
  const isAudio = (w: { audiobook_work_id?: number | null; media_kind?: string }) =>
    !!w.audiobook_work_id || w.media_kind === "audio";
  const audioCount = (works ?? []).filter(isAudio).length;
  const bookCount = (works?.length ?? 0) - audioCount;
  // Filter by format, then order by the chosen sort. Client-side: the library is already fully
  // loaded for the grid, so this stays a cheap in-memory pass.
  const shown = useMemo(() => {
    const arr = (works ?? []).filter((w) =>
      media === "all" ? true : media === "audio" ? isAudio(w) : !isAudio(w));
    const byTitle = (a: typeof arr[number], b: typeof arr[number]) => a.title.localeCompare(b.title);
    switch (sort) {
      case "title": arr.sort(byTitle); break;
      case "author": arr.sort((a, b) => (a.author ?? "").localeCompare(b.author ?? "") || byTitle(a, b)); break;
      case "updated": arr.sort((a, b) => (b.last_update_at ?? "").localeCompare(a.last_update_at ?? "")); break;
      case "added":
      default: arr.sort((a, b) => b.id - a.id); break; // higher id ⇒ more recently added
    }
    return arr;
  }, [works, media, sort]);
  const SORTS = [
    { value: "added", label: t("library.sortAdded") },
    { value: "title", label: t("library.sortTitle") },
    { value: "author", label: t("library.sortAuthor") },
    { value: "updated", label: t("library.sortUpdated") },
  ];

  const chip = (id: number | null, label: string, count?: number) => {
    const on = activeShelf === id;
    return (
      <button
        key={id ?? "all"}
        onClick={() => setShelf(id)}
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

  const actions = (
    <div className="flex flex-wrap items-center gap-2">
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
  );

  return (
    <main className="page-in">
      {/* Premium header band — matches the home/Discover/BrowseCatalog chrome (accent-tinted full-
          bleed hero with eyebrow + Newsreader title), so the management surface reads as part of the
          redesign. Actions + the shelf-filter chips live in the same band. */}
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

          {/* Shelf filter (All + each bookshelf). Create/manage shelves lives in Settings → Bookshelves.
              Chips scroll horizontally on narrow screens; "Manage shelves" stays pinned outside the
              scroll so it never clips off the right edge on mobile. */}
          <div className="mt-5 flex items-center gap-2">
            <div className="flex min-w-0 flex-1 items-center gap-2 overflow-x-auto pb-1 scrollbar-none">
              {chip(null, t("library.shelfAll"))}
              {shelves.map((s) => chip(s.id, s.name, s.count))}
            </div>
            {shelves.length > 0 && (
              <Link to="/settings#bookshelves" className="shrink-0 px-2 text-xs text-muted underline hover:text-text">
                {t("library.manageShelves")}
              </Link>
            )}
          </div>
        </div>
      </section>

      <div className="mx-auto max-w-6xl px-4 pb-10 pt-6 sm:px-6">
      {isLoading && <PosterGridSkeleton count={12} />}

      {!isLoading && isError && (
        <EmptyState
          title={t("library.loadErrorTitle")}
          hint={t("library.loadErrorHint")}
          action={<Button variant="primary" onClick={() => refetch()}>{t("common.retry")}</Button>}
        />
      )}

      {!isLoading && !isError && (!works || works.length === 0) && (
        q ? (
          <EmptyState
            title={t("library.noMatchTitle", { query: q })}
            hint={t("library.noMatchHint")}
          />
        ) : (
          <EmptyState
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

      {!isLoading && q && works && works.length > 0 && (
        <p className="mb-3 text-sm text-muted">
          {t("library.resultsFor", { count: works.length, query: q })}
        </p>
      )}

      {/* Controls row: format filter (reading vs listening, only when there's an audiobook to split
          out) on the left, sort order on the right. */}
      {!isLoading && !isError && works && works.length > 0 && (
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
          {audioCount > 0 ? (
            <div role="group" aria-label={t("library.filterByFormat")} className="inline-flex overflow-hidden rounded-lg border border-border text-sm">
              {([
                ["all", t("library.filterAll", { n: works.length })],
                ["books", t("library.filterBooks", { n: bookCount })],
                ["audio", t("library.filterAudiobooks", { n: audioCount })],
              ] as const).map(([key, label]) => (
                <button
                  key={key}
                  aria-pressed={media === key}
                  onClick={() => setMedia(key)}
                  className={`px-3 py-1.5 font-medium transition ${
                    media === key ? "bg-accent text-accent-fg" : "bg-surface text-muted hover:bg-surface-2 hover:text-text"
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>
          ) : <span />}
          <div className="flex items-center gap-2 text-sm text-muted">
            <span className="hidden sm:inline">{t("library.sort")}</span>
            <div className="w-[170px]">
              <Select value={sort} onChange={setSort} options={SORTS} />
            </div>
          </div>
        </div>
      )}

      {!isLoading && !isError && works && works.length > 0 && shown.length === 0 && (
        <EmptyState
          title={media === "audio" ? t("library.noAudioTitle") : t("library.noBooksTitle")}
          hint={media === "audio" ? t("library.noAudioHint") : t("library.noBooksHint")}
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
