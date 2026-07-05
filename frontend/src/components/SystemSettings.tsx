import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, SystemConfig } from "../api/client";
import { qk } from "../api/queryKeys";
import { Badge, Button, Card, InfoHint, inputCls, Toggle } from "./ui";

type FieldType = "text" | "number" | "bool" | "select";
interface Field {
  key: string;
  label: string;
  type: FieldType;
  help: string;
  options?: string[];
  placeholder?: string;
}
interface Group { title: string; titleKey: string; help: string; fields: Field[] }

// Declarative layout — keys match the backend's config_store.EDITABLE. `title` is the stable
// identifier the tabs filter by; `titleKey` is the translated heading shown to the user.
const buildGroups = (t: TFunction): Group[] => [
  {
    title: "Cloudflare solver",
    titleKey: t("sysconfig.cloudflare.title"),
    help: t("sysconfig.cloudflare.help"),
    fields: [
      { key: "flaresolverr_url", label: t("sysconfig.cloudflare.solverUrl"), type: "text", placeholder: "http://localhost:8191",
        help: t("sysconfig.cloudflare.solverUrlHelp") },
      { key: "flaresolverr_timeout_s", label: t("sysconfig.cloudflare.timeout"), type: "number",
        help: t("sysconfig.cloudflare.timeoutHelp") },
      { key: "flaresolverr_clearance_ttl_s", label: t("sysconfig.cloudflare.clearance"), type: "number",
        help: t("sysconfig.cloudflare.clearanceHelp") },
    ],
  },
  {
    title: "Comix crawler",
    titleKey: t("sysconfig.comix.title"),
    help: t("sysconfig.comix.help"),
    fields: [
      { key: "comix_browser_enabled", label: t("sysconfig.comix.enable"), type: "bool",
        help: t("sysconfig.comix.enableHelp") },
      { key: "comix_browser_pages_per_tick", label: t("sysconfig.comix.pagesPerTick"), type: "number",
        help: t("sysconfig.comix.pagesPerTickHelp") },
      { key: "solver_chrome_path", label: t("sysconfig.comix.chromePath"), type: "text", placeholder: t("sysconfig.comix.chromePathPlaceholder"),
        help: t("sysconfig.comix.chromePathHelp") },
    ],
  },
  {
    title: "Image cache",
    titleKey: t("sysconfig.imgcache.title"),
    help: t("sysconfig.imgcache.help"),
    fields: [
      { key: "imgcache_max_mb", label: t("sysconfig.imgcache.cap"), type: "number",
        help: t("sysconfig.imgcache.capHelp") },
    ],
  },
  // (The "Automatic backups" group lived here but duplicated AutoBackupSection on the Backup tab;
  //  dropped in the Settings reorg — auto_backup_* are still edited there.)
  {
    title: "Crawl defaults",
    titleKey: t("sysconfig.crawl.title"),
    help: t("sysconfig.crawl.help"),
    fields: [
      { key: "index_max_pages", label: t("sysconfig.crawl.maxPages"), type: "number",
        help: t("sysconfig.crawl.maxPagesHelp") },
      { key: "index_max_depth", label: t("sysconfig.crawl.maxDepth"), type: "number",
        help: t("sysconfig.crawl.maxDepthHelp") },
      { key: "index_stop_after_idle_pages", label: t("sysconfig.crawl.idlePages"), type: "number",
        help: t("sysconfig.crawl.idlePagesHelp") },
      { key: "index_max_pending_frontier", label: t("sysconfig.crawl.frontier"), type: "number",
        help: t("sysconfig.crawl.frontierHelp") },
    ],
  },
  {
    title: "Login & security",
    titleKey: t("sysconfig.login.title"),
    help: t("sysconfig.login.help"),
    fields: [
      { key: "login_max_attempts", label: t("sysconfig.login.maxAttempts"), type: "number",
        help: t("sysconfig.login.maxAttemptsHelp") },
      { key: "login_window_seconds", label: t("sysconfig.login.window"), type: "number",
        help: t("sysconfig.login.windowHelp") },
      { key: "min_password_length", label: t("sysconfig.login.minPassword"), type: "number",
        help: t("sysconfig.login.minPasswordHelp") },
    ],
  },
  {
    title: "List imports",
    titleKey: t("sysconfig.lists.title"),
    help: t("sysconfig.lists.help"),
    fields: [
      { key: "list_sync_interval_hours", label: t("sysconfig.lists.interval"), type: "number",
        help: t("sysconfig.lists.intervalHelp") },
    ],
  },
  {
    title: "Logging",
    titleKey: t("sysconfig.logging.title"),
    help: t("sysconfig.logging.help"),
    fields: [
      { key: "log_level", label: t("sysconfig.logging.level"), type: "select", options: ["DEBUG", "INFO", "WARNING", "ERROR"],
        help: t("sysconfig.logging.levelHelp") },
    ],
  },
];

/** Renders + saves a SUBSET of the system-config groups (by title), so each group can live on the
 *  tab it belongs to (Login→Users, Crawl/Comix→Indexing, Image cache→Storage, Cloudflare→Integrations,
 *  Logging→Backups). The PUT is a partial merge that sends ONLY this card's keys, so multiple cards
 *  (and RegistrationModeCard) editing the same shared ["system-config"] never clobber each other. */
export function SystemConfigCard({ groups: titles }: { groups: string[] }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: qk.systemConfig(), queryFn: api.getSystemConfig });
  const [f, setF] = useState<Record<string, string | number | boolean> | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => { if (q.data && f === null) setF({ ...q.data.values }); }, [q.data, f]);

  const groups = buildGroups(t).filter((g) => titles.includes(g.title));
  const myKeys = groups.flatMap((g) => g.fields.map((fld) => fld.key));

  const save = useMutation({
    mutationFn: () => api.putSystemConfig(Object.fromEntries(myKeys.map((k) => [k, f![k]]))),
    onSuccess: (d: SystemConfig) => {
      qc.setQueryData(qk.systemConfig(), d);
      setF({ ...d.values });
      setSaved(true);
      setTimeout(() => setSaved(false), 2500);
    },
  });

  if (!q.data || !f) return <Card className="mb-4 p-4"><p className="text-sm text-muted">{t("common.loading")}</p></Card>;
  const overridden = new Set(q.data.overridden);

  const renderField = (fld: Field) => {
    const v = f[fld.key];
    const labelEl = (
      <span className="flex items-center gap-1.5 text-xs text-muted">
        {fld.label}
        <InfoHint text={fld.help} />
        {overridden.has(fld.key) && <Badge tone="violet">{t("sysconfig.custom")}</Badge>}
      </span>
    );
    if (fld.type === "bool") {
      return (
        <div key={fld.key} className="flex items-center justify-between gap-2 py-1">
          {labelEl}
          <Toggle checked={!!v} onChange={(b) => setF({ ...f, [fld.key]: b })} label="" />
        </div>
      );
    }
    return (
      <label key={fld.key} className="block">
        {labelEl}
        {fld.type === "select" ? (
          <select className={`${inputCls} mt-1`} value={String(v)} onChange={(e) => setF({ ...f, [fld.key]: e.target.value })}>
            {fld.options!.map((o) => <option key={o} value={o}>{o}</option>)}
          </select>
        ) : (
          <input className={`${inputCls} mt-1`} type={fld.type === "number" ? "number" : "text"}
            value={v as string | number} placeholder={fld.placeholder} spellCheck={false}
            onChange={(e) => setF({ ...f, [fld.key]: fld.type === "number" ? Number(e.target.value) : e.target.value })} />
        )}
      </label>
    );
  };

  return (
    <>
      {groups.map((g) => (
        <Card key={g.title} className="mb-4 p-4">
          <h3 className="mb-2 flex items-center gap-1.5 font-semibold">{g.titleKey}<InfoHint text={g.help} /></h3>
          <div className="grid gap-3 sm:grid-cols-2">{g.fields.map(renderField)}</div>
        </Card>
      ))}
      <div className="mb-4 flex items-center gap-2">
        <Button variant="primary" disabled={save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? t("common.saving") : t("common.save")}
        </Button>
        {saved && <Badge tone="green">{t("sysconfig.saved")}</Badge>}
        {save.isError && <span className="text-sm text-red-500">{(save.error as Error).message}</span>}
      </div>
    </>
  );
}
