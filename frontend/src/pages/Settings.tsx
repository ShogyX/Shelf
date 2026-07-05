import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Badge, Button, Card, CardHeader, Disclosure, FormField, inputCls, Modal, SectionHeader, Select, SettingRow, Spinner, StatusChip, Toggle } from "../components/ui";
import { MetadataProvidersCard, AcquisitionCard, ReadingAppsCard } from "../components/IntegrationsManager";
import { ChannelsCard, EventPrefsCard, AdminNotifyCard } from "../components/settings/NotificationCards";
import StatisticsPanel from "../components/StatisticsPanel";
import BookshelvesPanel from "../components/settings/BookshelvesPanel";
import IssuesPanel from "../components/IssuesPanel";
import InsightsPanel from "../components/InsightsPanel";
import StorageSettings from "../components/StorageSettings";
import { SystemConfigCard } from "../components/SystemSettings";
import LayoutSettings from "../components/catalog/LayoutSettings";
import FeaturedSettings from "../components/catalog/FeaturedSettings";
import ThemePicker from "../components/ThemePicker";
import { UsersPanel } from "./Users";
import { api, BackupEntry, RestoreMode, RestorePlan } from "../api/client";
import { qk } from "../api/queryKeys";
import { useApp, AUDIO_SPEEDS } from "../store";
import { setLocale, normalizeLocale } from "../i18n";
import { perfModeEnabled, setPerfMode, isPerfModeExplicit } from "../lib/perfMode";
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
  const { t } = useTranslation();
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
      <CardHeader title={t("settings.crawl.title")} hint={t("settings.crawl.hint")} />
      <div className="flex flex-wrap items-end gap-x-5 gap-y-3">
        <Field label={t("settings.crawl.cycleInterval")}>
          <div className="flex items-center gap-2">{num("tick_seconds")}<span className="text-xs text-muted">{t("settings.crawl.unitSeconds")}</span></div>
        </Field>
        <Field label={t("settings.crawl.chaptersPerCycle")}>{num("chapters_per_tick")}</Field>
        <Field label={t("settings.crawl.parallelFetches")}>{num("parallel_fetches")}</Field>
        <Field label={t("settings.crawl.checkEvery")}>
          <div className="flex items-center gap-2">{num("refresh_hours")}<span className="text-xs text-muted">{t("settings.crawl.unitHours")}</span></div>
        </Field>
        <div className="flex items-center gap-2">
          <Button size="sm" variant="primary" disabled={save.isPending || !form}
                  onClick={() => form && save.mutate(form)}>
            {save.isPending ? t("common.saving") : t("common.save")}
          </Button>
          {saved && <Badge tone="green">{t("settings.crawl.applied")}</Badge>}
          {form && JSON.stringify(form) !== JSON.stringify(MODERATE) && (
            <button className="text-xs text-muted underline hover:text-text"
                    onClick={() => setForm({ ...MODERATE })}>
              {t("settings.crawl.resetModerate")}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function CrawlIdentityCard() {
  const { t } = useTranslation();
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
      <CardHeader title={t("settings.crawlIdentity.title")} hint={t("settings.crawlIdentity.hint")} />
      <div className="space-y-3">
        <Field label={t("settings.crawlIdentity.userAgent")}>
          <input
            type="text"
            value={form?.user_agent ?? ""}
            placeholder="ShelfReader/0.1 (+https://example.org/shelf; polite-self-host-ingester)"
            onChange={(e) => setForm((f) => (f ? { ...f, user_agent: e.target.value } : f))}
            className={inputCls}
          />
        </Field>
        <Field label={t("settings.crawlIdentity.contactEmail")}>
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
            {save.isPending ? t("common.saving") : t("common.save")}
          </Button>
          {saved && <Badge tone="green">{t("settings.crawl.applied")}</Badge>}
        </div>
      </div>
      <p className="mt-2 text-xs text-muted">
        {t("settings.crawlIdentity.envNote")} (<code>SHELF_USER_AGENT</code> /{" "}
        <code>SHELF_CONTACT_EMAIL</code>.)
      </p>
    </Card>
  );
}

function BlocklistCard() {
  const { t } = useTranslation();
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
        title={<>{t("settings.blocklist.title")} <span className="text-sm font-normal text-muted">· {t("settings.blocklist.count", { count: items.length })}</span></>}
        hint={t("settings.blocklist.hint")} />
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
              {t("settings.blocklist.unblock")}
            </Button>
          </div>
        ))}
      </div>
    </Card>
  );
}

function IndexingCard() {
  const { t } = useTranslation();
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
      <CardHeader title={t("settings.indexing.title")} hint={t("settings.indexing.hint")} />
      <Field label={t("settings.indexing.idleLabel")}>
        <div className="flex items-center gap-2">
          <input
            type="number"
            min={1}
            value={idle ?? ""}
            onChange={(e) => setIdle(Math.max(1, Number(e.target.value) || 1))}
            className={`${inputCls} w-28!`}
          />
          <span className="text-xs text-muted">{t("settings.indexing.idlePages")}</span>
          <Button
            size="sm"
            variant="primary"
            disabled={save.isPending || idle == null}
            onClick={() => save.mutate()}
          >
            {save.isPending ? t("common.saving") : t("common.save")}
          </Button>
          {saved && <Badge tone="green">{t("settings.indexing.saved")}</Badge>}
        </div>
      </Field>
      <p className="mt-2 text-xs text-muted">
        {t("settings.indexing.footPre")}{" "}
        <span className="text-text">{t("settings.indexing.sources")}</span>{t("settings.indexing.footMid")}{" "}
        <span className="text-text">{t("settings.indexing.jobs")}</span>.
      </p>
    </Card>
  );
}

/** Admin: the shared SMTP server every user sends Kindle/email through. */
function GlobalSmtpCard() {
  const { t } = useTranslation();
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
        title={t("settings.smtp.title")}
        hint={t("settings.smtp.hint")}
        badge={<StatusChip tone={smtp.data?.configured ? "success" : "warning"}>
          {smtp.data?.configured ? t("settings.smtp.configured") : t("settings.smtp.notConfigured")}
        </StatusChip>} />
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label={t("settings.smtp.host")}>
          <input className={inputCls} placeholder="smtp.gmail.com"
            value={form.smtp_host} onChange={(e) => set("smtp_host", e.target.value)} />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label={t("settings.smtp.port")}>
            <input className={inputCls} inputMode="numeric"
              value={form.smtp_port} onChange={(e) => set("smtp_port", e.target.value)} />
          </Field>
          <Field label={t("settings.smtp.security")}>
            <select className={inputCls} value={form.smtp_security}
              onChange={(e) => set("smtp_security", e.target.value)}>
              <option value="starttls">STARTTLS</option>
              <option value="ssl">SSL</option>
              <option value="none">{t("settings.smtp.securityNone")}</option>
            </select>
          </Field>
        </div>
        <Field label={t("settings.smtp.username")}>
          <input className={inputCls} autoComplete="off"
            value={form.smtp_username} onChange={(e) => set("smtp_username", e.target.value)} />
        </Field>
        <Field label={pwSet ? t("settings.smtp.passwordSaved") : t("settings.smtp.password")}>
          <input className={inputCls} type="password" autoComplete="new-password"
            placeholder={pwSet ? "••••••••" : ""}
            value={form.smtp_password} onChange={(e) => set("smtp_password", e.target.value)} />
        </Field>
        <Field label={t("settings.smtp.fromAddress")}>
          <input className={inputCls} type="email" placeholder="shelf@example.com"
            value={form.smtp_from} onChange={(e) => set("smtp_from", e.target.value)} />
        </Field>
      </div>
      <div className="mt-3 flex justify-end">
        <Button variant="primary" disabled={save.isPending} onClick={() => save.mutate()}>
          {saved ? t("settings.savedCheck") : save.isPending ? t("common.saving") : t("common.save")}
        </Button>
      </div>
    </Card>
  );
}

function KindleCard() {
  const { t } = useTranslation();
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
        title={t("settings.kindle.title")}
        hint={t("settings.kindle.hint")}
        badge={<Badge tone={ready ? "green" : "amber"}>{ready ? t("settings.kindle.emailReady") : t("settings.kindle.emailNotSetUp")}</Badge>} />
      {ready && settings.data?.smtp_from && (
        <p className="mb-3 text-xs text-muted">{t("settings.kindle.sendsFrom", { address: settings.data.smtp_from })}</p>
      )}

      <div className="grid gap-3 sm:grid-cols-2">
        <Field label={t("settings.kindle.kindleEmail")}>
          <input className={inputCls} type="email" placeholder="device@kindle.com"
            value={kindleEmail} onChange={(e) => setKindleEmail(e.target.value)} />
        </Field>
        <Field label={t("settings.kindle.personalEmail")}>
          <input className={`${inputCls} opacity-70`} type="email" value={me?.email ?? ""}
            readOnly disabled placeholder={t("settings.kindle.personalPlaceholder")} />
          <p className="mt-1 text-[11px] text-muted">{t("settings.kindle.personalHint")}</p>
        </Field>
      </div>

      <div className="mt-3 flex justify-end">
        <Button variant="primary" onClick={save}>{saved ? t("settings.savedCheck") : t("common.save")}</Button>
      </div>
    </Card>
  );
}

function BookCatalogCard() {
  const { t } = useTranslation();
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
      <CardHeader title={t("settings.bookCatalog.title")} hint={t("settings.bookCatalog.hint")} />
      {d && (
        <div className="mb-3 text-xs text-muted">
          {t("settings.bookCatalog.rowsPhase", { rows: d.book_rows.toLocaleString() })} <b>{d.phase}</b>
          {d.last_full_at ? t("settings.bookCatalog.lastFullPass", { when: new Date(d.last_full_at).toLocaleString() }) : ""}
        </div>
      )}
      {form && (
        <div className="space-y-3">
          <Toggle
            checked={form.enabled}
            onChange={(v) => setForm({ ...form, enabled: v })}
            label={t("settings.bookCatalog.enabled")}
          />
          <div className="flex flex-wrap items-end gap-x-5 gap-y-3">
            <Field label={t("settings.bookCatalog.hotSetCap")}>
              <input
                type="number"
                min={0}
                value={form.hot_set_cap}
                onChange={(e) => setForm({ ...form, hot_set_cap: e.target.value })}
                className={`${inputCls} w-32!`}
              />
            </Field>
            <Field label={t("settings.bookCatalog.closeness")}>
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
                {save.isPending ? t("common.saving") : t("common.save")}
              </Button>
              {saved && <Badge tone="green">{t("settings.bookCatalog.saved")}</Badge>}
              <Button size="sm" variant="outline" disabled={syncing} onClick={syncNow}>
                {syncing ? t("settings.bookCatalog.seeding") : t("settings.bookCatalog.syncNow")}
              </Button>
            </div>
          </div>
        </div>
      )}
    </Card>
  );
}

const ROUTE_LABEL_KEYS: Record<string, string> = {
  torrent: "settings.fetchPriority.routeTorrent",
  pipeline: "settings.fetchPriority.routePipeline",
  libgen: "settings.fetchPriority.routeLibgen",
  web_index: "settings.fetchPriority.routeWebIndex",
  readarr: "settings.fetchPriority.routeReadarr",
  kapowarr: "settings.fetchPriority.routeKapowarr",
};

function FetchPriorityCard() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: qk.fetchPriority(), queryFn: api.getFetchPriority });
  const [order, setOrder] = useState<string[] | null>(null);
  const [saved, setSaved] = useState(false);
  // Acquisition order is a single GLOBAL order (admin-only) — seed from the global list.
  useEffect(() => {
    if (q.data && order === null) setOrder(q.data.global);
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

  async function save() {
    if (!order) return;
    await api.setGlobalFetchPriority(order);
    await qc.invalidateQueries({ queryKey: qk.fetchPriority() });
    setSaved(true);
    setTimeout(() => setSaved(false), 2000);
  }

  return (
    <Card className="mb-4 p-4">
      <CardHeader title={t("settings.fetchPriority.title")} hint={t("settings.fetchPriority.hint")} />
      {order && (
        <div className="space-y-1.5">
          {order.map((r, i) => (
            <div
              key={r}
              className="flex items-center justify-between gap-2 rounded-lg border border-border p-2"
            >
              <span className="text-sm">
                <span className="mr-2 text-xs text-muted">{i + 1}.</span>
                {ROUTE_LABEL_KEYS[r] ? t(ROUTE_LABEL_KEYS[r]) : r}
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
        <Button size="sm" variant="primary" disabled={!order} onClick={() => save()}>
          {t("settings.fetchPriority.setGlobal")}
        </Button>
        {saved && <Badge tone="green">{t("settings.fetchPriority.globalSaved")}</Badge>}
      </div>
    </Card>
  );
}

/** Admin-only: how often Shelf re-attempts titles it has marked unavailable, wired to the shared
 *  system-config get/PUT (same query key + endpoint AutoBackupSection uses). */
function MissingRecheckCard() {
  const { t } = useTranslation();
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
      <CardHeader title={t("settings.missingRecheck.title")} hint={t("settings.missingRecheck.hint")} />
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label={t("settings.missingRecheck.everyDays")}>
          <input type="number" min={1} className={inputCls} value={f.missing_recheck_days as number}
            onChange={(e) => setF({ ...f, missing_recheck_days: Number(e.target.value) })} />
        </Field>
        <Field label={t("settings.missingRecheck.perRun")}>
          <input type="number" min={1} className={inputCls} value={f.missing_recheck_batch as number}
            onChange={(e) => setF({ ...f, missing_recheck_batch: Number(e.target.value) })} />
        </Field>
      </div>
      <div className="mt-3 flex items-center justify-between gap-2 py-1">
        <span className="text-xs text-muted">
          {t("settings.missingRecheck.autoRequestSeries")}
        </span>
        <Toggle checked={!!f.auto_request_series}
          onChange={(b) => setF({ ...f, auto_request_series: b })} label="" />
      </div>
      <div className="mt-3 flex items-center gap-2">
        <Button variant="primary" disabled={save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? t("common.saving") : t("common.save")}
        </Button>
        {saved && <Badge tone="green">{t("settings.missingRecheck.saved")}</Badge>}
        {save.isError && <span className="text-sm text-red-500">{(save.error as Error).message}</span>}
      </div>
    </Card>
  );
}

/** Per-user 18+ opt-in. Only the categories an admin has unlocked (the gate) can be turned on;
 *  if the admin has disabled 18+ entirely there's nothing to opt into. */
function AdultContentCard() {
  const { t } = useTranslation();
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
        title={t("settings.adult.title")}
        hint={t("settings.adult.hint")}
        badge={<Badge tone="red">18+</Badge>} />
      {gate.length === 0 ? (
        <p className="text-sm text-muted">
          {t("settings.adult.disabled")}
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
                  title={on ? t("settings.adult.hideCat", { cat }) : t("settings.adult.showCat", { cat })}
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

// Per-account audiobook defaults. The player reads these (initial speed, skip jumps, auto-advance)
// and writes the speed back when changed mid-listen, so the last-used speed sticks across books.
function ListeningCard() {
  const { t } = useTranslation();
  const prefs = useApp((s) => s.prefs);
  const setPrefs = useApp((s) => s.setPrefs);
  const skipOpts = [5, 10, 15, 30, 45, 60].map((n) => ({ value: String(n), label: `${n}s` }));
  return (
    <Card className="mb-4 p-4">
      <CardHeader
        title={t("settings.listening.title")}
        desc={t("settings.listening.desc")}
        badge={<Badge tone="violet">{t("settings.listening.badge")}</Badge>} />
      <div>
        <SettingRow label={t("settings.listening.playbackSpeed")}
          hint={t("settings.listening.playbackSpeedHint")}>
          <div className="flex flex-wrap gap-1.5">
            {AUDIO_SPEEDS.map((r) => (
              <button
                key={r}
                type="button"
                onClick={() => setPrefs({ audioSpeed: r })}
                className={`rounded-md border px-2.5 py-1 text-xs font-medium transition ${
                  prefs.audioSpeed === r
                    ? "border-accent bg-accent text-accent-fg"
                    : "border-border text-muted hover:bg-surface-2"
                }`}
              >
                {r}×
              </button>
            ))}
          </div>
        </SettingRow>
        <SettingRow label={t("settings.listening.skipBack")} hint={t("settings.listening.skipBackHint")}>
          <div className="w-24">
            <Select value={String(prefs.audioSkipBack)}
              onChange={(v) => setPrefs({ audioSkipBack: Number(v) })} options={skipOpts} />
          </div>
        </SettingRow>
        <SettingRow label={t("settings.listening.skipForward")} hint={t("settings.listening.skipForwardHint")}>
          <div className="w-24">
            <Select value={String(prefs.audioSkipForward)}
              onChange={(v) => setPrefs({ audioSkipForward: Number(v) })} options={skipOpts} />
          </div>
        </SettingRow>
        <SettingRow label={t("settings.listening.autoplayNext")}
          hint={t("settings.listening.autoplayNextHint")}>
          <Toggle checked={prefs.audioAutoplayNext}
            onChange={(v) => setPrefs({ audioAutoplayNext: v })} />
        </SettingRow>
      </div>
    </Card>
  );
}

/** Self-service Account tab — any user edits their own login username, display name, email, and
 *  password. Distinct from the admin Users tab (which manages everyone). */
function AccountPanel() {
  const { t } = useTranslation();
  const me = useCurrentUser();
  const refresh = useAuth((s) => s.refresh);
  const toast = useApp((s) => s.toast);
  const [username, setUsername] = useState(me?.username ?? "");
  const [displayName, setDisplayName] = useState(me?.display_name ?? "");
  const [email, setEmail] = useState(me?.email ?? "");
  const [curPw, setCurPw] = useState("");
  const [newPw, setNewPw] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!me) return;
    setUsername(me.username); setDisplayName(me.display_name ?? ""); setEmail(me.email ?? "");
  }, [me?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const dirty = username.trim() !== (me?.username ?? "")
    || displayName.trim() !== (me?.display_name ?? "")
    || email.trim() !== (me?.email ?? "");

  const run = async (p: Promise<unknown>, ok: string, after?: () => void) => {
    setErr(null); setBusy(true);
    try { await p; await refresh(); toast(ok, "success"); after?.(); }
    catch (e) { setErr((e as Error).message); }
    finally { setBusy(false); }
  };

  return (
    <>
      <Card className="mb-4 p-4">
        <CardHeader title={t("settings.account.title")} desc={t("settings.account.desc")} />
        {err && <p className="mb-3 rounded-xl border border-red-400/30 bg-red-500/10 px-3 py-2 text-sm text-red-500">{err}</p>}
        <div className="grid gap-3 sm:grid-cols-2">
          <FormField label={t("auth.username")} hint={t("settings.account.usernameHint")}>
            <input className={inputCls} value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" />
          </FormField>
          <FormField label={t("settings.account.displayName")}>
            <input className={inputCls} value={displayName} onChange={(e) => setDisplayName(e.target.value)} placeholder={me?.username} />
          </FormField>
          <FormField label={t("auth.email")} hint={t("settings.account.emailHint")}>
            <input className={inputCls} type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder={t("settings.account.emailPlaceholder")} />
          </FormField>
        </div>
        <div className="mt-3 flex justify-end">
          <Button variant="primary" disabled={busy || !dirty || !username.trim()}
            onClick={() => run(api.updateMe({ username: username.trim(), display_name: displayName.trim(), email: email.trim() || null }), t("settings.account.profileSaved"))}>
            {t("common.save")}
          </Button>
        </div>
      </Card>

      <LanguageCard />

      <Card className="mb-4 p-4">
        <CardHeader title={t("settings.account.changePassword")} desc={t("settings.account.changePasswordDesc")} />
        <div className="grid gap-3 sm:grid-cols-2">
          <FormField label={t("settings.account.currentPassword")}>
            <input className={inputCls} type="password" value={curPw} onChange={(e) => setCurPw(e.target.value)} autoComplete="current-password" />
          </FormField>
          <FormField label={t("auth.newPassword")} hint={t("settings.account.newPasswordHint")}>
            <input className={inputCls} type="password" value={newPw} onChange={(e) => setNewPw(e.target.value)} autoComplete="new-password" />
          </FormField>
        </div>
        <div className="mt-3 flex justify-end">
          <Button variant="primary" disabled={busy || !curPw || newPw.length < 8}
            onClick={() => run(api.updateMe({ password: newPw, current_password: curPw }), t("settings.account.passwordChanged"), () => { setCurPw(""); setNewPw(""); })}>
            {t("settings.account.changePassword")}
          </Button>
        </div>
      </Card>
    </>
  );
}

/** UI-language switcher. Changing it applies locally at once (i18n.changeLanguage + localStorage)
 *  and persists to the profile optimistically — it's fine if the user is briefly not authenticated;
 *  the local switch + localStorage still take effect. Reflects the current value from `user.locale`. */
function LanguageCard() {
  const { t } = useTranslation();
  const me = useCurrentUser();
  const refresh = useAuth((s) => s.refresh);
  const toast = useApp((s) => s.toast);
  const current = normalizeLocale(me?.locale);
  const change = (code: string) => {
    const locale = normalizeLocale(code);
    setLocale(locale);
    api.updateMe({ locale }).then(() => refresh()).catch(() => {
      /* not signed in / offline — the local switch above still applies */
    });
    toast(t("settings.language.saved"), "success");
  };
  return (
    <Card className="mb-4 p-4">
      <CardHeader title={t("settings.language.title")} desc={t("settings.language.desc")} />
      <div className="max-w-xs">
        <Select
          label={t("settings.language.label")}
          value={current}
          onChange={change}
          options={[
            { value: "en", label: t("settings.language.english") },
            { value: "no", label: t("settings.language.norwegian") },
          ]}
        />
      </div>
    </Card>
  );
}

/** Device-local Performance mode toggle (#1): drops the GPU-heavy blur/aurora/grain effects. Stored
 *  per-device in localStorage (a laptop and a desktop want different answers), NOT on the account. */
function PerformanceCard() {
  const { t } = useTranslation();
  const [on, setOn] = useState(() => perfModeEnabled());
  const [explicit, setExplicit] = useState(() => isPerfModeExplicit());
  return (
    <Card className="mb-4 p-4">
      <CardHeader title={t("settings.performance.title")} hint={t("settings.performance.hint")} />
      <div className="flex items-center justify-between gap-3">
        <span className="text-sm text-text">
          {t("settings.performance.toggleLabel")}
          {!explicit && (
            <span className="ml-2 text-xs text-muted">{t("settings.performance.autoNote")}</span>
          )}
        </span>
        <Toggle checked={on} onChange={(v) => { setOn(v); setExplicit(true); setPerfMode(v); }} />
      </div>
    </Card>
  );
}

function AppearancePanel() {
  const { t } = useTranslation();
  const isAdmin = useIsAdmin();
  return (
    <>
      <SectionHeader>{t("settings.appearance.heading")}</SectionHeader>
      <Card className="mb-4 p-4">
        <CardHeader title={t("settings.appearance.themeTitle")} desc={t("settings.appearance.themeDesc")} />
        <ThemePicker columns={4} />
      </Card>

      <PerformanceCard />

      <SectionHeader>{t("settings.appearance.listening")}</SectionHeader>
      <ListeningCard />

      <SectionHeader>{t("settings.appearance.delivery")}</SectionHeader>
      <KindleCard />
      <AdultContentCard />

      {isAdmin && (
        <>
          <SectionHeader>{t("settings.appearance.discovery")}</SectionHeader>
          <FeaturedSettings />
          <Disclosure title={t("settings.discovery.layoutTitle")}
            subtitle={t("settings.discovery.layoutSubtitle")}>
            <LayoutSettings />
          </Disclosure>
        </>
      )}
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
/** Admin: Cloudflare Access (#16). When set, creating/approving a Shelf user also adds their email to
 *  a Zero Trust Access application policy, and deleting/rejecting removes it — so the admin never has
 *  to touch the Cloudflare dashboard. The API token is write-only (never returned). */
function CloudflareAccessCard() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useApp((s) => s.toast);
  const q = useQuery({ queryKey: qk.cloudflareAccess(), queryFn: api.getCloudflareAccess });
  const [f, setF] = useState<{ account_id: string; app_id: string; policy_id: string; api_token: string; enabled: boolean } | null>(null);
  useEffect(() => {
    if (q.data && f === null)
      setF({ account_id: q.data.account_id, app_id: q.data.app_id, policy_id: q.data.policy_id, api_token: "", enabled: q.data.enabled });
  }, [q.data, f]);
  const save = useMutation({
    mutationFn: () => api.setCloudflareAccess({ ...f! }),   // blank api_token preserves the stored one
    onSuccess: () => { qc.invalidateQueries({ queryKey: qk.cloudflareAccess() }); setF((p) => p && { ...p, api_token: "" }); toast(t("cloudflare.saved"), "success"); },
    onError: (e) => toast((e as Error).message, "error"),
  });
  const test = useMutation({
    mutationFn: () => api.testCloudflareAccess(),
    onSuccess: () => toast(t("cloudflare.testOk"), "success"),
    onError: (e) => toast((e as Error).message, "error"),
  });
  if (!f) return null;
  const set = (k: keyof typeof f, v: string | boolean) => setF({ ...f, [k]: v });
  return (
    <Card className="mb-4 p-4">
      <CardHeader title={t("cloudflare.title")} hint={t("cloudflare.hint")} />
      <div className="space-y-2.5">
        <FormField label={t("cloudflare.accountId")}>
          <input className={inputCls} value={f.account_id} onChange={(e) => set("account_id", e.target.value)} spellCheck={false} />
        </FormField>
        <FormField label={t("cloudflare.appId")}>
          <input className={inputCls} value={f.app_id} onChange={(e) => set("app_id", e.target.value)} spellCheck={false} />
        </FormField>
        <FormField label={t("cloudflare.policyId")}>
          <input className={inputCls} value={f.policy_id} onChange={(e) => set("policy_id", e.target.value)} spellCheck={false} />
        </FormField>
        <FormField label={t("cloudflare.token")}>
          <input type="password" className={inputCls} value={f.api_token} autoComplete="off" spellCheck={false}
            placeholder={q.data?.api_token_set ? "••••••••" : ""} onChange={(e) => set("api_token", e.target.value)} />
        </FormField>
        <div className="flex flex-wrap items-center justify-between gap-3 pt-1">
          <label className="flex items-center gap-2 text-sm text-text">
            <Toggle checked={f.enabled} onChange={(v) => set("enabled", v)} />{t("cloudflare.enabled")}
          </label>
          <div className="flex gap-2">
            <Button size="sm" variant="ghost" disabled={test.isPending} onClick={() => test.mutate()}>
              {test.isPending ? t("cloudflare.testing") : t("cloudflare.test")}
            </Button>
            <Button size="sm" variant="primary" disabled={save.isPending} onClick={() => save.mutate()}>
              {save.isPending ? "…" : t("common.save")}
            </Button>
          </div>
        </div>
      </div>
    </Card>
  );
}

function IntegrationsPanel() {
  return (
    <>
      <MetadataProvidersCard />
      {/* Cloudflare solver now renders as a provider box inside AcquisitionCard, next to VirusTotal. */}
      <AcquisitionCard />
      <ReadingAppsCard />
      <CloudflareAccessCard />
    </>
  );
}

/** Admin: which languages Shelf grabs + stocks (#14). Toggling a supported language saves immediately
 *  and drives the crawler/metadata queries + the language badges. Only English + Norwegian today. */
function ContentLanguagesCard() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: qk.contentLanguages(), queryFn: api.getContentLanguages });
  const [saved, setSaved] = useState(false);
  const enabled = new Set(q.data?.enabled ?? []);
  const save = useMutation({
    mutationFn: (langs: string[]) => api.setContentLanguages(langs),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.contentLanguages() });
      qc.invalidateQueries({ queryKey: qk.catalog() });   // language-gated catalog shifts
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    },
  });
  const toggle = (code: string) => {
    const next = new Set(enabled);
    next.has(code) ? next.delete(code) : next.add(code);
    save.mutate([...next]);
  };
  return (
    <Card className="mb-4 p-4">
      <CardHeader title={t("settings.contentLanguages.title")} hint={t("settings.contentLanguages.hint")} />
      <div className="flex flex-wrap items-center gap-1.5">
        {(q.data?.supported ?? []).map(({ code, name }) => {
          const on = enabled.has(code);
          return (
            <button
              key={code}
              type="button"
              disabled={save.isPending}
              onClick={() => toggle(code)}
              aria-pressed={on}
              className={`rounded-full border px-2.5 py-1 text-xs transition disabled:opacity-50 ${
                on ? "border-accent bg-accent/10 text-text" : "border-border text-muted hover:bg-surface-2"
              }`}
            >
              {on ? "✓ " : ""}{name}
            </button>
          );
        })}
        {saved && <Badge tone="green">{t("settings.contentLanguages.saved")}</Badge>}
      </div>
    </Card>
  );
}

/** Acquisition tab (admin-only): the global content-language set, the global acquisition-route order,
 *  missing-recheck cadence, the content blocklist, and list-import config. */
function AcquisitionPanel() {
  return (
    <>
      <ContentLanguagesCard />
      <FetchPriorityCard />
      <MissingRecheckCard />
      <BlocklistCard />
      <SystemConfigCard groups={["List imports"]} />
    </>
  );
}

const BACKUP_LEVELS: { value: "settings" | "data" | "full"; labelKey: string; detailKey: string }[] = [
  { value: "settings", labelKey: "settings.backup.levelSettings", detailKey: "settings.backup.levelSettingsDetail" },
  { value: "data", labelKey: "settings.backup.levelData", detailKey: "settings.backup.levelDataDetail" },
  { value: "full", labelKey: "settings.backup.levelFull", detailKey: "settings.backup.levelFullDetail" },
];

const MODE_META: { value: RestoreMode; labelKey: string; hintKey: string }[] = [
  { value: "skip", labelKey: "settings.backup.modeSkip", hintKey: "settings.backup.modeSkipHint" },
  { value: "merge", labelKey: "settings.backup.modeMerge", hintKey: "settings.backup.modeMergeHint" },
  { value: "replace", labelKey: "settings.backup.modeReplace", hintKey: "settings.backup.modeReplaceHint" },
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
  const { t } = useTranslation();
  return (
    <div className="inline-flex rounded-[11px] border border-[var(--hair-strong,var(--border))] bg-surface-2 p-0.5" role="group" aria-label={t("settings.backup.restoreModeAria")}>
      {MODE_META.map((m) => {
        const on = value === m.value;
        return (
          <button
            key={m.value}
            type="button"
            disabled={disabled}
            aria-pressed={on}
            title={t(m.hintKey)}
            onClick={() => onChange(m.value)}
            className={`rounded-[9px] px-3 py-1.5 text-xs font-semibold transition ${
              on
                ? m.value === "replace"
                  ? "bg-red-500 text-white shadow-sm"
                  : "bg-accent text-accent-fg shadow-sm"
                : "text-[var(--text-soft,var(--muted))] hover:text-text"
            } ${disabled ? "cursor-not-allowed opacity-40" : ""}`}
          >
            {t(m.labelKey)}
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
  const { t } = useTranslation();
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
      toast(t("settings.backup.restoredToast", { count: n, level: r.level, warnings: w }), "success");
      setTimeout(() => window.location.reload(), 1200);
    },
    onError: (e) => toast((e as Error).message, "error"),
  });

  const p = planQ.data;
  const sections = p ? [...p.sections, p.media] : [];
  const willChange = sections.filter((s) => modes[s.key] && modes[s.key] !== "skip");
  return (
    <Modal
      title={t("settings.backup.restoreModalTitle")}
      width="w-[44rem]"
      onClose={() => !commit.isPending && onClose()}
      footer={
        <>
          <Button variant="ghost" disabled={commit.isPending} onClick={onClose}>{t("common.cancel")}</Button>
          <Button
            variant={willChange.some((s) => modes[s.key] === "replace") ? "danger" : "primary"}
            disabled={commit.isPending || !p || willChange.length === 0}
            onClick={() => commit.mutate()}
          >
            {commit.isPending
              ? t("settings.backup.restoring")
              : willChange.length === 0
                ? t("settings.backup.nothingSelected")
                : t("settings.backup.restoreN", { count: willChange.length })}
          </Button>
        </>
      }
    >
      {planQ.isLoading || !p ? (
        <Spinner label={t("settings.backup.readingBackup")} />
      ) : (
        <>
          <p className="text-sm text-[var(--text-soft,var(--muted))]">
            {t("settings.backup.fromA")} <b className="text-text">{p.manifest.level}</b> {t("settings.backup.backupWord")}
            {p.manifest.created_at ? t("settings.backup.takenAt", { when: new Date(p.manifest.created_at).toLocaleString() }) : ""}.
            {p.target_empty
              ? ` ${t("settings.backup.instanceEmpty")}`
              : ` ${t("settings.backup.instanceHasData")}`}
          </p>
          <div className="mt-3 space-y-2">
            {sections.map((s) => {
              const rows = "backup_rows" in s ? s.backup_rows : (s as any).backup_files;
              const here = "target_rows" in s ? (s as any).target_rows : undefined;
              const unit = "backup_files" in s ? t("settings.backup.unitFiles") : t("settings.backup.unitRows");
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
                              <Badge tone="violet">{t("settings.backup.badgeBackup", { count: rows.toLocaleString(), unit })}</Badge>
                              {here != null && <Badge>{t("settings.backup.badgeHere", { count: here.toLocaleString(), unit })}</Badge>}
                            </>
                          : <Badge>{t("settings.backup.notInBackup")}</Badge>}
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
            <b className="text-text">{t("settings.backup.modeSkip")}</b> {t("settings.backup.legendSkip")} · <b className="text-text">{t("settings.backup.modeMerge")}</b> {t("settings.backup.legendMerge")} · <b className="text-red-500">{t("settings.backup.modeReplace")}</b> {t("settings.backup.legendReplace")} {t("settings.backup.legendAtomic")}
          </p>
        </>
      )}
    </Modal>
  );
}

function BackupRow({ b, onRestore, onDelete, deleting }: {
  b: BackupEntry; onRestore: () => void; onDelete: () => void; deleting: boolean;
}) {
  const { t } = useTranslation();
  const building = b.status === "building";
  const failed = b.status === "failed";
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-[var(--hair,var(--border))] bg-surface p-3">
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-1.5">
          {b.level && <Badge tone="violet">{b.level}</Badge>}
          <Badge tone={b.origin === "uploaded" ? "amber" : "default"}>{b.origin}</Badge>
          {building && <StatusChip tone="info">{t("settings.backup.building")}</StatusChip>}
          {failed && <StatusChip tone="danger">{t("settings.backup.failed")}</StatusChip>}
          {b.valid && !b.restorable && <StatusChip tone="warning">{t("settings.backup.newerVersion")}</StatusChip>}
          <span className="truncate text-sm font-medium text-text" title={b.name}>{b.name}</span>
        </div>
        <div className="mt-0.5 text-xs text-[var(--text-soft,var(--muted))]">
          {fmtBytes(b.size_bytes)}
          {b.created_at ? ` · ${new Date(b.created_at).toLocaleString()}` : ""}
          {b.media_files ? ` · ${t("settings.backup.mediaFilesSuffix", { count: b.media_files.toLocaleString() })}` : ""}
          {failed && b.error ? ` · ${b.error}` : ""}
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-1">
        {building ? (
          <Spinner />
        ) : (
          <>
            <Button size="sm" variant="primary" disabled={!b.restorable} onClick={onRestore}
              title={b.restorable ? t("settings.backup.restoreFromThis")
                : t("settings.backup.newerVersionTip")}>
              {t("settings.backup.restore")}
            </Button>
            {b.valid && (
              <Button size="sm" variant="ghost"
                onClick={() => { window.location.href = api.storedBackupUrl(b.name); }}>
                {t("common.download")}
              </Button>
            )}
          </>
        )}
        <Button size="sm" variant="danger" disabled={deleting} onClick={onDelete} title={t("settings.backup.delete")}>🗑</Button>
      </div>
    </div>
  );
}

/** Scheduled-backup controls, wired to the shared system-config get/PUT (same query key + endpoint
 *  SystemSettings uses), so editing here round-trips with that page. */
function AutoBackupSection() {
  const { t } = useTranslation();
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
      <CardHeader title={t("settings.autoBackup.title")} desc={t("settings.autoBackup.desc")}
        hint={t("settings.autoBackup.hint")} />
      <div className="mb-3.5 flex items-center justify-between gap-4 rounded-xl border border-[var(--hair,var(--border))] bg-surface px-3.5 py-3">
        <div className="min-w-0">
          <div className="text-sm font-semibold text-text">{t("settings.autoBackup.enableTitle")}</div>
          <div className="text-xs text-[var(--text-soft,var(--muted))]">{t("settings.autoBackup.enableSubtext")}</div>
        </div>
        <Toggle checked={!!f.auto_backup_enabled}
          onChange={(b) => setF({ ...f, auto_backup_enabled: b })} label="" />
      </div>
      <div className="grid gap-x-4 sm:grid-cols-3">
        <FormField label={t("settings.autoBackup.level")}>
          <select className={inputCls} value={String(f.auto_backup_level)}
            onChange={(e) => setF({ ...f, auto_backup_level: e.target.value })}>
            {BACKUP_LEVELS.map((l) => (<option key={l.value} value={l.value}>{t(l.labelKey)}</option>))}
          </select>
        </FormField>
        <FormField label={t("settings.autoBackup.intervalHours")}>
          <input type="number" min={1} className={inputCls} value={f.auto_backup_interval_hours as number}
            onChange={(e) => setF({ ...f, auto_backup_interval_hours: Number(e.target.value) })} />
        </FormField>
        <FormField label={t("settings.autoBackup.keepNewest")}>
          <input type="number" min={1} className={inputCls} value={f.auto_backup_keep as number}
            onChange={(e) => setF({ ...f, auto_backup_keep: Number(e.target.value) })} />
        </FormField>
      </div>
      <div className="mt-3 flex items-center gap-2">
        <Button variant="primary" disabled={save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? t("common.saving") : t("settings.autoBackup.saveSchedule")}
        </Button>
        {saved && <Badge tone="green">{t("settings.autoBackup.saved")}</Badge>}
        {save.isError && <span className="text-sm text-red-500">{(save.error as Error).message}</span>}
      </div>
    </Card>
  );
}

function BackupPanel() {
  const { t } = useTranslation();
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
    onError: (e: any) => setMsg(e?.message || t("settings.backup.createError")),
  });
  const upload = useMutation({
    mutationFn: (file: File) => api.uploadBackup(file),
    onSuccess: () => { setMsg(null); refresh(); },
    onError: (e: any) => setMsg(e?.message || t("settings.backup.uploadError")),
  });
  const del = useMutation({
    mutationFn: (name: string) => api.deleteBackup(name),
    onSuccess: refresh,
  });
  const confirm = useConfirm();
  const restoreSnap = useMutation({
    mutationFn: (name: string) => api.restoreDbSnapshot(name),
    onSuccess: () => setMsg(t("settings.backup.snapRestartMsg")),
    onError: (e: any) => setMsg(e?.message || t("settings.backup.restoreError")),
  });
  const delSnap = useMutation({
    mutationFn: (name: string) => api.deleteDbSnapshot(name),
    onSuccess: refresh,
  });
  const onRestoreSnap = async (name: string) => {
    if (await confirm({
      title: t("settings.backup.snapRestoreTitle"),
      message: t("settings.backup.snapRestoreMsg", { name }),
      danger: true, confirmText: t("settings.backup.snapRestoreConfirm"),
    })) restoreSnap.mutate(name);
  };
  const onDeleteSnap = async (name: string) => {
    if (await confirm({
      title: t("settings.backup.snapDeleteTitle"), message: t("settings.backup.snapDeleteMsg", { name }),
      danger: true, confirmText: t("settings.backup.delete"),
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
      <CardHeader title={t("settings.backup.title")} desc={t("settings.backup.desc")}
        hint={t("settings.backup.hint")} />

      <FormField label={t("settings.backup.size")} hint={t(sel.detailKey)}>
        <select className={inputCls} value={level} onChange={(e) => setLevel(e.target.value as any)}>
          {BACKUP_LEVELS.map((l) => (<option key={l.value} value={l.value}>{t(l.labelKey)}</option>))}
        </select>
      </FormField>
      <div className="flex flex-wrap gap-2">
        <Button variant="primary" disabled={create.isPending} onClick={() => create.mutate()}>
          {create.isPending ? t("settings.backup.starting") : t("settings.backup.createBackup")}
        </Button>
        {/* Direct cookie-authed navigation streams even a multi-GB archive to disk without storing it.
            Guard the navigation target on the fixed level allow-list so the value assigned to
            location.href is provably not attacker-controlled. */}
        <Button variant="outline" onClick={() => {
          if (BACKUP_LEVELS.some((l) => l.value === level)) window.location.href = api.backupUrl(level);
        }}>
          {t("settings.backup.downloadDirectly")}
        </Button>
        <Button variant="outline" disabled={upload.isPending} onClick={() => fileRef.current?.click()}>
          {upload.isPending ? t("settings.backup.uploading") : t("settings.backup.uploadBackup")}
        </Button>
        <input ref={fileRef} type="file" accept=".zip,application/zip" className="hidden"
               onChange={onPickUpload} />
      </div>
      {msg && <p className="mt-2 text-xs text-red-500">{msg}</p>}
      {listQ.data && (
        <p className="mt-2 text-xs text-[var(--text-soft,var(--muted))]">{t("settings.backup.freeOnDisk", { size: fmtBytes(listQ.data.free_bytes) })}</p>
      )}

      <div className="mt-5">
        <div className="font-display mb-2 border-b border-[var(--hair,var(--border))] pb-2 text-xs font-semibold uppercase tracking-wide text-[var(--text-soft,var(--muted))]">
          {t("settings.backup.storedBackups")}{backups.length ? ` (${backups.length})` : ""}
        </div>
        {listQ.isLoading ? (
          <Spinner label={t("settings.backup.loadingBackups")} />
        ) : backups.length === 0 ? (
          <p className="text-sm text-[var(--text-soft,var(--muted))]">{t("settings.backup.noBackups")}</p>
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
          {t("settings.backup.dbSnapshots")}{(listQ.data?.db_snapshots?.length ?? 0) ? ` (${listQ.data!.db_snapshots.length})` : ""}
        </div>
        <p className="mb-2 text-xs leading-snug text-[var(--text-soft,var(--muted))]">
          {t("settings.backup.snapExplainPre")} <b className="text-text">{t("settings.backup.snapExplainAll")}</b> {t("settings.backup.snapExplainPost")}
        </p>
        {(listQ.data?.db_snapshots ?? []).length === 0 ? (
          <p className="text-sm text-[var(--text-soft,var(--muted))]">{t("settings.backup.noSnapshots")}</p>
        ) : (
          <div className="space-y-2">
            {listQ.data!.db_snapshots.map((s) => (
              <div key={s.name} className="flex items-center justify-between gap-3 rounded-xl border border-[var(--hair,var(--border))] bg-surface p-3">
                <div className="min-w-0">
                  <div className="truncate font-mono text-xs text-text">{s.name}</div>
                  <div className="mt-0.5 text-[11px] text-[var(--text-soft,var(--muted))]">
                    {fmtBytes(s.size_bytes)}
                    {s.created_at ? ` · ${new Date(s.created_at).toLocaleString()}` : ""}
                    {!s.restorable ? ` · ${t("settings.backup.notDbFile")}` : ""}
                  </div>
                </div>
                <div className="flex shrink-0 gap-1.5">
                  <Button size="sm" variant="outline" disabled={!s.restorable || restoreSnap.isPending}
                    onClick={() => onRestoreSnap(s.name)}>
                    {restoreSnap.isPending && restoreSnap.variables === s.name ? t("settings.backup.restoring") : t("settings.backup.restore")}
                  </Button>
                  <Button size="sm" variant="danger" disabled={delSnap.isPending}
                    onClick={() => onDeleteSnap(s.name)} title={t("settings.backup.snapDeleteTitle")}>✕</Button>
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

/** Collapsed advanced crawl config (indexing tab) — a component so its labels can use i18n. */
function AdvancedCrawlDisclosure() {
  const { t } = useTranslation();
  return (
    <Disclosure title={t("settings.indexing.advancedTitle")} subtitle={t("settings.indexing.advancedSubtitle")}>
      <SystemConfigCard groups={["Crawl defaults", "Comix crawler"]} />
    </Disclosure>
  );
}

/** Collapsed raw telemetry tables (insights tab) — a component so its labels can use i18n. */
function DetailedTelemetryDisclosure() {
  const { t } = useTranslation();
  return (
    <Disclosure title={t("settings.insights.telemetryTitle")} subtitle={t("settings.insights.telemetrySubtitle")}>
      <StatisticsPanel />
    </Disclosure>
  );
}

// `label` and `group` hold i18n KEYS (resolved to the active language at render time), not literals.
type TabDef = { id: string; label: string; icon: string; group: string; admin?: boolean; render: () => React.ReactNode };

// Left-rail groups (redesign): Personal / Library & sources / System — i18n keys.
const SETTINGS_GROUPS = ["settings.groupPersonal", "settings.groupLibrary", "settings.groupSystem"] as const;

const TAB_DEFS: TabDef[] = [
  // Account + preferences merged into one personal tab (#7): profile/password/locale followed by
  // theme, performance and the other appearance/preference cards.
  { id: "account", label: "settings.tabAccount", icon: "👤", group: "settings.groupPersonal", render: () => (
    <>
      <AccountPanel />
      <AppearancePanel />
    </>
  ) },
  { id: "notifications", label: "settings.tabNotifications", icon: "🔔", group: "settings.groupPersonal", render: () => <NotificationsPanel /> },
  // Visible to everyone — a user sees issues they raised; admins (or issues.view_all holders) see all.
  { id: "issues", label: "settings.tabIssues", icon: "🚩", group: "settings.groupPersonal", render: () => <IssuesPanel /> },
  { id: "bookshelves", label: "settings.tabBookshelves", icon: "🗂", group: "settings.groupLibrary", render: () => <BookshelvesPanel /> },
  { id: "acquisition", label: "settings.tabAcquisition", icon: "⤓", group: "settings.groupLibrary", admin: true, render: () => <AcquisitionPanel /> },
  // Operator-wide providers only, so this tab is admin-only.
  { id: "integrations", label: "settings.tabIntegrations", icon: "🔌", group: "settings.groupLibrary", admin: true, render: () => <IntegrationsPanel /> },
  { id: "indexing", label: "settings.tabIndexing", icon: "🌐", group: "settings.groupLibrary", admin: true, render: () => (
    <>
      {/* Commonly-tuned config stays open; advanced + read-only telemetry collapse to cut bloat. */}
      <IndexingCard />
      <CrawlIdentityCard />
      <BookCatalogCard />
      <AdvancedCrawlDisclosure />
    </>
  ) },
  { id: "users", label: "settings.tabUsers", icon: "👥", group: "settings.groupSystem", admin: true, render: () => <UsersPanel /> },
  { id: "storage", label: "settings.tabStorage", icon: "💾", group: "settings.groupSystem", admin: true, render: () => (
    <>
      <StorageSettings />
      <SystemConfigCard groups={["Image cache"]} />
    </>
  ) },
  { id: "backup", label: "settings.tabBackup", icon: "🛡", group: "settings.groupSystem", admin: true, render: () => (
    <>
      <BackupPanel />
      <AutoBackupSection />
      <SystemConfigCard groups={["Logging"]} />
    </>
  ) },
  // Insights = charts over the same data; the raw request/VT/pipeline tables stay under a disclosure.
  { id: "statistics", label: "settings.tabInsights", icon: "📊", group: "settings.groupSystem", admin: true, render: () => (
    <>
      <InsightsPanel />
      <div className="mt-5">
        <DetailedTelemetryDisclosure />
      </div>
    </>
  ) },
];

export default function Settings() {
  const { t } = useTranslation();
  const isAdmin = useIsAdmin();
  // Memoize so `tabs` is a stable reference — otherwise the validity effect below (dep: [tabs])
  // re-runs every render on a fresh array identity.
  const tabs = useMemo(() => TAB_DEFS.filter((td) => !td.admin || isAdmin), [isAdmin]);

  const initial = () => {
    const hash = window.location.hash.replace(/^#/, "");
    const stored = localStorage.getItem("settings-tab") || "";
    const wanted = hash || stored;
    return tabs.some((td) => td.id === wanted) ? wanted : tabs[0].id;
  };
  const [active, setActive] = useState<string>(initial);

  // Keep the selection valid if admin status resolves after first render.
  useEffect(() => {
    if (!tabs.some((td) => td.id === active)) setActive(tabs[0].id);
  }, [tabs, active]);

  const select = (id: string) => {
    setActive(id);
    localStorage.setItem("settings-tab", id);
    history.replaceState(null, "", `#${id}`);
  };

  const current = tabs.find((td) => td.id === active) ?? tabs[0];

  return (
    <main className="page-in mx-auto max-w-6xl px-4 py-8 sm:px-6">
      <h1 className="font-display mb-6 text-3xl font-semibold tracking-tight text-text">{t("settings.title")}</h1>
      <div className="flex flex-col gap-7 lg:flex-row lg:items-start">
        {/* Left rail (sticky on desktop; a horizontal scroll strip on mobile). */}
        <aside className="lg:sticky lg:top-20 lg:w-[216px] lg:shrink-0">
          <nav className="flex gap-1 overflow-x-auto scrollbar-none [mask-image:linear-gradient(to_right,#000_92%,transparent)] lg:flex-col lg:gap-0 lg:[mask-image:none]" aria-label={t("settings.title")}>
            {SETTINGS_GROUPS.map((g) => {
              const items = tabs.filter((td) => td.group === g);
              if (items.length === 0) return null;
              return (
                <div key={g} className="flex shrink-0 gap-1 lg:mb-4 lg:block lg:gap-0">
                  <div className="hidden px-3 pb-2 text-[11px] font-bold uppercase tracking-wider text-muted lg:block">{t(g)}</div>
                  {items.map((td) => {
                    const on = td.id === current.id;
                    return (
                      <button
                        key={td.id}
                        onClick={() => select(td.id)}
                        className={`relative flex shrink-0 items-center gap-2.5 whitespace-nowrap rounded-[10px] px-3 py-2 text-sm font-semibold transition lg:w-full ${
                          on ? "bg-[color-mix(in_srgb,var(--accent)_16%,transparent)] text-text" : "text-muted hover:text-text"
                        }`}
                      >
                        <span className={`absolute left-0 top-2 bottom-2 w-[3px] rounded-r bg-accent transition-opacity ${on ? "opacity-100" : "opacity-0"} hidden lg:block`} />
                        {t(td.label)}
                      </button>
                    );
                  })}
                </div>
              );
            })}
          </nav>
        </aside>

        <div className="min-w-0 flex-1" role="tabpanel" aria-label={t(current.label)}>{current.render()}</div>
      </div>

      <p className="mt-8 text-center text-xs text-muted">
        {t("settings.footer")}
      </p>
    </main>
  );
}
