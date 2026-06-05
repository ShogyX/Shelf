import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Badge, Button, Card } from "../components/ui";
import IntegrationsCard from "../components/IntegrationsCard";
import QueuedHooksCard from "../components/QueuedHooksCard";
import { api } from "../api/client";
import ThemePicker from "../components/ThemePicker";
import { useIsAdmin } from "../auth";

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block text-xs text-muted">
      {label}
      <div className="mt-1">{children}</div>
    </label>
  );
}

const inputCls = "w-full rounded-lg border border-border bg-bg px-3 py-2 text-sm text-text";

const MODERATE = { tick_seconds: 10, chapters_per_tick: 3, parallel_fetches: 4 };

function CrawlSpeedSection() {
  const qc = useQueryClient();
  const tuning = useQuery({ queryKey: ["crawl-tuning"], queryFn: api.getCrawlTuning });
  const [form, setForm] = useState<typeof MODERATE | null>(null);
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    if (tuning.data && form === null) setForm({ ...tuning.data });
  }, [tuning.data, form]);

  const save = useMutation({
    mutationFn: (body: typeof MODERATE) => api.putCrawlTuning(body),
    onSuccess: (t) => {
      setForm({ ...t });
      setSaved(true);
      qc.invalidateQueries({ queryKey: ["crawl-tuning"] });
      setTimeout(() => setSaved(false), 2500);
    },
  });

  const num = (k: keyof typeof MODERATE) => (
    <input
      type="number"
      min={1}
      value={form?.[k] ?? ""}
      onChange={(e) => setForm((f) => (f ? { ...f, [k]: Math.max(1, Number(e.target.value) || 1) } : f))}
      className="w-24 rounded-lg border border-border bg-bg px-3 py-1.5 text-sm"
    />
  );

  return (
    <div>
      <h2 className="mb-2 font-semibold">Crawl speed</h2>
      <p className="mb-3 text-sm text-muted">
        How fast the backfill + index crawlers run. Changes apply <b>live</b> to running and future
        jobs — no restart. Each source's own rate limits (set per-source on{" "}
        <span className="text-text">Sources</span>) still apply, so raising these never bypasses
        per-site politeness.
      </p>
      <div className="flex flex-wrap items-end gap-x-5 gap-y-3">
        <Field label="Cycle interval (seconds)">
          <div className="flex items-center gap-2">{num("tick_seconds")}<span className="text-xs text-muted">s</span></div>
        </Field>
        <Field label="Chapters per cycle">{num("chapters_per_tick")}</Field>
        <Field label="Parallel fetches">{num("parallel_fetches")}</Field>
        <div className="flex items-center gap-2">
          <Button size="sm" variant="primary" disabled={save.isPending || !form}
                  onClick={() => form && save.mutate(form)}>
            {save.isPending ? "Saving…" : "Save"}
          </Button>
          {saved && <Badge tone="green">applied</Badge>}
          {form && JSON.stringify(form) !== JSON.stringify(MODERATE) && (
            <button className="text-xs text-muted underline hover:text-text"
                    onClick={() => setForm({ ...MODERATE })}>
              reset to Moderate
            </button>
          )}
        </div>
      </div>
      <p className="mt-2 text-xs text-muted">
        Lower interval + higher chapters/parallel = faster but more load on sources (and a higher
        chance of rate-limiting). Backfill and indexing now have independent budgets, so they no
        longer slow each other down when run together.
      </p>
    </div>
  );
}

function CrawlIdentityCard() {
  const qc = useQueryClient();
  const identity = useQuery({ queryKey: ["operator-identity"], queryFn: api.getOperatorIdentity });
  const [form, setForm] = useState<{ user_agent: string; contact_email: string } | null>(null);
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    if (identity.data && form === null) setForm({ ...identity.data });
  }, [identity.data, form]);

  const save = useMutation({
    mutationFn: (body: { user_agent: string; contact_email: string }) =>
      api.putOperatorIdentity(body),
    onSuccess: (d) => {
      setForm({ ...d });
      setSaved(true);
      qc.invalidateQueries({ queryKey: ["operator-identity"] });
      setTimeout(() => setSaved(false), 2500);
    },
  });

  return (
    <Card className="mb-4 p-4">
      <h2 className="mb-2 font-semibold">Crawl identity</h2>
      <p className="mb-3 text-sm text-muted">
        How the polite fetcher identifies itself to every source it touches: a{" "}
        <span className="text-text">User-Agent</span> (carries the project name + a contact link,
        and is matched against each site's <span className="text-text">robots.txt</span>) and a{" "}
        <span className="text-text">contact email</span> (sent as the <code>From</code> header so a
        site admin can reach you). Changes apply <b>live</b> to running and future crawls — no
        restart. Leave a field blank to reset it to the built-in default.
      </p>
      <div className="space-y-3">
        <Field label="User-Agent">
          <input
            type="text"
            value={form?.user_agent ?? ""}
            placeholder="ShelfReader/0.1 (+https://example.org/shelf; polite-self-host-ingester)"
            onChange={(e) => setForm((f) => (f ? { ...f, user_agent: e.target.value } : f))}
            className={inputCls}
          />
        </Field>
        <Field label="Contact email (From header)">
          <input
            type="email"
            value={form?.contact_email ?? ""}
            placeholder="you@example.org"
            onChange={(e) => setForm((f) => (f ? { ...f, contact_email: e.target.value } : f))}
            className={inputCls}
          />
        </Field>
        <div className="flex items-center gap-2">
          <Button size="sm" variant="primary" disabled={save.isPending || !form}
                  onClick={() => form && save.mutate(form)}>
            {save.isPending ? "Saving…" : "Save"}
          </Button>
          {saved && <Badge tone="green">applied</Badge>}
        </div>
      </div>
      <p className="mt-2 text-xs text-muted">
        Keep this honest and reachable — it's how sites identify your self-hosted crawler and how a
        site owner contacts you. (Env defaults: <code>SHELF_USER_AGENT</code> /{" "}
        <code>SHELF_CONTACT_EMAIL</code>.)
      </p>
    </Card>
  );
}

function BlocklistCard() {
  const qc = useQueryClient();
  const blocks = useQuery({ queryKey: ["index-blocks"], queryFn: api.listBlocks });
  const del = useMutation({
    mutationFn: (id: number) => api.deleteBlock(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["index-blocks"] }),
  });
  const items = blocks.data ?? [];
  if (items.length === 0) return null; // nothing blocked → keep settings tidy

  return (
    <Card className="mb-4 p-4">
      <h2 className="mb-1 font-semibold">Blocked content</h2>
      <p className="mb-3 text-sm text-muted">
        URLs and domains you've removed from the index. They won't be re-discovered by crawls or
        hooked. Unblock to allow them again. {items.length} blocked.
      </p>
      <div className="space-y-1.5">
        {items.map((b) => (
          <div key={b.id} className="flex items-center justify-between gap-2 rounded-lg border border-border p-2.5">
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <Badge tone={b.scope === "domain" ? "amber" : "default"}>{b.scope}</Badge>
                <span className="truncate text-sm" title={b.value}>{b.title || b.value}</span>
              </div>
              <div className="truncate text-xs text-muted">{b.value}{b.reason ? ` · ${b.reason}` : ""}</div>
            </div>
            <Button size="sm" variant="ghost" disabled={del.isPending} onClick={() => del.mutate(b.id)}>
              Unblock
            </Button>
          </div>
        ))}
      </div>
    </Card>
  );
}

function IndexingCard() {
  const qc = useQueryClient();
  const cfg = useQuery({ queryKey: ["index-config"], queryFn: api.getIndexConfig });
  const [idle, setIdle] = useState<number | null>(null);
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    if (cfg.data && idle === null) setIdle(cfg.data.stop_after_idle_pages);
  }, [cfg.data, idle]);

  const save = useMutation({
    mutationFn: () => api.putIndexConfig(Math.max(1, idle ?? 200)),
    onSuccess: () => {
      setSaved(true);
      qc.invalidateQueries({ queryKey: ["index-config"] });
      setTimeout(() => setSaved(false), 2500);
    },
  });

  return (
    <Card className="mb-4 p-4">
      <CrawlSpeedSection />
      <div className="my-4 border-t border-border" />
      <h2 className="mb-2 font-semibold">Indexing</h2>
      <p className="mb-3 text-sm text-muted">
        New index crawls run with <span className="text-text">no page cap</span> — they keep
        indexing until the whole site is covered. After a long stretch with nothing new (no title
        and no new link) they stop looking for more pages but still finish whatever's queued, so
        no found content is left behind. This is the default threshold for new crawls; you can
        also override it per-crawl on the <span className="text-text">Jobs</span> page.
      </p>
      <Field label="Stop discovering after this many pages with nothing new (the crawl still finishes its queue)">
        <div className="flex items-center gap-2">
          <input
            type="number"
            min={1}
            value={idle ?? ""}
            onChange={(e) => setIdle(Math.max(1, Number(e.target.value) || 1))}
            className="w-28 rounded-lg border border-border bg-bg px-3 py-1.5 text-sm"
          />
          <span className="text-xs text-muted">idle pages</span>
          <Button
            size="sm"
            variant="primary"
            disabled={save.isPending || idle == null}
            onClick={() => save.mutate()}
          >
            {save.isPending ? "Saving…" : "Save"}
          </Button>
          {saved && <Badge tone="green">saved</Badge>}
        </div>
      </Field>
      <p className="mt-2 text-xs text-muted">
        Crawls also obey robots.txt and each source's rate limits (set per-source on{" "}
        <span className="text-text">Sources</span>); live progress is on{" "}
        <span className="text-text">Jobs</span>.
      </p>
    </Card>
  );
}

function KindleCard() {
  const qc = useQueryClient();
  const settings = useQuery({ queryKey: ["settings"], queryFn: api.getSettings });
  const [form, setForm] = useState({
    kindle_email: "", smtp_host: "", smtp_port: "587", smtp_username: "",
    smtp_password: "", smtp_from: "", smtp_security: "starttls", email_to: "",
  });
  const [pwSet, setPwSet] = useState(false);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    const d = settings.data;
    if (!d) return;
    setForm({
      kindle_email: d.kindle_email ?? "",
      smtp_host: d.delivery?.smtp_host ?? "",
      smtp_port: String(d.delivery?.smtp_port ?? 587),
      smtp_username: d.delivery?.smtp_username ?? "",
      smtp_password: "",
      smtp_from: d.delivery?.smtp_from ?? "",
      smtp_security: d.delivery?.smtp_security ?? "starttls",
      email_to: d.delivery?.email_to ?? "",
    });
    setPwSet(!!d.delivery?.smtp_password_set);
  }, [settings.data]);

  const set = (k: string, v: string) => setForm((f) => ({ ...f, [k]: v }));

  async function save() {
    const delivery: Record<string, unknown> = {
      smtp_host: form.smtp_host, smtp_port: parseInt(form.smtp_port) || 587,
      smtp_username: form.smtp_username, smtp_from: form.smtp_from,
      smtp_security: form.smtp_security, email_to: form.email_to,
    };
    if (form.smtp_password) delivery.smtp_password = form.smtp_password; // only if entered
    await api.saveSettings({ kindle_email: form.kindle_email.trim(), delivery });
    await qc.invalidateQueries({ queryKey: ["settings"] });
    setSaved(true);
    setTimeout(() => setSaved(false), 1500);
  }

  return (
    <Card className="mb-4 p-4">
      <div className="mb-2 flex items-center gap-2">
        <h2 className="font-semibold">Send to Kindle / email</h2>
        <Badge tone={settings.data?.smtp_configured ? "green" : "amber"}>
          {settings.data?.smtp_configured ? "email ready" : "email off"}
        </Badge>
      </div>
      <p className="mb-3 text-sm text-muted">
        Provide your email provider’s SMTP login to email EPUBs to your Kindle or your own
        inbox. For Kindle, add the “From” address to your Amazon “Approved Personal Document
        E-mail List”. Use the <b>📤 Send</b> button on any work.
      </p>

      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Kindle email">
          <input className={inputCls} type="email" placeholder="device@kindle.com"
            value={form.kindle_email} onChange={(e) => set("kindle_email", e.target.value)} />
        </Field>
        <Field label="Personal email">
          <input className={inputCls} type="email" placeholder="you@example.com"
            value={form.email_to} onChange={(e) => set("email_to", e.target.value)} />
        </Field>
      </div>

      <div className="mt-3 border-t border-border pt-3">
        <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-muted">
          SMTP login
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="SMTP host">
            <input className={inputCls} placeholder="smtp.gmail.com"
              value={form.smtp_host} onChange={(e) => set("smtp_host", e.target.value)} />
          </Field>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Port">
              <input className={inputCls} inputMode="numeric"
                value={form.smtp_port} onChange={(e) => set("smtp_port", e.target.value)} />
            </Field>
            <Field label="Security">
              <select className={inputCls} value={form.smtp_security}
                onChange={(e) => set("smtp_security", e.target.value)}>
                <option value="starttls">STARTTLS</option>
                <option value="ssl">SSL</option>
                <option value="none">None</option>
              </select>
            </Field>
          </div>
          <Field label="Username">
            <input className={inputCls} autoComplete="off"
              value={form.smtp_username} onChange={(e) => set("smtp_username", e.target.value)} />
          </Field>
          <Field label={pwSet ? "Password (saved — leave blank to keep)" : "Password"}>
            <input className={inputCls} type="password" autoComplete="new-password"
              placeholder={pwSet ? "••••••••" : ""}
              value={form.smtp_password} onChange={(e) => set("smtp_password", e.target.value)} />
          </Field>
          <Field label="From address (sender)">
            <input className={inputCls} type="email" placeholder="you@example.com"
              value={form.smtp_from} onChange={(e) => set("smtp_from", e.target.value)} />
          </Field>
        </div>
      </div>

      <div className="mt-3 flex justify-end">
        <Button variant="primary" onClick={save}>{saved ? "Saved ✓" : "Save"}</Button>
      </div>
    </Card>
  );
}

function NotificationsCard() {
  const qc = useQueryClient();
  const settings = useQuery({ queryKey: ["settings"], queryFn: api.getSettings });
  const [url, setUrl] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    if (settings.data && url === null) setUrl(settings.data.apprise_url ?? "");
  }, [settings.data, url]);

  async function save() {
    await api.saveSettings({ apprise_url: (url ?? "").trim() });
    await qc.invalidateQueries({ queryKey: ["settings"] });
    setSaved(true);
    setTimeout(() => setSaved(false), 1500);
  }

  return (
    <Card className="mb-4 p-4">
      <h2 className="mb-2 font-semibold">Push notifications</h2>
      <p className="mb-3 text-sm text-muted">
        Get a push when a title is auto-added to one of your shelves with{" "}
        <b>notify on add</b> enabled. Paste an{" "}
        <a className="underline hover:text-text" href="https://github.com/caronc/apprise#supported-notifications"
          target="_blank" rel="noreferrer">Apprise URL</a>{" "}
        for your service (e.g. <code>ntfy://…</code>, <code>tgram://…</code>, <code>pover://…</code>).
        Leave blank to disable.
      </p>
      <div className="flex items-end gap-2">
        <Field label="Apprise URL">
          <input className={inputCls} placeholder="ntfy://ntfy.sh/your-topic"
            value={url ?? ""} onChange={(e) => setUrl(e.target.value)} />
        </Field>
        <Button variant="primary" disabled={url === null} onClick={save}>
          {saved ? "Saved ✓" : "Save"}
        </Button>
      </div>
    </Card>
  );
}

function GoodreadsCard() {
  const qc = useQueryClient();
  const conn = useQuery({ queryKey: ["my-goodreads"], queryFn: api.getMyGoodreads });
  const [gid, setGid] = useState<string | null>(null);
  const [shelf, setShelf] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  useEffect(() => {
    const d = conn.data;
    if (d && gid === null) {
      setGid(d.goodreads_user_id ?? "");
      setShelf(d.shelf ?? "to-read");
    }
  }, [conn.data, gid]);

  const refresh = () => qc.invalidateQueries({ queryKey: ["my-goodreads"] });

  async function run(label: string, fn: () => Promise<unknown>) {
    setBusy(label);
    setMsg(null);
    try {
      await fn();
      await refresh();
    } catch (e) {
      setMsg((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  const c = conn.data;
  return (
    <Card className="mb-4 p-4">
      <div className="mb-2 flex items-center gap-2">
        <h2 className="font-semibold">Goodreads want-to-read</h2>
        <Badge tone={c?.connected ? "green" : "default"}>
          {c?.connected ? "connected" : "not connected"}
        </Badge>
      </div>
      <p className="mb-3 text-sm text-muted">
        Connect <b>your own</b> public Goodreads shelf. Titles on it are auto-added to your library
        as they appear in the index. Choose where they land by marking a bookshelf as the{" "}
        <span className="text-text">Goodreads destination</span> on the{" "}
        <span className="text-text">Library</span> page; otherwise they go straight to your library.
      </p>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Goodreads user ID or profile URL">
          <input className={inputCls} placeholder="12345 or goodreads.com/user/show/12345-name"
            value={gid ?? ""} onChange={(e) => setGid(e.target.value)} />
        </Field>
        <Field label="Shelf">
          <input className={inputCls} placeholder="to-read"
            value={shelf ?? ""} onChange={(e) => setShelf(e.target.value)} />
        </Field>
      </div>
      {c?.last_error && <p className="mt-2 text-xs text-red-500">Last sync error: {c.last_error}</p>}
      {msg && <p className="mt-2 text-xs text-red-500">{msg}</p>}
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Button variant="primary" disabled={!!busy || !(gid ?? "").trim()}
          onClick={() => run("save", () => api.connectGoodreads({
            goodreads_user_id: (gid ?? "").trim(), shelf: (shelf ?? "").trim() || "to-read" }))}>
          {busy === "save" ? "Saving…" : c?.connected ? "Update & sync" : "Connect & sync"}
        </Button>
        {c?.connected && (
          <>
            <Button disabled={!!busy} onClick={() => run("sync", api.syncGoodreads)}>
              {busy === "sync" ? "Syncing…" : "Sync now"}
            </Button>
            <Button variant="ghost" disabled={!!busy}
              onClick={() => run("disconnect", api.disconnectGoodreads)}>
              Disconnect
            </Button>
          </>
        )}
      </div>
    </Card>
  );
}

function MetadataStatsCard() {
  const stats = useQuery({ queryKey: ["metadata-stats"], queryFn: api.getMetadataStats });
  const data = stats.data;
  if (!data) return null;
  const label: Record<string, string> = { ranobedb: "RanobeDB", googlebooks: "Google Books" };
  return (
    <Card className="mb-4 p-4">
      <h2 className="mb-1 font-semibold">Metadata coverage</h2>
      <p className="mb-3 text-sm text-muted">
        How much of the {data.total_library_works} hooked title{data.total_library_works === 1 ? "" : "s"} each
        provider recognises. Unmatched titles keep their source metadata.
      </p>
      <div className="space-y-3">
        {data.providers.map((p) => {
          const pct = Math.round(p.match_ratio * 100);
          return (
            <div key={p.provider}>
              <div className="mb-1 flex items-center justify-between text-sm">
                <span className="font-medium">{label[p.provider] ?? p.provider}</span>
                <span className="text-muted">
                  {p.matched}/{p.total} matched ({pct}%) · {p.unmatched} unrecognised
                </span>
              </div>
              <div className="h-2 w-full overflow-hidden rounded-full bg-surface-2">
                <div className="h-full rounded-full bg-accent" style={{ width: `${pct}%` }} />
              </div>
              <div className="mt-1 text-[11px] text-muted">
                confidence — high {p.high_confidence} · medium {p.medium_confidence} · low {p.low_confidence}
              </div>
            </div>
          );
        })}
        {data.providers.every((p) => p.matched === 0) && (
          <p className="text-xs text-muted">
            No matches yet. Connect RanobeDB / Google Books under Integrations and run a sync.
          </p>
        )}
      </div>
    </Card>
  );
}

export default function Settings() {
  const isAdmin = useIsAdmin();
  async function exportData() {
    const works = await api.listWorks();
    const out: any = { exported_at: new Date().toISOString(), works: [] };
    for (const w of works) {
      const progress = await api.getProgress(w.id).catch(() => null);
      out.works.push({ ...w, progress });
    }
    const blob = new Blob([JSON.stringify(out, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "shelf-export.json";
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <main className="mx-auto max-w-6xl px-4 py-8">
      <h1 className="mb-6 text-2xl font-semibold">Settings</h1>

      {/* Two columns on wide screens: personal settings on the left, operator surfaces on the
          right. Each column stacks its own cards (clean masonry without row-height gaps). */}
      <div className="grid items-start gap-x-6 lg:grid-cols-2">
        <div>
          <Card className="mb-4 p-4">
            <h2 className="mb-3 font-semibold">Color mode</h2>
            <ThemePicker columns={3} />
            <p className="mt-3 text-xs text-muted">
              Every mode is gently toned for comfortable reading. Typography (font, size, spacing,
              width) is adjusted live inside the reader via the “Aa” button.
            </p>
          </Card>

          <KindleCard />
          <NotificationsCard />
          <GoodreadsCard />

          <Card className="mb-4 p-4">
            <h2 className="mb-2 font-semibold">Backup & export</h2>
            <p className="mb-3 text-sm text-muted">
              Download your library and reading progress as JSON.
            </p>
            <Button onClick={exportData}>Export library JSON</Button>
          </Card>
        </div>

        {/* Operator-wide surfaces — admins only (per-user library: regular users don't manage
            shared integrations, the index crawler, or the global blocklist). */}
        {isAdmin && (
          <div>
            <MetadataStatsCard />
            <IntegrationsCard />
            <QueuedHooksCard />
            <IndexingCard />
            <CrawlIdentityCard />
            <BlocklistCard />
          </div>
        )}
      </div>

      <p className="mt-8 text-center text-xs text-muted">
        Shelf ingests only sources you are permitted to read. See the README for the full sourcing policy.
      </p>
    </main>
  );
}
