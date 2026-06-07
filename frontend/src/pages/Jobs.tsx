import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, CrawlPolicy, DownloadJob, Job, Work } from "../api/client";
import { Badge, Button, Card, EmptyState, Spinner } from "../components/ui";
import { CrawlPolicyFields, policyFrom } from "../components/CrawlPolicy";
import { useApp } from "../store";
import { CrawlStats, PageReader, SiteCard } from "../components/IndexShared";

const STATUS_TONE: Record<string, "green" | "amber" | "violet" | "red" | "default"> = {
  done: "green",
  running: "violet",
  scheduled: "amber",
  paused: "default",
  failed: "red",
};

const DL_TONE: Record<string, "green" | "amber" | "violet" | "red" | "default"> = {
  imported: "green",
  downloading: "violet",
  completed: "violet",
  queued: "amber",
  retry: "amber",
  failed: "red",
};
const DL_LABEL: Record<string, string> = {
  queued: "Queued",
  downloading: "Downloading",
  completed: "Importing",
  retry: "Trying next source",
  imported: "Done",
  failed: "Failed",
};

export default function Jobs() {
  // Poll so the slow backfill is visibly progressing.
  const qc = useQueryClient();
  const toast = useApp((st) => st.toast);
  const jobs = useQuery({
    queryKey: ["jobs"],
    queryFn: api.listJobs,
    // Only poll while something is actually running/scheduled; idle (all done/failed) stops.
    refetchInterval: (q) =>
      (q.state.data ?? []).some((j) => j.status === "running" || j.status === "scheduled")
        ? 4000
        : false,
  });
  const works = useQuery({ queryKey: ["works"], queryFn: () => api.listWorks() });
  // Usenet fetch/download jobs (Acquire / Grab / Series). Poll while any are in flight.
  const downloads = useQuery({
    queryKey: ["downloads"],
    queryFn: api.listDownloads,
    refetchInterval: (q) =>
      (q.state.data ?? []).some((d) =>
        ["queued", "downloading", "completed", "retry"].includes(d.status))
        ? 3000
        : false,
  });
  // Indexing crawls (moved here from the Index page).
  const sites = useQuery({
    queryKey: ["index-sites"],
    queryFn: api.listIndexSites,
    refetchInterval: (q) =>
      (q.state.data ?? []).some((s) => s.status === "active") ? 2500 : false,
  });
  const [openPage, setOpenPage] = useState<number | null>(null);

  const workById = new Map<number, Work>((works.data ?? []).map((w) => [w.id, w]));

  const reap = useMutation({
    mutationFn: api.reapJobs,
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      qc.invalidateQueries({ queryKey: ["works"] });
      toast(
        r.revived > 0
          ? `Revived ${r.revived} stalled job${r.revived === 1 ? "" : "s"}.`
          : "No stalled jobs to revive.",
        "success"
      );
    },
    onError: (e) => toast((e as Error).message, "error"),
  });

  return (
    <main className="mx-auto max-w-3xl px-4 py-8">
      <div className="mb-1 flex items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold">Crawl jobs</h1>
        <Button
          variant="outline"
          size="sm"
          disabled={reap.isPending}
          onClick={() => reap.mutate()}
          title="Re-trigger jobs stuck on a cleared rate-limit, a crash, or an orphaned work"
        >
          {reap.isPending ? "Reviving…" : "Revive stalled jobs"}
        </Button>
      </div>
      <p className="mb-6 text-sm text-muted">
        Backfills drain slowly within each source's rate budget and resume after restarts.
        A reaper automatically retriggers jobs that stall on a request limit or crash (while
        the title still has chapters to gather); use “Revive stalled jobs” to run it now.
      </p>

      {/* Index-crawl observability + the indexing crawls themselves (moved from the Index page). */}
      <div className="mb-2 text-sm font-semibold text-muted">Indexing</div>
      <CrawlStats />
      {(sites.data?.length ?? 0) > 0 && (
        <div className="mb-2 space-y-3">
          {sites.data!.map((s) => (
            <SiteCard key={s.id} site={s} onOpenPage={setOpenPage} />
          ))}
        </div>
      )}

      {/* Usenet fetches (Acquire / Grab / Series) — what the user just queued. */}
      <div className="mb-2 mt-6 text-sm font-semibold text-muted">Fetches</div>
      {downloads.isLoading && <Spinner label="Loading fetches…" />}
      {!downloads.isLoading && (downloads.data?.length ?? 0) === 0 && (
        <EmptyState title="No fetches yet" hint="Acquire a book or a series to download it via usenet." />
      )}
      <div className="mb-6 space-y-2">
        {downloads.data?.map((d) => (
          <DownloadRow key={d.id} dl={d} />
        ))}
      </div>

      <div className="mb-2 mt-6 text-sm font-semibold text-muted">Backfill jobs</div>
      {jobs.isLoading && <Spinner label="Loading jobs…" />}
      {!jobs.isLoading && (!jobs.data || jobs.data.length === 0) && (
        <EmptyState title="No crawl jobs" hint="Hook a work to start a slow backfill." />
      )}

      <div className="space-y-3">
        {jobs.data?.map((job: Job) => (
          <JobRow key={job.id} job={job} work={workById.get(job.work_id)} />
        ))}
      </div>

      {openPage != null && <PageReader pageId={openPage} onClose={() => setOpenPage(null)} />}
    </main>
  );
}

function DownloadRow({ dl }: { dl: DownloadJob }) {
  const qc = useQueryClient();
  const toast = useApp((s) => s.toast);
  const del = useMutation({
    mutationFn: () => api.deleteDownload(dl.id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["downloads"] }),
    onError: (e) => toast((e as Error).message, "error"),
  });
  const active = ["queued", "downloading", "completed", "retry"].includes(dl.status);
  const mb = dl.size ? `${(dl.size / 1_000_000).toFixed(1)} MB` : null;
  return (
    <Card className="p-3">
      <div className="flex items-center justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="truncate font-medium">{dl.title}</span>
            <Badge tone={DL_TONE[dl.status] ?? "default"}>{DL_LABEL[dl.status] ?? dl.status}</Badge>
            {dl.fmt && <Badge>{dl.fmt}</Badge>}
            {dl.grab_kind === "auto" && <Badge tone="violet">auto</Badge>}
          </div>
          {dl.release_title && (
            <div className="truncate text-xs text-muted" title={dl.release_title}>
              {dl.release_title}
              {mb ? ` · ${mb}` : ""}
            </div>
          )}
          {dl.error && <div className="truncate text-xs text-red-500" title={dl.error}>⚠ {dl.error}</div>}
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {active && <Spinner label="" />}
          <Button
            size="sm"
            variant="ghost"
            disabled={del.isPending}
            title="Remove from the fetch list"
            onClick={() => del.mutate()}
          >
            ✕
          </Button>
        </div>
      </div>
    </Card>
  );
}

function JobRow({ job, work }: { job: Job; work: Work | undefined }) {
  const qc = useQueryClient();
  const toast = useApp((s) => s.toast);
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
  const retry = useMutation({
    mutationFn: () => api.retryJob(job.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      qc.invalidateQueries({ queryKey: ["works"] });
    },
    onError: (e) => toast((e as Error).message, "error"),
  });
  const remove = useMutation({
    mutationFn: () => api.deleteJob(job.id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["jobs"] }),
    onError: (e) => toast((e as Error).message, "error"),
  });
  const terminal = job.status === "done" || job.status === "failed";
  const save = useMutation({
    mutationFn: () => api.setCrawlPolicy(job.work_id, policy),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["works"] });
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
            <span className="truncate font-medium">{work?.title ?? `Work #${job.work_id}`}</span>
            <Badge>{job.kind}</Badge>
            <Badge tone={STATUS_TONE[job.status] ?? "default"}>{job.status}</Badge>
            {policyActive && <Badge tone="violet">throttled</Badge>}
          </div>
          {job.last_error && (
            <p className="mt-1 text-xs text-red-500" title={job.last_error}>⚠ {job.last_error}</p>
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
            ⚙ Crawl settings
          </Button>
          {job.status === "paused" ? (
            <Button size="sm" onClick={() => resume.mutate()}>
              Resume
            </Button>
          ) : (
            !terminal && (
              <Button size="sm" variant="ghost" onClick={() => pause.mutate()}>
                Pause
              </Button>
            )
          )}
          {terminal && (
            <Button
              size="sm"
              variant="ghost"
              disabled={retry.isPending}
              title="Re-queue failed chapters and run this job again"
              onClick={() => retry.mutate()}
            >
              {retry.isPending ? "Renewing…" : "↻ Renew"}
            </Button>
          )}
          <Button
            size="sm"
            variant="danger"
            disabled={remove.isPending}
            title="Delete this job and stop the crawl (won't auto-restart). Gathered chapters are kept; resume later with Renew or the work's 'Check for updates'."
            onClick={() => remove.mutate()}
          >
            ✕
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
