// Library stocking (admin): pre-fetch catalog works through Prowlarr/SABnzbd so they're instantly
// available when a user acquires them. Configure a stock directory, queue a filtered selection
// (media category / genre / theme / popularity, capped), and watch the pool fill in the background.
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, MEDIA_CATEGORIES, StockItem, StockJob } from "../api/client";
import { qk } from "../api/queryKeys";
import { Badge, Button, Card, Disclosure, EmptyState, inputCls, Modal, Select, Spinner } from "../components/ui";
import { useConfirm } from "../components/confirm";
import { useApp } from "../store";

const STATUS_TONE: Record<string, "green" | "amber" | "violet" | "red" | "default"> = {
  stocked: "green",
  downloading: "violet",
  searching: "violet",
  pending: "default",
  unavailable: "amber",
  failed: "red",
};
const STATUS_ORDER = ["stocked", "downloading", "searching", "pending", "unavailable", "failed"];

const OVERALL_TONE: Record<string, "green" | "amber" | "violet" | "default"> = {
  complete: "green",
  working: "violet",
  "needs attention": "amber",
  empty: "default",
};

function fmtSize(bytes: number): string {
  if (!bytes) return "";
  if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(1)} GB`;
  return `${(bytes / 1e6).toFixed(0)} MB`;
}

function StockConfigCard() {
  const qc = useQueryClient();
  const summary = useQuery({
    queryKey: qk.stockSummary(),
    queryFn: api.getStockSummary,
    refetchInterval: 5000,
  });
  const [dir, setDir] = useState<string | null>(null);
  const d = summary.data;
  const value = dir ?? d?.stock_dir ?? "";
  const save = useMutation({
    mutationFn: () => api.setStockConfig(value.trim() || null),
    onSuccess: () => { setDir(null); qc.invalidateQueries({ queryKey: qk.stockSummary() }); },
  });

  return (
    <Card className="mb-4 p-4">
      <div className="mb-2 flex items-center gap-2">
        <h2 className="font-semibold">Stocking</h2>
        <Badge tone={d?.configured ? "green" : "amber"}>{d?.configured ? "ready" : "not ready"}</Badge>
      </div>
      <p className="mb-3 text-sm text-muted">
        Pre-download catalog works through the <b>Prowlarr → SABnzbd</b> pipeline and keep them on disk,
        so when a user acquires one it's served instantly (no second download). Every selected work is
        searched on usenet — including web-crawled titles. Books search the ebook categories; comics &amp;
        manga search the comic categories (CBZ/CBR), configurable on the Prowlarr integration. Needs the
        pipeline configured under <span className="text-text">Settings → Integrations</span>.
      </p>
      {!d?.pipeline_configured && (
        <p className="mb-3 rounded-lg border border-amber-400/30 bg-amber-500/10 p-2 text-sm">
          The Prowlarr + SABnzbd pipeline isn't fully enabled yet — stocking can't run until it is.
        </p>
      )}
      <label className="block text-sm">
        <span className="text-muted">Stock directory (kept apart from user downloads)</span>
        <div className="mt-1 flex gap-2">
          <input className={`${inputCls} flex-1`} placeholder="/mnt/NAS-Pool/media/Stock"
            value={value} onChange={(e) => setDir(e.target.value)} />
          <Button variant="primary" disabled={save.isPending} onClick={() => save.mutate()}>
            {save.isPending ? "Saving…" : "Save"}
          </Button>
        </div>
      </label>
    </Card>
  );
}

function QueueCard() {
  const qc = useQueryClient();
  const summary = useQuery({ queryKey: qk.stockSummary(), queryFn: api.getStockSummary });
  const [name, setName] = useState<string>("");             // operator's batch name (optional)
  const [media, setMedia] = useState<string>("");           // "" = all categories
  const [cat, setCat] = useState<string>("");               // "kind:slug" genre/theme, "" = all
  const [sort, setSort] = useState<string>("popularity");
  const [limit, setLimit] = useState<string>("200");
  const [variant, setVariant] = useState<"ebook" | "audiobook" | "both">("ebook");
  const [note, setNote] = useState<string | null>(null);

  const cats = useQuery({
    queryKey: qk.catalogCategories(media),
    queryFn: () => api.catalogCategories(media || undefined),
  });

  const queue = useMutation({
    mutationFn: () => {
      const [dimension, value] = cat ? cat.split(":") : [undefined, undefined];
      return api.queueStock({
        name: name.trim() || undefined,
        media: media || undefined,
        dimension, value,
        sort,
        limit: Math.max(1, Math.min(5000, parseInt(limit) || 200)),
        variant,
      });
    },
    onSuccess: (r) => {
      setNote(
        r.queued
          ? `Created “${r.name}” — ${r.queued} title${r.queued === 1 ? "" : "s"} queued (skipped ${r.skipped} already stocked, ${r.selected} matched).`
          : `Nothing new to stock — all ${r.skipped} matched titles are already queued.`,
      );
      setName("");
      qc.invalidateQueries({ queryKey: qk.stockSummary() });
      qc.invalidateQueries({ queryKey: qk.stockJobs() });
      qc.invalidateQueries({ queryKey: qk.catalogCategories() });
    },
    onError: (e) => setNote((e as Error).message),
  });

  const ready = summary.data?.configured;
  return (
    <Card className="mb-4 p-4">
      <h2 className="mb-1 font-semibold">Queue stocking</h2>
      <p className="mb-3 text-sm text-muted">
        Select what to stock — by media type, genre/theme, and popularity — capped so it's a curated
        batch, not the whole catalog. The most popular matches are fetched first, in the background.
      </p>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <label className="text-xs text-muted">Name
          <input className={`${inputCls} mt-1`} placeholder="e.g. Top Sci-Fi"
            value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <Select label="Media" value={media}
          onChange={(v) => { setMedia(v); setCat(""); }}
          options={[{ value: "", label: "All" }, ...MEDIA_CATEGORIES.map((c) => ({ value: c, label: c }))]} />
        <Select label="Genre / theme" value={cat} onChange={setCat}
          options={[
            { value: "", label: "Any" },
            ...(cats.data?.categories ?? []).map((c) => ({
              value: `${c.kind}:${c.slug}`,
              label: `${c.label} (${c.kind}, ${c.count})`,
            })),
          ]} />
        <div className="col-span-full">
          <Disclosure title="More options" subtitle="Sort & cap">
            <div className="grid gap-3 sm:grid-cols-2">
              <Select label="Sort" value={sort} onChange={setSort}
                options={[
                  { value: "popularity", label: "Most popular" },
                  { value: "new", label: "Newest" },
                  { value: "title", label: "Title A–Z" },
                ]} />
              <Select label="Format" value={variant}
                onChange={(v) => setVariant(v as "ebook" | "audiobook" | "both")}
                options={[
                  { value: "ebook", label: "Ebook" },
                  { value: "audiobook", label: "Audiobook" },
                  { value: "both", label: "Both" },
                ]} />
              <label className="text-xs text-muted">Cap
                <input className={`${inputCls} mt-1`} type="number" min={1} max={2000}
                  value={limit} onChange={(e) => setLimit(e.target.value)} />
              </label>
            </div>
          </Disclosure>
        </div>
        <div className="col-span-full">
          <Button variant="primary" disabled={!ready || queue.isPending} onClick={() => queue.mutate()}>
            {queue.isPending ? "Queuing…" : "Queue selection"}
          </Button>
        </div>
      </div>
      {note && <p className="mt-2 text-sm text-muted">{note}</p>}
    </Card>
  );
}

function ProgressBar({ value }: { value: number }) {
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-border">
      <div className="h-full rounded-full bg-accent transition-all"
        style={{ width: `${Math.round(Math.min(1, Math.max(0, value)) * 100)}%` }} />
    </div>
  );
}

function StockJobsList({ onOpen }: { onOpen: (id: number) => void }) {
  const jobs = useQuery({
    queryKey: qk.stockJobs(), queryFn: api.listStockJobs, refetchInterval: 5000,
  });
  const rows = jobs.data ?? [];
  return (
    <Card className="p-4">
      <h2 className="mb-3 font-semibold">Stock batches</h2>
      {jobs.isLoading ? <Spinner label="Loading…" /> : rows.length === 0 ? (
        <EmptyState title="No stock batches yet" hint="Queue a selection above — give it a name to track it here." />
      ) : (
        <div className="space-y-2">
          {rows.map((j) => <StockJobCard key={j.id ?? "ungrouped"} job={j} onOpen={onOpen} />)}
        </div>
      )}
    </Card>
  );
}

function StockJobCard({ job, onOpen }: { job: StockJob; onOpen: (id: number) => void }) {
  const id = job.id ?? 0;
  const pct = Math.round(job.progress * 100);
  return (
    <button
      className="block w-full rounded-lg border border-border p-3 text-left transition hover:border-accent/60 hover:bg-bg/40"
      onClick={() => onOpen(id)}
    >
      <div className="mb-1 flex items-center justify-between gap-2">
        <span className="truncate font-medium">{job.name}</span>
        <div className="flex shrink-0 items-center gap-1.5">
          {job.variant !== "ebook" && (
            <Badge tone="violet">{job.variant === "both" ? "ebook + audio" : "audiobook"}</Badge>
          )}
          {job.issues > 0 && <Badge tone="amber">⚠ {job.issues} need{job.issues === 1 ? "s" : ""} attention</Badge>}
          <Badge tone={OVERALL_TONE[job.overall] ?? "default"}>{job.overall}</Badge>
        </div>
      </div>
      <ProgressBar value={job.progress} />
      <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-muted">
        <span>{job.stocked}/{job.total} stocked ({pct}%)</span>
        {job.in_flight > 0 && <span>{job.in_flight} in progress</span>}
        {job.issues > 0 && <span className="text-amber-600">{job.issues} issue{job.issues === 1 ? "" : "s"}</span>}
        {job.stocked_size > 0 && <span>{fmtSize(job.stocked_size)}</span>}
        {job.media_category && <span>· {job.media_category}</span>}
      </div>
    </button>
  );
}

function StockJobModal({ id, onClose }: { id: number; onClose: () => void }) {
  const qc = useQueryClient();
  const confirm = useConfirm();
  const toast = useApp((s) => s.toast);
  const detail = useQuery({
    queryKey: qk.stockJob(id),
    queryFn: () => api.getStockJob(id),
    refetchInterval: 4000,
  });

  const inval = () => {
    qc.invalidateQueries({ queryKey: qk.stockJob(id) });
    qc.invalidateQueries({ queryKey: qk.stockJobs() });
    qc.invalidateQueries({ queryKey: qk.stockSummary() });
    qc.invalidateQueries({ queryKey: qk.catalogCategories() });
  };
  const retry = useMutation({
    mutationFn: () => api.retryStockJob(id),
    onSuccess: (r) => { toast(`Requeued ${r.requeued} item(s) to retry`, "success"); inval(); },
    onError: (e) => toast((e as Error).message, "error"),
  });
  const delItem = useMutation({ mutationFn: (sid: number) => api.deleteStock(sid), onSuccess: inval });
  const delJob = useMutation({
    mutationFn: (deleteFiles: boolean) => api.deleteStockJob(id, deleteFiles),
    onSuccess: () => { inval(); onClose(); },
    onError: (e) => toast((e as Error).message, "error"),
  });

  const j = detail.data;
  return (
    <Modal
      variant="fullscreen-sheet"
      width="max-w-2xl"
      onClose={onClose}
      title={
        <span className="min-w-0">
          <span className="block truncate">{j?.name ?? "Stock batch"}</span>
          {j && (
            <span className="block text-xs font-normal text-muted">
              {j.stocked}/{j.total} stocked · {j.in_flight} in progress
              {j.issues > 0 ? ` · ${j.issues} need attention` : ""}
            </span>
          )}
        </span>
      }
      footer={j && (
        <div className="flex justify-end">
          <Button size="sm" variant="danger" disabled={delJob.isPending}
            onClick={async () => {
              if (await confirm({
                message: `Delete the “${j.name}” batch (${j.total} item(s))? The stocked files stay on disk so already-served titles keep working.`,
                danger: true, confirmText: "Delete batch",
              })) delJob.mutate(false);
            }}>
            Delete batch
          </Button>
        </div>
      )}
    >
      {!j ? <Spinner label="Loading…" /> : (
        <>
          <ProgressBar value={j.progress} />
              <div className="mt-2 mb-3 flex flex-wrap gap-1.5">
                {STATUS_ORDER.filter((s) => j.counts[s]).map((s) => (
                  <Badge key={s} tone={STATUS_TONE[s] ?? "default"}>{s}: {j.counts[s]}</Badge>
                ))}
                {j.stocked_size > 0 && <Badge>{fmtSize(j.stocked_size)} on disk</Badge>}
              </div>

              {j.problem_items.length > 0 && (
                <div className="mb-3 rounded-lg border border-amber-500/30 bg-amber-500/5 p-2.5">
                  <div className="mb-1.5 flex items-center justify-between gap-2">
                    <span className="text-sm font-medium">⚠ {j.problem_items.length} item(s) need attention</span>
                    <Button size="sm" variant="primary" disabled={retry.isPending}
                      onClick={() => retry.mutate()}>
                      {retry.isPending ? "Requeuing…" : "Retry all"}
                    </Button>
                  </div>
                  <div className="space-y-0.5">
                    {j.problem_items.slice(0, 8).map((it) => (
                      <div key={it.id} className="truncate text-xs text-muted">
                        <span className="text-amber-600">{it.status}</span> · {it.title}
                        {it.error ? ` — ${it.error}` : ""}
                      </div>
                    ))}
                    {j.problem_items.length > 8 && (
                      <div className="text-xs text-muted">…and {j.problem_items.length - 8} more</div>
                    )}
                  </div>
                </div>
              )}

              <div className="mb-1 text-xs font-medium text-muted">
                Titles ({j.total}){j.items.length < j.total ? ` · showing first ${j.items.length}` : ""}
              </div>
              <div className="divide-y divide-border">
                {j.items.map((it) => (
                  <StockItemRow key={it.id} it={it}
                    onDelete={async () => {
                      if (await confirm({ message: `Remove “${it.title}” from stock?`, danger: true, confirmText: "Remove" }))
                        delItem.mutate(it.id);
                    }} />
                ))}
              </div>
            </>
          )}
    </Modal>
  );
}

function StockItemRow({ it, onDelete }: { it: StockItem; onDelete: () => void }) {
  const mb = it.size ? `${(it.size / 1_000_000).toFixed(1)} MB` : "";
  return (
    <div className="flex items-center justify-between gap-2 py-1.5">
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <span className="truncate text-sm">{it.title}</span>
          <Badge tone={STATUS_TONE[it.status] ?? "default"}>{it.status}</Badge>
        </div>
        {(it.author || mb || it.error) && (
          <div className="truncate text-xs text-muted">
            {[it.author, mb, it.error].filter(Boolean).join(" · ")}
          </div>
        )}
      </div>
      <Button size="sm" variant="ghost" title="Remove this title from stock" onClick={onDelete}>✕</Button>
    </div>
  );
}

export default function Stock() {
  const [openJob, setOpenJob] = useState<number | null>(null);
  return (
    <main className="mx-auto max-w-4xl px-4 py-8">
      <h1 className="mb-1 text-2xl font-semibold">Library stocking</h1>
      <p className="mb-6 text-sm text-muted">
        Operator pre-fetch of catalog works through the usenet pipeline — stocked titles are served
        instantly to users, with no per-user download.
      </p>
      <StockConfigCard />
      <QueueCard />
      <StockJobsList onOpen={setOpenJob} />
      {openJob !== null && <StockJobModal id={openJob} onClose={() => setOpenJob(null)} />}
    </main>
  );
}
