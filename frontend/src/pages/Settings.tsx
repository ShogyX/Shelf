import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Badge, Button, Card, CardHeader, Disclosure, FormField, inputCls, Modal, Spinner, StatusChip, Toggle } from "../components/ui";
import { MetadataProvidersCard, AcquisitionCard, ReadingAppsCard } from "../components/IntegrationsManager";
import { ChannelsCard, EventPrefsCard, AdminNotifyCard } from "../components/settings/NotificationCards";
import StatisticsPanel from "../components/StatisticsPanel";
import BookshelvesPanel from "../components/settings/BookshelvesPanel";
import InsightsPanel from "../components/InsightsPanel";
import StorageSettings from "../components/StorageSettings";
import { SystemConfigCard } from "../components/SystemSettings";
import LayoutSettings from "../components/catalog/LayoutSettings";
import ThemePicker from "../components/ThemePicker";
import { api, BackupEntry, RestoreMode, RestorePlan } from "../api/client";
import { qk } from "../api/queryKeys";
import { useApp } from "../store";
import { useConfirm } from "../components/confirm";
import { useHasPermission, useIsAdmin, useAuth, useCurrentUser } from "../auth";
import { MEDIA_CATEGORIES } from "../api/client";

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block text-xs text-muted">
      {label}
      <div className="mt-1">{children}</div>
    </label>
  );
}

const MODERATE = { tick_seconds: 10, chapters_per_tick: 3, parallel_fetches: 4, refresh_hours: 6 };

function CrawlSpeedSection() {
  const qc = useQueryClient();
  const tuning = useQuery({ queryKey: qk.crawlTuning(), queryFn: api.getCrawlTuning });
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
      qc.invalidateQueries({ queryKey: qk.crawlTuning() });
      setTimeout(() => setSaved(false), 2500);
    },
  });

  const num = (k: keyof typeof MODERATE) => (
    <input
      type="number"
      min={1}
      value={form?.[k] ?? ""}
      onChange={(e) => setForm((f) => (f ? { ...f, [k]: Math.max(1, Number(e.target.value) || 1) } : f))}
      className={`${inputCls} w-24!`}
    />
  );

  return (
    <div>
      <CardHeader title="Crawl speed" hint={<>How fast the backfill + index crawlers run. Changes apply live to running
          and future jobs — no restart. Each source's own rate limits (set per-source on Sources)
          still apply, so raising these never bypasses per-site politeness. Lower interval + higher
          chapters/parallel = faster but more load on sources (and a higher chance of rate-limiting).
          Backfill and indexing have independent budgets, so they no longer slow each other down when
          run together.</>} />
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
  const identity = useQuery({ queryKey: qk.operatorIdentity(), queryFn: api.getOperatorIdentity });
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
      qc.invalidateQueries({ queryKey: qk.operatorIdentity() });
      setTimeout(() => setSaved(false), 2500);
    },
  });

  return (
    <Card className="mb-4 p-4">
      <CardHeader title="Crawl identity" hint={<>How the polite fetcher identifies itself to every source: a User-Agent
          (project name + contact link, matched against each site's robots.txt) and a contact email
          (sent as the From header so a site admin can reach you). Changes apply live to running and
          future crawls — no restart. Leave a field blank to reset it to the built-in default.</>} />
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
  const blocks = useQuery({ queryKey: qk.indexBlocks(), queryFn: api.listBlocks });
  const del = useMutation({
    mutationFn: (id: number) => api.deleteBlock(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.indexBlocks() }),
  });
  const items = blocks.data ?? [];
  if (items.length === 0) return null; // nothing blocked → keep settings tidy

  return (
    <Card className="mb-4 p-4">
      <CardHeader
        title={<>Blocked content <span className="text-sm font-normal text-muted">· {items.length} blocked</span></>}
        hint={<>URLs and domains you've removed from the index. They won't be re-discovered
          by crawls or hooked. Unblock to allow them again.</>} />
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
  const cfg = useQuery({ queryKey: qk.indexConfig(), queryFn: api.getIndexConfig });
  const [idle, setIdle] = useState<number | null>(null);
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    if (cfg.data && idle === null) setIdle(cfg.data.stop_after_idle_pages);
  }, [cfg.data, idle]);

  const save = useMutation({
    mutationFn: () => api.putIndexConfig(Math.max(1, idle ?? 200)),
    onSuccess: () => {
      setSaved(true);
      qc.invalidateQueries({ queryKey: qk.indexConfig() });
      setTimeout(() => setSaved(false), 2500);
    },
  });

  return (
    <Card className="mb-4 p-4">
      <CrawlSpeedSection />
      <div className="my-4 border-t border-border" />
      <CardHeader title="Indexing" hint={<>New index crawls run with no page cap — they keep indexing until the whole
          site is covered. After a long stretch with nothing new (no title, no new link) they stop
          looking for more pages but still finish whatever's queued, so nothing found is left behind.
          This is the default threshold for new crawls; override it per-crawl on the Jobs page.</>} />
      <Field label="Stop discovering after this many pages with nothing new (the crawl still finishes its queue)">
        <div className="flex items-center gap-2">
          <input
            type="number"
            min={1}
            value={idle ?? ""}
            onChange={(e) => setIdle(Math.max(1, Number(e.target.value) || 1))}
            className={`${inputCls} w-28!`}
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
  const smtp = useQuery({ queryKey: qk.globalSmtp(), queryFn: api.getGlobalSmtp });
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
    onSuccess: () => { qc.invalidateQueries({ queryKey: qk.globalSmtp() });
      qc.invalidateQueries({ queryKey: qk.settings() });
      setSaved(true); setTimeout(() => setSaved(false), 1500); },
  });
  const pwSet = !!smtp.data?.smtp_password_set;
  return (
    <Card className="mb-4 p-4">
      <CardHeader
        title="Email server (SMTP)"
        hint={<>The shared mail server every user sends through (Send-to-Kindle, shelf
            auto-Kindle, notifications). Users only set their own destination address — they never
            see these credentials. Add the From address to each Kindle's Approved Personal Document
            list.</>}
        badge={<StatusChip tone={smtp.data?.configured ? "success" : "warning"}>
          {smtp.data?.configured ? "configured" : "not configured"}
        </StatusChip>} />
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
  const me = useCurrentUser();
  const qc = useQueryClient();
  const settings = useQuery({ queryKey: qk.settings(), queryFn: api.getSettings });
  const [kindleEmail, setKindleEmail] = useState("");
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    const d = settings.data;
    if (!d) return;
    setKindleEmail(d.kindle_email ?? "");
  }, [settings.data]);

  async function save() {
    await api.saveSettings({ kindle_email: kindleEmail.trim() });
    await qc.invalidateQueries({ queryKey: qk.settings() });
    setSaved(true);
    setTimeout(() => setSaved(false), 1500);
  }

  if (!canSend) return null;  // user not permitted to send-to-Kindle / set a delivery target
  const ready = settings.data?.smtp_configured;
  return (
    <Card className="mb-4 p-4">
      <CardHeader
        title="Send to Kindle / email"
        hint={<>Set where your EPUBs go — your Kindle and/or your own inbox — then use
            the 📤 Send button on any work. Mail is sent from the shared address configured by an
            administrator; for Kindle, add that address to your Amazon "Approved Personal Document
            E-mail List". If the mail server hasn't been configured yet, sending is off.</>}
        badge={<Badge tone={ready ? "green" : "amber"}>{ready ? "email ready" : "email not set up"}</Badge>} />
      {ready && settings.data?.smtp_from && (
        <p className="mb-3 text-xs text-muted">Sends from {settings.data.smtp_from}</p>
      )}

      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Kindle email">
          <input className={inputCls} type="email" placeholder="device@kindle.com"
            value={kindleEmail} onChange={(e) => setKindleEmail(e.target.value)} />
        </Field>
        <Field label="Personal email">
          <input className={`${inputCls} opacity-70`} type="email" value={me?.email ?? ""}
            readOnly disabled placeholder="(set on your account)" />
          <p className="mt-1 text-[11px] text-muted">Your account email — “Send to email” delivers here.</p>
        </Field>
      </div>

      <div className="mt-3 flex justify-end">
        <Button variant="primary" onClick={save}>{saved ? "Saved ✓" : "Save"}</Button>
      </div>
    </Card>
  );
}

function BookCatalogCard() {
  const qc = useQueryClient();
  const status = useQuery({ queryKey: qk.bookCatalog(), queryFn: api.getBookCatalogConfig });
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
      qc.invalidateQueries({ queryKey: qk.bookCatalog() });
      setTimeout(() => setSaved(false), 2000);
    },
  });

  async function syncNow() {
    setSyncing(true);
    try {
      await api.syncBookCatalog();
      await qc.invalidateQueries({ queryKey: qk.bookCatalog() });
    } finally {
      setSyncing(false);
    }
  }

  const d = status.data;
  return (
    <Card className="mb-4 p-4">
      <CardHeader title="Book catalog" hint={<>A hybrid book catalog: a persistent hot set of popular titles (seeded from
          Open Library trending + popular subjects and Google Books) plus live resolve — a search
          with no close local match is looked up against the book APIs on the fly and cached. Add a
          Google Books integration with an API key to lift its keyless quota; Open Library needs no
          key.</>} />
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
                className={`${inputCls} w-32!`}
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
                className={`${inputCls} w-28!`}
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
  torrent: "Torrent (Prowlarr → qBittorrent)",
  pipeline: "Usenet (Prowlarr → SABnzbd)",
  libgen: "Anna's Archive (fallback)",
  web_index: "Web index (crawl & hook)",
  readarr: "Readarr (book manager)",
  kapowarr: "Kapowarr (comic manager)",
};

function FetchPriorityCard() {
  const qc = useQueryClient();
  const isAdmin = useIsAdmin();
  const q = useQuery({ queryKey: qk.fetchPriority(), queryFn: api.getFetchPriority });
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
    await qc.invalidateQueries({ queryKey: qk.fetchPriority() });
    setSaved(global ? "global" : "yours");
    setTimeout(() => setSaved(""), 2000);
  }

  return (
    <Card className="mb-4 p-4">
      <CardHeader title="Fetch source priority" hint={<>When you acquire a title (or it's auto-fetched from Goodreads), Shelf tries
          these sources in order and uses the first that can deliver it. Move the most-preferred to
          the top.</>} />
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

/** Admin-only: how often Shelf re-attempts titles it has marked unavailable, wired to the shared
 *  system-config get/PUT (same query key + endpoint AutoBackupSection uses). */
function MissingRecheckCard() {
  const isAdmin = useIsAdmin();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: qk.systemConfig(), queryFn: api.getSystemConfig, enabled: isAdmin });
  const [f, setF] = useState<Record<string, string | number | boolean> | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => { if (q.data && f === null) setF({ ...q.data.values }); }, [q.data, f]);

  const save = useMutation({
    mutationFn: () =>
      api.putSystemConfig({
        missing_recheck_days: Number(f!.missing_recheck_days),
        missing_recheck_batch: Number(f!.missing_recheck_batch),
        auto_request_series: !!f!.auto_request_series,
      }),
    onSuccess: (d) => {
      qc.setQueryData(qk.systemConfig(), d);
      setF({ ...d.values });
      setSaved(true);
      setTimeout(() => setSaved(false), 2500);
    },
  });

  if (!isAdmin || !q.data || !f) return null;
  return (
    <Card className="mb-4 p-4">
      <CardHeader title="Re-checking unavailable titles" hint={<>Titles Shelf couldn't find are parked and periodically re-attempted. These
          control how often a parked title becomes due again and how many are re-checked each run.</>} />
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Re-check unavailable titles every N days">
          <input type="number" min={1} className={inputCls} value={f.missing_recheck_days as number}
            onChange={(e) => setF({ ...f, missing_recheck_days: Number(e.target.value) })} />
        </Field>
        <Field label="Titles re-checked per run">
          <input type="number" min={1} className={inputCls} value={f.missing_recheck_batch as number}
            onChange={(e) => setF({ ...f, missing_recheck_batch: Number(e.target.value) })} />
        </Field>
      </div>
      <div className="mt-3 flex items-center justify-between gap-2 py-1">
        <span className="text-xs text-muted">
          Auto-request the rest of a series when you fetch one of its books
        </span>
        <Toggle checked={!!f.auto_request_series}
          onChange={(b) => setF({ ...f, auto_request_series: b })} label="" />
      </div>
      <div className="mt-3 flex items-center gap-2">
        <Button variant="primary" disabled={save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? "Saving…" : "Save"}
        </Button>
        {saved && <Badge tone="green">saved</Badge>}
        {save.isError && <span className="text-sm text-red-500">{(save.error as Error).message}</span>}
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
      qc.invalidateQueries({ queryKey: qk.catalogRows() });   // 18+ titles appear/disappear immediately
      qc.invalidateQueries({ queryKey: qk.catalog() });
    } finally {
      setSaving(false);
    }
  };
  return (
    <Card className="mb-4 p-4">
      <CardHeader
        title="Adult content (18+)"
        hint={<>Show explicit 18+ content in these categories. On by default — turn off
            any category you don't want to see. Only categories an administrator permits are shown
            here, and your choice applies to your account only.</>}
        badge={<Badge tone="red">18+</Badge>} />
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
  const isAdmin = useIsAdmin();
  return (
    <>
      <Card className="mb-4 p-4">
        <CardHeader title="Appearance" desc="Pick a theme — every surface, the reader, and covers retint instantly." />
        <ThemePicker columns={4} />
      </Card>
      <KindleCard />
      <AdultContentCard />
      {isAdmin && <LayoutSettings />}
    </>
  );
}

function NotificationsPanel() {
  const isAdmin = useIsAdmin();
  return (
    <>
      <ChannelsCard />
      <EventPrefsCard />
      {isAdmin && (
        <>
          <GlobalSmtpCard />
          <AdminNotifyCard />
        </>
      )}
    </>
  );
}

/** Integrations tab — operator-wide providers, admin-only. (Goodreads want-to-read is now covered by
 *  the Imports page, so it no longer surfaces in Settings.) */
function IntegrationsPanel() {
  return (
    <>
      <MetadataProvidersCard />
      {/* Cloudflare solver now renders as a provider box inside AcquisitionCard, next to VirusTotal. */}
      <AcquisitionCard />
      <ReadingAppsCard />
    </>
  );
}

/** Acquisition tab — per-user fetch priority + missing-recheck for everyone; the global content
 *  blocklist is an operator surface, admin-only. */
function AcquisitionPanel() {
  const isAdmin = useIsAdmin();
  return (
    <>
      <FetchPriorityCard />
      <MissingRecheckCard />
      {isAdmin && <BlocklistCard />}
      {isAdmin && <SystemConfigCard groups={["List imports"]} />}
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
    <div className="inline-flex rounded-[11px] border border-[var(--hair-strong,var(--border))] bg-surface-2 p-0.5" role="group" aria-label="Restore mode">
      {MODE_META.map((m) => {
        const on = value === m.value;
        return (
          <button
            key={m.value}
            type="button"
            disabled={disabled}
            aria-pressed={on}
            title={m.hint}
            onClick={() => onChange(m.value)}
            className={`rounded-[9px] px-3 py-1.5 text-xs font-semibold transition ${
              on
                ? m.value === "replace"
                  ? "bg-red-500 text-white shadow-sm"
                  : "bg-accent text-accent-fg shadow-sm"
                : "text-[var(--text-soft,var(--muted))] hover:text-text"
            } ${disabled ? "cursor-not-allowed opacity-40" : ""}`}
          >
            {m.label}
          </button>
        );
      })}
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
    queryKey: qk.restorePlan(name), queryFn: () => api.backupPlan(name), staleTime: Infinity,
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
          <p className="text-sm text-[var(--text-soft,var(--muted))]">
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
                  className={`rounded-xl border border-[var(--hair,var(--border))] bg-surface p-3 ${!s.in_backup ? "opacity-50" : ""}`}>
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div className="min-w-0 flex-1">
                      <div className="text-sm font-semibold text-text">{s.label}</div>
                      <div className="text-xs text-[var(--text-soft,var(--muted))]">{s.description}</div>
                      <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                        {s.in_backup
                          ? <>
                              <Badge tone="violet">backup: {rows.toLocaleString()} {unit}</Badge>
                              {here != null && <Badge>here: {here.toLocaleString()} {unit}</Badge>}
                            </>
                          : <Badge>not in this backup</Badge>}
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
          <p className="mt-3 text-xs leading-snug text-[var(--text-soft,var(--muted))]">
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
    <div className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-[var(--hair,var(--border))] bg-surface p-3">
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-1.5">
          {b.level && <Badge tone="violet">{b.level}</Badge>}
          <Badge tone={b.origin === "uploaded" ? "amber" : "default"}>{b.origin}</Badge>
          {building && <StatusChip tone="info">building…</StatusChip>}
          {failed && <StatusChip tone="danger">failed</StatusChip>}
          {b.valid && !b.restorable && <StatusChip tone="warning">newer version</StatusChip>}
          <span className="truncate text-sm font-medium text-text" title={b.name}>{b.name}</span>
        </div>
        <div className="mt-0.5 text-xs text-[var(--text-soft,var(--muted))]">
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
        <Button size="sm" variant="danger" disabled={deleting} onClick={onDelete} title="Delete">🗑</Button>
      </div>
    </div>
  );
}

/** Scheduled-backup controls, wired to the shared system-config get/PUT (same query key + endpoint
 *  SystemSettings uses), so editing here round-trips with that page. */
function AutoBackupSection() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: qk.systemConfig(), queryFn: api.getSystemConfig });
  const [f, setF] = useState<Record<string, string | number | boolean> | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => { if (q.data && f === null) setF({ ...q.data.values }); }, [q.data, f]);

  const save = useMutation({
    mutationFn: () =>
      api.putSystemConfig({
        auto_backup_enabled: !!f!.auto_backup_enabled,
        auto_backup_level: f!.auto_backup_level,
        auto_backup_interval_hours: Number(f!.auto_backup_interval_hours),
        auto_backup_keep: Number(f!.auto_backup_keep),
      }),
    onSuccess: (d) => {
      qc.setQueryData(qk.systemConfig(), d);
      setF({ ...d.values });
      setSaved(true);
      setTimeout(() => setSaved(false), 2500);
    },
  });

  if (!q.data || !f) return null;
  return (
    <Card className="mb-4 p-4">
      <CardHeader title="Automatic backups" desc="Scheduled instance backups so an unattended install isn't left with zero backups."
        hint={<>Scheduled instance backups so an unattended install isn't left with zero
          backups. App-created backups beyond the kept count are pruned (uploads are never pruned).</>} />
      <div className="mb-3.5 flex items-center justify-between gap-4 rounded-xl border border-[var(--hair,var(--border))] bg-surface px-3.5 py-3">
        <div className="min-w-0">
          <div className="text-sm font-semibold text-text">Enable scheduled backups</div>
          <div className="text-xs text-[var(--text-soft,var(--muted))]">Pruned beyond the kept count; uploads are never pruned.</div>
        </div>
        <Toggle checked={!!f.auto_backup_enabled}
          onChange={(b) => setF({ ...f, auto_backup_enabled: b })} label="" />
      </div>
      <div className="grid gap-x-4 sm:grid-cols-3">
        <FormField label="Backup level">
          <select className={inputCls} value={String(f.auto_backup_level)}
            onChange={(e) => setF({ ...f, auto_backup_level: e.target.value })}>
            {BACKUP_LEVELS.map((l) => (<option key={l.value} value={l.value}>{l.label}</option>))}
          </select>
        </FormField>
        <FormField label="Interval (hours)">
          <input type="number" min={1} className={inputCls} value={f.auto_backup_interval_hours as number}
            onChange={(e) => setF({ ...f, auto_backup_interval_hours: Number(e.target.value) })} />
        </FormField>
        <FormField label="Keep newest">
          <input type="number" min={1} className={inputCls} value={f.auto_backup_keep as number}
            onChange={(e) => setF({ ...f, auto_backup_keep: Number(e.target.value) })} />
        </FormField>
      </div>
      <div className="mt-3 flex items-center gap-2">
        <Button variant="primary" disabled={save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? "Saving…" : "Save schedule"}
        </Button>
        {saved && <Badge tone="green">saved</Badge>}
        {save.isError && <span className="text-sm text-red-500">{(save.error as Error).message}</span>}
      </div>
    </Card>
  );
}

function BackupPanel() {
  const qc = useQueryClient();
  const [level, setLevel] = useState<"settings" | "data" | "full">("settings");
  const [restoreName, setRestoreName] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const listQ = useQuery({
    queryKey: qk.backups(),
    queryFn: () => api.listBackups(),
    // Poll while a build is in progress so it flips to "ready" on its own.
    refetchInterval: (q) =>
      (q.state.data?.backups ?? []).some((b) => b.status === "building") ? 2000 : false,
  });
  const refresh = () => qc.invalidateQueries({ queryKey: qk.backups() });

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
  const confirm = useConfirm();
  const restoreSnap = useMutation({
    mutationFn: (name: string) => api.restoreDbSnapshot(name),
    onSuccess: () => setMsg("Restoring — the server is restarting. This page will reconnect in a moment."),
    onError: (e: any) => setMsg(e?.message || "Restore failed."),
  });
  const delSnap = useMutation({
    mutationFn: (name: string) => api.deleteDbSnapshot(name),
    onSuccess: refresh,
  });
  const onRestoreSnap = async (name: string) => {
    if (await confirm({
      title: "Restore the entire database",
      message: `Replace ALL current data with “${name}” and restart the server? The current database is safety-copied first, but everything since this snapshot is lost and any in-progress work is interrupted.`,
      danger: true, confirmText: "Replace & restart",
    })) restoreSnap.mutate(name);
  };
  const onDeleteSnap = async (name: string) => {
    if (await confirm({
      title: "Delete snapshot", message: `Delete the database snapshot “${name}”? This frees disk but the snapshot can't be recovered.`,
      danger: true, confirmText: "Delete",
    })) delSnap.mutate(name);
  };

  function onPickUpload(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (file) { setMsg(null); upload.mutate(file); }
  }

  const sel = BACKUP_LEVELS.find((l) => l.value === level)!;
  const backups = listQ.data?.backups ?? [];
  return (
    <Card className="mb-4 p-4">
      <CardHeader title="Backups" desc="Snapshots a fresh or existing Shelf install can restore from."
        hint={<>Snapshots a fresh (or existing) Shelf install can restore from. Backups
          created here and ones you upload from another machine both appear below as selectable
          objects — pick one to restore, choosing per section what to bring in.</>} />

      <FormField label="Backup size" hint={sel.detail}>
        <select className={inputCls} value={level} onChange={(e) => setLevel(e.target.value as any)}>
          {BACKUP_LEVELS.map((l) => (<option key={l.value} value={l.value}>{l.label}</option>))}
        </select>
      </FormField>
      <div className="flex flex-wrap gap-2">
        <Button variant="primary" disabled={create.isPending} onClick={() => create.mutate()}>
          {create.isPending ? "Starting…" : "Create backup"}
        </Button>
        {/* Direct cookie-authed navigation streams even a multi-GB archive to disk without storing it.
            Guard the navigation target on the fixed level allow-list so the value assigned to
            location.href is provably not attacker-controlled. */}
        <Button variant="outline" onClick={() => {
          if (BACKUP_LEVELS.some((l) => l.value === level)) window.location.href = api.backupUrl(level);
        }}>
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
        <p className="mt-2 text-xs text-[var(--text-soft,var(--muted))]">{fmtBytes(listQ.data.free_bytes)} free on the backup disk.</p>
      )}

      <div className="mt-5">
        <div className="font-display mb-2 border-b border-[var(--hair,var(--border))] pb-2 text-xs font-semibold uppercase tracking-wide text-[var(--text-soft,var(--muted))]">
          Stored backups{backups.length ? ` (${backups.length})` : ""}
        </div>
        {listQ.isLoading ? (
          <Spinner label="Loading backups…" />
        ) : backups.length === 0 ? (
          <p className="text-sm text-[var(--text-soft,var(--muted))]">No backups yet. Create one above, or upload an existing .zip.</p>
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

      <div className="mt-6">
        <div className="font-display mb-2 border-b border-[var(--hair,var(--border))] pb-2 text-xs font-semibold uppercase tracking-wide text-[var(--text-soft,var(--muted))]">
          Full-database snapshots{(listQ.data?.db_snapshots?.length ?? 0) ? ` (${listQ.data!.db_snapshots.length})` : ""}
        </div>
        <p className="mb-2 text-xs leading-snug text-[var(--text-soft,var(--muted))]">
          Whole-database file copies kept next to the live database (automatic pre-operation safety
          copies and recovery files). Restoring one replaces <b className="text-text">all</b> data with that exact snapshot
          and restarts the server — the current database is safety-copied first.
        </p>
        {(listQ.data?.db_snapshots ?? []).length === 0 ? (
          <p className="text-sm text-[var(--text-soft,var(--muted))]">No database snapshots found.</p>
        ) : (
          <div className="space-y-2">
            {listQ.data!.db_snapshots.map((s) => (
              <div key={s.name} className="flex items-center justify-between gap-3 rounded-xl border border-[var(--hair,var(--border))] bg-surface p-3">
                <div className="min-w-0">
                  <div className="truncate font-mono text-xs text-text">{s.name}</div>
                  <div className="mt-0.5 text-[11px] text-[var(--text-soft,var(--muted))]">
                    {fmtBytes(s.size_bytes)}
                    {s.created_at ? ` · ${new Date(s.created_at).toLocaleString()}` : ""}
                    {!s.restorable ? " · not a database file" : ""}
                  </div>
                </div>
                <div className="flex shrink-0 gap-1.5">
                  <Button size="sm" variant="outline" disabled={!s.restorable || restoreSnap.isPending}
                    onClick={() => onRestoreSnap(s.name)}>
                    {restoreSnap.isPending && restoreSnap.variables === s.name ? "Restoring…" : "Restore"}
                  </Button>
                  <Button size="sm" variant="danger" disabled={delSnap.isPending}
                    onClick={() => onDeleteSnap(s.name)} title="Delete snapshot">✕</Button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {restoreName && <RestoreModal name={restoreName} onClose={() => setRestoreName(null)} />}
    </Card>
  );
}

type TabDef = { id: string; label: string; icon: string; group: string; admin?: boolean; render: () => React.ReactNode };

// Left-rail groups (redesign): Personal / Library & sources / System.
const SETTINGS_GROUPS = ["Personal", "Library & sources", "System"] as const;

const TAB_DEFS: TabDef[] = [
  { id: "appearance", label: "Preferences", icon: "🎚", group: "Personal", render: () => <AppearancePanel /> },
  { id: "notifications", label: "Notifications", icon: "🔔", group: "Personal", render: () => <NotificationsPanel /> },
  { id: "bookshelves", label: "Bookshelves", icon: "🗂", group: "Library & sources", render: () => <BookshelvesPanel /> },
  { id: "acquisition", label: "Acquisition", icon: "⤓", group: "Library & sources", render: () => <AcquisitionPanel /> },
  // Operator-wide providers only, so this tab is admin-only.
  { id: "integrations", label: "Integrations", icon: "🔌", group: "Library & sources", admin: true, render: () => <IntegrationsPanel /> },
  { id: "indexing", label: "Indexing", icon: "🌐", group: "Library & sources", admin: true, render: () => (
    <>
      {/* Commonly-tuned config stays open; advanced + read-only telemetry collapse to cut bloat. */}
      <IndexingCard />
      <CrawlIdentityCard />
      <BookCatalogCard />
      <Disclosure title="Advanced crawl settings" subtitle="Crawl-default caps and the comix browser crawler">
        <SystemConfigCard groups={["Crawl defaults", "Comix crawler"]} />
      </Disclosure>
    </>
  ) },
  { id: "storage", label: "Storage", icon: "💾", group: "System", admin: true, render: () => (
    <>
      <StorageSettings />
      <SystemConfigCard groups={["Image cache"]} />
    </>
  ) },
  { id: "backup", label: "Backup", icon: "🛡", group: "System", admin: true, render: () => (
    <>
      <BackupPanel />
      <AutoBackupSection />
      <SystemConfigCard groups={["Logging"]} />
    </>
  ) },
  // Insights = charts over the same data; the raw request/VT/pipeline tables stay under a disclosure.
  { id: "statistics", label: "Insights", icon: "📊", group: "System", admin: true, render: () => (
    <>
      <InsightsPanel />
      <div className="mt-5">
        <Disclosure title="Detailed telemetry" subtitle="Raw request-stats, VirusTotal usage and pipeline tables">
          <StatisticsPanel />
        </Disclosure>
      </div>
    </>
  ) },
];

export default function Settings() {
  const isAdmin = useIsAdmin();
  // Memoize so `tabs` is a stable reference — otherwise the validity effect below (dep: [tabs])
  // re-runs every render on a fresh array identity.
  const tabs = useMemo(() => TAB_DEFS.filter((t) => !t.admin || isAdmin), [isAdmin]);

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
    <main className="page-in mx-auto max-w-6xl px-4 py-8 sm:px-6">
      <h1 className="font-display mb-6 text-3xl font-semibold tracking-tight text-text">Settings</h1>
      <div className="flex flex-col gap-7 lg:flex-row lg:items-start">
        {/* Left rail (sticky on desktop; a horizontal scroll strip on mobile). */}
        <aside className="lg:sticky lg:top-20 lg:w-[216px] lg:shrink-0">
          <nav className="flex gap-1 overflow-x-auto scrollbar-none [mask-image:linear-gradient(to_right,#000_92%,transparent)] lg:flex-col lg:gap-0 lg:[mask-image:none]" aria-label="Settings sections">
            {SETTINGS_GROUPS.map((g) => {
              const items = tabs.filter((t) => t.group === g);
              if (items.length === 0) return null;
              return (
                <div key={g} className="flex shrink-0 gap-1 lg:mb-4 lg:block lg:gap-0">
                  <div className="hidden px-3 pb-2 text-[11px] font-bold uppercase tracking-wider text-muted lg:block">{g}</div>
                  {items.map((t) => {
                    const on = t.id === current.id;
                    return (
                      <button
                        key={t.id}
                        onClick={() => select(t.id)}
                        className={`relative flex shrink-0 items-center gap-2.5 whitespace-nowrap rounded-[10px] px-3 py-2 text-sm font-semibold transition lg:w-full ${
                          on ? "bg-[color-mix(in_srgb,var(--accent)_16%,transparent)] text-text" : "text-muted hover:text-text"
                        }`}
                      >
                        <span className={`absolute left-0 top-2 bottom-2 w-[3px] rounded-r bg-accent transition-opacity ${on ? "opacity-100" : "opacity-0"} hidden lg:block`} />
                        {t.label}
                      </button>
                    );
                  })}
                </div>
              );
            })}
          </nav>
        </aside>

        <div className="min-w-0 flex-1" role="tabpanel" aria-label={current.label}>{current.render()}</div>
      </div>

      <p className="mt-8 text-center text-xs text-muted">
        Shelf ingests only sources you are permitted to read. See the README for the full sourcing policy.
      </p>
    </main>
  );
}
