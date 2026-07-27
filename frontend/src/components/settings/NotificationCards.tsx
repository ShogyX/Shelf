import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";

import { api, NotificationChannel, NotificationEvent } from "../../api/client";
import { qk } from "../../api/queryKeys";
import { X } from "lucide-react";
import {
  Badge, Button, Card, CardHeader, FormField, inputCls, Modal, ProviderCard,
  StatusChip, Toggle,
} from "../ui";

const hhmm = () => new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

type FieldSpec = { name: string; label: string; placeholder?: string; required?: boolean; secret?: boolean };
type KindSpec = { kind: string; label: string; fields: FieldSpec[]; help?: string };

// Guided per-service forms — the backend turns these into an Apprise URL. "Advanced" takes a raw URL.
// `label` is the service's own brand name (not translated); only the help/field copy is localized.
const buildKinds = (t: TFunction): KindSpec[] => [
  { kind: "ntfy", label: "ntfy", help: t("notify.kind.ntfyHelp"),
    fields: [
      { name: "topic", label: t("notify.field.topic"), placeholder: "my-shelf-alerts", required: true },
      { name: "server", label: t("notify.field.serverOptional"), placeholder: "ntfy.sh" },
      { name: "token", label: t("notify.field.accessTokenOptional"), secret: true },
    ] },
  { kind: "pushover", label: "Pushover",
    fields: [
      { name: "user_key", label: t("notify.field.userKey"), required: true, secret: true },
      { name: "token", label: t("notify.field.appToken"), required: true, secret: true },
    ] },
  { kind: "telegram", label: "Telegram", help: t("notify.kind.telegramHelp"),
    fields: [
      { name: "bot_token", label: t("notify.field.botToken"), required: true, secret: true },
      { name: "chat_id", label: t("notify.field.chatId"), required: true },
    ] },
  { kind: "discord", label: "Discord",
    fields: [{ name: "webhook", label: t("notify.field.webhookUrl"), required: true, secret: true,
               placeholder: "https://discord.com/api/webhooks/…" }] },
  { kind: "slack", label: "Slack",
    fields: [{ name: "webhook", label: t("notify.field.webhookUrl"), required: true, secret: true,
               placeholder: "https://hooks.slack.com/services/…" }] },
  { kind: "email", label: "Email", fields: [],
    help: t("notify.kind.emailHelp") },
  { kind: "apprise", label: t("notify.kind.advancedLabel"),
    fields: [{ name: "url", label: t("notify.field.appriseUrl"), required: true, secret: true,
               placeholder: "ntfy://ntfy.sh/topic" }] },
];
const kindLabels = (t: TFunction): Record<string, string> =>
  Object.fromEntries(buildKinds(t).map((k) => [k.kind, k.label]));

/** The add/edit form for one channel kind. Used by both ChannelsCard and the admin global channel. */
export function ChannelForm({ onSave, busy }: {
  onSave: (body: { kind: string; config: Record<string, string> }) => void; busy?: boolean;
}) {
  const { t } = useTranslation();
  const KINDS = buildKinds(t);
  const [kind, setKind] = useState("ntfy");
  const [vals, setVals] = useState<Record<string, string>>({});
  const spec = KINDS.find((k) => k.kind === kind)!;
  const ready = spec.fields.filter((f) => f.required).every((f) => (vals[f.name] || "").trim());

  function submit() {
    const config: Record<string, string> = {};
    for (const f of spec.fields) if ((vals[f.name] || "").trim()) config[f.name] = vals[f.name].trim();
    onSave({ kind, config });
    setVals({});
  }

  return (
    <div>
      <FormField label={t("notify.service")} hint={spec.help}>
        <select className={inputCls} value={kind}
          onChange={(e) => { setKind(e.target.value); setVals({}); }}>
          {KINDS.map((k) => <option key={k.kind} value={k.kind}>{k.label}</option>)}
        </select>
      </FormField>
      {spec.fields.map((f) => (
        <FormField key={f.name} label={f.label}>
          <input className={inputCls} type={f.secret ? "password" : "text"}
            autoComplete="off" placeholder={f.placeholder}
            value={vals[f.name] ?? ""}
            onChange={(e) => setVals((v) => ({ ...v, [f.name]: e.target.value }))} />
        </FormField>
      ))}
      <div className="mt-2 flex justify-end">
        <Button variant="primary" size="sm" disabled={!ready || busy} onClick={submit}>
          {busy ? t("common.saving") : t("notify.addChannel")}
        </Button>
      </div>
    </div>
  );
}

export function ChannelsCard() {
  const { t } = useTranslation();
  const KIND_LABEL = kindLabels(t);
  const qc = useQueryClient();
  const channels = useQuery({ queryKey: qk.notifChannels(), queryFn: api.listChannels });
  const [adding, setAdding] = useState(false);
  const [testMsg, setTestMsg] = useState<Record<number, { ok: boolean; error?: string; at: string }>>({});
  const refresh = () => qc.invalidateQueries({ queryKey: qk.notifChannels() });

  const create = useMutation({
    mutationFn: api.createChannel,
    onSuccess: () => { setAdding(false); refresh(); },
  });
  const toggle = useMutation({
    mutationFn: (c: NotificationChannel) => api.updateChannel(c.id, { enabled: !c.enabled }),
    onSuccess: refresh,
  });
  const remove = useMutation({ mutationFn: api.deleteChannel, onSuccess: refresh });
  const test = useMutation({
    mutationFn: api.testChannel,
    onSuccess: (r, id) =>
      setTestMsg((m) => ({ ...m, [id]: { ok: r.ok, error: r.error ?? undefined, at: hhmm() } })),
    onError: (e, id) =>
      setTestMsg((m) => ({ ...m, [id]: { ok: false, error: (e as Error).message, at: hhmm() } })),
  });

  const list = channels.data ?? [];
  return (
    <Card className="mb-4 p-5">
      <CardHeader
        title={t("notify.channels.title")}
        hint={<>{t("notify.channels.hintPre")}<em>{t("notify.channels.hintEm")}</em>{t("notify.channels.hintPost")}</>}
        desc={t("notify.channels.desc")}
      />

      {list.length > 0 && (
        <div className="mb-3 grid items-start gap-3 sm:grid-cols-2">
          {list.map((c) => (
            <ProviderCard
              key={c.id}
              name={KIND_LABEL[c.kind] ?? c.kind}
              desc={c.label || undefined}
              statusTone={c.enabled ? "success" : "neutral"}
              statusLabel={c.enabled ? t("notify.enabled") : t("notify.disabled")}
              actions={
                <div className="flex shrink-0 items-center gap-1.5">
                  {testMsg[c.id] && (
                    <span className="flex items-center gap-1 text-[11px] text-muted" title={testMsg[c.id].error}>
                      <StatusChip tone={testMsg[c.id].ok ? "success" : "danger"}>
                        {testMsg[c.id].ok ? t("notify.sent") : t("notify.failed")}
                      </StatusChip>
                      {testMsg[c.id].at}
                    </span>
                  )}
                  <button className="px-1 text-xs text-muted hover:text-text disabled:opacity-50"
                    disabled={test.isPending && test.variables === c.id}
                    onClick={() => test.mutate(c.id)}>{test.isPending && test.variables === c.id ? t("notify.testing") : t("notify.test")}</button>
                  <Toggle checked={c.enabled} onChange={() => toggle.mutate(c)} label="" />
                  <button className="px-1 text-red-500 hover:text-red-400" title={t("notify.remove")}
                    onClick={() => remove.mutate(c.id)}><X className="h-4 w-4" /></button>
                </div>
              }
            />
          ))}
        </div>
      )}

      <Button size="sm" onClick={() => setAdding(true)}>{t("notify.addChannelPlus")}</Button>
      {create.isError && <p className="mt-2 text-sm text-red-500">{(create.error as Error).message}</p>}

      {adding && (
        <Modal
          title={t("notify.addChannel")}
          onClose={() => setAdding(false)}
          width="w-[28rem]"
        >
          <ChannelForm busy={create.isPending}
            onSave={(b) => create.mutate({ ...b, label: KIND_LABEL[b.kind] })} />
        </Modal>
      )}
    </Card>
  );
}

/** Grouped event-preference checkboxes. Shared by the user card and the admin ops card. */
function EventToggles({ events, onChange, pending }: {
  events: NotificationEvent[]; onChange: (key: string, on: boolean) => void; pending?: boolean;
}) {
  const groups = useMemo(() => {
    const m = new Map<string, NotificationEvent[]>();
    for (const e of events) { if (!m.has(e.category)) m.set(e.category, []); m.get(e.category)!.push(e); }
    return [...m.entries()];
  }, [events]);
  return (
    <div className="grid gap-5">
      {groups.map(([cat, evs]) => (
        <div key={cat}>
          <div className="font-display mb-1 border-b border-[var(--hair,var(--border))] pb-2 text-xs font-semibold uppercase tracking-wide text-[var(--text-soft,var(--muted))]">{cat}</div>
          <div className="grid">
            {evs.map((e) => (
              <div key={e.key}
                className="flex items-center justify-between gap-4 border-b border-[var(--hair,var(--border))] py-2.5 last:border-0">
                <div className="min-w-0">
                  <div className="text-sm font-medium text-text">{e.label}</div>
                  <div className="text-xs leading-snug text-[var(--text-soft,var(--muted))]">{e.description}</div>
                </div>
                <div className="shrink-0">
                  <Toggle checked={e.enabled}
                    onChange={(on) => { if (!pending) onChange(e.key, on); }} />
                </div>
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

export function EventPrefsCard() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const prefs = useQuery({ queryKey: qk.notifPrefs(), queryFn: api.getNotifPrefs });
  const save = useMutation({
    mutationFn: api.setNotifPrefs,
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.notifPrefs() }),
  });
  return (
    <Card className="mb-4 p-5">
      <CardHeader title={t("notify.prefs.title")} desc={t("notify.prefs.desc")} />
      {prefs.data && (
        <EventToggles events={prefs.data} pending={save.isPending}
          onChange={(key, on) => save.mutate({ [key]: on })} />
      )}
    </Card>
  );
}

export function AdminNotifyCard() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const global = useQuery({ queryKey: qk.notifGlobalChannel(), queryFn: api.getGlobalChannel });
  const prefs = useQuery({ queryKey: qk.notifAdminPrefs(), queryFn: api.getAdminNotifPrefs });
  const setGlobal = useMutation({
    mutationFn: api.setGlobalChannel,
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.notifGlobalChannel() }),
  });
  const savePrefs = useMutation({
    mutationFn: api.setAdminNotifPrefs,
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.notifAdminPrefs() }),
  });

  const [bk, setBk] = useState("announcement");
  const [bt, setBt] = useState("");
  const [bb, setBb] = useState("");
  const [sent, setSent] = useState<string | null>(null);
  const broadcast = useMutation({
    mutationFn: api.broadcastNotification,
    onSuccess: (r) => { setSent(t("notify.broadcast.sentToUsers", { count: r.recipients })); setBt(""); setBb(""); },
  });

  return (
    <Card className="mb-4 p-5">
      <CardHeader
        title={t("notify.admin.title")}
        hint={t("notify.admin.hint")}
        desc={t("notify.admin.desc")}
      />

      <div className="mb-5">
        <div className="font-display mb-2 flex items-center gap-2 border-b border-[var(--hair,var(--border))] pb-2 text-xs font-semibold uppercase tracking-wide text-[var(--text-soft,var(--muted))]">
          {t("notify.admin.globalFallback")} {global.data && <Badge>{global.data.kind}</Badge>}
        </div>
        <ChannelForm busy={setGlobal.isPending}
          onSave={(b) => setGlobal.mutate({ ...b, label: "Global" })} />
      </div>

      <div className="mb-5">
        <div className="font-display mb-2 border-b border-[var(--hair,var(--border))] pb-2 text-xs font-semibold uppercase tracking-wide text-[var(--text-soft,var(--muted))]">{t("notify.admin.alertMe")}</div>
        {prefs.data && (
          <EventToggles events={prefs.data} pending={savePrefs.isPending}
            onChange={(key, on) => savePrefs.mutate({ [key]: on })} />
        )}
      </div>

      <div>
        <div className="font-display mb-2 border-b border-[var(--hair,var(--border))] pb-2 text-xs font-semibold uppercase tracking-wide text-[var(--text-soft,var(--muted))]">
          {t("notify.broadcast.title")}
        </div>
        <FormField label={t("notify.broadcast.type")}>
          <select className={inputCls} value={bk} onChange={(e) => setBk(e.target.value)}>
            <option value="announcement">{t("notify.broadcast.announcement")}</option>
            <option value="downtime">{t("notify.broadcast.downtime")}</option>
          </select>
        </FormField>
        <FormField label={t("notify.broadcast.titleField")}>
          <input className={inputCls} placeholder={t("notify.broadcast.titleField")} value={bt}
            onChange={(e) => setBt(e.target.value)} />
        </FormField>
        <FormField label={t("notify.broadcast.messageField")}>
          <textarea className={inputCls} rows={3} placeholder={t("notify.broadcast.messageField")} value={bb}
            onChange={(e) => setBb(e.target.value)} />
        </FormField>
        <div className="flex items-center justify-between gap-3">
          <span className="text-xs text-green-600">{sent}</span>
          <Button variant="primary" size="sm" disabled={!bt.trim() || broadcast.isPending}
            onClick={() => { setSent(null); broadcast.mutate({ kind: bk, title: bt.trim(), body: bb.trim() }); }}>
            {broadcast.isPending ? t("notify.broadcast.sending") : t("notify.broadcast.send")}
          </Button>
        </div>
      </div>
    </Card>
  );
}
