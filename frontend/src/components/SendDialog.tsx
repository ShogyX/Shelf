import { useState } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
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
  const { t } = useTranslation();
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
      setMsg({ ok: true, text: t("send.sentToRecipient", { count: r.chapters, to: r.to }) });
    } catch (e) {
      setMsg({ ok: false, text: (e as Error).message });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title={t("send.title")} onClose={onClose}>
      <p className="mb-4 truncate text-sm text-[var(--text-soft,var(--muted))]">{title}</p>

        {/* Chapter range (optional) */}
        <div className="grid grid-cols-2 gap-3">
          <FormField label={t("send.fromChapter")}>
            <input type="number" min={1} value={start}
              onChange={(e) => setStart(Math.max(1, parseInt(e.target.value) || 1))}
              className={inputCls} />
          </FormField>
          <FormField label={t("send.countBlankAll")}>
            <input type="number" min={1} value={limit} placeholder={t("send.all")}
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
          {t("send.download")}
        </a>

        {/* Email delivery (Kindle or personal) */}
        <div className="rounded-2xl border border-[var(--hair-strong,var(--border))] bg-surface-2/40 p-3.5">
          <div className="mb-2.5 text-[13px] font-semibold text-text">{t("send.emailIt")}</div>
          {smtpOk ? (
            <>
              <div className="mb-2.5 flex flex-wrap gap-1.5">
                {kindle && (
                  <Chip onClick={() => { setEmail(kindle); setTouched(true); }}>
                    {t("send.kindlePrefix", { email: kindle })}
                  </Chip>
                )}
                {personal && (
                  <Chip onClick={() => { setEmail(personal); setTouched(true); }}>
                    {t("send.personalPrefix", { email: personal })}
                  </Chip>
                )}
              </div>
              <input
                type="email"
                value={recipient}
                placeholder={t("send.recipientPlaceholder")}
                onChange={(e) => { setEmail(e.target.value); setTouched(true); }}
                className={inputCls}
              />
              <p className="mt-1.5 text-[11px] leading-snug text-[var(--text-soft,var(--muted))]">
                {t("send.kindleHint")}
              </p>
              <Button
                variant="primary"
                className="mt-2.5 w-full"
                disabled={busy || recipient.trim().indexOf("@") < 0}
                onClick={send}
              >
                {busy ? t("send.sending") : t("send.sendByEmail")}
              </Button>
            </>
          ) : (
            <p className="text-xs leading-snug text-[var(--text-soft,var(--muted))]">
              {t("send.notConfiguredPre")}
              <Link to="/settings" className="text-accent underline">{t("send.settingsLink")}</Link>
              {t("send.notConfiguredPost")}
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
