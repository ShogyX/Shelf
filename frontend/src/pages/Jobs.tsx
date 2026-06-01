import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, CrawlPolicy, Job, Work } from "../api/client";
import { Badge, Button, Card, EmptyState, Spinner } from "../components/ui";
import { CrawlPolicyFields, policyFrom } from "../components/CrawlPolicy";

const STATUS_TONE: Record<string, "green" | "amber" | "violet" | "red" | "default"> = {
  done: "green",
  running: "violet",
  scheduled: "amber",
  paused: "default",
  failed: "red",
};

export default function Jobs() {
  // Poll so the slow backfill is visibly progressing.
  const jobs = useQuery({ queryKey: ["jobs"], queryFn: api.listJobs, refetchInterval: 4000 });
  const works = useQuery({ queryKey: ["works"], queryFn: () => api.listWorks() });

  const workById = new Map<number, Work>((works.data ?? []).map((w) => [w.id, w]));

  return (
    <main className="mx-auto max-w-3xl px-4 py-8">
      <h1 className="mb-1 text-2xl font-semibold">Crawl jobs</h1>
      <p className="mb-6 text-sm text-muted">
        Backfills drain slowly within each source's rate budget and resume after restarts.
        Use “Crawl settings” to throttle a title's speed, daily amount, and allowed hours.
      </p>

      {jobs.isLoading && <Spinner label="Loading jobs…" />}
      {!jobs.isLoading && (!jobs.data || jobs.data.length === 0) && (
        <EmptyState title="No crawl jobs" hint="Hook a work to start a slow backfill." />
      )}

      <div className="space-y-3">
        {jobs.data?.map((job: Job) => (
          <JobRow key={job.id} job={job} work={workById.get(job.work_id)} />
        ))}
      </div>
    </main>
  );
}

function JobRow({ job, work }: { job: Job; work: Work | undefined }) {
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [policy, setPolicy] = useState<Partial<CrawlPolicy>>(work ? policyFrom(work) : {});

  const pause = useMutation({
    mutationFn: () => api.pauseJob(job.id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["jobs"] }),
  });
  const resume = useMutation({
    mutationFn: () => api.resumeJob(job.id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["jobs"] }),
  });
  const save = useMutation({
    mutationFn: () => api.setCrawlPolicy(job.work_id, policy),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["works"] });
      setEditing(false);
    },
  });

  const gathered = work?.chapters_fetched ?? 0;
  const total = work?.total_chapters_expected ?? work?.total_chapters_known ?? 0;
  const pct = total > 0 ? Math.min(100, Math.round((gathered / total) * 100)) : 0;
  const policyActive =
    work &&
    (work.crawl_interval_s != null ||
      work.crawl_daily_limit != null ||
      work.crawl_window_start != null);

  return (
    <Card className="p-4">
      <div className="flex items-center justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="truncate font-medium">{work?.title ?? `Work #${job.work_id}`}</span>
            <Badge>{job.kind}</Badge>
            <Badge tone={STATUS_TONE[job.status] ?? "default"}>{job.status}</Badge>
            {policyActive && <Badge tone="violet">throttled</Badge>}
          </div>
          {job.last_error && (
            <p className="mt-1 truncate text-xs text-red-500">⚠ {job.last_error}</p>
          )}
        </div>
        <div className="flex shrink-0 gap-2">
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              setPolicy(work ? policyFrom(work) : {});
              setEditing((e) => !e);
            }}
          >
            ⚙ Crawl settings
          </Button>
          {job.status === "paused" ? (
            <Button size="sm" onClick={() => resume.mutate()}>
              Resume
            </Button>
          ) : (
            job.status !== "done" &&
            job.status !== "failed" && (
              <Button size="sm" variant="ghost" onClick={() => pause.mutate()}>
                Pause
              </Button>
            )
          )}
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
            {gathered}
            {total ? ` / ${total}` : ""} chapters gathered
          </span>
          <span>{job.status === "done" ? "100%" : `${pct}%`}</span>
        </div>
      </div>

      {editing && (
        <div className="mt-3 rounded-lg border border-border bg-surface-2/40 p-3">
          <CrawlPolicyFields value={policy} onChange={setPolicy} />
          <div className="mt-3 flex items-center justify-end gap-2">
            <span className="mr-auto text-xs text-muted">
              Blank = source default. Hours are UTC.
            </span>
            <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>
              Cancel
            </Button>
            <Button size="sm" variant="primary" disabled={save.isPending} onClick={() => save.mutate()}>
              {save.isPending ? "Saving…" : "Save"}
            </Button>
          </div>
        </div>
      )}
    </Card>
  );
}
