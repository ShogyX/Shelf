// Settings → Bookshelves: create, configure, download and delete the user's named shelves. Holds the
// shelf-management surface moved out of the Library page (the old ShelfBar settings card + the create
// dialog), with every mutation/endpoint preserved (create/update/delete/download bookshelves).
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, Bookshelf } from "../../api/client";
import { qk } from "../../api/queryKeys";
import { Button, Card, CardHeader, useDialogFocus } from "../ui";
import { useConfirm } from "../confirm";
import { useApp } from "../../store";
import { useIsAdmin } from "../../auth";

// Mirrors the per-shelf automation flags (kept identical to the Library create dialog). Note there's
// no per-shelf "auto-update" toggle — every actively-releasing title in the library is refreshed
// automatically.
const FLAG_FIELDS: { key: keyof Bookshelf; label: string; hint: string }[] = [
  { key: "auto_kindle", label: "Auto-send to Kindle", hint: "Email newly gathered chapters to your Kindle automatically" },
  { key: "notify_on_add", label: "Notify on add", hint: "Push a notification when a title is added to this shelf (incl. via a watched path)" },
  { key: "notify_email", label: "Email on add", hint: "Email the book to your personal address when it's added to this shelf" },
  { key: "goodreads_target", label: "Goodreads destination", hint: "Auto-hooked Goodreads titles (your default shelf) land here" },
];

/** Highlighted modal to create a bookshelf: name, automation, an external Goodreads shelf, and the
 *  works to put on it. (Moved verbatim from pages/Library — calls api.createBookshelf.) */
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

/** One shelf's settings card: automation toggles, the external Goodreads shelf, an admin-only watch
 *  path, plus Download / Delete. (Moved from the Library ShelfBar settings card — same mutations.) */
function ShelfSettings({ shelf }: { shelf: Bookshelf }) {
  const qc = useQueryClient();
  const toast = useApp((s) => s.toast);
  const confirm = useConfirm();
  const isAdmin = useIsAdmin();
  const [grShelf, setGrShelf] = useState(shelf.goodreads_shelf ?? "");
  const [watchPath, setWatchPath] = useState(shelf.watch_path ?? "");
  const inval = () => qc.invalidateQueries({ queryKey: qk.bookshelves() });

  useEffect(() => {
    setGrShelf(shelf.goodreads_shelf ?? "");
    setWatchPath(shelf.watch_path ?? "");
  }, [shelf]);

  const update = useMutation({
    mutationFn: (patch: Partial<Bookshelf>) => api.updateBookshelf(shelf.id, patch),
    onSuccess: () => inval(),
  });
  const remove = useMutation({
    mutationFn: () => api.deleteBookshelf(shelf.id),
    onSuccess: () => inval(),
  });

  const toggle = (key: keyof Bookshelf, label: string, hint: string) => (
    <label className="flex items-center gap-2 text-sm" title={hint}>
      <input
        type="checkbox"
        checked={Boolean(shelf[key])}
        disabled={update.isPending}
        onChange={(e) => update.mutate({ [key]: e.target.checked })}
      />
      {label}
    </label>
  );

  return (
    <Card className="mb-3 p-3.5">
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="text-sm font-semibold">
          “{shelf.name}” <span className="font-normal text-muted">· {shelf.count} title{shelf.count === 1 ? "" : "s"}</span>
        </div>
      </div>
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
          onClick={() => api.downloadLibrary({ shelf_id: shelf.id }).catch((e) => toast((e as Error).message, "error"))}>
          ⬇ Download shelf
        </Button>
        <Button size="sm" variant="danger" disabled={remove.isPending}
          onClick={async () => {
            if (await confirm({ title: "Delete shelf", message: `Delete shelf “${shelf.name}”? The titles stay in your library.`, danger: true }))
              remove.mutate();
          }}>
          Delete shelf
        </Button>
      </div>
    </Card>
  );
}

export default function BookshelvesPanel() {
  const { data: shelves = [] } = useQuery({ queryKey: qk.bookshelves(), queryFn: api.listBookshelves });
  const [showDialog, setShowDialog] = useState(false);

  return (
    <Card className="mb-4 p-4">
      <CardHeader
        title="Bookshelves"
        desc="Group titles into named shelves and tune each shelf's automation."
        hint={<>Bookshelves group your library however you like. Each shelf can auto-send new chapters
          to Kindle, email or notify you when a title is added, act as a Goodreads destination, and
          (for admins) watch a folder for new books. Manage which titles are on a shelf from the 🗂
          Shelves button on any work, or filter the library by shelf in Browse.</>}
        badge={<Button size="sm" variant="primary" onClick={() => setShowDialog(true)}>+ New shelf</Button>}
      />
      {shelves.length === 0 ? (
        <p className="text-sm text-muted">
          No shelves yet — group titles into one with “+ New shelf”.
        </p>
      ) : (
        <div>
          {shelves.map((s) => (
            <ShelfSettings key={s.id} shelf={s} />
          ))}
        </div>
      )}
      {showDialog && (
        <ShelfDialog
          onClose={() => setShowDialog(false)}
          onCreated={() => setShowDialog(false)}
        />
      )}
    </Card>
  );
}
