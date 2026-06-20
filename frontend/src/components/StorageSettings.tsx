import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, PathMapping, StorageState } from "../api/client";
import { qk } from "../api/queryKeys";
import { Badge, Button, Card, InfoHint, inputCls } from "./ui";

/** One overridable app directory: shows the path in use as the placeholder; the input is the
 *  override (blank = use the default). */
function PathField({ label, hint, value, placeholder, onChange }: {
  label: string; hint: string; value: string; placeholder: string; onChange: (v: string) => void;
}) {
  return (
    <label className="block">
      <span className="flex items-center gap-1.5 text-xs text-muted">{label}<InfoHint text={hint} /></span>
      <input className={`${inputCls} mt-1`} value={value} placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)} spellCheck={false} />
    </label>
  );
}

export default function StorageSettings() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: qk.storage(), queryFn: api.getStorage });
  const [f, setF] = useState<{
    media_dir: string; covers_dir: string; backup_dir: string; stock_dir: string;
    sab_library_path: string; sab_category: string; libgen_download_dir: string;
    audiobook_library_path: string;
    sab_path_mappings: PathMapping[];
  } | null>(null);
  const [saved, setSaved] = useState(false);
  const [migrate, setMigrate] = useState(false);
  const [migrateNote, setMigrateNote] = useState<string | null>(null);

  useEffect(() => {
    if (q.data && f === null) {
      const d = q.data;
      setF({
        media_dir: d.image_cache_dir.override, covers_dir: d.covers_dir.override,
        backup_dir: d.backups_dir.override, stock_dir: d.stock_dir,
        sab_library_path: d.sab_library_path, sab_category: d.sab_category,
        libgen_download_dir: d.libgen_download_dir,
        audiobook_library_path: d.audiobook_library_path,
        sab_path_mappings: d.sab_path_mappings.length ? d.sab_path_mappings : [{ remote: "", local: "" }],
      });
    }
  }, [q.data, f]);

  const save = useMutation({
    mutationFn: () => api.putStorage({
      ...f!,
      sab_path_mappings: (f!.sab_path_mappings || []).filter((m) => m.remote || m.local),
      migrate,
    }),
    onSuccess: (d: StorageState) => {
      qc.setQueryData(qk.storage(), d);
      setSaved(true);
      const m = d.migrated || {};
      const moved = Object.entries(m).map(([k, n]) => `${k.replace("_dir", "")}: ${n}`).join(", ");
      setMigrateNote(migrate ? (moved ? `Moved existing files (${moved}).` : "Nothing to move.") : null);
      setTimeout(() => setSaved(false), 2500);
    },
  });

  if (!q.data || !f) return <Card className="mb-4 p-4"><p className="text-sm text-muted">Loading…</p></Card>;
  const d = q.data;
  const setMap = (i: number, k: keyof PathMapping, v: string) =>
    setF({ ...f, sab_path_mappings: f.sab_path_mappings.map((m, j) => j === i ? { ...m, [k]: v } : m) });

  return (
    <>
      <Card className="mb-4 p-4">
        <h2 className="mb-1 flex items-center gap-1.5 font-semibold">
          Library pool & directories
          <InfoHint text={<>The stock directory is the central on-disk pool where items live; a user's
            library is just a list of pointers into it (and into monitored folders, which act as
            secondary pools) — re-pointing a path only changes where NEW files are written/read, it
            does not move existing data. Blank a field to use the built-in default.</>} />
        </h2>
        <p className="mb-3 text-xs text-muted">
          Changing the image-cache path needs a restart to remount it; the others apply immediately.
        </p>
        <div className="grid gap-3 sm:grid-cols-2">
          <PathField label="Stock pool (central item store)" value={f.stock_dir}
            placeholder="/mnt/pool/stock"
            hint="Where stocked/pooled on-disk items are kept. The central pool every library points into."
            onChange={(v) => setF({ ...f, stock_dir: v })} />
          <PathField label="Media root (web content + image cache)" value={f.media_dir}
            placeholder={d.image_cache_dir.effective}
            hint="The on-disk root for web-crawled/captured content: comic pages (comics/), book media
              (books/), descrambled captures, and the evictable image cache (imgcache/). This IS the
              web-crawl ingest target. Served at /media, so it needs a restart to remount."
            onChange={(v) => setF({ ...f, media_dir: v })} />
          <PathField label="Cover store" value={f.covers_dir}
            placeholder={d.covers_dir.effective}
            hint="Durable cover art (never evicted). Where fetched covers persist."
            onChange={(v) => setF({ ...f, covers_dir: v })} />
          <PathField label="Backups" value={f.backup_dir}
            placeholder={d.backups_dir.effective}
            hint="Where app-created + uploaded backups are stored. Keep this OUTSIDE the image cache."
            onChange={(v) => setF({ ...f, backup_dir: v })} />
        </div>
      </Card>

      <Card className="mb-4 p-4">
        <h2 className="mb-3 flex items-center gap-1.5 font-semibold">
          Download paths
          <InfoHint text={<>Where the acquisition pipelines drop files before they're imported into the
            pool. SAB path mappings translate the SABnzbd host's paths to the paths this app reads.</>} />
        </h2>
        {d.sab_configured ? (
          <div className="space-y-3">
            <div className="grid gap-3 sm:grid-cols-2">
              <PathField label="SABnzbd library path" value={f.sab_library_path}
                placeholder="(import in place)"
                hint="Where verified SAB downloads are promoted to. Blank = import in place from SAB's drop folder."
                onChange={(v) => setF({ ...f, sab_library_path: v })} />
              <label className="block">
                <span className="text-xs text-muted">SABnzbd category</span>
                <input className={`${inputCls} mt-1`} value={f.sab_category} placeholder="shelf"
                  onChange={(e) => setF({ ...f, sab_category: e.target.value })} spellCheck={false} />
              </label>
            </div>
            <div>
              <span className="flex items-center gap-1.5 text-xs text-muted">
                SABnzbd path mappings (remote → local)
                <InfoHint text="Map the path SABnzbd reports (on its host) to the path this app can read (its mount). One row per mapping." />
              </span>
              <div className="mt-1 space-y-1.5">
                {f.sab_path_mappings.map((m, i) => (
                  <div key={i} className="flex items-center gap-2">
                    <input className={inputCls} value={m.remote} placeholder="/downloads (SAB host)"
                      onChange={(e) => setMap(i, "remote", e.target.value)} spellCheck={false} />
                    <span className="text-muted">→</span>
                    <input className={inputCls} value={m.local} placeholder="/mnt/sab (this app)"
                      onChange={(e) => setMap(i, "local", e.target.value)} spellCheck={false} />
                    <button className="px-1 text-red-500 hover:text-red-400" title="Remove"
                      onClick={() => setF({ ...f, sab_path_mappings: f.sab_path_mappings.filter((_, j) => j !== i) })}>✕</button>
                  </div>
                ))}
                <button className="text-xs text-accent hover:underline"
                  onClick={() => setF({ ...f, sab_path_mappings: [...f.sab_path_mappings, { remote: "", local: "" }] })}>
                  + add mapping
                </button>
              </div>
            </div>
          </div>
        ) : (
          <p className="text-sm text-muted">No SABnzbd integration — configure one under Integrations to set download paths.</p>
        )}
        {d.libgen_configured && (
          <div className="mt-3">
            <PathField label="Open-libraries (libgen) download dir" value={f.libgen_download_dir}
              placeholder="(stock pool)"
              hint="Where the libgen/open-libraries pipeline saves downloads before import. Blank = the stock pool."
              onChange={(v) => setF({ ...f, libgen_download_dir: v })} />
          </div>
        )}
        <div className="mt-3">
          <PathField label="Audiobook library path" value={f.audiobook_library_path}
            placeholder="(Audiobooks dir next to books)"
            hint="Where audiobooks are stored, separate from ebooks. Blank = a sibling “Audiobooks” directory next to the books library."
            onChange={(v) => setF({ ...f, audiobook_library_path: v })} />
        </div>
      </Card>

      <Card className="mb-4 p-4">
        <h2 className="mb-1 flex items-center gap-1.5 font-semibold">
          Monitored folders <span className="text-sm font-normal text-muted">· secondary pools</span>
          <InfoHint text={<>Folders watched for new titles. Each acts as a secondary on-disk pool: imported
            items stay where they are and libraries only point at them. Add/remove monitored folders on
            the Add page.</>} />
        </h2>
        {d.watched_folders.length === 0 ? (
          <p className="text-sm text-muted">None. Add one from the Add page.</p>
        ) : (
          <ul className="space-y-1 text-sm">
            {d.watched_folders.map((w) => (
              <li key={w.id} className="flex items-center gap-2">
                <span className="font-mono text-xs">{w.path}</span>
                {!w.enabled && <Badge>disabled</Badge>}
              </li>
            ))}
          </ul>
        )}
      </Card>

      <label className="mb-2 flex items-center gap-2 text-sm text-muted">
        <input type="checkbox" checked={migrate} onChange={(e) => setMigrate(e.target.checked)} />
        Move existing files to the new locations on save
        <InfoHint text={<>When a directory changes, also MOVE its current contents to the new path
          (skip-existing). Instant on the same filesystem; a slower recursive copy across mounts. Do
          this while the app is quiet. Without it, only NEW files use the new path.</>} />
      </label>
      <div className="flex items-center gap-2">
        <Button variant="primary" disabled={save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? (migrate ? "Saving & moving…" : "Saving…") : "Save storage paths"}
        </Button>
        {saved && <Badge tone="green">saved</Badge>}
        {migrateNote && <span className="text-sm text-green-600">{migrateNote}</span>}
        {save.isError && <span className="text-sm text-red-500">{(save.error as Error).message}</span>}
      </div>
    </>
  );
}
