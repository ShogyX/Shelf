// Browse library — the full poster grid + multi-select, filtered by shelf and the nav ?q= search.
// This is where the old Library "management" grid moved: shelf filter (All + each shelf), the
// Select / Download(N) / Cancel multi-select toolbar, and the shared <LibraryGrid>. The cinematic
// home (hero + rails) stays on "/"; this is the dense, manage-everything surface.
import { Link, useSearchParams } from "react-router-dom";
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import { qk } from "../api/queryKeys";
import { Button, EmptyState, PosterGridSkeleton } from "../components/ui";
import { useApp } from "../store";
import { useIsAdmin } from "../auth";
import LibraryGrid from "../components/LibraryGrid";

export default function BrowseLibrary() {
  const qc = useQueryClient();
  const toast = useApp((s) => s.toast);
  const isAdmin = useIsAdmin();
  const [sp, setSp] = useSearchParams();
  const q = (sp.get("q") ?? "").trim();
  // ?shelf=<id> filters to that shelf; ?shelf=all (or absent) = the whole library.
  const shelfParam = sp.get("shelf");
  const activeShelf = shelfParam && shelfParam !== "all" ? Number(shelfParam) : null;

  const [media, setMedia] = useState<"all" | "books" | "audio">("all"); // reading vs listening filter
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
        `Checked ${r.works_checked} title${r.works_checked === 1 ? "" : "s"}: ` +
          `${r.works_updated} updated, ${r.new_chapters} new chapter${r.new_chapters === 1 ? "" : "s"}.`,
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
  const shown = (works ?? []).filter((w) =>
    media === "all" ? true : media === "audio" ? isAudio(w) : !isAudio(w));

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
            title="Download the selected works as EPUBs (ZIP)"
          >
            {downloading ? "Preparing…" : `⬇ Download (${selected.size})`}
          </Button>
          <Button variant="ghost" onClick={() => { setSelecting(false); setSelected(new Set()); }}>
            Cancel
          </Button>
        </>
      ) : (
        <Button variant="outline" title="Select works to download as EPUBs" onClick={() => setSelecting(true)}>
          ☑ Select
        </Button>
      )}
      {isAdmin && (
        <Button
          variant="outline"
          title="Re-check ALL ongoing titles for newly released chapters (admin)"
          disabled={checkAll.isPending}
          onClick={() => checkAll.mutate()}
        >
          {checkAll.isPending ? "Checking…" : "⟳ Check updates"}
        </Button>
      )}
      <Link to="/discover">
        <Button variant="outline">+ Add a work</Button>
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
            Your shelf
          </div>
          <div className="flex flex-wrap items-end justify-between gap-3">
            <h1 className="font-display text-[34px] font-semibold leading-[1.05] tracking-tight text-text sm:text-[44px]">
              Browse library
            </h1>
            {actions}
          </div>

          {/* Shelf filter (All + each bookshelf). Create/manage shelves lives in Settings → Bookshelves.
              Chips scroll horizontally on narrow screens; "Manage shelves" stays pinned outside the
              scroll so it never clips off the right edge on mobile. */}
          <div className="mt-5 flex items-center gap-2">
            <div className="flex min-w-0 flex-1 items-center gap-2 overflow-x-auto pb-1 scrollbar-none">
              {chip(null, "All")}
              {shelves.map((s) => chip(s.id, s.name, s.count))}
            </div>
            {shelves.length > 0 && (
              <Link to="/settings#bookshelves" className="shrink-0 px-2 text-xs text-muted underline hover:text-text">
                Manage shelves
              </Link>
            )}
          </div>
        </div>
      </section>

      <div className="mx-auto max-w-6xl px-4 pb-10 pt-6 sm:px-6">
      {isLoading && <PosterGridSkeleton count={12} />}

      {!isLoading && isError && (
        <EmptyState
          title="Couldn’t load your library"
          hint="Something went wrong fetching your works — this isn’t the same as an empty shelf."
          action={<Button variant="primary" onClick={() => refetch()}>Retry</Button>}
        />
      )}

      {!isLoading && !isError && (!works || works.length === 0) && (
        q ? (
          <EmptyState
            title={`No works match “${q}”`}
            hint="Try a different title, author, or keyword."
          />
        ) : (
          <EmptyState
            title="Your shelf is empty"
            hint="Browse the index to find and hook a title, or import a file you own."
            action={
              <Link to="/discover">
                <Button variant="primary">Add your first work</Button>
              </Link>
            }
          />
        )
      )}

      {!isLoading && q && works && works.length > 0 && (
        <p className="mb-3 text-sm text-muted">
          {works.length} result{works.length === 1 ? "" : "s"} for “{q}”
        </p>
      )}

      {/* Reading vs listening: filter the library by format. Audiobooks = titles with a 🎧 listen
          option; Books = the read-only rest. Only shown once there's an audiobook to split out. */}
      {!isLoading && !isError && works && works.length > 0 && audioCount > 0 && (
        <div role="group" aria-label="Filter by format" className="mb-4 inline-flex overflow-hidden rounded-lg border border-border text-sm">
          {([
            ["all", `All (${works.length})`],
            ["books", `📖 Books (${bookCount})`],
            ["audio", `🎧 Audiobooks (${audioCount})`],
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
      )}

      {!isLoading && !isError && works && works.length > 0 && shown.length === 0 && (
        <EmptyState
          title={media === "audio" ? "No audiobooks yet" : "No books here"}
          hint={media === "audio"
            ? "Titles with a 🎧 listen option will show here."
            : "Every title in this view has an audiobook — switch to All or Audiobooks."}
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
