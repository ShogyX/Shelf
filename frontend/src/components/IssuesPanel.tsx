// Issues (flagging) UI. IssuesPanel is a Settings tab: a user sees issues THEY raised; an admin — or
// a user granted `issues.view_all` — sees everyone's, and admins resolve/reopen with a note.
// ReportIssueDialog is the "flag this title" sheet, opened from a work's detail.
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, Issue, ISSUE_KINDS } from "../api/client";
import { qk } from "../api/queryKeys";
import { Badge, Button, Card, EmptyState, Modal, Select, Spinner, StatusChip } from "./ui";
import { useApp } from "../store";
import { useConfirm } from "./confirm";

export const KIND_LABEL: Record<string, string> = {
  no_content: "issues.kindNoContent",
  wrong_metadata: "issues.kindWrongMetadata",
  broken_file: "issues.kindBrokenFile",
  wrong_language: "issues.kindWrongLanguage",
  other: "issues.kindOther",
};

function shortDate(iso: string | null): string {
  if (!iso) return "";
  const ms = new Date(iso).getTime();
  return isNaN(ms) ? "" : new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

function useInvalidateIssues() {
  const qc = useQueryClient();
  return () => {
    qc.invalidateQueries({ queryKey: qk.issues() });
    qc.invalidateQueries({ queryKey: qk.issuesCount() });
  };
}

/** The "flag this title" sheet. Any signed-in user can raise an issue against a work. */
export function ReportIssueDialog({ workId, title, onClose }: {
  workId: number; title: string; onClose: () => void;
}) {
  const { t } = useTranslation();
  const toast = useApp((s) => s.toast);
  const invalidate = useInvalidateIssues();
  const [kind, setKind] = useState<string>("no_content");
  const [desc, setDesc] = useState("");
  const submit = useMutation({
    mutationFn: () => api.createIssue({ work_id: workId, kind, description: desc.trim() }),
    onSuccess: () => { invalidate(); toast(t("issues.reportedToast"), "success"); onClose(); },
    onError: (e) => toast((e as Error).message, "error"),
  });
  return (
    <Modal
      title={t("issues.reportTitle", { title })}
      onClose={onClose}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>{t("common.cancel")}</Button>
          <Button variant="primary" disabled={!desc.trim() || submit.isPending} onClick={() => submit.mutate()}>
            {submit.isPending ? t("issues.reporting") : t("issues.report")}
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <Select
          label={t("issues.kind")}
          value={kind}
          onChange={setKind}
          options={ISSUE_KINDS.map((k) => ({ value: k, label: t(KIND_LABEL[k]) }))}
        />
        <textarea
          className="w-full rounded-md border border-[var(--hair-strong,var(--border))] bg-bg px-2.5 py-2 text-sm text-text outline-none transition focus:border-accent"
          rows={4}
          placeholder={t("issues.descPlaceholder")}
          value={desc}
          onChange={(e) => setDesc(e.target.value)}
        />
      </div>
    </Modal>
  );
}

function IssueRow({ issue, canViewOthers }: { issue: Issue; canViewOthers: boolean }) {
  const { t } = useTranslation();
  const toast = useApp((s) => s.toast);
  const confirm = useConfirm();
  const invalidate = useInvalidateIssues();
  const [note, setNote] = useState(issue.admin_note ?? "");
  const update = useMutation({
    mutationFn: (status: "open" | "resolved") =>
      api.updateIssue(issue.id, { status, admin_note: note.trim() || undefined }),
    onSuccess: () => { invalidate(); toast(t("issues.updated"), "success"); },
    onError: (e) => toast((e as Error).message, "error"),
  });
  const del = useMutation({
    mutationFn: () => api.deleteIssue(issue.id),
    onSuccess: () => { invalidate(); toast(t("issues.deleted")); },
    onError: (e) => toast((e as Error).message, "error"),
  });
  const showReporter = (canViewOthers || issue.mine) && !!issue.username;
  return (
    <Card className="p-3.5">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="font-semibold text-text">{issue.title || t("issues.untitled")}</span>
            <Badge>{t(KIND_LABEL[issue.kind] ?? "issues.kindOther")}</Badge>
            <StatusChip tone={issue.status === "resolved" ? "success" : "warning"}>
              {t(issue.status === "resolved" ? "issues.resolved" : "issues.openState")}
            </StatusChip>
          </div>
          <p className="mt-1 whitespace-pre-wrap text-sm text-[var(--text-soft,var(--muted))]">{issue.description}</p>
          <div className="mt-1 text-xs text-muted">
            {showReporter && <>{t("issues.by", { name: issue.username })} · </>}
            {shortDate(issue.created_at)}
            {issue.admin_note && <> · <span className="italic">{t("issues.noteLabel", { note: issue.admin_note })}</span></>}
          </div>
        </div>
        <div className="flex shrink-0 flex-col items-end gap-1.5">
          {issue.can_resolve && (
            issue.status === "open" ? (
              <Button size="sm" variant="primary" disabled={update.isPending} onClick={() => update.mutate("resolved")}>
                {t("issues.resolve")}
              </Button>
            ) : (
              <Button size="sm" variant="ghost" disabled={update.isPending} onClick={() => update.mutate("open")}>
                {t("issues.reopen")}
              </Button>
            )
          )}
          {(issue.mine || issue.can_resolve) && (
            <button
              type="button"
              className="text-[11px] text-muted underline-offset-2 transition hover:text-red-500 hover:underline"
              onClick={async () => {
                if (await confirm({ title: t("issues.deleteTitle"), message: t("issues.deleteMsg"), confirmText: t("common.delete") }))
                  del.mutate();
              }}
            >
              {t("common.delete")}
            </button>
          )}
        </div>
      </div>
      {issue.can_resolve && (
        <input
          className="mt-2 w-full rounded-md border border-[var(--hair,var(--border))] bg-bg px-2.5 py-1.5 text-sm text-text outline-none transition focus:border-accent"
          placeholder={t("issues.notePlaceholder")}
          value={note}
          onChange={(e) => setNote(e.target.value)}
        />
      )}
    </Card>
  );
}

export default function IssuesPanel() {
  const { t } = useTranslation();
  const [status, setStatus] = useState("");
  const [scope, setScope] = useState("all");
  const countQ = useQuery({ queryKey: qk.issuesCount(), queryFn: api.issuesCount });
  const viewAll = countQ.data?.view_all ?? false;
  const effScope = viewAll ? scope : "mine";
  const q = useQuery({
    queryKey: qk.issues({ status, scope: effScope }),
    queryFn: () => api.listIssues({ status: status || undefined, scope: viewAll ? scope : undefined }),
  });
  const issues = q.data ?? [];
  return (
    <>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <Select
          label={t("issues.filterStatus")}
          value={status}
          onChange={setStatus}
          options={[
            { value: "", label: t("issues.all") },
            { value: "open", label: t("issues.openState") },
            { value: "resolved", label: t("issues.resolved") },
          ]}
        />
        {viewAll && (
          <Select
            label={t("issues.filterScope")}
            value={scope}
            onChange={setScope}
            options={[
              { value: "all", label: t("issues.scopeAll") },
              { value: "mine", label: t("issues.scopeMine") },
            ]}
          />
        )}
      </div>
      {q.isLoading ? (
        <Spinner label={t("issues.loading")} />
      ) : issues.length === 0 ? (
        <EmptyState title={t("issues.emptyTitle")} hint={t("issues.emptyHint")} />
      ) : (
        <div className="space-y-2.5">
          {issues.map((i) => <IssueRow key={i.id} issue={i} canViewOthers={viewAll} />)}
        </div>
      )}
    </>
  );
}
