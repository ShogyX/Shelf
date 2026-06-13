import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, SystemConfig } from "../api/client";
import { Badge, Button, Card, InfoHint, Toggle } from "./ui";

type FieldType = "text" | "number" | "bool" | "select";
interface Field {
  key: string;
  label: string;
  type: FieldType;
  help: string;
  options?: string[];
  placeholder?: string;
}
interface Group { title: string; help: string; fields: Field[] }

// Declarative layout — keys match the backend's config_store.EDITABLE.
const GROUPS: Group[] = [
  {
    title: "Cloudflare solver",
    help: "A FlareSolverr-compatible proxy used to pass Cloudflare challenges. Honored immediately.",
    fields: [
      { key: "flaresolverr_url", label: "Solver URL", type: "text", placeholder: "http://10.10.102.23:8191",
        help: "FlareSolverr-compatible endpoint. Blank disables it (falls back to the in-app browser). e.g. http://host:8191" },
      { key: "flaresolverr_timeout_s", label: "Solve timeout (s)", type: "number",
        help: "Max seconds to let the solver work a single challenge before giving up." },
      { key: "flaresolverr_clearance_ttl_s", label: "Clearance reuse (s)", type: "number",
        help: "How long a solved cf_clearance is replayed before re-solving (cf_clearance lasts ~30 min)." },
    ],
  },
  {
    title: "Comix crawler",
    help: "comix.to is read with an evasion-hardened browser (zendriver) that passes its Turnstile challenge.",
    fields: [
      { key: "comix_browser_enabled", label: "Enable comix browser crawl", type: "bool",
        help: "Crawl comix.to via the headful browser. Needs zendriver + an X server (Xvfb) + Chromium. Off = skip comix." },
      { key: "comix_browser_pages_per_tick", label: "Browse pages per tick", type: "number",
        help: "How many /browse pages (28 titles each) to crawl per tick. Higher = faster catalog fill, more load." },
      { key: "solver_chrome_path", label: "Chromium path", type: "text", placeholder: "(auto-detect)",
        help: "Path to the Chromium binary for the solver/crawler. Blank = auto-detect the bundled Playwright build." },
    ],
  },
  {
    title: "Image cache",
    help: "On-disk LRU cache for covers + remote chapter images.",
    fields: [
      { key: "imgcache_max_mb", label: "Cache size cap (MB)", type: "number",
        help: "A periodic sweep LRU-evicts the image cache back under this. Cached images re-fetch on miss. 0 = no cap." },
    ],
  },
  {
    title: "Automatic backups",
    help: "Scheduled instance backups so an unattended install isn't left with zero backups.",
    fields: [
      { key: "auto_backup_enabled", label: "Enable scheduled backups", type: "bool",
        help: "Periodically create a backup automatically." },
      { key: "auto_backup_level", label: "Backup level", type: "select", options: ["settings", "data", "full"],
        help: "settings = config only · data = the whole DB (no media) · full = DB + media (can be tens of GB)." },
      { key: "auto_backup_interval_hours", label: "Interval (hours)", type: "number",
        help: "Hours between automatic backups." },
      { key: "auto_backup_keep", label: "Keep newest", type: "number",
        help: "How many app-created backups to retain; older ones are pruned (uploads are never pruned)." },
    ],
  },
  {
    title: "Crawl defaults",
    help: "Defaults for NEW index crawls (per-site overrides live on the Jobs page).",
    fields: [
      { key: "index_max_pages", label: "Max pages per crawl", type: "number",
        help: "Hard page cap for a crawl. 0 = unlimited (stops on the idle threshold instead)." },
      { key: "index_max_depth", label: "Max crawl depth", type: "number",
        help: "Loop-guard depth bound (URLs are de-duped). Keep loose so deep pagination is still reached." },
      { key: "index_stop_after_idle_pages", label: "Stop after idle pages", type: "number",
        help: "After this many consecutive pages with nothing new, stop discovering more (queued pages still drain)." },
      { key: "index_max_pending_frontier", label: "Max pending frontier", type: "number",
        help: "Safety ceiling on how far the pending queue may run ahead of fetching. Set well above any real catalog." },
    ],
  },
  {
    title: "Login & security",
    help: "Brute-force protection + password policy.",
    fields: [
      { key: "login_max_attempts", label: "Max login attempts", type: "number",
        help: "Failed logins (per username + per IP) before a temporary lockout." },
      { key: "login_window_seconds", label: "Lockout window (s)", type: "number",
        help: "Sliding window / lockout duration for failed logins." },
      { key: "min_password_length", label: "Min password length", type: "number",
        help: "Minimum characters required when setting a password." },
    ],
  },
  {
    title: "Logging",
    help: "Root log verbosity — applied live.",
    fields: [
      { key: "log_level", label: "Log level", type: "select", options: ["DEBUG", "INFO", "WARNING", "ERROR"],
        help: "DEBUG shows per-tick crawl detail; INFO is the readable default." },
    ],
  },
];

const input = "w-full rounded-lg border border-border bg-bg px-3 py-2 text-sm text-text";

export default function SystemSettings() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["system-config"], queryFn: api.getSystemConfig });
  const [f, setF] = useState<Record<string, string | number | boolean> | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => { if (q.data && f === null) setF({ ...q.data.values }); }, [q.data, f]);

  const save = useMutation({
    mutationFn: () => api.putSystemConfig(f!),
    onSuccess: (d: SystemConfig) => {
      qc.setQueryData(["system-config"], d);
      setF({ ...d.values });
      setSaved(true);
      setTimeout(() => setSaved(false), 2500);
    },
  });

  if (!q.data || !f) return <Card className="mb-4 p-4"><p className="text-sm text-muted">Loading…</p></Card>;
  const overridden = new Set(q.data.overridden);

  const renderField = (fld: Field) => {
    const v = f[fld.key];
    const labelEl = (
      <span className="flex items-center gap-1.5 text-xs text-muted">
        {fld.label}
        <InfoHint text={fld.help} />
        {overridden.has(fld.key) && <Badge tone="violet">custom</Badge>}
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
          <select className={`${input} mt-1`} value={String(v)} onChange={(e) => setF({ ...f, [fld.key]: e.target.value })}>
            {fld.options!.map((o) => <option key={o} value={o}>{o}</option>)}
          </select>
        ) : (
          <input className={`${input} mt-1`} type={fld.type === "number" ? "number" : "text"}
            value={v as string | number} placeholder={fld.placeholder} spellCheck={false}
            onChange={(e) => setF({ ...f, [fld.key]: fld.type === "number" ? Number(e.target.value) : e.target.value })} />
        )}
      </label>
    );
  };

  return (
    <>
      <Card className="mb-4 p-4">
        <h2 className="flex items-center gap-1.5 font-semibold">
          System configuration
          <InfoHint text={<>Behavioral settings moved out of environment variables — editable here and
            honored without a restart, defaulting to the env/built-in value until you change them.
            Boot + security-critical vars (host, port, database, CSP, trusted proxy, cookies, setup
            token) stay in the environment by design.</>} />
        </h2>
      </Card>
      {GROUPS.map((g) => (
        <Card key={g.title} className="mb-4 p-4">
          <h3 className="mb-2 flex items-center gap-1.5 font-semibold">{g.title}<InfoHint text={g.help} /></h3>
          <div className="grid gap-3 sm:grid-cols-2">{g.fields.map(renderField)}</div>
        </Card>
      ))}
      <div className="flex items-center gap-2">
        <Button variant="primary" disabled={save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? "Saving…" : "Save system settings"}
        </Button>
        {saved && <Badge tone="green">saved</Badge>}
        {save.isError && <span className="text-sm text-red-500">{(save.error as Error).message}</span>}
      </div>
    </>
  );
}
