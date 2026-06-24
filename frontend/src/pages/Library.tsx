import { Link, useNavigate } from "react-router-dom";
import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query";
import { api, Bookshelf, MetaCandidate, SeriesBook, Work } from "../api/client";
import { qk } from "../api/queryKeys";
import { useEffect, useState } from "react";
import { Badge, Button, Card, Disclosure, EmptyState, FormField, inputCls, Modal, OverflowMenu, PageHeader, PosterGridSkeleton, Spinner, StatusChip, useDialogFocus, useEdgeFlip } from "../components/ui";
import { useConfirm } from "../components/confirm";
import Cover, { coverSrc } from "../components/Cover";
import SendDialog from "../components/SendDialog";
import type { Tone } from "../components/IndexShared";
import { useIsAdmin } from "../auth";
import { useApp } from "../store";
import { useAudio } from "../audioStore";
import LibraryHome from "../components/LibraryHome";
import WorkDetailModal from "../components/WorkDetailModal";

// One clear, friendly state per title (computed server-side as work.library_status).
const STATUS_BADGE: Record<string, { label: string; tone: Tone; icon: string; help: string }> = {
  paused: { label: "Paused", tone: "default", icon: "⏸",
    help: "Automatic updates are off — Resume to gather new chapters again." },
  gathering: { label: "Gathering", tone: "amber", icon: "↓",
    help: "Downloading chapters now." },
  ongoing: { label: "Ongoing", tone: "violet", icon: "●",
    help: "Caught up — new chapters are gathered as the series releases them." },
  complete: { label: "Complete", tone: "green", icon: "✓",
    help: "The series has finished and every chapter is gathered." },
  incomplete: { label: "Incomplete", tone: "red", icon: "!",
    help: "Some chapters are missing or couldn't be fetched." },
};

/** Per-work control to toggle which bookshelves the work is on. */
export function ShelfMenu({ work, shelves }: { work: Work; shelves: Bookshelf[] }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const { ref, style } = useEdgeFlip<HTMLDivElement>(open, 192, "left"); // 192 = w-48; clamp into the viewport
  const on = new Set(work.shelf_ids);
  const toggle = useMutation({
    mutationFn: ({ shelfId, add }: { shelfId: number; add: boolean }) =>
      add ? api.addWorkToShelf(shelfId, work.id) : api.removeWorkFromShelf(shelfId, work.id),
    // Optimistic: flip this work's shelf_ids across every cached works list so the checkbox AND the
    // "🗂 Shelves (N)" count update instantly. onSettled invalidation reconciles (incl. shelf-filtered
    // lists, where a removed work should drop out — left to the refetch, not done optimistically).
    onMutate: async ({ shelfId, add }) => {
      await qc.cancelQueries({ queryKey: qk.works() });
      const prev = qc.getQueriesData<Work[]>({ queryKey: qk.works() });
      for (const [key, list] of prev) {
        if (!list) continue;
        qc.setQueryData<Work[]>(
          key,
          list.map((w) =>
            w.id !== work.id
              ? w
              : {
                  ...w,
                  shelf_ids: add
                    ? [...new Set([...w.shelf_ids, shelfId])]
                    : w.shelf_ids.filter((id) => id !== shelfId),
                },
          ),
        );
      }
      return { prev };
    },
    onError: (_e, _v, ctx) => {
      for (const [key, list] of ctx?.prev ?? []) qc.setQueryData(key, list);
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: qk.works() });
      qc.invalidateQueries({ queryKey: qk.bookshelves() });
    },
  });
  if (shelves.length === 0) return null;
  return (
    <div ref={ref} className="relative">
      <Button size="sm" variant="outline" title="Add to a bookshelf" onClick={() => setOpen((o) => !o)}>
        🗂 Shelves{on.size ? ` (${on.size})` : ""}
      </Button>
      {open && (
        <>
          <div className="fixed inset-0 z-10" onClick={() => setOpen(false)} />
          <div
            style={style}
            className="absolute left-0 top-full z-20 w-48 max-w-[calc(100vw-1rem)] rounded-lg border border-border bg-surface p-1 shadow-xl"
          >
            {shelves.map((s) => (
              <label
                key={s.id}
                className="flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 text-sm hover:bg-surface-2"
              >
                <input
                  type="checkbox"
                  checked={on.has(s.id)}
                  disabled={toggle.isPending}
                  onChange={(e) => toggle.mutate({ shelfId: s.id, add: e.target.checked })}
                />
                <span className="truncate">{s.name}</span>
              </label>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

function useDebounced<T>(value: T, ms = 250): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

// Note: there's no per-shelf "auto-update" toggle — every actively-releasing title in your library
// is refreshed automatically. Pause a specific title from the Jobs page if you want to stop it.
const FLAG_FIELDS: { key: keyof Bookshelf; label: string; hint: string }[] = [
  { key: "auto_kindle", label: "Auto-send to Kindle", hint: "Email newly gathered chapters to your Kindle automatically" },
  { key: "notify_on_add", label: "Notify on add", hint: "Push a notification when a title is added to this shelf (incl. via a watched path)" },
  { key: "notify_email", label: "Email on add", hint: "Email the book to your personal address when it's added to this shelf" },
  { key: "goodreads_target", label: "Goodreads destination", hint: "Auto-hooked Goodreads titles (your default shelf) land here" },
];

/** Highlighted modal to create a bookshelf: name, automation, an external Goodreads shelf, and
 *  the works to put on it. */
function ShelfDialog({ onClose, onCreated }: { onClose: () => void; onCreated: (id: number) => void }) {
  const toast = useApp((s) => s.toast);
  const { data: works = [] } = useQuery({ queryKey: qk.works("", null), queryFn: () => api.listWorks() });
  const [name, setName] = useState("");
  const [flags, setFlags] = useState({
    auto_kindle: false, notify_on_add: false, notify_email: false,
    goodreads_target: false,
  });
  const [grShelf, setGrShelf] = useState("");
  const [picked, setPicked] = useState<Set<number>>(new Set());
  const [wq, setWq] = useState("");
  const [busy, setBusy] = useState(false);

  const filtered = works.filter(
    (w) => !wq || (w.title + " " + (w.author ?? "")).toLowerCase().includes(wq.toLowerCase())
  );
  const togglePick = (id: number) =>
    setPicked((s) => {
      const n = new Set(s);
      n.has(id) ? n.delete(id) : n.add(id);
      return n;
    });

  async function create() {
    setBusy(true);
    try {
      const s = await api.createBookshelf({
        name: name.trim(), ...flags,
        goodreads_shelf: grShelf.trim() || null,
        work_ids: [...picked],
      });
      onCreated(s.id);
      onClose();
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  }

  const focusRef = useDialogFocus(onClose);
  return (
    <>
      <div className="fixed inset-0 z-40 bg-black/50" onClick={onClose} />
      <div
        ref={focusRef}
        role="dialog"
        aria-modal="true"
        aria-label="New bookshelf"
        tabIndex={-1}
        className="fixed left-1/2 top-1/2 z-50 flex max-h-[90vh] w-[34rem] max-w-[calc(100vw-1.5rem)] -translate-x-1/2 -translate-y-1/2 flex-col rounded-2xl border border-accent/40 bg-surface shadow-2xl ring-1 ring-accent/20"
      >
        <div className="flex items-center justify-between border-b border-border px-5 py-3">
          <h2 className="font-semibold">New bookshelf</h2>
          <button className="text-muted hover:text-text" aria-label="Close" onClick={onClose}>✕</button>
        </div>
        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-5 py-4">
          <label className="block text-xs text-muted">
            Name
            <input
              autoFocus
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Favorites, Reading now…"
              className="mt-1 w-full rounded-lg border border-border bg-bg px-3 py-2 text-sm text-text"
            />
          </label>

          <div>
            <div className="mb-1.5 text-xs text-muted">Automation</div>
            <div className="flex flex-wrap gap-x-6 gap-y-2">
              {FLAG_FIELDS.map((f) => (
                <label key={f.key} className="flex items-center gap-2 text-sm" title={f.hint}>
                  <input
                    type="checkbox"
                    checked={Boolean((flags as Record<string, boolean>)[f.key])}
                    onChange={(e) => setFlags((s) => ({ ...s, [f.key]: e.target.checked }))}
                  />
                  {f.label}
                </label>
              ))}
            </div>
          </div>

          <label className="block text-xs text-muted">
            External Goodreads shelf (optional)
            <input
              value={grShelf}
              onChange={(e) => setGrShelf(e.target.value)}
              placeholder="e.g. to-read, currently-reading"
              className="mt-1 w-full rounded-lg border border-border bg-bg px-3 py-2 text-sm text-text"
            />
            <span className="mt-1 block text-[11px] text-muted">
              Titles on this Goodreads shelf auto-hook onto this bookshelf (uses your Goodreads
              connection in Settings).
            </span>
          </label>

          <div>
            <div className="mb-1.5 flex items-center justify-between text-xs text-muted">
              <span>Add works {picked.size ? `(${picked.size} selected)` : ""}</span>
              <input
                value={wq}
                onChange={(e) => setWq(e.target.value)}
                placeholder="filter…"
                className="w-32 rounded-lg border border-border bg-bg px-2 py-1 text-xs"
              />
            </div>
            <div className="max-h-48 overflow-y-auto rounded-lg border border-border">
              {filtered.length === 0 && (
                <div className="p-3 text-xs text-muted">No works in your library yet.</div>
              )}
              {filtered.map((w) => (
                <label
                  key={w.id}
                  className="flex cursor-pointer items-center gap-2 px-3 py-1.5 text-sm hover:bg-surface-2"
                >
                  <input type="checkbox" checked={picked.has(w.id)} onChange={() => togglePick(w.id)} />
                  <span className="truncate">{w.title}</span>
                  <span className="ml-auto shrink-0 truncate text-xs text-muted">{w.author ?? ""}</span>
                </label>
              ))}
            </div>
          </div>
        </div>
        <div className="flex justify-end gap-2 border-t border-border px-5 py-3">
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button variant="primary" disabled={!name.trim() || busy} onClick={create}>
            {busy ? "Creating…" : "Create shelf"}
          </Button>
        </div>
      </div>
    </>
  );
}

/** Shelf tabs (All + each bookshelf), create-new, and the active shelf's automation settings. */
function ShelfBar({
  shelves,
  active,
  onSelect,
  activeShelf,
  onNew,
}: {
  shelves: Bookshelf[];
  active: number | null;
  onSelect: (id: number | null) => void;
  activeShelf: Bookshelf | null;
  onNew: () => void;
}) {
  const qc = useQueryClient();
  const toast = useApp((s) => s.toast);
  const confirm = useConfirm();
  const [showSettings, setShowSettings] = useState(false);
  const [grShelf, setGrShelf] = useState("");
  const [watchPath, setWatchPath] = useState("");
  const isAdmin = useIsAdmin();
  const inval = () => qc.invalidateQueries({ queryKey: qk.bookshelves() });

  useEffect(() => {
    setGrShelf(activeShelf?.goodreads_shelf ?? "");
    setWatchPath(activeShelf?.watch_path ?? "");
  }, [activeShelf]);

  const update = useMutation({
    mutationFn: (patch: Partial<Bookshelf>) => api.updateBookshelf(active!, patch),
    onSuccess: () => inval(),
  });
  const remove = useMutation({
    mutationFn: () => api.deleteBookshelf(active!),
    onSuccess: () => { onSelect(null); setShowSettings(false); inval(); },
  });

  const tab = (id: number | null, label: string, count?: number) => {
    const isActive = active === id;
    return (
      <button
        key={id ?? "all"}
        onClick={() => onSelect(id)}
        className={`group inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full px-3.5 py-1.5 text-sm transition ${
          isActive
            ? "bg-accent font-semibold text-accent-fg shadow-sm"
            : "border border-border bg-bg text-muted hover:bg-surface-2 hover:text-text"
        }`}
      >
        <span className="max-w-[11rem] truncate">{label}</span>
        {count != null && (
          <span
            className={`rounded-full px-1.5 py-px text-[11px] font-medium tabular-nums ${
              isActive ? "bg-accent-fg/20 text-accent-fg" : "bg-surface-2 text-muted group-hover:text-text"
            }`}
          >
            {count}
          </span>
        )}
      </button>
    );
  };
  const toggle = (key: keyof Bookshelf, label: string, hint: string) => (
    <label className="flex items-center gap-2 text-sm" title={hint}>
      <input
        type="checkbox"
        checked={Boolean(activeShelf?.[key])}
        disabled={update.isPending}
        onChange={(e) => update.mutate({ [key]: e.target.checked })}
      />
      {label}
    </label>
  );

  return (
    <section className="mb-6 rounded-2xl border border-border bg-surface/50 p-3.5">
      <div className="mb-2.5 flex items-center justify-between gap-2">
        <h2 className="flex items-center gap-2 text-sm font-semibold text-text">
          <span aria-hidden>🗂</span> Bookshelves
        </h2>
        <div className="flex items-center gap-1.5">
          <Button size="sm" variant="outline" onClick={onNew}>+ New shelf</Button>
          {activeShelf && (
            <Button
              size="sm"
              variant={showSettings ? "primary" : "ghost"}
              title="Bookshelf automation & actions"
              onClick={() => setShowSettings((s) => !s)}
            >
              ⚙ Settings
            </Button>
          )}
        </div>
      </div>
      <div className="flex items-center gap-2 overflow-x-auto pb-1 scrollbar-none">
        {tab(null, "All")}
        {shelves.map((s) => tab(s.id, s.name, s.count))}
        {shelves.length === 0 && (
          <span className="px-1 text-xs text-muted">
            No shelves yet — group titles into one with “+ New shelf”.
          </span>
        )}
      </div>

      {activeShelf && showSettings && (
        <Card className="mt-3 p-3">
          <div className="mb-2 text-sm font-semibold">“{activeShelf.name}” settings</div>
          <div className="flex flex-wrap gap-x-6 gap-y-2">
            {FLAG_FIELDS.map((f) => toggle(f.key, f.label, f.hint))}
          </div>
          <label className="mt-3 block text-xs text-muted">
            External Goodreads shelf
            <span className="ml-1 flex items-center gap-2">
              <input
                value={grShelf}
                onChange={(e) => setGrShelf(e.target.value)}
                placeholder="e.g. to-read"
                className="mt-1 w-48 rounded-lg border border-border bg-bg px-3 py-1.5 text-sm text-text"
              />
              <Button size="sm" variant="outline" disabled={update.isPending}
                onClick={() => update.mutate({ goodreads_shelf: grShelf.trim() || null })}>
                Save
              </Button>
            </span>
          </label>
          {isAdmin && (
            <label className="mt-3 block text-xs text-muted">
              Monitored path (admin) — new books found here are added to this shelf and trigger its
              notify / Kindle / email actions
              <span className="ml-1 flex items-center gap-2">
                <input
                  value={watchPath}
                  onChange={(e) => setWatchPath(e.target.value)}
                  placeholder="/mnt/NAS-Pool/media/Books"
                  className="mt-1 w-80 rounded-lg border border-border bg-bg px-3 py-1.5 text-sm text-text"
                />
                <Button size="sm" variant="outline" disabled={update.isPending}
                  onClick={() => update.mutate({ watch_path: watchPath.trim() || null })}>
                  Save
                </Button>
              </span>
            </label>
          )}
          <div className="mt-3 flex gap-2">
            <Button size="sm" variant="outline" title="Download every work on this shelf as EPUBs (ZIP)"
              onClick={() => api.downloadLibrary({ shelf_id: activeShelf.id }).catch((e) => toast((e as Error).message, "error"))}>
              ⬇ Download shelf
            </Button>
            <Button size="sm" variant="danger"
              onClick={async () => {
                if (await confirm({ title: "Delete shelf", message: `Delete shelf “${activeShelf.name}”? The titles stay in your library.`, danger: true }))
                  remove.mutate();
              }}>
              Delete shelf
            </Button>
          </div>
        </Card>
      )}
    </section>
  );
}

export default function Library() {
  const qc = useQueryClient();
  const toast = useApp((s) => s.toast);
  const confirm = useConfirm();
  const navigate = useNavigate();
  const isAdmin = useIsAdmin();
  const [sendWork, setSendWork] = useState<Work | null>(null);
  const [fixWork, setFixWork] = useState<Work | null>(null);
  const [detailId, setDetailId] = useState<number | null>(null); // work whose detail sheet is open
  const [media, setMedia] = useState<"all" | "books" | "audio">("all"); // reading vs listening filter
  const [query, setQuery] = useState("");
  const [activeShelf, setActiveShelf] = useState<number | null>(null); // null = all of library
  const [showShelfDialog, setShowShelfDialog] = useState(false);
  const [selecting, setSelecting] = useState(false);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [downloading, setDownloading] = useState(false);
  const q = useDebounced(query.trim());
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
  });
  const activeShelfObj = shelves.find((s) => s.id === activeShelf) ?? null;
  // Reading vs listening: a title is an "audiobook" if it has a paired audiobook (the "listen"
  // format) — books are the read-only rest. The filter narrows the grid; counts label the tabs.
  const isAudio = (w: Work) => !!w.audiobook_work_id || w.media_kind === "audio";
  const audioCount = (works ?? []).filter(isAudio).length;
  const bookCount = (works?.length ?? 0) - audioCount;
  const shown = (works ?? []).filter((w) =>
    media === "all" ? true : media === "audio" ? isAudio(w) : !isAudio(w));

  const del = useMutation({
    mutationFn: (id: number) => api.deleteWork(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.works() }),
  });

  const repair = useMutation({
    mutationFn: (id: number) => api.repairWork(id),
    onSuccess: (rep) => {
      qc.invalidateQueries({ queryKey: qk.works() });
      const acted = rep.actions.length ? rep.actions.join("; ") : "no fixable issues found";
      toast(`Diagnosis: ${rep.health}. ${rep.detail ?? ""} — ${acted}.`);
    },
    onError: (e) => toast((e as Error).message, "error"),
  });

  const checkOne = useMutation({
    mutationFn: (id: number) => api.checkWorkUpdates(id),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: qk.works() });
      if (!r.checked) toast("This title's source doesn't get new chapters.");
      else if (r.error) toast(`Update check failed: ${r.error}`, "error");
      else if (r.new_chapters > 0)
        toast(`Found ${r.new_chapters} new chapter${r.new_chapters === 1 ? "" : "s"} — gathering now.`, "success");
      else toast(r.metadata_changed ? "Metadata refreshed; no new chapters." : "Already up to date.");
    },
    onError: (e) => toast((e as Error).message, "error"),
  });

  const resumeOne = useMutation({
    mutationFn: (id: number) => api.resumeWork(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.works() });
      toast("Resumed — checking for new chapters and gathering any outstanding ones.", "success");
    },
    onError: (e) => toast((e as Error).message, "error"),
  });

  const pauseOne = useMutation({
    mutationFn: (id: number) => api.pauseWork(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.works() });
      toast("Paused — automatic updates are off for this title.");
    },
    onError: (e) => toast((e as Error).message, "error"),
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

  // Home state = no search, no active shelf → the cinematic hero + rails are the primary surface, so
  // the full management grid (search, multi-select, shelves) collapses behind a Disclosure (spec).
  // When searching or viewing a shelf, the management surface is the whole point → render it open.
  const isHome = !q && !activeShelf;

  // The legacy management surface (search bar, shelves, multi-select grid). Same markup/handlers as
  // before — only wrapped, not changed. Collapsed by default on the home; always open otherwise.
  const management = (
    <>
      <PageHeader eyebrow="Your shelf" title="Library" />

      {/* Centralized action bar — search + every page-level action in one orderly row. */}
      <Card className="mb-5 p-2.5">
        <div className="flex flex-col gap-2.5 sm:flex-row sm:items-center">
          <div className="relative min-w-0 flex-1">
            <span className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted">
              🔍
            </span>
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search your library by title, author or description…"
              className="w-full rounded-lg border border-border bg-bg py-2 pl-10 pr-9 text-sm focus:border-accent focus:outline-none"
            />
            {query && (
              <button
                onClick={() => setQuery("")}
                className="absolute right-2.5 top-1/2 -translate-y-1/2 text-xs text-muted hover:text-text"
              >
                clear
              </button>
            )}
          </div>
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
            <Link to="/index">
              <Button variant="primary">+ Add a work</Button>
            </Link>
          </div>
        </div>
      </Card>

      <ShelfBar
        shelves={shelves}
        active={activeShelf}
        onSelect={setActiveShelf}
        activeShelf={activeShelfObj}
        onNew={() => setShowShelfDialog(true)}
      />

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
              <Link to="/index">
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

      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6">
        {buildGridItems(shown).map((it) => {
          if (it.kind === "series")
            return <SeriesLibraryCard key={`series:${it.name}`} name={it.name} books={it.books} />;
          const w = it.work;
          // A title may also have a shared audiobook (the "listen" format): audiobook_work_id points
          // at that separate audio Work, which lives in stock — not the library — and is offered as a
          // download alongside Read, so the user sees ONE title and picks ebook or audiobook.
          const audiobookId = w.audiobook_work_id;
          // No overflow-hidden on the Card: the OverflowMenu dropdown must escape it. The cover
          // wrappers clip themselves (+ rounded-t-xl keeps the card's rounded top).
          return (
            <Card key={w.id} className="group relative hover-lift">
              {selecting && (
                <label className="absolute left-2 top-2 z-10 flex h-7 w-7 cursor-pointer items-center justify-center rounded-md border border-border bg-surface/90 shadow">
                  <input
                    type="checkbox"
                    checked={selected.has(w.id)}
                    onChange={() => toggleSelected(w.id)}
                  />
                </label>
              )}
              {selecting ? (
                <button className="block w-full text-left" onClick={() => toggleSelected(w.id)}>
                  <div className="aspect-[2/3] w-full overflow-hidden rounded-t-xl">
                    <Cover title={w.title} author={w.author} coverUrl={w.cover_url} />
                  </div>
                </button>
              ) : (
                <button type="button" onClick={() => setDetailId(w.id)} className="block w-full text-left" title={`Open “${w.title}”`}>
                  <div className="aspect-[2/3] w-full overflow-hidden rounded-t-xl">
                    <Cover title={w.title} author={w.author} coverUrl={w.cover_url} />
                  </div>
                </button>
              )}
              <div className="space-y-1 p-3">
                <button type="button" onClick={() => setDetailId(w.id)} className="block w-full text-left font-medium leading-tight hover:underline line-clamp-2">
                  {w.title}
                </button>
                <div className="text-xs text-muted line-clamp-1">{w.author ?? "Unknown author"}</div>
                <div className="flex flex-wrap items-center gap-1.5 pt-1">
                  {audiobookId && <Badge tone="violet">🎧 + Audiobook</Badge>}
                  {/* One clear status, plus the chapter count. */}
                  {(() => {
                    const s = STATUS_BADGE[w.library_status] ?? STATUS_BADGE.ongoing;
                    return (
                      <span title={w.health_detail ?? s.help}>
                        <Badge tone={s.tone}>{s.icon} {s.label}</Badge>
                      </span>
                    );
                  })()}
                  {(() => {
                    // Never display fewer than we've gathered (a serial can pass its old ceiling).
                    const total = Math.max(
                      w.total_chapters_expected ?? w.total_chapters_known ?? 0,
                      w.chapters_fetched,
                    );
                    return <Badge>{w.chapters_fetched}{total ? `/${total}` : ""} ch</Badge>;
                  })()}
                  {(w.start_chapter ?? 1) > 1 && (
                    <span title={`Hooked from chapter ${w.start_chapter} — earlier chapters were skipped`}>
                      <Badge tone="violet">from Ch. {w.start_chapter}</Badge>
                    </span>
                  )}
                </div>
                {(() => {
                  // Progress bar only while actively gathering — a caught-up or incomplete title
                  // isn't "in progress", so a bar there just reads as broken.
                  if (w.library_status !== "gathering") return null;
                  const total = Math.max(
                    w.total_chapters_expected ?? w.total_chapters_known ?? 0,
                    w.chapters_fetched,
                  );
                  if (!total || w.chapters_fetched >= total) return null;
                  const pct = Math.min(100, Math.round((w.chapters_fetched / total) * 100));
                  return (
                    <div className="pt-1">
                      <div className="h-1 w-full overflow-hidden rounded-full bg-surface-2">
                        <div className="h-full rounded-full bg-accent" style={{ width: `${pct}%` }} />
                      </div>
                      <div className="mt-0.5 text-[10px] text-muted">
                        gathering {w.chapters_fetched}/{total}
                      </div>
                    </div>
                  );
                })()}
                {/* One obvious primary (Read), plus the two controls that don't fit a plain menu —
                    the 🎧 Listen download <a> and the Shelves popover (ShelfMenu) — kept beside it as
                    compact always-visible controls. Everything else (Send / maintenance / Remove)
                    moves into the ⋯ overflow, killing the up-to-8-button hover cluster. Conditions,
                    disabled and isPending behavior are unchanged. */}
                <div className="flex flex-wrap items-center gap-1.5 pt-2">
                  <Button size="sm" variant="primary" onClick={() => navigate(`/read/${w.id}`)}>
                    Read
                  </Button>
                  {audiobookId && (
                    <button
                      // playWork must run inside the tap (iOS requires play() in a user gesture).
                      onClick={() => useAudio.getState().playWork(audiobookId)}
                      title="Play the audiobook"
                      className="inline-flex items-center gap-1 rounded-lg border border-border px-2.5 py-1 text-xs font-medium hover:bg-surface-2"
                    >
                      🎧 Listen
                    </button>
                  )}
                  <ShelfMenu work={w} shelves={shelves} />
                  <OverflowMenu
                    label={`More actions for ${w.title}`}
                    items={[
                      audiobookId && {
                        label: "🎧 Download audiobook",
                        onClick: () => {
                          const a = document.createElement("a");
                          a.href = api.audioUrl(audiobookId); a.download = "";
                          a.click();
                        },
                      },
                      {
                        label: "📤 Send",
                        onClick: () => setSendWork(w),
                      },
                      {
                        label: "✎ Fix metadata",
                        onClick: () => setFixWork(w),
                      },
                      w.library_status === "incomplete" && {
                        label: repair.isPending && repair.variables === w.id ? "Fixing…" : "🩺 Fix",
                        disabled: repair.isPending && repair.variables === w.id,
                        onClick: () => repair.mutate(w.id),
                      },
                      w.library_status === "paused" && {
                        label: resumeOne.isPending && resumeOne.variables === w.id ? "Resuming…" : "▶ Resume",
                        disabled: resumeOne.isPending && resumeOne.variables === w.id,
                        onClick: () => resumeOne.mutate(w.id),
                      },
                      w.hooked && w.library_status !== "paused" && w.status === "ongoing" && {
                        label: checkOne.isPending && checkOne.variables === w.id ? "Checking…" : "⟳ Updates",
                        disabled: checkOne.isPending && checkOne.variables === w.id,
                        onClick: () => checkOne.mutate(w.id),
                      },
                      w.hooked && w.library_status !== "paused" && w.status === "ongoing" && {
                        label: pauseOne.isPending && pauseOne.variables === w.id ? "Pausing…" : "⏸ Pause",
                        disabled: pauseOne.isPending && pauseOne.variables === w.id,
                        onClick: () => pauseOne.mutate(w.id),
                      },
                      {
                        label: "Remove",
                        danger: true,
                        onClick: async () => {
                          if (await confirm({ title: "Remove from library", message: `Remove “${w.title}” from your library?`, danger: true, confirmText: "Remove" }))
                            del.mutate(w.id);
                        },
                      },
                    ]}
                  />
                </div>
              </div>
            </Card>
          );
        })}
      </div>
    </>
  );

  return (
    <>
    {/* Cinematic home (billboard hero + rails) in the default state; the management grid below stays
        accessible one click away behind a Disclosure so it doesn't compete with the home. */}
    {isHome && <LibraryHome />}
    <main className="page-in mx-auto max-w-6xl px-4 py-8">
      {isHome ? (
        <Disclosure title="Manage library — all books & shelves" subtitle="Search, multi-select, bookshelves and per-title actions">
          {management}
        </Disclosure>
      ) : (
        management
      )}

      {detailId != null && (
        <WorkDetailModal workId={detailId} onClose={() => setDetailId(null)} />
      )}
      {sendWork && (
        <SendDialog workId={sendWork.id} title={sendWork.title} onClose={() => setSendWork(null)} />
      )}
      {fixWork && (
        <FixMetadataDialog work={fixWork} onClose={() => setFixWork(null)} />
      )}
      {showShelfDialog && (
        <ShelfDialog
          onClose={() => setShowShelfDialog(false)}
          onCreated={(id) => { setShowShelfDialog(false); setActiveShelf(id); }}
        />
      )}
    </main>
    </>
  );
}

/** Correct a library work's metadata: edit title/author/series/cover directly, or search a metadata
 *  provider and apply a match. Saves via PATCH /works/{id}. */
export function FixMetadataDialog({ work, onClose }: { work: Work; onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useApp((s) => s.toast);
  const [title, setTitle] = useState(work.title);
  const [author, setAuthor] = useState(work.author ?? "");
  const [series, setSeries] = useState(work.series ?? "");
  const [seriesPos, setSeriesPos] = useState(work.series_position != null ? String(work.series_position) : "");
  const [coverUrl, setCoverUrl] = useState(work.cover_url ?? "");
  const [q, setQ] = useState(work.title);
  const [candidates, setCandidates] = useState<MetaCandidate[] | null>(null);
  // Provenance: where this title was fetched (source/file) + the catalog/import metadata used — so a
  // wrong match is diagnosable. source_work_ref is editable to fix the fetching source.
  const prov = useQuery({ queryKey: ["work-provenance", work.id], queryFn: () => api.getWorkProvenance(work.id) });
  const [sourceRef, setSourceRef] = useState<string | null>(null); // null = not yet seeded from provenance
  useEffect(() => { if (prov.data && sourceRef === null) setSourceRef(prov.data.source_ref ?? ""); }, [prov.data, sourceRef]);

  const search = useMutation({
    mutationFn: () => api.searchWorkMetadata(work.id, q.trim(), author.trim() || undefined),
    onSuccess: (rows) => setCandidates(rows),
    onError: (e) => toast((e as Error).message, "error"),
  });
  const save = useMutation({
    mutationFn: () => api.updateWorkMetadata(work.id, {
      title: title.trim(),
      author: author.trim() || null,
      series: series.trim() || null,
      series_position: seriesPos.trim() ? Number(seriesPos) : null,
      cover_url: coverUrl.trim() || null,
      ...(sourceRef !== null ? { source_work_ref: sourceRef.trim() || null } : {}),
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.works() });
      qc.invalidateQueries({ queryKey: qk.work(work.id) });
      qc.invalidateQueries({ queryKey: qk.continue() });
      toast(`Updated “${title.trim()}”`, "success");
      onClose();
    },
    onError: (e) => toast((e as Error).message, "error"),
  });
  const applyCandidate = (c: MetaCandidate) => {
    setTitle(c.title);
    if (c.author) setAuthor(c.author);
    if (c.cover_url) setCoverUrl(c.cover_url);
  };

  return (
    <Modal
      variant="fullscreen-sheet"
      width="max-w-lg"
      title="Fix metadata"
      onClose={onClose}
      footer={
        <div className="flex w-full justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button variant="primary" disabled={save.isPending || !title.trim()} onClick={() => {
            if (seriesPos.trim() && !Number.isFinite(Number(seriesPos))) {
              toast("Vol # must be a number.", "error");
              return;
            }
            save.mutate();
          }}>
            {save.isPending ? "Saving…" : "Save"}
          </Button>
        </div>
      }
    >
      <div className="space-y-4">
        {/* Provenance — where this title was fetched, the catalog metadata used, and what was
            originally requested. Surfaces a wrong match (e.g. a file/source that doesn't match the
            requested title) so the user can correct the metadata and the fetching source. */}
        {prov.data && (prov.data.source_name || prov.data.source_ref || prov.data.filename || prov.data.catalog_title || prov.data.request_title) && (
          <div className="rounded-2xl border border-[var(--hair-strong,var(--border))] bg-surface-2/40 p-3 text-xs">
            <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-[var(--text-soft,var(--muted))]">Where this came from</div>
            <div className="space-y-1.5">
              {(prov.data.source_name || prov.data.source_ref) && (
                <div className="flex gap-2">
                  <span className="w-20 shrink-0 text-muted">Source</span>
                  <span className="min-w-0 flex-1 break-words text-text">
                    {prov.data.source_name || "—"}{prov.data.source_ref ? ` · ${prov.data.source_ref}` : ""}
                    {prov.data.source_url && (
                      <a href={prov.data.source_url} target="_blank" rel="noreferrer" className="ml-1 text-accent underline">open</a>
                    )}
                  </span>
                </div>
              )}
              {prov.data.filename && (
                <div className="flex gap-2">
                  <span className="w-20 shrink-0 text-muted">File</span>
                  <span className="min-w-0 flex-1 break-words text-text">{prov.data.filename}</span>
                </div>
              )}
              {prov.data.catalog_title && (
                <div className="flex gap-2">
                  <span className="w-20 shrink-0 text-muted">Catalog</span>
                  <span className="min-w-0 flex-1 break-words text-text">
                    {prov.data.catalog_title}{prov.data.catalog_author ? ` · ${prov.data.catalog_author}` : ""}
                    {prov.data.catalog_domain ? ` · ${prov.data.catalog_domain}` : ""}
                  </span>
                </div>
              )}
              {prov.data.request_title && (
                <div className="flex gap-2">
                  <span className="w-20 shrink-0 text-muted">Requested</span>
                  <span className="min-w-0 flex-1 break-words text-text">
                    {prov.data.request_title}{prov.data.request_author ? ` · ${prov.data.request_author}` : ""}
                    {(prov.data.request_detail || prov.data.request_origin) ? ` · via ${prov.data.request_detail || prov.data.request_origin}` : ""}
                  </span>
                </div>
              )}
            </div>
          </div>
        )}
        <div className="flex gap-3">
          <div className="h-28 w-20 shrink-0 overflow-hidden rounded-lg border border-[var(--hair-strong,var(--border))]">
            <Cover title={title || work.title} author={author} coverUrl={coverUrl || null} small />
          </div>
          <div className="min-w-0 flex-1">
            <FormField label="Title">
              <input className={inputCls} value={title} onChange={(e) => setTitle(e.target.value)} />
            </FormField>
            <FormField label="Author">
              <input className={inputCls} value={author} onChange={(e) => setAuthor(e.target.value)} placeholder="(unknown)" />
            </FormField>
          </div>
        </div>
        <div className="flex gap-2">
          <div className="flex-1">
            <FormField label="Series">
              <input className={inputCls} value={series} onChange={(e) => setSeries(e.target.value)} placeholder="(none)" />
            </FormField>
          </div>
          <div className="w-24">
            <FormField label="Vol #">
              <input className={inputCls} value={seriesPos} onChange={(e) => setSeriesPos(e.target.value)} inputMode="decimal" placeholder="–" />
            </FormField>
          </div>
        </div>
        <FormField label="Cover URL">
          <input className={inputCls} value={coverUrl} onChange={(e) => setCoverUrl(e.target.value)} placeholder="https://…  (blank = generated cover)" />
        </FormField>
        {prov.data && (prov.data.source_name || prov.data.source_ref) && (
          <FormField label={<>Source reference{prov.data.source_name ? <span className="opacity-70"> ({prov.data.source_name})</span> : null}</>}>
            <input className={inputCls} value={sourceRef ?? ""} onChange={(e) => setSourceRef(e.target.value)}
              placeholder="this title's ref / URL on the source — fix a wrong fetching source" />
          </FormField>
        )}

        <div className="border-t border-[var(--hair,var(--border))] pt-3">
          <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-[var(--text-soft,var(--muted))]">Search a metadata provider</div>
          <div className="flex gap-2">
            <input
              className={inputCls}
              value={q}
              onChange={(e) => setQ(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") search.mutate(); }}
              placeholder="Title to search…"
            />
            <Button variant="outline" disabled={search.isPending || !q.trim()} onClick={() => search.mutate()}>
              {search.isPending ? "Searching…" : "Search"}
            </Button>
          </div>
          {candidates && candidates.length === 0 && (
            <p className="mt-2 text-xs leading-snug text-[var(--text-soft,var(--muted))]">
              No matches — or no metadata providers are enabled (Settings → Integrations). You can still edit the fields above by hand.
            </p>
          )}
          {candidates && candidates.length > 0 && (
            <div className="mt-2 space-y-1.5">
              {candidates.map((c) => (
                <div key={`${c.provider}:${c.ref}`} className="flex items-center gap-2.5 rounded-xl border border-[var(--hair-strong,var(--border))] bg-surface p-2">
                  <div className="h-14 w-10 shrink-0 overflow-hidden rounded border border-[var(--hair,var(--border))]">
                    <Cover title={c.title} author={c.author} coverUrl={c.cover_url} small />
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-sm font-medium text-text">{c.title}</div>
                    <div className="mt-0.5 flex items-center gap-1.5 truncate text-xs text-[var(--text-soft,var(--muted))]">
                      <span className="truncate">{c.author ?? "Unknown"}{c.year ? ` · ${c.year}` : ""}</span>
                      <StatusChip tone="neutral">{c.provider}</StatusChip>
                    </div>
                  </div>
                  <Button size="sm" variant="ghost" onClick={() => applyCandidate(c)}>Use</Button>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </Modal>
  );
}

// --- Series grouping in the library ---------------------------------------------------------
type GridItem = { kind: "work"; work: Work } | { kind: "series"; name: string; books: Work[] };

// Collapse library works that belong to the same series into ONE grid entry (books ordered by
// series position); standalone works pass through unchanged.
function buildGridItems(works: Work[] | undefined): GridItem[] {
  const out: GridItem[] = [];
  const seen = new Set<string>();
  for (const w of works ?? []) {
    if (w.series) {
      if (seen.has(w.series)) continue;
      const books = (works ?? [])
        .filter((x) => x.series === w.series)
        .sort((a, b) => (a.series_position ?? 9999) - (b.series_position ?? 9999));
      // Only collapse into a Series card when 2+ owned volumes share the series. A single owned
      // volume renders as a normal work card (read in one tap, no series-modal detour). (F31)
      if (books.length >= 2) {
        seen.add(w.series);
        out.push({ kind: "series", name: w.series, books });
      } else {
        out.push({ kind: "work", work: w });
      }
    } else {
      out.push({ kind: "work", work: w });
    }
  }
  return out;
}

function SeriesLibraryCard({ name, books }: { name: string; books: Work[] }) {
  const [open, setOpen] = useState(false);
  const cover = books.find((b) => b.cover_url)?.cover_url ?? null;
  const first = books[0];
  return (
    <>
      <Card className="group relative overflow-hidden hover-lift">
        <button
          className="block w-full text-left"
          onClick={() => setOpen(true)}
          title={`Open the “${name}” series`}
        >
          <div className="aspect-[2/3] w-full overflow-hidden">
            <Cover title={name} author={first?.author ?? null} coverUrl={cover} />
          </div>
        </button>
        <div className="space-y-1 p-3">
          <button
            className="block w-full text-left font-medium leading-tight line-clamp-2 hover:underline"
            onClick={() => setOpen(true)}
          >
            {name}
          </button>
          <div className="text-xs text-muted line-clamp-1">{first?.author ?? "Series"}</div>
          <div className="flex flex-wrap items-center gap-1.5 pt-1">
            <Badge tone="violet">Series</Badge>
            <Badge>{books.length} in library</Badge>
          </div>
          <div className="pt-2">
            <Button size="sm" variant="primary" onClick={() => setOpen(true)}>
              Open series
            </Button>
          </div>
        </div>
      </Card>
      {open && <SeriesLibraryModal name={name} books={books} onClose={() => setOpen(false)} />}
    </>
  );
}

function SeriesLibraryModal({
  name,
  books,
  onClose,
}: {
  name: string;
  books: Work[];
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const toast = useApp((s) => s.toast);
  const seedId = books[0]?.id;
  const full = useQuery({
    queryKey: qk.workSeries(seedId),
    queryFn: () => api.workSeries(seedId),
    enabled: !!seedId,
  });
  const focusRef = useDialogFocus(onClose);   // Escape + focus trap/restore (shared dialog behavior)
  const confirm = useConfirm();

  // Remove the WHOLE series from the library: delete every owned volume at once (volumes not in the
  // library are untouched). Fixes the gap where a series could only be removed one volume at a time.
  const removeSeries = useMutation({
    // Sequential, not Promise.all: concurrent DELETEs storm SQLite (each does a write txn + cache clear)
    // and a mid-flight failure would leave the series half-removed. One at a time is the safe default.
    mutationFn: async () => {
      for (const b of books) await api.deleteWork(b.id);
    },
    onSuccess: () => {
      toast(`Removed “${name}” (${books.length} volume${books.length === 1 ? "" : "s"}) from your library`, "success");
      onClose();
    },
    onError: (e) => toast((e as Error).message, "error"),
    // Always refresh the library, even on partial failure, so the UI reflects what was actually deleted.
    onSettled: () => {
      qc.invalidateQueries({ queryKey: qk.works() });
      qc.invalidateQueries({ queryKey: qk.continue() });
    },
  });

  const vols: SeriesBook[] = full.data?.books ?? [];
  const missing = vols.filter((v) => !v.in_library && v.ref && v.catalog_id);
  const seedCatalog = vols.find((v) => v.catalog_id)?.catalog_id ?? null;

  const fetchMissing = useMutation({
    mutationFn: () =>
      api.acquireSeries(seedCatalog!, { refs: missing.map((m) => m.ref!) }),
    onSuccess: (r) => {
      toast(`Fetching ${r.results.length} missing volume(s) — see the Jobs tab`, "success");
      qc.invalidateQueries({ queryKey: qk.downloads() });
      qc.invalidateQueries({ queryKey: qk.works() });
    },
    onError: (e) => toast((e as Error).message, "error"),
  });

  return (
    <div
      className="fixed inset-0 z-50 flex justify-center overflow-y-auto bg-black/50 p-0 sm:p-6"
      onClick={onClose}
    >
      <div
        ref={focusRef}
        role="dialog"
        aria-modal="true"
        aria-label={`Series: ${name}`}
        tabIndex={-1}
        className="relative h-full w-full max-w-xl overflow-y-auto bg-surface sm:h-auto sm:rounded-2xl sm:shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="sticky top-0 z-10 flex items-center justify-between gap-2 border-b border-border bg-surface px-4 py-3">
          <div className="truncate font-semibold">
            {name}
            {vols.length ? (
              <span className="text-muted">
                {" "}
                · {vols.length - missing.length}/{vols.length} owned
              </span>
            ) : (
              <span className="text-muted"> · {books.length} in library</span>
            )}
          </div>
          <div className="flex shrink-0 items-center gap-1">
            <Button
              size="sm"
              variant="danger"
              disabled={removeSeries.isPending}
              onClick={async () => {
                if (await confirm({
                  title: "Remove series",
                  message: `Remove all ${books.length} owned volume${books.length === 1 ? "" : "s"} of “${name}” from your library? (Volumes you don't own are unaffected.)`,
                  danger: true,
                  confirmText: "Remove series",
                })) removeSeries.mutate();
              }}
            >
              {removeSeries.isPending ? "Removing…" : "Remove series"}
            </Button>
            <Button size="sm" variant="ghost" aria-label="Close" onClick={onClose}>
              ✕
            </Button>
          </div>
        </div>
        <div className="px-4 py-3">
          {full.isLoading && <Spinner label="Finding the full series…" />}
          {!full.isLoading && missing.length > 0 && (
            <div className="mb-3 flex items-center justify-between gap-2 rounded-lg border border-amber-500/30 bg-amber-500/5 p-2.5 text-sm">
              <span>
                ⚠ {missing.length} book{missing.length === 1 ? "" : "s"} in this series{" "}
                {missing.length === 1 ? "is" : "are"} missing from your library.
              </span>
              {seedCatalog && (
                <Button
                  size="sm"
                  variant="primary"
                  disabled={fetchMissing.isPending}
                  onClick={() => fetchMissing.mutate()}
                >
                  {fetchMissing.isPending ? "Fetching…" : `Fetch ${missing.length} missing`}
                </Button>
              )}
            </div>
          )}
          <div className="space-y-1">
            {(vols.length ? vols : books.map((b) => ({
              title: b.title, author: b.author, year: null, position: b.series_position,
              cover_url: b.cover_url, ref: null, catalog_id: null,
              hooked_work_id: b.id, in_library: true,
            }) as SeriesBook)).map((v, i) => {
              // A volume is readable when the server says it's in the user's library AND knows which
              // local work it maps to. (in_library is computed server-side from the user's own
              // membership, so the hooked work is always theirs — no need to cross-check the grid.)
              const owned = !!(v.in_library && v.hooked_work_id);
              return (
                <div
                  key={v.ref ?? `${v.title}:${i}`}
                  className="flex items-center gap-2 rounded px-1 py-1 text-sm hover:bg-bg/50"
                >
                  {v.cover_url ? (
                    <img
                      src={coverSrc(v.cover_url) ?? ""}
                      alt=""
                      loading="lazy"
                      className="h-10 w-7 shrink-0 rounded border border-border object-cover"
                      onError={(e) => (e.currentTarget.style.display = "none")}
                    />
                  ) : null}
                  <span className="min-w-0 flex-1 truncate">
                    {v.position != null ? <span className="text-muted">#{v.position} </span> : ""}
                    {v.title}
                    {v.year ? <span className="text-muted"> ({v.year})</span> : null}
                  </span>
                  {owned ? (
                    <Button
                      size="sm"
                      variant="primary"
                      onClick={() => navigate(`/read/${v.hooked_work_id}`)}
                    >
                      Read
                    </Button>
                  ) : v.in_library ? (
                    <Badge tone="green">in library</Badge>
                  ) : (
                    <Badge tone="amber">missing</Badge>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
}
