import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, Job, Work } from "../api/client";
import { Badge, Button, Card, EmptyState, Spinner } from "../components/ui";

const STATUS_TONE: Record<string, "green" | "amber" | "violet" | "red" | "default"> = {
  done: "green",
  running: "violet",
  scheduled: "amber",
  paused: "default",
  failed: "red",
};

export default function Jobs() {
  const qc = useQueryClient();
  // Poll so the slow backfill is visibly progressing.
  const jobs = useQuery({ queryKey: ["jobs"], queryFn: api.listJobs, refetchInterval: 4000 });
  const works = useQuery({ queryKey: ["works"], queryFn: () => api.listWorks() });

  const pause = useMutation({
    mutationFn: (id: number) => api.pauseJob(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["jobs"] }),
  });
  const resume = useMutation({
    mutationFn: (id: number) => api.resumeJob(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["jobs"] }),
  });

  const workById = new Map<number, Work>((works.data ?? []).map((w) => [w.id, w]));

  return (
    <main className="mx-auto max-w-3xl px-4 py-8">
      <h1 className="mb-1 text-2xl font-semibold">Crawl jobs</h1>
      <p className="mb-6 text-sm text-muted">
        Backfills drain slowly within each source's rate budget and resume after restarts.
      </p>

      {jobs.isLoading && <Spinner label="Loading jobs…" />}
      {!jobs.isLoading && (!jobs.data || jobs.data.length === 0) && (
        <EmptyState title="No crawl jobs" hint="Hook a work to start a slow backfill." />
      )}

      <div className="space-y-3">
        {jobs.data?.map((job: Job) => {
          const work = workById.get(job.work_id);
          const gathered = work?.chapters_fetched ?? 0;
          const total = work?.total_chapters_expected ?? work?.total_chapters_known ?? 0;
          const pct = total > 0 ? Math.min(100, Math.round((gathered / total) * 100)) : 0;
          return (
            <Card key={job.id} className="p-4">
              <div className="flex items-center justify-between gap-2">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="truncate font-medium">{work?.title ?? `Work #${job.work_id}`}</span>
                    <Badge>{job.kind}</Badge>
                    <Badge tone={STATUS_TONE[job.status] ?? "default"}>{job.status}</Badge>
                  </div>
                  {job.last_error && (
                    <p className="mt-1 truncate text-xs text-red-500">⚠ {job.last_error}</p>
                  )}
                </div>
                <div className="flex gap-2">
                  {job.status === "paused" ? (
                    <Button size="sm" onClick={() => resume.mutate(job.id)}>
                      Resume
                    </Button>
                  ) : (
                    job.status !== "done" &&
                    job.status !== "failed" && (
                      <Button size="sm" variant="ghost" onClick={() => pause.mutate(job.id)}>
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
                    {gathered}{total ? ` / ${total}` : ""} chapters gathered
                  </span>
                  <span>{job.status === "done" ? "100%" : `${pct}%`}</span>
                </div>
              </div>
            </Card>
          );
        })}
      </div>
    </main>
  );
}
