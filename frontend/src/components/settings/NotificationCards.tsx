import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { api, NotificationChannel, NotificationEvent } from "../../api/client";
import { qk } from "../../api/queryKeys";
import { Badge, Button, Card, InfoHint, inputCls, Toggle } from "../ui";

const hhmm = () => new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

type FieldSpec = { name: string; label: string; placeholder?: string; required?: boolean; secret?: boolean };
type KindSpec = { kind: string; label: string; fields: FieldSpec[]; help?: string };

// Guided per-service forms — the backend turns these into an Apprise URL. "Advanced" takes a raw URL.
const KINDS: KindSpec[] = [
  { kind: "ntfy", label: "ntfy", help: "Install the ntfy app, pick a topic name, and subscribe to it.",
    fields: [
      { name: "topic", label: "Topic", placeholder: "my-shelf-alerts", required: true },
      { name: "server", label: "Server (optional)", placeholder: "ntfy.sh" },
      { name: "token", label: "Access token (optional)", secret: true },
    ] },
  { kind: "pushover", label: "Pushover",
    fields: [
      { name: "user_key", label: "User key", required: true, secret: true },
      { name: "token", label: "Application token", required: true, secret: true },
    ] },
  { kind: "telegram", label: "Telegram", help: "Create a bot with @BotFather, message it, then enter your chat id.",
    fields: [
      { name: "bot_token", label: "Bot token", required: true, secret: true },
      { name: "chat_id", label: "Chat ID", required: true },
    ] },
  { kind: "discord", label: "Discord",
    fields: [{ name: "webhook", label: "Webhook URL", required: true, secret: true,
               placeholder: "https://discord.com/api/webhooks/…" }] },
  { kind: "slack", label: "Slack",
    fields: [{ name: "webhook", label: "Webhook URL", required: true, secret: true,
               placeholder: "https://hooks.slack.com/services/…" }] },
  { kind: "email", label: "Email", fields: [],
    help: "Emails the personal address set under Delivery, via the shared mail server." },
  { kind: "apprise", label: "Advanced (Apprise URL)",
    fields: [{ name: "url", label: "Apprise URL", required: true, secret: true,
               placeholder: "ntfy://ntfy.sh/topic" }] },
];
const KIND_LABEL = Object.fromEntries(KINDS.map((k) => [k.kind, k.label]));

/** The add/edit form for one channel kind. Used by both ChannelsCard and the admin global channel. */
export function ChannelForm({ onSave, busy }: {
  onSave: (body: { kind: string; config: Record<string, string> }) => void; busy?: boolean;
}) {
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
    <div className="rounded-lg border border-border p-3">
      <label className="block text-xs text-muted">Service
        <select className={`${inputCls} mt-1`} value={kind}
          onChange={(e) => { setKind(e.target.value); setVals({}); }}>
          {KINDS.map((k) => <option key={k.kind} value={k.kind}>{k.label}</option>)}
        </select>
      </label>
      {spec.help && <p className="mt-2 text-xs text-muted">{spec.help}</p>}
      <div className="mt-2 grid gap-2">
        {spec.fields.map((f) => (
          <label key={f.name} className="block text-xs text-muted">{f.label}
            <input className={`${inputCls} mt-1`} type={f.secret ? "password" : "text"}
              autoComplete="off" placeholder={f.placeholder}
              value={vals[f.name] ?? ""}
              onChange={(e) => setVals((v) => ({ ...v, [f.name]: e.target.value }))} />
          </label>
        ))}
      </div>
      <div className="mt-3 flex justify-end">
        <Button variant="primary" size="sm" disabled={!ready || busy} onClick={submit}>
          {busy ? "Saving…" : "Add channel"}
        </Button>
      </div>
    </div>
  );
}

export function ChannelsCard() {
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
    <Card className="mb-4 p-4">
      <h2 className="mb-3 flex items-center gap-1.5 font-semibold">
        Notification channels
        <InfoHint text={<>Connect where your notifications go — a phone push app (ntfy, Pushover),
          Telegram, a Discord/Slack webhook, or email. Add as many as you like. Choose <em>which</em>
          notifications you get below.</>} />
      </h2>

      {list.length > 0 && (
        <div className="mb-3 grid gap-2">
          {list.map((c) => (
            <div key={c.id} className="flex items-center gap-2 rounded-lg border border-border px-3 py-2">
              <Badge tone={c.enabled ? "green" : "amber"}>{KIND_LABEL[c.kind] ?? c.kind}</Badge>
              <span className="min-w-0 flex-1 truncate text-sm">{c.label || ""}</span>
              {testMsg[c.id] && (
                <span className="flex items-center gap-1 text-xs text-muted" title={testMsg[c.id].error}>
                  <Badge tone={testMsg[c.id].ok ? "green" : "red"}>
                    {testMsg[c.id].ok ? "sent" : "failed"}
                  </Badge>
                  {testMsg[c.id].at}
                </span>
              )}
              <button className="text-xs text-muted hover:text-text disabled:opacity-50"
                disabled={test.isPending && test.variables === c.id}
                onClick={() => test.mutate(c.id)}>{test.isPending && test.variables === c.id ? "Testing…" : "Test"}</button>
              <Toggle checked={c.enabled} onChange={() => toggle.mutate(c)} />
              <button className="text-xs text-red-500 hover:underline"
                onClick={() => remove.mutate(c.id)}>Remove</button>
            </div>
          ))}
        </div>
      )}

      {adding ? (
        <ChannelForm busy={create.isPending}
          onSave={(b) => create.mutate({ ...b, label: KIND_LABEL[b.kind] })} />
      ) : (
        <Button size="sm" onClick={() => setAdding(true)}>+ Add channel</Button>
      )}
      {create.isError && <p className="mt-2 text-sm text-red-500">{(create.error as Error).message}</p>}
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
    <div className="grid gap-4">
      {groups.map(([cat, evs]) => (
        <div key={cat}>
          <div className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-muted">{cat}</div>
          <div className="grid gap-2">
            {evs.map((e) => (
              <label key={e.key} className="flex items-start gap-2.5">
                <input type="checkbox" className="mt-1" checked={e.enabled} disabled={pending}
                  onChange={(ev) => onChange(e.key, ev.target.checked)} />
                <span>
                  <span className="block text-sm text-text">{e.label}</span>
                  <span className="block text-xs text-muted">{e.description}</span>
                </span>
              </label>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

export function EventPrefsCard() {
  const qc = useQueryClient();
  const prefs = useQuery({ queryKey: qk.notifPrefs(), queryFn: api.getNotifPrefs });
  const save = useMutation({
    mutationFn: api.setNotifPrefs,
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.notifPrefs() }),
  });
  return (
    <Card className="mb-4 p-4">
      <h2 className="mb-3 font-semibold">Notify me about…</h2>
      {prefs.data && (
        <EventToggles events={prefs.data} pending={save.isPending}
          onChange={(key, on) => save.mutate({ [key]: on })} />
      )}
    </Card>
  );
}

export function AdminNotifyCard() {
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
    onSuccess: (r) => { setSent(`Sent to ${r.recipients} user(s).`); setBt(""); setBb(""); },
  });

  return (
    <Card className="mb-4 p-4">
      <h2 className="mb-3 flex items-center gap-1.5 font-semibold">
        Admin notifications
        <InfoHint text={<>Operator alerts (health, errors, failed jobs, integration & backup status)
          go to every admin's channels for the events enabled here. The global channel is a fallback
          target for admins who haven't set up their own. The broadcast notifies all users.</>} />
      </h2>

      <div className="mb-4">
        <div className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-muted">
          Global fallback channel {global.data && <Badge tone="green">{global.data.kind}</Badge>}
        </div>
        <ChannelForm busy={setGlobal.isPending}
          onSave={(b) => setGlobal.mutate({ ...b, label: "Global" })} />
      </div>

      <div className="mb-4">
        <div className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-muted">Alert me about…</div>
        {prefs.data && (
          <EventToggles events={prefs.data} pending={savePrefs.isPending}
            onChange={(key, on) => savePrefs.mutate({ [key]: on })} />
        )}
      </div>

      <div>
        <div className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-muted">
          Broadcast to all users
        </div>
        <div className="grid gap-2">
          <label className="block text-xs text-muted">Type
            <select className={`${inputCls} mt-1`} value={bk} onChange={(e) => setBk(e.target.value)}>
              <option value="announcement">Announcement</option>
              <option value="downtime">Planned downtime</option>
            </select>
          </label>
          <input className={inputCls} placeholder="Title" value={bt}
            onChange={(e) => setBt(e.target.value)} />
          <textarea className={inputCls} rows={3} placeholder="Message" value={bb}
            onChange={(e) => setBb(e.target.value)} />
          <div className="flex items-center justify-between">
            <span className="text-xs text-green-600">{sent}</span>
            <Button variant="primary" size="sm" disabled={!bt.trim() || broadcast.isPending}
              onClick={() => { setSent(null); broadcast.mutate({ kind: bk, title: bt.trim(), body: bb.trim() }); }}>
              {broadcast.isPending ? "Sending…" : "Send to all users"}
            </Button>
          </div>
        </div>
      </div>
    </Card>
  );
}
