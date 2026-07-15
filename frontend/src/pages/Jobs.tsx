// This file now exports only JobRow (the per-job row used by SourcesHub). The standalone Jobs page
// was removed — /jobs redirects to /sources (App.tsx).
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, CrawlPolicy, Job, Work } from "../api/client";
import { qk } from "../api/queryKeys";
import { Badge, Button, Card } from "../components/ui";
import { CrawlPolicyFields, policyFrom } from "../components/CrawlPolicy";
import { useConfirm } from "../components/confirm";
import { useApp } from "../store";
import { TriangleAlert, X } from "lucide-react";

const STATUS_TONE: Record<string, "green" | "amber" | "violet" | "red" | "default"> = {
  done: "green",
  running: "violet",
  scheduled: "amber",
  paused: "default",
  failed: "red",
};

export function JobRow({ job, work }: { job: Job; work: Work | undefined }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useApp((s) => s.toast);
  const confirm = useConfirm();
  const [editing, setEditing] = useState(false);
  const [policy, setPolicy] = useState<Partial<CrawlPolicy>>(work ? policyFrom(work) : {});

  const pause = useMutation({
    mutationFn: () => api.pauseJob(job.id),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.jobs() }),
  });
  const resume = useMutation({
    mutationFn: () => api.resumeJob(job.id),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.jobs() }),
  });
  const retry = useMutation({
    mutationFn: () => api.retryJob(job.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.jobs() });
      qc.invalidateQueries({ queryKey: qk.works() });
    },
    onError: (e) => toast((e as Error).message, "error"),
  });
  const remove = useMutation({
    mutationFn: () => api.deleteJob(job.id),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.jobs() }),
    onError: (e) => toast((e as Error).message, "error"),
  });
  const terminal = job.status === "done" || job.status === "failed";
  const save = useMutation({
    mutationFn: () => api.setCrawlPolicy(job.work_id, policy),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.works() });
      setEditing(false);
    },
  });

  const gathered = work?.chapters_fetched ?? 0;
  // Clamp so a serial that passed its old advertised total never reads as "gathered > total".
  const total = Math.max(work?.total_chapters_expected ?? work?.total_chapters_known ?? 0, gathered);
  const pct = total > 0 ? Math.min(100, Math.round((gathered / total) * 100)) : 0;
  const policyActive =
    work &&
    (work.crawl_interval_s != null ||
      work.crawl_window_start != null);

  return (
    <Card className="p-4">
      <div className="flex items-center justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="truncate font-medium">{work?.title ?? t("jobs.workNumber", { id: job.work_id })}</span>
            <Badge>{job.kind}</Badge>
            <Badge tone={STATUS_TONE[job.status] ?? "default"}>{job.status}</Badge>
            {policyActive && <Badge tone="violet">{t("jobs.throttled")}</Badge>}
          </div>
          {job.last_error && (
            <p className="mt-1 text-xs text-red-500" title={job.last_error}><TriangleAlert className="mr-1 inline h-3.5 w-3.5 -mt-px" />{job.last_error}</p>
          )}
        </div>
        <div className="flex flex-wrap justify-end gap-2">
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              setPolicy(work ? policyFrom(work) : {});
              setEditing((e) => !e);
            }}
          >
            {t("jobs.crawlSettings")}
          </Button>
          {job.status === "paused" ? (
            <Button size="sm" onClick={() => resume.mutate()}>
              {t("jobs.resume")}
            </Button>
          ) : (
            !terminal && (
              <Button size="sm" variant="ghost" onClick={() => pause.mutate()}>
                {t("jobs.pause")}
              </Button>
            )
          )}
          {terminal && (
            <Button
              size="sm"
              variant="ghost"
              disabled={retry.isPending}
              title={t("jobs.renewTitle")}
              onClick={() => retry.mutate()}
            >
              {retry.isPending ? t("jobs.renewing") : t("jobs.renew")}
            </Button>
          )}
          <Button
            size="sm"
            variant="danger"
            disabled={remove.isPending}
            title={t("jobs.deleteTitle")}
            onClick={async () => {
              if (await confirm({
                title: t("jobs.deleteConfirmTitle"),
                message: t("jobs.deleteConfirmMessage", { title: work?.title ?? t("jobs.thisWork") }),
                danger: true,
              })) remove.mutate();
            }}
          >
            <X className="h-4 w-4" />
          </Button>
        </div>
      </div>

      <div className="mt-3">
        <div className="h-2 w-full overflow-hidden rounded-full bg-surface-2">
          <div
            className="h-full rounded-full bg-accent transition-all"
            style={{ width: `${job.status === "done" ? 100 : pct}%` }}
          />
        </div>
        <div className="mt-1 flex justify-between text-xs text-muted">
          <span>
            {t("jobs.chaptersGathered", { gathered, total: total ? t("jobs.totalSuffix", { total }) : "" })}
          </span>
          <span>{job.status === "done" ? "100%" : `${pct}%`}</span>
        </div>
      </div>

      {editing && (
        <div className="mt-3 rounded-lg border border-border bg-surface-2/40 p-3">
          <CrawlPolicyFields value={policy} onChange={setPolicy} />
          <div className="mt-3 flex items-center justify-end gap-2">
            <span className="mr-auto text-xs text-muted">
              {t("jobs.blankHint")}
            </span>
            <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>
              {t("jobs.cancel")}
            </Button>
            <Button size="sm" variant="primary" disabled={save.isPending} onClick={() => save.mutate()}>
              {save.isPending ? t("jobs.saving") : t("jobs.save")}
            </Button>
          </div>
        </div>
      )}
    </Card>
  );
}
