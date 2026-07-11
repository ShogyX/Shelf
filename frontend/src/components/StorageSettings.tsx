import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, PathMapping, StorageState } from "../api/client";
import { qk } from "../api/queryKeys";
import { Badge, Button, Card, InfoHint, inputCls } from "./ui";
import { X } from "lucide-react";

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
  const { t } = useTranslation();
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
      setMigrateNote(migrate ? (moved ? t("storage.movedFiles", { moved }) : t("storage.nothingToMove")) : null);
      setTimeout(() => setSaved(false), 2500);
    },
  });

  if (!q.data || !f) return <Card className="mb-4 p-4"><p className="text-sm text-muted">{t("common.loading")}</p></Card>;
  const d = q.data;
  const setMap = (i: number, k: keyof PathMapping, v: string) =>
    setF({ ...f, sab_path_mappings: f.sab_path_mappings.map((m, j) => j === i ? { ...m, [k]: v } : m) });

  return (
    <>
      <Card className="mb-4 p-4">
        <h2 className="mb-1 flex items-center gap-1.5 font-semibold">
          {t("storage.pool.title")}
          <InfoHint text={t("storage.pool.hint")} />
        </h2>
        <p className="mb-3 text-xs text-muted">
          {t("storage.pool.restartNote")}
        </p>
        <div className="grid gap-3 sm:grid-cols-2">
          <PathField label={t("storage.pool.stock")} value={f.stock_dir}
            placeholder="/mnt/pool/stock"
            hint={t("storage.pool.stockHint")}
            onChange={(v) => setF({ ...f, stock_dir: v })} />
          <PathField label={t("storage.pool.media")} value={f.media_dir}
            placeholder={d.image_cache_dir.effective}
            hint={t("storage.pool.mediaHint")}
            onChange={(v) => setF({ ...f, media_dir: v })} />
          <PathField label={t("storage.pool.covers")} value={f.covers_dir}
            placeholder={d.covers_dir.effective}
            hint={t("storage.pool.coversHint")}
            onChange={(v) => setF({ ...f, covers_dir: v })} />
          <PathField label={t("storage.pool.backups")} value={f.backup_dir}
            placeholder={d.backups_dir.effective}
            hint={t("storage.pool.backupsHint")}
            onChange={(v) => setF({ ...f, backup_dir: v })} />
        </div>
      </Card>

      <Card className="mb-4 p-4">
        <h2 className="mb-3 flex items-center gap-1.5 font-semibold">
          {t("storage.download.title")}
          <InfoHint text={t("storage.download.hint")} />
        </h2>
        {d.sab_configured ? (
          <div className="space-y-3">
            <div className="grid gap-3 sm:grid-cols-2">
              <PathField label={t("storage.download.sabLibrary")} value={f.sab_library_path}
                placeholder={t("storage.download.sabLibraryPlaceholder")}
                hint={t("storage.download.sabLibraryHint")}
                onChange={(v) => setF({ ...f, sab_library_path: v })} />
              <label className="block">
                <span className="text-xs text-muted">{t("storage.download.sabCategory")}</span>
                <input className={`${inputCls} mt-1`} value={f.sab_category} placeholder="shelf"
                  onChange={(e) => setF({ ...f, sab_category: e.target.value })} spellCheck={false} />
              </label>
            </div>
            <div>
              <span className="flex items-center gap-1.5 text-xs text-muted">
                {t("storage.download.sabMappings")}
                <InfoHint text={t("storage.download.sabMappingsHint")} />
              </span>
              <div className="mt-1 space-y-1.5">
                {f.sab_path_mappings.map((m, i) => (
                  <div key={i} className="flex items-center gap-2">
                    <input className={inputCls} value={m.remote} placeholder={t("storage.download.remotePlaceholder")}
                      onChange={(e) => setMap(i, "remote", e.target.value)} spellCheck={false} />
                    <span className="text-muted">→</span>
                    <input className={inputCls} value={m.local} placeholder={t("storage.download.localPlaceholder")}
                      onChange={(e) => setMap(i, "local", e.target.value)} spellCheck={false} />
                    <button className="px-1 text-red-500 hover:text-red-400" title={t("storage.remove")}
                      onClick={() => setF({ ...f, sab_path_mappings: f.sab_path_mappings.filter((_, j) => j !== i) })}><X className="h-4 w-4" /></button>
                  </div>
                ))}
                <button className="text-xs text-accent hover:underline"
                  onClick={() => setF({ ...f, sab_path_mappings: [...f.sab_path_mappings, { remote: "", local: "" }] })}>
                  {t("storage.download.addMapping")}
                </button>
              </div>
            </div>
          </div>
        ) : (
          <p className="text-sm text-muted">{t("storage.download.noSab")}</p>
        )}
        {d.libgen_configured && (
          <div className="mt-3">
            <PathField label={t("storage.download.libgen")} value={f.libgen_download_dir}
              placeholder={t("storage.download.libgenPlaceholder")}
              hint={t("storage.download.libgenHint")}
              onChange={(v) => setF({ ...f, libgen_download_dir: v })} />
          </div>
        )}
        <div className="mt-3">
          <PathField label={t("storage.download.audiobook")} value={f.audiobook_library_path}
            placeholder={t("storage.download.audiobookPlaceholder")}
            hint={t("storage.download.audiobookHint")}
            onChange={(v) => setF({ ...f, audiobook_library_path: v })} />
        </div>
      </Card>

      <Card className="mb-4 p-4">
        <h2 className="mb-1 flex items-center gap-1.5 font-semibold">
          {t("storage.watched.title")} <span className="text-sm font-normal text-muted">{t("storage.watched.subtitle")}</span>
          <InfoHint text={t("storage.watched.hint")} />
        </h2>
        {d.watched_folders.length === 0 ? (
          <p className="text-sm text-muted">{t("storage.watched.empty")}</p>
        ) : (
          <ul className="space-y-1 text-sm">
            {d.watched_folders.map((w) => (
              <li key={w.id} className="flex items-center gap-2">
                <span className="font-mono text-xs">{w.path}</span>
                {!w.enabled && <Badge>{t("storage.watched.disabled")}</Badge>}
              </li>
            ))}
          </ul>
        )}
      </Card>

      <label className="mb-2 flex items-center gap-2 text-sm text-muted">
        <input type="checkbox" checked={migrate} onChange={(e) => setMigrate(e.target.checked)} />
        {t("storage.migrate.label")}
        <InfoHint text={t("storage.migrate.hint")} />
      </label>
      <div className="flex items-center gap-2">
        <Button variant="primary" disabled={save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? (migrate ? t("storage.savingMoving") : t("common.saving")) : t("storage.savePaths")}
        </Button>
        {saved && <Badge tone="green">{t("storage.saved")}</Badge>}
        {migrateNote && <span className="text-sm text-green-600">{migrateNote}</span>}
        {save.isError && <span className="text-sm text-red-500">{(save.error as Error).message}</span>}
      </div>
    </>
  );
}
