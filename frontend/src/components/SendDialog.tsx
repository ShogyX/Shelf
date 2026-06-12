import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { Button, Modal } from "./ui";

export default function SendDialog({
  workId,
  title,
  onClose,
}: {
  workId: number;
  title: string;
  onClose: () => void;
}) {
  const settings = useQuery({ queryKey: ["settings"], queryFn: api.getSettings });
  const [email, setEmail] = useState("");
  const [touched, setTouched] = useState(false);
  const [start, setStart] = useState(1);
  const [limit, setLimit] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const kindle = settings.data?.kindle_email || "";
  const personal = settings.data?.delivery?.email_to || "";
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
      <p className="mb-4 truncate text-sm text-muted">{title}</p>

        {/* Chapter range (optional) */}
        <div className="mb-4 grid grid-cols-2 gap-3">
          <label className="text-xs text-muted">
            From chapter
            <input type="number" min={1} value={start}
              onChange={(e) => setStart(Math.max(1, parseInt(e.target.value) || 1))}
              className="mt-1 w-full rounded-lg border border-border bg-bg px-2 py-1.5 text-sm text-text" />
          </label>
          <label className="text-xs text-muted">
            Count (blank = all)
            <input type="number" min={1} value={limit} placeholder="all"
              onChange={(e) => setLimit(e.target.value)}
              className="mt-1 w-full rounded-lg border border-border bg-bg px-2 py-1.5 text-sm text-text" />
          </label>
        </div>

        {/* Download — format follows the content (EPUB for text, CBZ for comics/manga). */}
        <a
          href={api.downloadUrl(workId, start, limit ? parseInt(limit) : undefined)}
          download
          className="mb-4 flex w-full items-center justify-center gap-2 rounded-lg border border-border py-2.5 text-sm font-medium text-text hover:bg-surface-2"
        >
          ⤓ Download
        </a>

        {/* Email delivery (Kindle or personal) */}
        <div className="rounded-lg border border-border p-3">
          <div className="mb-2 text-sm font-medium">Email it</div>
          {smtpOk ? (
            <>
              <div className="mb-2 flex flex-wrap gap-1.5">
                {kindle && (
                  <button onClick={() => { setEmail(kindle); setTouched(true); }}
                    className="rounded-full border border-border px-2.5 py-1 text-xs hover:bg-surface-2">
                    Kindle: {kindle}
                  </button>
                )}
                {personal && (
                  <button onClick={() => { setEmail(personal); setTouched(true); }}
                    className="rounded-full border border-border px-2.5 py-1 text-xs hover:bg-surface-2">
                    Personal: {personal}
                  </button>
                )}
              </div>
              <input
                type="email"
                value={recipient}
                placeholder="your-device@kindle.com or you@example.com"
                onChange={(e) => { setEmail(e.target.value); setTouched(true); }}
                className="w-full rounded-lg border border-border bg-bg px-2.5 py-2 text-sm text-text"
              />
              <p className="mt-1 text-[11px] text-muted">
                For Kindle, add the sender address to your Amazon “Approved Personal Document
                E-mail List” first.
              </p>
              <Button
                variant="primary"
                className="mt-2 w-full"
                disabled={busy || recipient.trim().indexOf("@") < 0}
                onClick={send}
              >
                {busy ? "Sending…" : "📤 Send EPUB by email"}
              </Button>
            </>
          ) : (
            <p className="text-xs text-muted">
              Email delivery isn’t configured. Add your SMTP login in{" "}
              <Link to="/settings" className="text-accent underline">Settings → Send to Kindle</Link>
              {" "}— meanwhile you can download the EPUB and use Amazon’s Send-to-Kindle app or USB.
            </p>
          )}
          {msg && (
            <p className={`mt-2 text-sm ${msg.ok ? "text-green-500" : "text-red-500"}`}>{msg.text}</p>
          )}
        </div>
    </Modal>
  );
}
