import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Badge, Button, Card, Modal, Tabs, Toggle } from "../components/ui";
import { MetadataProvidersCard, AcquisitionCard } from "../components/IntegrationsManager";
import QueuedHooksCard from "../components/QueuedHooksCard";
import { api } from "../api/client";
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
      <div className="mb-2 flex items-center gap-2">
        <h2 className="font-semibold">Email server (SMTP)</h2>
        <Badge tone={smtp.data?.configured ? "green" : "amber"}>
          {smtp.data?.configured ? "configured" : "not configured"}
        </Badge>
      </div>
      <p className="mb-3 text-sm text-muted">
        The shared mail server every user sends through (Send-to-Kindle, shelf auto-Kindle,
        notifications). Users only set their own destination address — they never see these
        credentials. Add the <span className="text-text">From</span> address to each Kindle’s
        Approved Personal Document list.
      </p>
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
        <h2 className="font-semibold">Send to Kindle / email</h2>
        <Badge tone={ready ? "green" : "amber"}>{ready ? "email ready" : "email not set up"}</Badge>
      </div>
      <p className="mb-3 text-sm text-muted">
        Set where your EPUBs go — your Kindle and/or your own inbox — then use the <b>📤 Send</b>
        button on any work. {ready ? (
          <>Mail is sent from <span className="text-text">{settings.data?.smtp_from || "the shared address"}</span>;
          for Kindle, add that address to your Amazon “Approved Personal Document E-mail List”.</>
        ) : (
          <>The mail server hasn’t been configured by an administrator yet, so sending is off.</>
        )}
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
      <h2 className="mb-1 font-semibold">Book catalog</h2>
      <p className="mb-3 text-sm text-muted">
        A hybrid book catalog: a persistent <b>hot set</b> of popular titles (seeded from Open
        Library trending + popular subjects and Google Books) plus <b>live resolve</b> — a search
        with no close local match is looked up against the book APIs on the fly and cached. Add a{" "}
        <span className="text-text">Google Books</span> integration with an API key to lift its
        keyless quota; Open Library needs no key.
      </p>
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
      <h2 className="mb-1 font-semibold">Fetch source priority</h2>
      <p className="mb-3 text-sm text-muted">
        When you acquire a title (or it's auto-fetched from Goodreads), Shelf tries these sources
        in order and uses the first that can deliver it. Drag the most-preferred to the top.
      </p>
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
      <div className="mb-1 flex items-center gap-2">
        <h2 className="font-semibold">Adult content (18+)</h2>
        <Badge tone="red">18+</Badge>
      </div>
      {gate.length === 0 ? (
        <p className="text-sm text-muted">
          Explicit 18+ content is disabled on this instance. An administrator can enable it per
          category, after which you can choose to show it here.
        </p>
      ) : (
        <>
          <p className="mb-2 text-sm text-muted">
            Show explicit 18+ content in these categories. Off by default — only the categories an
            administrator has unlocked are shown here, and your choice applies to your account only.
          </p>
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
        <h2 className="mb-3 font-semibold">Color mode</h2>
        <ThemePicker columns={3} />
        <p className="mt-3 text-xs text-muted">
          Every mode is gently toned for comfortable reading. Typography (font, size, spacing,
          width) is adjusted live inside the reader via the “Aa” button.
        </p>
      </Card>
      <Card className="mb-4 p-4">
        <h2 className="mb-1 font-semibold">Index categories</h2>
        <p className="mb-1 text-sm text-muted">
          Choose which media categories show on the Index page. Hidden ones are removed from the
          discovery rows for your account only.
        </p>
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

function BackupPanel() {
  const [level, setLevel] = useState<"settings" | "data" | "full">("settings");
  const [restoreMsg, setRestoreMsg] = useState<string | null>(null);
  const [pendingFile, setPendingFile] = useState<File | null>(null);   // chosen file → restore modal
  const fileRef = useRef<HTMLInputElement>(null);
  const restore = useMutation({
    mutationFn: async ({ file, wipe }: { file: File; wipe: boolean }) =>
      api.restoreBackup(file, wipe),
    onSuccess: (r) => {
      const n = Object.values(r.loaded || {}).reduce((a, b) => a + b, 0);
      setRestoreMsg(`Restored ${n} records (${r.level} backup). Reloading…`);
      setTimeout(() => window.location.reload(), 1500);
    },
    onError: (e: any) => setRestoreMsg(e?.message || "Restore failed."),
  });

  function onPickRestore(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = ""; // allow re-picking the same file
    if (file) setPendingFile(file);   // open the restore modal with clear button choices
  }
  function doRestore(wipe: boolean) {
    if (!pendingFile) return;
    setRestoreMsg(null);
    restore.mutate({ file: pendingFile, wipe });
    setPendingFile(null);
  }

  const sel = BACKUP_LEVELS.find((l) => l.value === level)!;
  return (
    <Card className="mb-4 p-4">
      <h2 className="mb-2 font-semibold">Backup &amp; restore</h2>
      <p className="mb-3 text-sm text-muted">
        Download a snapshot a fresh Shelf install can import to resume from here. Choose how much to
        include — bigger backups re-gather less.
      </p>
      <Field label="Backup size">
        <select className={inputCls} value={level} onChange={(e) => setLevel(e.target.value as any)}>
          {BACKUP_LEVELS.map((l) => (
            <option key={l.value} value={l.value}>{l.label}</option>
          ))}
        </select>
      </Field>
      <p className="mt-1 mb-3 text-xs text-muted">{sel.detail}</p>
      <div className="flex flex-wrap gap-2">
        {/* Direct (cookie-authed) navigation so even a multi-GB archive streams to disk. */}
        <Button onClick={() => { window.location.href = api.backupUrl(level); }}>
          Download backup (.zip)
        </Button>
        <Button variant="outline" disabled={restore.isPending} onClick={() => fileRef.current?.click()}>
          {restore.isPending ? "Restoring…" : "Restore from backup…"}
        </Button>
        <input ref={fileRef} type="file" accept=".zip,application/zip" className="hidden"
               onChange={onPickRestore} />
      </div>
      {restoreMsg && <p className="mt-2 text-xs text-muted">{restoreMsg}</p>}

      {pendingFile && (
        <Modal
          title="Restore from backup"
          width="w-[30rem]"
          onClose={() => setPendingFile(null)}
          footer={
            <>
              <Button variant="ghost" onClick={() => setPendingFile(null)}>Cancel</Button>
              <Button variant="outline" onClick={() => doRestore(false)}>Import into empty instance</Button>
              <Button variant="danger" onClick={() => doRestore(true)}>Erase &amp; replace</Button>
            </>
          }
        >
          <p className="text-sm text-muted">
            Restoring <span className="text-text">{pendingFile.name}</span>. Restore is meant for a
            fresh install:
          </p>
          <ul className="mt-2 space-y-1 text-sm text-muted">
            <li>• <b className="text-text">Import into empty instance</b> — only works if this Shelf has no data yet.</li>
            <li>• <b className="text-red-500">Erase &amp; replace</b> — permanently deletes ALL current data first, then imports. This can't be undone.</li>
          </ul>
        </Modal>
      )}
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
      <BookCatalogCard />
      <IndexingCard />
      <CrawlIdentityCard />
      <BlocklistCard />
    </>
  ) },
  { id: "automation", label: "Automation", admin: true, render: () => <QueuedHooksCard /> },
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
