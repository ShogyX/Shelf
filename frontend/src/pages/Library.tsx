import { Link, useNavigate } from "react-router-dom";
import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query";
import { api, Bookshelf, ContinueItem, SeriesBook, Work } from "../api/client";
import { useEffect, useState } from "react";
import { Badge, Button, Card, EmptyState, Spinner, useDialogFocus } from "../components/ui";
import { useConfirm } from "../components/confirm";
import Cover, { coverSrc } from "../components/Cover";
import SendDialog from "../components/SendDialog";
import type { Tone } from "../components/IndexShared";
import { useIsAdmin } from "../auth";
import { useApp } from "../store";

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
function ShelfMenu({ work, shelves }: { work: Work; shelves: Bookshelf[] }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const on = new Set(work.shelf_ids);
  const toggle = useMutation({
    mutationFn: ({ shelfId, add }: { shelfId: number; add: boolean }) =>
      add ? api.addWorkToShelf(shelfId, work.id) : api.removeWorkFromShelf(shelfId, work.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["works"] });
      qc.invalidateQueries({ queryKey: ["bookshelves"] });
    },
  });
  if (shelves.length === 0) return null;
  return (
    <div className="relative">
      <Button size="sm" variant="outline" title="Add to a bookshelf" onClick={() => setOpen((o) => !o)}>
        🗂 Shelves{on.size ? ` (${on.size})` : ""}
      </Button>
      {open && (
        <>
          <div className="fixed inset-0 z-10" onClick={() => setOpen(false)} />
          <div className="absolute z-20 mt-1 w-48 rounded-lg border border-border bg-surface p-1 shadow-xl">
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

function ContinueReading() {
  const qc = useQueryClient();
  const { data } = useQuery({ queryKey: ["continue"], queryFn: api.continueReading,
    refetchOnMount: "always" });   // always re-pull on return from the reader (progress may have moved)
  const clear = useMutation({
    mutationFn: (workId: number) => api.clearProgress(workId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["continue"] }),
  });
  if (!data || data.length === 0) return null;
  return (
    <section className="mb-9">
      <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-muted">
        Continue reading
      </h2>
      <div className="flex gap-3 overflow-x-auto pb-2 scrollbar-thin">
        {data.map((it: ContinueItem) => (
          <div
            key={it.work_id}
            className="group relative flex w-72 shrink-0 gap-3 rounded-xl border border-border bg-surface p-3 transition hover:border-accent/60"
          >
            <button
              title="Remove from Continue reading"
              disabled={clear.isPending}
              onClick={() => clear.mutate(it.work_id)}
              className="absolute right-1.5 top-1.5 z-10 rounded-full bg-surface-2/90 px-1.5 text-xs text-muted opacity-100 transition hover:text-text sm:opacity-0 sm:group-hover:opacity-100"
            >
              ✕
            </button>
            <Link to={`/read/${it.work_id}/${it.chapter_id}`} className="flex min-w-0 flex-1 gap-3">
              <div className="h-24 w-16 shrink-0 overflow-hidden rounded-md">
                <Cover title={it.title} coverUrl={it.cover_url} small />
              </div>
              <div className="flex min-w-0 flex-1 flex-col">
                <div className="truncate font-medium leading-tight">{it.title}</div>
                <div className="mt-0.5 truncate text-xs text-muted">{it.chapter_title}</div>
                <div className="mt-auto">
                  <div className="mb-1 h-1.5 w-full overflow-hidden rounded-full bg-surface-2">
                    <div className="h-full rounded-full bg-accent" style={{ width: `${it.percent}%` }} />
                  </div>
                  <div className="flex items-center justify-between text-[11px] text-muted">
                    <span>{it.percent}%</span>
                    <span className="text-accent opacity-100 transition sm:opacity-0 sm:group-hover:opacity-100">Resume →</span>
                  </div>
                </div>
              </div>
            </Link>
          </div>
        ))}
      </div>
    </section>
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
  const { data: works = [] } = useQuery({ queryKey: ["works", "", null], queryFn: () => api.listWorks() });
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
  const inval = () => qc.invalidateQueries({ queryKey: ["bookshelves"] });

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
  const { data: shelves = [] } = useQuery({ queryKey: ["bookshelves"], queryFn: api.listBookshelves });
  const { data: works, isLoading, isError, refetch } = useQuery({
    queryKey: ["works", q, activeShelf],
    queryFn: () => api.listWorks(q, { shelfId: activeShelf ?? undefined }),
  });
  const activeShelfObj = shelves.find((s) => s.id === activeShelf) ?? null;

  const del = useMutation({
    mutationFn: (id: number) => api.deleteWork(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["works"] }),
  });

  const repair = useMutation({
    mutationFn: (id: number) => api.repairWork(id),
    onSuccess: (rep) => {
      qc.invalidateQueries({ queryKey: ["works"] });
      const acted = rep.actions.length ? rep.actions.join("; ") : "no fixable issues found";
      toast(`Diagnosis: ${rep.health}. ${rep.detail ?? ""} — ${acted}.`);
    },
    onError: (e) => toast((e as Error).message, "error"),
  });

  const checkOne = useMutation({
    mutationFn: (id: number) => api.checkWorkUpdates(id),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["works"] });
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
      qc.invalidateQueries({ queryKey: ["works"] });
      toast("Resumed — checking for new chapters and gathering any outstanding ones.", "success");
    },
    onError: (e) => toast((e as Error).message, "error"),
  });

  const pauseOne = useMutation({
    mutationFn: (id: number) => api.pauseWork(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["works"] });
      toast("Paused — automatic updates are off for this title.");
    },
    onError: (e) => toast((e as Error).message, "error"),
  });

  const checkAll = useMutation({
    mutationFn: () => api.checkAllUpdates(),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["works"] });
      toast(
        `Checked ${r.works_checked} title${r.works_checked === 1 ? "" : "s"}: ` +
          `${r.works_updated} updated, ${r.new_chapters} new chapter${r.new_chapters === 1 ? "" : "s"}.`,
        "success"
      );
    },
    onError: (e) => toast((e as Error).message, "error"),
  });

  return (
    <main className="mx-auto max-w-5xl px-4 py-8">
      <h1 className="mb-4 text-2xl font-semibold">Library</h1>

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

      {!q && !activeShelf && <ContinueReading />}

      {isLoading && <Spinner label="Loading library…" />}

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

      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 md:grid-cols-4">
        {buildGridItems(works).map((it) => {
          if (it.kind === "series")
            return <SeriesLibraryCard key={`series:${it.name}`} name={it.name} books={it.books} />;
          const w = it.work;
          return (
            <Card key={w.id} className="group relative overflow-hidden">
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
                  <div className="aspect-[2/3] w-full overflow-hidden">
                    <Cover title={w.title} author={w.author} coverUrl={w.cover_url} />
                  </div>
                </button>
              ) : (
                <Link to={`/read/${w.id}`} className="block">
                  <div className="aspect-[2/3] w-full overflow-hidden">
                    <Cover title={w.title} author={w.author} coverUrl={w.cover_url} />
                  </div>
                </Link>
              )}
              <div className="space-y-1 p-3">
                <Link to={`/read/${w.id}`} className="block font-medium leading-tight hover:underline line-clamp-2">
                  {w.title}
                </Link>
                <div className="text-xs text-muted line-clamp-1">{w.author ?? "Unknown author"}</div>
                <div className="flex flex-wrap items-center gap-1.5 pt-1">
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
                <div className="flex flex-wrap gap-1.5 pt-2 opacity-100 transition sm:opacity-0 sm:group-hover:opacity-100">
                  <Button size="sm" variant="primary" onClick={() => navigate(`/read/${w.id}`)}>
                    Read
                  </Button>
                  <Button size="sm" variant="outline" title="Send to Kindle / export EPUB"
                    onClick={() => setSendWork(w)}>
                    📤 Send
                  </Button>
                  <ShelfMenu work={w} shelves={shelves} />
                  {w.library_status === "incomplete" && (
                    <Button
                      size="sm"
                      variant="outline"
                      title={w.health_detail ?? "Diagnose and fix missing chapters"}
                      disabled={repair.isPending && repair.variables === w.id}
                      onClick={() => repair.mutate(w.id)}
                    >
                      {repair.isPending && repair.variables === w.id ? "Fixing…" : "🩺 Fix"}
                    </Button>
                  )}
                  {w.library_status === "paused" && (
                    <Button
                      size="sm"
                      variant="primary"
                      title="Resume automatic updates — gather outstanding chapters and check for new releases"
                      disabled={resumeOne.isPending && resumeOne.variables === w.id}
                      onClick={() => resumeOne.mutate(w.id)}
                    >
                      {resumeOne.isPending && resumeOne.variables === w.id ? "Resuming…" : "▶ Resume"}
                    </Button>
                  )}
                  {w.hooked && w.library_status !== "paused" && w.status === "ongoing" && (
                    <>
                      <Button
                        size="sm"
                        variant="outline"
                        title={
                          w.last_checked_at
                            ? `Check for new chapters (last checked ${new Date(w.last_checked_at).toLocaleString()})`
                            : "Check for new chapters"
                        }
                        disabled={checkOne.isPending && checkOne.variables === w.id}
                        onClick={() => checkOne.mutate(w.id)}
                      >
                        {checkOne.isPending && checkOne.variables === w.id ? "Checking…" : "⟳ Updates"}
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        title="Pause automatic updates for this title"
                        disabled={pauseOne.isPending && pauseOne.variables === w.id}
                        onClick={() => pauseOne.mutate(w.id)}
                      >
                        {pauseOne.isPending && pauseOne.variables === w.id ? "Pausing…" : "⏸ Pause"}
                      </Button>
                    </>
                  )}
                  <Button
                    size="sm"
                    variant="danger"
                    onClick={async () => {
                      if (await confirm({ title: "Remove from library", message: `Remove “${w.title}” from your library?`, danger: true, confirmText: "Remove" }))
                        del.mutate(w.id);
                    }}
                  >
                    Remove
                  </Button>
                </div>
              </div>
            </Card>
          );
        })}
      </div>

      {sendWork && (
        <SendDialog workId={sendWork.id} title={sendWork.title} onClose={() => setSendWork(null)} />
      )}
      {showShelfDialog && (
        <ShelfDialog
          onClose={() => setShowShelfDialog(false)}
          onCreated={(id) => { setShowShelfDialog(false); setActiveShelf(id); }}
        />
      )}
    </main>
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
      seen.add(w.series);
      const books = (works ?? [])
        .filter((x) => x.series === w.series)
        .sort((a, b) => (a.series_position ?? 9999) - (b.series_position ?? 9999));
      out.push({ kind: "series", name: w.series, books });
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
      <Card className="group relative overflow-hidden">
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
    queryKey: ["work-series", seedId],
    queryFn: () => api.workSeries(seedId),
    enabled: !!seedId,
  });
  const focusRef = useDialogFocus(onClose);   // Escape + focus trap/restore (shared dialog behavior)

  const vols: SeriesBook[] = full.data?.books ?? [];
  const missing = vols.filter((v) => !v.in_library && v.ref && v.catalog_id);
  const seedCatalog = vols.find((v) => v.catalog_id)?.catalog_id ?? null;

  const fetchMissing = useMutation({
    mutationFn: () =>
      api.acquireSeries(seedCatalog!, { refs: missing.map((m) => m.ref!) }),
    onSuccess: (r) => {
      toast(`Fetching ${r.results.length} missing volume(s) — see the Jobs tab`, "success");
      qc.invalidateQueries({ queryKey: ["downloads"] });
      qc.invalidateQueries({ queryKey: ["works"] });
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
        <div className="sticky top-0 z-10 flex items-center justify-between gap-2 border-b border-border bg-surface/95 px-4 py-3 backdrop-blur">
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
          <Button size="sm" variant="ghost" aria-label="Close" onClick={onClose}>
            ✕
          </Button>
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
