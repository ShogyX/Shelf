import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Badge, Button, Card, InfoHint, Modal, Spinner, Tabs, Toggle } from "../components/ui";
import { MetadataProvidersCard, AcquisitionCard } from "../components/IntegrationsManager";
import QueuedHooksCard from "../components/QueuedHooksCard";
import RequestStatsCard from "../components/RequestStatsCard";
import StorageSettings from "../components/StorageSettings";
import { api, BackupEntry, RestoreMode, RestorePlan } from "../api/client";
import { useApp } from "../store";
import ThemePicker from "../components/ThemePicker";
import { CategoryToggles } from "../components/catalog/CatalogRows";
import { useHasPermission, useIsAdmin, useAuth } from "../auth";
import { MEDIA_CATEGORIES } from "../api/client";

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block text-xs text-muted">
      {label}
      <div className="mt-1">{children}</div>
    </label>
  );
}

const inputCls = "w-full rounded-lg border border-border bg-bg px-3 py-2 text-sm text-text";

const MODERATE = { tick_seconds: 10, chapters_per_tick: 3, parallel_fetches: 4, refresh_hours: 6 };

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
      <h2 className="mb-3 flex items-center gap-1.5 font-semibold">
        Crawl speed
        <InfoHint text={<>How fast the backfill + index crawlers run. Changes apply live to running
          and future jobs — no restart. Each source's own rate limits (set per-source on Sources)
          still apply, so raising these never bypasses per-site politeness. Lower interval + higher
          chapters/parallel = faster but more load on sources (and a higher chance of rate-limiting).
          Backfill and indexing have independent budgets, so they no longer slow each other down when
          run together.</>} />
      </h2>
      <div className="flex flex-wrap items-end gap-x-5 gap-y-3">
        <Field label="Cycle interval (seconds)">
          <div className="flex items-center gap-2">{num("tick_seconds")}<span className="text-xs text-muted">s</span></div>
        </Field>
        <Field label="Chapters per cycle">{num("chapters_per_tick")}</Field>
        <Field label="Parallel fetches">{num("parallel_fetches")}</Field>
        <Field label="Check for new chapters every">
          <div className="flex items-center gap-2">{num("refresh_hours")}<span className="text-xs text-muted">hours</span></div>
        </Field>
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
      <h2 className="mb-3 flex items-center gap-1.5 font-semibold">
        Crawl identity
        <InfoHint text={<>How the polite fetcher identifies itself to every source: a User-Agent
          (project name + contact link, matched against each site's robots.txt) and a contact email
          (sent as the From header so a site admin can reach you). Changes apply live to running and
          future crawls — no restart. Leave a field blank to reset it to the built-in default.</>} />
      </h2>
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
      <h2 className="mb-3 flex items-center gap-1.5 font-semibold">
        Blocked content <span className="text-sm font-normal text-muted">· {items.length} blocked</span>
        <InfoHint text={<>URLs and domains you've removed from the index. They won't be re-discovered
          by crawls or hooked. Unblock to allow them again.</>} />
      </h2>
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
      <h2 className="mb-3 flex items-center gap-1.5 font-semibold">
        Indexing
        <InfoHint text={<>New index crawls run with no page cap — they keep indexing until the whole
          site is covered. After a long stretch with nothing new (no title, no new link) they stop
          looking for more pages but still finish whatever's queued, so nothing found is left behind.
          This is the default threshold for new crawls; override it per-crawl on the Jobs page.</>} />
      </h2>
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

/** Admin: the shared SMTP server every user sends Kindle/email through. */
function GlobalSmtpCard() {
  const qc = useQueryClient();
  const smtp = useQuery({ queryKey: ["global-smtp"], queryFn: api.getGlobalSmtp });
  const [form, setForm] = useState({
    smtp_host: "", smtp_port: "587", smtp_username: "", smtp_from: "",
    smtp_security: "starttls", smtp_password: "",
  });
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    const d = smtp.data;
    if (!d) return;
    setForm({
      smtp_host: d.smtp_host ?? "", smtp_port: String(d.smtp_port ?? 587),
      smtp_username: d.smtp_username ?? "", smtp_from: d.smtp_from ?? "",
      smtp_security: d.smtp_security ?? "starttls", smtp_password: "",
    });
  }, [smtp.data]);
  const set = (k: keyof typeof form, v: string) => setForm((f) => ({ ...f, [k]: v }));
  const save = useMutation({
    mutationFn: () => api.setGlobalSmtp({
      smtp_host: form.smtp_host.trim(), smtp_port: parseInt(form.smtp_port) || 587,
      smtp_username: form.smtp_username.trim(), smtp_from: form.smtp_from.trim(),
      smtp_security: form.smtp_security,
      ...(form.smtp_password ? { smtp_password: form.smtp_password } : {}),
    }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["global-smtp"] });
      qc.invalidateQueries({ queryKey: ["settings"] });
      setSaved(true); setTimeout(() => setSaved(false), 1500); },
  });
  const pwSet = !!smtp.data?.smtp_password_set;
  return (
    <Card className="mb-4 p-4">
      <div className="mb-3 flex items-center gap-2">
        <h2 className="flex items-center gap-1.5 font-semibold">
          Email server (SMTP)
          <InfoHint text={<>The shared mail server every user sends through (Send-to-Kindle, shelf
            auto-Kindle, notifications). Users only set their own destination address — they never
            see these credentials. Add the From address to each Kindle's Approved Personal Document
            list.</>} />
        </h2>
        <Badge tone={smtp.data?.configured ? "green" : "amber"}>
          {smtp.data?.configured ? "configured" : "not configured"}
        </Badge>
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
          <input className={inputCls} type="email" placeholder="shelf@example.com"
            value={form.smtp_from} onChange={(e) => set("smtp_from", e.target.value)} />
        </Field>
      </div>
      <div className="mt-3 flex justify-end">
        <Button variant="primary" disabled={save.isPending} onClick={() => save.mutate()}>
          {saved ? "Saved ✓" : save.isPending ? "Saving…" : "Save"}
        </Button>
      </div>
    </Card>
  );
}

function KindleCard() {
  const canSend = useHasPermission("send.kindle");
  const qc = useQueryClient();
  const settings = useQuery({ queryKey: ["settings"], queryFn: api.getSettings });
  const [form, setForm] = useState({ kindle_email: "", email_to: "" });
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    const d = settings.data;
    if (!d) return;
    setForm({ kindle_email: d.kindle_email ?? "", email_to: d.delivery?.email_to ?? "" });
  }, [settings.data]);

  const set = (k: keyof typeof form, v: string) => setForm((f) => ({ ...f, [k]: v }));

  async function save() {
    await api.saveSettings({
      kindle_email: form.kindle_email.trim(),
      delivery: { email_to: form.email_to.trim() },
    });
    await qc.invalidateQueries({ queryKey: ["settings"] });
    setSaved(true);
    setTimeout(() => setSaved(false), 1500);
  }

  if (!canSend) return null;  // user not permitted to send-to-Kindle / set a delivery target
  const ready = settings.data?.smtp_configured;
  return (
    <Card className="mb-4 p-4">
      <div className="mb-2 flex items-center gap-2">
        <h2 className="flex items-center gap-1.5 font-semibold">
          Send to Kindle / email
          <InfoHint text={<>Set where your EPUBs go — your Kindle and/or your own inbox — then use
            the 📤 Send button on any work. Mail is sent from the shared address configured by an
            administrator; for Kindle, add that address to your Amazon "Approved Personal Document
            E-mail List". If the mail server hasn't been configured yet, sending is off.</>} />
        </h2>
        <Badge tone={ready ? "green" : "amber"}>{ready ? "email ready" : "email not set up"}</Badge>
      </div>
      {ready && settings.data?.smtp_from && (
        <p className="mb-3 text-xs text-muted">Sends from {settings.data.smtp_from}</p>
      )}

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
      <h2 className="mb-3 flex items-center gap-1.5 font-semibold">
        Push notifications
        <InfoHint text={<>Get a push when a title is auto-added to one of your shelves with "notify
          on add" enabled. Paste an <a className="underline hover:text-text"
          href="https://github.com/caronc/apprise#supported-notifications" target="_blank"
          rel="noreferrer">Apprise URL</a> for your service (e.g. ntfy://…, tgram://…, pover://…).
          Leave blank to disable.</>} />
      </h2>
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
      <div className="mb-3 flex items-center gap-2">
        <h2 className="flex items-center gap-1.5 font-semibold">
          Goodreads want-to-read
          <InfoHint text={<>Connect your own public Goodreads shelf. Titles on it are auto-added to
            your library as they appear in the index. Choose where they land by marking a bookshelf
            as the Goodreads destination on the Library page; otherwise they go straight to your
            library.</>} />
        </h2>
        <Badge tone={c?.connected ? "green" : "default"}>
          {c?.connected ? "connected" : "not connected"}
        </Badge>
      </div>
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

function BookCatalogCard() {
  const qc = useQueryClient();
  const status = useQuery({ queryKey: ["book-catalog"], queryFn: api.getBookCatalogConfig });
  const [form, setForm] = useState<{ enabled: boolean; hot_set_cap: string; closeness_threshold: string } | null>(null);
  const [saved, setSaved] = useState(false);
  const [syncing, setSyncing] = useState(false);

  useEffect(() => {
    if (status.data && form === null)
      setForm({
        enabled: status.data.config.enabled,
        hot_set_cap: String(status.data.config.hot_set_cap),
        closeness_threshold: String(status.data.config.closeness_threshold),
      });
  }, [status.data, form]);

  const save = useMutation({
    mutationFn: () =>
      api.putBookCatalogConfig({
        enabled: form!.enabled,
        hot_set_cap: Math.max(0, parseInt(form!.hot_set_cap, 10) || 0),
        closeness_threshold: Math.min(1, Math.max(0, parseFloat(form!.closeness_threshold) || 0)),
      }),
    onSuccess: () => {
      setSaved(true);
      qc.invalidateQueries({ queryKey: ["book-catalog"] });
      setTimeout(() => setSaved(false), 2000);
    },
  });

  async function syncNow() {
    setSyncing(true);
    try {
      await api.syncBookCatalog();
      await qc.invalidateQueries({ queryKey: ["book-catalog"] });
    } finally {
      setSyncing(false);
    }
  }

  const d = status.data;
  return (
    <Card className="mb-4 p-4">
      <h2 className="mb-3 flex items-center gap-1.5 font-semibold">
        Book catalog
        <InfoHint text={<>A hybrid book catalog: a persistent hot set of popular titles (seeded from
          Open Library trending + popular subjects and Google Books) plus live resolve — a search
          with no close local match is looked up against the book APIs on the fly and cached. Add a
          Google Books integration with an API key to lift its keyless quota; Open Library needs no
          key.</>} />
      </h2>
      {d && (
        <div className="mb-3 text-xs text-muted">
          {d.book_rows.toLocaleString()} book rows · seed phase: <b>{d.phase}</b>
          {d.last_full_at ? ` · last full pass ${new Date(d.last_full_at).toLocaleString()}` : ""}
        </div>
      )}
      {form && (
        <div className="space-y-3">
          <Toggle
            checked={form.enabled}
            onChange={(v) => setForm({ ...form, enabled: v })}
            label="Enabled (seeding + live resolve)"
          />
          <div className="flex flex-wrap items-end gap-x-5 gap-y-3">
            <Field label="Hot-set cap (max seeded book rows)">
              <input
                type="number"
                min={0}
                value={form.hot_set_cap}
                onChange={(e) => setForm({ ...form, hot_set_cap: e.target.value })}
                className="w-32 rounded-lg border border-border bg-bg px-3 py-1.5 text-sm"
              />
            </Field>
            <Field label="Closeness threshold (0–1; lower = more API calls)">
              <input
                type="number"
                min={0}
                max={1}
                step={0.05}
                value={form.closeness_threshold}
                onChange={(e) => setForm({ ...form, closeness_threshold: e.target.value })}
                className="w-28 rounded-lg border border-border bg-bg px-3 py-1.5 text-sm"
              />
            </Field>
            <div className="flex items-center gap-2">
              <Button
                size="sm"
                variant="primary"
                disabled={save.isPending || !form.hot_set_cap.trim() || !form.closeness_threshold.trim()}
                onClick={() => save.mutate()}
              >
                {save.isPending ? "Saving…" : "Save"}
              </Button>
              {saved && <Badge tone="green">saved</Badge>}
              <Button size="sm" variant="outline" disabled={syncing} onClick={syncNow}>
                {syncing ? "Seeding…" : "Sync now"}
              </Button>
            </div>
          </div>
        </div>
      )}
    </Card>
  );
}

const ROUTE_LABELS: Record<string, string> = {
  pipeline: "Usenet download (Prowlarr → SABnzbd)",
  libgen: "Open libraries (LibGen / Anna's — fallback)",
  web_index: "Web index (crawl & hook)",
  readarr: "Readarr (book manager)",
  kapowarr: "Kapowarr (comic manager)",
};

function FetchPriorityCard() {
  const qc = useQueryClient();
  const isAdmin = useIsAdmin();
  const q = useQuery({ queryKey: ["fetch-priority"], queryFn: api.getFetchPriority });
  const [order, setOrder] = useState<string[] | null>(null);
  const [saved, setSaved] = useState("");
  useEffect(() => {
    if (q.data && order === null) setOrder(q.data.effective);
  }, [q.data, order]);

  const move = (i: number, d: number) =>
    setOrder((o) => {
      if (!o) return o;
      const j = i + d;
      if (j < 0 || j >= o.length) return o;
      const n = [...o];
      [n[i], n[j]] = [n[j], n[i]];
      return n;
    });

  async function save(global: boolean) {
    if (!order) return;
    if (global) await api.setGlobalFetchPriority(order);
    else await api.setFetchPriority(order);
    await qc.invalidateQueries({ queryKey: ["fetch-priority"] });
    setSaved(global ? "global" : "yours");
    setTimeout(() => setSaved(""), 2000);
  }

  return (
    <Card className="mb-4 p-4">
      <h2 className="mb-3 flex items-center gap-1.5 font-semibold">
        Fetch source priority
        <InfoHint text={<>When you acquire a title (or it's auto-fetched from Goodreads), Shelf tries
          these sources in order and uses the first that can deliver it. Move the most-preferred to
          the top.</>} />
      </h2>
      {order && (
        <div className="space-y-1.5">
          {order.map((r, i) => (
            <div
              key={r}
              className="flex items-center justify-between gap-2 rounded-lg border border-border p-2"
            >
              <span className="text-sm">
                <span className="mr-2 text-xs text-muted">{i + 1}.</span>
                {ROUTE_LABELS[r] ?? r}
              </span>
              <div className="flex gap-1">
                <Button size="sm" variant="ghost" disabled={i === 0} onClick={() => move(i, -1)}>
                  ↑
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={i === order.length - 1}
                  onClick={() => move(i, 1)}
                >
                  ↓
                </Button>
              </div>
            </div>
          ))}
        </div>
      )}
      <div className="mt-3 flex items-center gap-2">
        <Button size="sm" variant="primary" disabled={!order} onClick={() => save(false)}>
          Save my order
        </Button>
        {isAdmin && (
          <Button size="sm" variant="outline" disabled={!order} onClick={() => save(true)}>
            Set as global default
          </Button>
        )}
        {saved && <Badge tone="green">{saved === "global" ? "global saved" : "saved"}</Badge>}
      </div>
    </Card>
  );
}

/** Per-user 18+ opt-in. Only the categories an admin has unlocked (the gate) can be turned on;
 *  if the admin has disabled 18+ entirely there's nothing to opt into. */
function AdultContentCard() {
  const qc = useQueryClient();
  const me = useAuth((s) => s.me);
  const refresh = useAuth((s) => s.refresh);
  const [saving, setSaving] = useState(false);
  const gate = me?.adult_allowed_categories ?? [];           // categories the admin unlocked
  const opted = new Set(me?.adult_categories ?? []);          // this user's own opt-in
  const toggle = async (cat: string) => {
    const next = new Set(opted);
    next.has(cat) ? next.delete(cat) : next.add(cat);
    setSaving(true);
    try {
      await api.setMyAdultCategories(MEDIA_CATEGORIES.filter((c) => next.has(c)));
      await refresh();                                        // re-pull me so the chips reflect saved state
      qc.invalidateQueries({ queryKey: ["catalog-rows"] });   // 18+ titles appear/disappear immediately
      qc.invalidateQueries({ queryKey: ["catalog"] });
    } finally {
      setSaving(false);
    }
  };
  return (
    <Card className="mb-4 p-4">
      <div className="mb-2 flex items-center gap-2">
        <h2 className="flex items-center gap-1.5 font-semibold">
          Adult content (18+)
          <InfoHint text={<>Show explicit 18+ content in these categories. On by default — turn off
            any category you don't want to see. Only categories an administrator permits are shown
            here, and your choice applies to your account only.</>} />
        </h2>
        <Badge tone="red">18+</Badge>
      </div>
      {gate.length === 0 ? (
        <p className="text-sm text-muted">
          Explicit 18+ content is disabled on this instance by an administrator.
        </p>
      ) : (
        <>
          <div className="flex flex-wrap gap-1.5">
            {gate.map((cat) => {
              const on = opted.has(cat);
              return (
                <button
                  key={cat}
                  type="button"
                  disabled={saving}
                  onClick={() => toggle(cat)}
                  title={on ? `Hide 18+ ${cat}` : `Show 18+ ${cat}`}
                  className={`rounded-full border px-2.5 py-1 text-xs transition ${
                    on
                      ? "border-accent bg-accent text-accent-fg"
                      : "border-border bg-surface text-muted hover:bg-surface-2"
                  }`}
                >
                  {on ? "✓ " : ""}
                  {cat}
                </button>
              );
            })}
          </div>
        </>
      )}
    </Card>
  );
}

function AppearancePanel() {
  return (
    <>
      <Card className="mb-4 p-4">
        <h2 className="mb-3 flex items-center gap-1.5 font-semibold">
          Color mode
          <InfoHint text={<>Every mode is gently toned for comfortable reading. Typography (font,
            size, spacing, width) is adjusted live inside the reader via the "Aa" button.</>} />
        </h2>
        <ThemePicker columns={3} />
      </Card>
      <Card className="mb-4 p-4">
        <h2 className="mb-3 flex items-center gap-1.5 font-semibold">
          Index categories
          <InfoHint text={<>Choose which media categories show on the Index page. Hidden ones are
            removed from the discovery rows for your account only.</>} />
        </h2>
        <CategoryToggles />
      </Card>
      <AdultContentCard />
    </>
  );
}

const BACKUP_LEVELS: { value: "settings" | "data" | "full"; label: string; detail: string }[] = [
  { value: "settings", label: "Settings only (smallest)",
    detail: "Config, library, progress + crawl position. The catalog re-indexes and chapter content re-downloads on the new install." },
  { value: "data", label: "Full database",
    detail: "Everything in the database, incl. chapter text, the discovery catalog and crawl index. No re-crawl/re-index — only images (comic pages, covers) re-fetch." },
  { value: "full", label: "Everything + media (largest)",
    detail: "The full database plus every media file (comic pages, cached covers). A complete clone — nothing is re-gathered. Can be very large." },
];

const MODE_META: { value: RestoreMode; label: string; hint: string }[] = [
  { value: "skip", label: "Skip", hint: "Leave this instance's data as-is — don't import." },
  { value: "merge", label: "Merge", hint: "Add items from the backup; keep what's already here." },
  { value: "replace", label: "Replace", hint: "Delete this section here, then load the backup's." },
];

/** Smart default for a section: empty instance → import everything; otherwise protect the target's
 *  existing config (skip settings + integrations) and merge content in. */
function defaultMode(key: string, inBackup: boolean, targetEmpty: boolean): RestoreMode {
  if (!inBackup) return "skip";
  if (targetEmpty) return "replace";
  return key === "settings" || key === "integrations" ? "skip" : "merge";
}

function ModeSelect({ value, disabled, onChange }: {
  value: RestoreMode; disabled?: boolean; onChange: (m: RestoreMode) => void;
}) {
  return (
    <div className="inline-flex overflow-hidden rounded-lg border border-border">
      {MODE_META.map((m) => (
        <button
          key={m.value}
          type="button"
          disabled={disabled}
          title={m.hint}
          onClick={() => onChange(m.value)}
          className={`px-2.5 py-1 text-xs transition ${
            value === m.value
              ? m.value === "replace" ? "bg-red-500/80 text-white" : "bg-accent text-accent-fg"
              : "bg-surface text-muted hover:bg-surface-2"
          } ${disabled ? "cursor-not-allowed opacity-40" : ""}`}
        >
          {m.label}
        </button>
      ))}
    </div>
  );
}

function fmtBytes(n: number): string {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(u.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  return `${(n / Math.pow(1024, i)).toFixed(i ? 1 : 0)} ${u[i]}`;
}

/** The per-section chooser for restoring ONE stored backup. Fetches the plan by name, then commits
 *  by name with the admin's skip/merge/replace choices. */
function RestoreModal({ name, onClose }: { name: string; onClose: () => void }) {
  const toast = useApp((s) => s.toast);
  const [modes, setModes] = useState<Record<string, RestoreMode>>({});
  // staleTime: Infinity so a background refetch can't wipe the admin's in-progress mode selections.
  const planQ = useQuery<RestorePlan>({
    queryKey: ["restore-plan", name], queryFn: () => api.backupPlan(name), staleTime: Infinity,
  });
  // Seed the default modes ONCE from the first plan load (not on every refetch — that reset
  // skip/merge/replace choices mid-interaction).
  const seeded = useRef(false);
  useEffect(() => {
    const p = planQ.data;
    if (!p || seeded.current) return;
    seeded.current = true;
    setModes(Object.fromEntries(
      [...p.sections, p.media].map((s) => [s.key, defaultMode(s.key, s.in_backup, p.target_empty)])));
  }, [planQ.data]);

  const commit = useMutation({
    mutationFn: () => api.commitRestore(name, modes),
    onSuccess: (r) => {
      const n = Object.values(r.loaded || {}).reduce((a, b) => a + b, 0);
      const w = r.warnings?.length ? ` (${r.warnings.join("; ")})` : "";
      // The whole DB just changed → a reload is the cleanest refresh, but via a non-blocking toast
      // (matching the app's Toaster pattern) instead of a blocking alert().
      toast(`Restored ${n} records from the ${r.level} backup${w}. Reloading…`, "success");
      setTimeout(() => window.location.reload(), 1200);
    },
    onError: (e) => toast((e as Error).message, "error"),
  });

  const p = planQ.data;
  const sections = p ? [...p.sections, p.media] : [];
  const willChange = sections.filter((s) => modes[s.key] && modes[s.key] !== "skip");
  return (
    <Modal
      title="Restore — choose what to import"
      width="w-[44rem]"
      onClose={() => !commit.isPending && onClose()}
      footer={
        <>
          <Button variant="ghost" disabled={commit.isPending} onClick={onClose}>Cancel</Button>
          <Button
            variant={willChange.some((s) => modes[s.key] === "replace") ? "danger" : "primary"}
            disabled={commit.isPending || !p || willChange.length === 0}
            onClick={() => commit.mutate()}
          >
            {commit.isPending
              ? "Restoring…"
              : willChange.length === 0
                ? "Nothing selected"
                : `Restore ${willChange.length} section${willChange.length === 1 ? "" : "s"}`}
          </Button>
        </>
      }
    >
      {planQ.isLoading || !p ? (
        <Spinner label="Reading backup…" />
      ) : (
        <>
          <p className="text-sm text-muted">
            From a <b className="text-text">{p.manifest.level}</b> backup
            {p.manifest.created_at ? ` taken ${new Date(p.manifest.created_at).toLocaleString()}` : ""}.
            {p.target_empty
              ? " This instance is empty — everything in the backup is selected."
              : " This instance already has data — pick what to bring in. Skipped sections are left untouched."}
          </p>
          <div className="mt-3 space-y-2">
            {sections.map((s) => {
              const rows = "backup_rows" in s ? s.backup_rows : (s as any).backup_files;
              const here = "target_rows" in s ? (s as any).target_rows : undefined;
              const unit = "backup_files" in s ? "files" : "rows";
              return (
                <div key={s.key}
                  className={`rounded-lg border border-border p-2.5 ${!s.in_backup ? "opacity-50" : ""}`}>
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div className="min-w-0 flex-1">
                      <div className="text-sm font-medium text-text">{s.label}</div>
                      <div className="text-xs text-muted">{s.description}</div>
                      <div className="mt-1 text-[11px] text-muted">
                        {s.in_backup
                          ? <>backup: {rows.toLocaleString()} {unit}
                              {here != null && <> · currently here: {here.toLocaleString()} {unit}</>}</>
                          : "not in this backup"}
                      </div>
                    </div>
                    <div className="shrink-0">
                      <ModeSelect
                        value={modes[s.key] ?? "skip"}
                        disabled={!s.in_backup}
                        onChange={(m) => setModes((prev) => ({ ...prev, [s.key]: m }))}
                      />
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
          <p className="mt-3 text-xs text-muted">
            <b className="text-text">Skip</b> keeps this instance's data · <b className="text-text">Merge</b> adds
            new items and keeps existing ones · <b className="text-red-500">Replace</b> erases that section here
            first, then loads the backup's (can't be undone). The whole restore is atomic — if anything
            fails it rolls back and nothing changes.
          </p>
        </>
      )}
    </Modal>
  );
}

function BackupRow({ b, onRestore, onDelete, deleting }: {
  b: BackupEntry; onRestore: () => void; onDelete: () => void; deleting: boolean;
}) {
  const building = b.status === "building";
  const failed = b.status === "failed";
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-border p-2.5">
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-1.5">
          {b.level && <Badge tone="violet">{b.level}</Badge>}
          <Badge tone={b.origin === "uploaded" ? "amber" : "default"}>{b.origin}</Badge>
          {building && <Badge tone="default">building…</Badge>}
          {failed && <Badge tone="red">failed</Badge>}
          {b.valid && !b.restorable && <Badge tone="red">newer version</Badge>}
          <span className="truncate text-sm text-text" title={b.name}>{b.name}</span>
        </div>
        <div className="mt-0.5 text-xs text-muted">
          {fmtBytes(b.size_bytes)}
          {b.created_at ? ` · ${new Date(b.created_at).toLocaleString()}` : ""}
          {b.media_files ? ` · ${b.media_files.toLocaleString()} media files` : ""}
          {failed && b.error ? ` · ${b.error}` : ""}
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-1">
        {building ? (
          <Spinner />
        ) : (
          <>
            <Button size="sm" variant="primary" disabled={!b.restorable} onClick={onRestore}
              title={b.restorable ? "Restore from this backup"
                : "This backup was made by a newer Shelf version — upgrade before restoring"}>
              Restore
            </Button>
            {b.valid && (
              <Button size="sm" variant="ghost"
                onClick={() => { window.location.href = api.storedBackupUrl(b.name); }}>
                Download
              </Button>
            )}
          </>
        )}
        <Button size="sm" variant="ghost" disabled={deleting} onClick={onDelete} title="Delete">🗑</Button>
      </div>
    </div>
  );
}

function BackupPanel() {
  const qc = useQueryClient();
  const [level, setLevel] = useState<"settings" | "data" | "full">("settings");
  const [restoreName, setRestoreName] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const listQ = useQuery({
    queryKey: ["backups"],
    queryFn: () => api.listBackups(),
    // Poll while a build is in progress so it flips to "ready" on its own.
    refetchInterval: (q) =>
      (q.state.data?.backups ?? []).some((b) => b.status === "building") ? 2000 : false,
  });
  const refresh = () => qc.invalidateQueries({ queryKey: ["backups"] });

  const create = useMutation({
    mutationFn: () => api.createBackup(level),
    onSuccess: () => { setMsg(null); refresh(); },
    onError: (e: any) => setMsg(e?.message || "Couldn't start the backup."),
  });
  const upload = useMutation({
    mutationFn: (file: File) => api.uploadBackup(file),
    onSuccess: () => { setMsg(null); refresh(); },
    onError: (e: any) => setMsg(e?.message || "Upload failed."),
  });
  const del = useMutation({
    mutationFn: (name: string) => api.deleteBackup(name),
    onSuccess: refresh,
  });

  function onPickUpload(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (file) { setMsg(null); upload.mutate(file); }
  }

  const sel = BACKUP_LEVELS.find((l) => l.value === level)!;
  const backups = listQ.data?.backups ?? [];
  return (
    <Card className="mb-4 p-4">
      <h2 className="mb-3 flex items-center gap-1.5 font-semibold">
        Backups
        <InfoHint text={<>Snapshots a fresh (or existing) Shelf install can restore from. Backups
          created here and ones you upload from another machine both appear below as selectable
          objects — pick one to restore, choosing per section what to bring in.</>} />
      </h2>

      <Field label="Backup size">
        <select className={inputCls} value={level} onChange={(e) => setLevel(e.target.value as any)}>
          {BACKUP_LEVELS.map((l) => (<option key={l.value} value={l.value}>{l.label}</option>))}
        </select>
      </Field>
      <p className="mt-1 mb-3 text-xs text-muted">{sel.detail}</p>
      <div className="flex flex-wrap gap-2">
        <Button variant="primary" disabled={create.isPending} onClick={() => create.mutate()}>
          {create.isPending ? "Starting…" : "Create backup"}
        </Button>
        {/* Direct cookie-authed navigation streams even a multi-GB archive to disk without storing it. */}
        <Button variant="outline" onClick={() => { window.location.href = api.backupUrl(level); }}>
          Download directly
        </Button>
        <Button variant="outline" disabled={upload.isPending} onClick={() => fileRef.current?.click()}>
          {upload.isPending ? "Uploading…" : "Upload backup…"}
        </Button>
        <input ref={fileRef} type="file" accept=".zip,application/zip" className="hidden"
               onChange={onPickUpload} />
      </div>
      {msg && <p className="mt-2 text-xs text-red-500">{msg}</p>}
      {listQ.data && (
        <p className="mt-2 text-xs text-muted">{fmtBytes(listQ.data.free_bytes)} free on the backup disk.</p>
      )}

      <div className="mt-4">
        <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">
          Stored backups{backups.length ? ` (${backups.length})` : ""}
        </div>
        {listQ.isLoading ? (
          <Spinner label="Loading backups…" />
        ) : backups.length === 0 ? (
          <p className="text-sm text-muted">No backups yet. Create one above, or upload an existing .zip.</p>
        ) : (
          <div className="space-y-2">
            {backups.map((b) => (
              <BackupRow
                key={b.name}
                b={b}
                deleting={del.isPending && del.variables === b.name}
                onRestore={() => setRestoreName(b.name)}
                onDelete={() => del.mutate(b.name)}
              />
            ))}
          </div>
        )}
      </div>

      {restoreName && <RestoreModal name={restoreName} onClose={() => setRestoreName(null)} />}
    </Card>
  );
}

type TabDef = { id: string; label: string; admin?: boolean; render: () => React.ReactNode };

const TAB_DEFS: TabDef[] = [
  { id: "appearance", label: "Appearance", render: () => <AppearancePanel /> },
  { id: "delivery", label: "Delivery & Notifications", render: () => (
    <>
      <KindleCard />
      <NotificationsCard />
    </>
  ) },
  { id: "goodreads", label: "Goodreads", render: () => <GoodreadsCard /> },
  { id: "acquisition", label: "Acquisition", render: () => <FetchPriorityCard /> },
  { id: "backup", label: "Backup", admin: true, render: () => <BackupPanel /> },
  // Operator-wide surfaces — admins only (regular users don't manage shared integrations,
  // the index crawler, or the global blocklist).
  { id: "integrations", label: "Integrations", admin: true, render: () => (
    <>
      <GlobalSmtpCard />
      <MetadataProvidersCard />
      <AcquisitionCard />
    </>
  ) },
  { id: "indexing", label: "Indexing", admin: true, render: () => (
    <>
      <RequestStatsCard />
      <BookCatalogCard />
      <IndexingCard />
      <CrawlIdentityCard />
      <BlocklistCard />
    </>
  ) },
  { id: "storage", label: "Storage", admin: true, render: () => <StorageSettings /> },
  { id: "automation", label: "Automation", admin: true, render: () => <QueuedHooksCard showEmpty /> },
];

export default function Settings() {
  const isAdmin = useIsAdmin();
  const tabs = TAB_DEFS.filter((t) => !t.admin || isAdmin);

  const initial = () => {
    const hash = window.location.hash.replace(/^#/, "");
    const stored = localStorage.getItem("settings-tab") || "";
    const wanted = hash || stored;
    return tabs.some((t) => t.id === wanted) ? wanted : tabs[0].id;
  };
  const [active, setActive] = useState<string>(initial);

  // Keep the selection valid if admin status resolves after first render.
  useEffect(() => {
    if (!tabs.some((t) => t.id === active)) setActive(tabs[0].id);
  }, [tabs, active]);

  const select = (id: string) => {
    setActive(id);
    localStorage.setItem("settings-tab", id);
    history.replaceState(null, "", `#${id}`);
  };

  const current = tabs.find((t) => t.id === active) ?? tabs[0];

  return (
    <main className="mx-auto max-w-4xl px-4 py-8">
      <h1 className="mb-6 text-2xl font-semibold">Settings</h1>
      <Tabs tabs={tabs} active={current.id} onChange={select} className="mb-6" />
      <div role="tabpanel" aria-label={current.label}>{current.render()}</div>

      <p className="mt-8 text-center text-xs text-muted">
        Shelf ingests only sources you are permitted to read. See the README for the full sourcing policy.
      </p>
    </main>
  );
}
