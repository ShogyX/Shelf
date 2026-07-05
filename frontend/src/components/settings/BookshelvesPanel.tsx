// Settings → Bookshelves: create, configure, download and delete the user's named shelves. Holds the
// shelf-management surface moved out of the Library page (the old ShelfBar settings card + the create
// dialog), with every mutation/endpoint preserved (create/update/delete/download bookshelves).
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
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
const buildFlagFields = (t: TFunction): { key: keyof Bookshelf; label: string; hint: string }[] => [
  { key: "auto_kindle", label: t("bookshelves.flag.autoKindle"), hint: t("bookshelves.flag.autoKindleHint") },
  { key: "notify_on_add", label: t("bookshelves.flag.notifyOnAdd"), hint: t("bookshelves.flag.notifyOnAddHint") },
  { key: "notify_email", label: t("bookshelves.flag.notifyEmail"), hint: t("bookshelves.flag.notifyEmailHint") },
  { key: "goodreads_target", label: t("bookshelves.flag.goodreadsTarget"), hint: t("bookshelves.flag.goodreadsTargetHint") },
];

/** Highlighted modal to create a bookshelf: name, automation, an external Goodreads shelf, and the
 *  works to put on it. (Moved verbatim from pages/Library — calls api.createBookshelf.) */
function ShelfDialog({ onClose, onCreated }: { onClose: () => void; onCreated: (id: number) => void }) {
  const { t } = useTranslation();
  const FLAG_FIELDS = buildFlagFields(t);
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
        aria-label={t("bookshelves.dialog.title")}
        tabIndex={-1}
        className="fixed left-1/2 top-1/2 z-50 flex max-h-[90vh] w-[34rem] max-w-[calc(100vw-1.5rem)] -translate-x-1/2 -translate-y-1/2 flex-col rounded-2xl border border-accent/40 bg-surface shadow-2xl ring-1 ring-accent/20"
      >
        <div className="flex items-center justify-between border-b border-border px-5 py-3">
          <h2 className="font-semibold">{t("bookshelves.dialog.title")}</h2>
          <button className="text-muted hover:text-text" aria-label={t("bookshelves.dialog.close")} onClick={onClose}>✕</button>
        </div>
        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-5 py-4">
          <label className="block text-xs text-muted">
            {t("bookshelves.dialog.name")}
            <input
              autoFocus
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={t("bookshelves.dialog.namePlaceholder")}
              className="mt-1 w-full rounded-lg border border-border bg-bg px-3 py-2 text-sm text-text"
            />
          </label>

          <div>
            <div className="mb-1.5 text-xs text-muted">{t("bookshelves.dialog.automation")}</div>
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
            {t("bookshelves.dialog.grShelf")}
            <input
              value={grShelf}
              onChange={(e) => setGrShelf(e.target.value)}
              placeholder={t("bookshelves.dialog.grShelfPlaceholder")}
              className="mt-1 w-full rounded-lg border border-border bg-bg px-3 py-2 text-sm text-text"
            />
            <span className="mt-1 block text-[11px] text-muted">
              {t("bookshelves.dialog.grShelfHint")}
            </span>
          </label>

          <div>
            <div className="mb-1.5 flex items-center justify-between text-xs text-muted">
              <span>{picked.size ? t("bookshelves.dialog.addWorksSelected", { count: picked.size }) : t("bookshelves.dialog.addWorks")}</span>
              <input
                value={wq}
                onChange={(e) => setWq(e.target.value)}
                placeholder={t("bookshelves.dialog.filterPlaceholder")}
                className="w-32 rounded-lg border border-border bg-bg px-2 py-1 text-xs"
              />
            </div>
            <div className="max-h-48 overflow-y-auto rounded-lg border border-border">
              {filtered.length === 0 && (
                <div className="p-3 text-xs text-muted">{t("bookshelves.dialog.noWorks")}</div>
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
          <Button variant="ghost" onClick={onClose}>{t("common.cancel")}</Button>
          <Button variant="primary" disabled={!name.trim() || busy} onClick={create}>
            {busy ? t("bookshelves.dialog.creating") : t("bookshelves.dialog.create")}
          </Button>
        </div>
      </div>
    </>
  );
}

/** One shelf's settings card: automation toggles, the external Goodreads shelf, an admin-only watch
 *  path, plus Download / Delete. (Moved from the Library ShelfBar settings card — same mutations.) */
function ShelfSettings({ shelf }: { shelf: Bookshelf }) {
  const { t } = useTranslation();
  const FLAG_FIELDS = buildFlagFields(t);
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
          “{shelf.name}” <span className="font-normal text-muted">· {t("bookshelves.titleCount", { count: shelf.count })}</span>
        </div>
      </div>
      <div className="flex flex-wrap gap-x-6 gap-y-2">
        {FLAG_FIELDS.map((f) => toggle(f.key, f.label, f.hint))}
      </div>
      <label className="mt-3 block text-xs text-muted">
        {t("bookshelves.settings.grShelf")}
        <span className="ml-1 flex items-center gap-2">
          <input
            value={grShelf}
            onChange={(e) => setGrShelf(e.target.value)}
            placeholder={t("bookshelves.settings.grShelfPlaceholder")}
            className="mt-1 w-48 rounded-lg border border-border bg-bg px-3 py-1.5 text-sm text-text"
          />
          <Button size="sm" variant="outline" disabled={update.isPending}
            onClick={() => update.mutate({ goodreads_shelf: grShelf.trim() || null })}>
            {t("common.save")}
          </Button>
        </span>
      </label>
      {isAdmin && (
        <label className="mt-3 block text-xs text-muted">
          {t("bookshelves.settings.watchPath")}
          <span className="ml-1 flex items-center gap-2">
            <input
              value={watchPath}
              onChange={(e) => setWatchPath(e.target.value)}
              placeholder="/mnt/NAS-Pool/media/Books"
              className="mt-1 w-80 rounded-lg border border-border bg-bg px-3 py-1.5 text-sm text-text"
            />
            <Button size="sm" variant="outline" disabled={update.isPending}
              onClick={() => update.mutate({ watch_path: watchPath.trim() || null })}>
              {t("common.save")}
            </Button>
          </span>
        </label>
      )}
      <div className="mt-3 flex gap-2">
        <Button size="sm" variant="outline" title={t("bookshelves.settings.downloadTitle")}
          onClick={() => api.downloadLibrary({ shelf_id: shelf.id }).catch((e) => toast((e as Error).message, "error"))}>
          {t("bookshelves.settings.download")}
        </Button>
        <Button size="sm" variant="danger" disabled={remove.isPending}
          onClick={async () => {
            if (await confirm({ title: t("bookshelves.settings.deleteTitle"), message: t("bookshelves.settings.deleteConfirm", { name: shelf.name }), danger: true }))
              remove.mutate();
          }}>
          {t("bookshelves.settings.delete")}
        </Button>
      </div>
    </Card>
  );
}

export default function BookshelvesPanel() {
  const { t } = useTranslation();
  const { data: shelves = [] } = useQuery({ queryKey: qk.bookshelves(), queryFn: api.listBookshelves });
  const [showDialog, setShowDialog] = useState(false);

  return (
    <Card className="mb-4 p-4">
      <CardHeader
        title={t("bookshelves.panel.title")}
        desc={t("bookshelves.panel.desc")}
        hint={t("bookshelves.panel.hint")}
        badge={<Button size="sm" variant="primary" onClick={() => setShowDialog(true)}>{t("bookshelves.panel.newShelf")}</Button>}
      />
      {shelves.length === 0 ? (
        <p className="text-sm text-muted">
          {t("bookshelves.panel.empty")}
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
