import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { qk } from "../api/queryKeys";
import { useCurrentUser } from "../auth";
import { Button, Chip, FormField, inputCls, Modal, StatusChip } from "./ui";

export default function SendDialog({
  workId,
  title,
  onClose,
}: {
  workId: number;
  title: string;
  onClose: () => void;
}) {
  const settings = useQuery({ queryKey: qk.settings(), queryFn: api.getSettings });
  const [email, setEmail] = useState("");
  const [touched, setTouched] = useState(false);
  const [start, setStart] = useState(1);
  const [limit, setLimit] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const kindle = settings.data?.kindle_email || "";
  const personal = useCurrentUser()?.email || "";  // the account email is the personal delivery target
  // Default recipient: last Kindle, else personal.
  const recipient = touched ? email : email || kindle || personal || "";
  const smtpOk = settings.data?.smtp_configured;

  async function send() {
    setMsg(null);
    setBusy(true);
    try {
      const r = await api.sendToKindle(workId, {
        to: recipient.trim(),
        start,
        limit: limit ? parseInt(limit) : undefined,
      });
      setMsg({ ok: true, text: `Sent ${r.chapters} chapter(s) to ${r.to}.` });
    } catch (e) {
      setMsg({ ok: false, text: (e as Error).message });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title="Send / export" onClose={onClose}>
      <p className="mb-4 truncate text-sm text-[var(--text-soft,var(--muted))]">{title}</p>

        {/* Chapter range (optional) */}
        <div className="grid grid-cols-2 gap-3">
          <FormField label="From chapter">
            <input type="number" min={1} value={start}
              onChange={(e) => setStart(Math.max(1, parseInt(e.target.value) || 1))}
              className={inputCls} />
          </FormField>
          <FormField label="Count (blank = all)">
            <input type="number" min={1} value={limit} placeholder="all"
              onChange={(e) => setLimit(e.target.value)}
              className={inputCls} />
          </FormField>
        </div>

        {/* Download — format follows the content (EPUB for text, CBZ for comics/manga). */}
        <a
          href={api.downloadUrl(workId, start, limit ? parseInt(limit) : undefined)}
          download
          className="mb-4 flex w-full items-center justify-center gap-2 rounded-lg border border-[var(--hair-strong,var(--border))] py-2.5 text-sm font-medium text-text transition hover:bg-surface-2"
        >
          ⤓ Download
        </a>

        {/* Email delivery (Kindle or personal) */}
        <div className="rounded-2xl border border-[var(--hair-strong,var(--border))] bg-surface-2/40 p-3.5">
          <div className="mb-2.5 text-[13px] font-semibold text-text">Email it</div>
          {smtpOk ? (
            <>
              <div className="mb-2.5 flex flex-wrap gap-1.5">
                {kindle && (
                  <Chip onClick={() => { setEmail(kindle); setTouched(true); }}>
                    Kindle: {kindle}
                  </Chip>
                )}
                {personal && (
                  <Chip onClick={() => { setEmail(personal); setTouched(true); }}>
                    Personal: {personal}
                  </Chip>
                )}
              </div>
              <input
                type="email"
                value={recipient}
                placeholder="your-device@kindle.com or you@example.com"
                onChange={(e) => { setEmail(e.target.value); setTouched(true); }}
                className={inputCls}
              />
              <p className="mt-1.5 text-[11px] leading-snug text-[var(--text-soft,var(--muted))]">
                For Kindle, add the sender address to your Amazon “Approved Personal Document
                E-mail List” first.
              </p>
              <Button
                variant="primary"
                className="mt-2.5 w-full"
                disabled={busy || recipient.trim().indexOf("@") < 0}
                onClick={send}
              >
                {busy ? "Sending…" : "📤 Send EPUB by email"}
              </Button>
            </>
          ) : (
            <p className="text-xs leading-snug text-[var(--text-soft,var(--muted))]">
              Email delivery isn’t configured. Add your SMTP login in{" "}
              <Link to="/settings" className="text-accent underline">Settings → Send to Kindle</Link>
              {" "}— meanwhile you can download the EPUB and use Amazon’s Send-to-Kindle app or USB.
            </p>
          )}
          {msg && (
            <div className="mt-2.5">
              <StatusChip tone={msg.ok ? "success" : "danger"}>{msg.text}</StatusChip>
            </div>
          )}
        </div>
    </Modal>
  );
}
