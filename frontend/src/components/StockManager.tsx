// Library stocking (admin) — folded into Sources & Acquisitions. Operator pre-fetch of catalog works
// through the Prowlarr → SABnzbd pipeline so a stocked title is served instantly (no per-user
// download). Compact: a config + daily-caps header, a collapsible "Queue a batch" form (incl.
// entire-catalog / exclude-web), the batch list + detail modal, and a read-only "feeding lists" strip.
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, MEDIA_CATEGORIES, StockItem, StockJob } from "../api/client";
import { qk } from "../api/queryKeys";
import {
  Badge, Button, Card, Disclosure, EmptyState, inputCls, Modal, Select, Spinner,
  StatusChip, Toggle,
} from "./ui";
import { useConfirm } from "./confirm";
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

// A compact "used / cap today" gauge — "unlimited" when the cap is 0.
function UsageGauge({ label, used, cap }: { label: string; used: number; cap: number }) {
  const unlimited = cap === 0;
  const pct = unlimited ? 0 : Math.min(100, Math.round((used / cap) * 100));
  const tone: "success" | "warning" | "danger" =
    unlimited || pct < 70 ? "success" : pct < 100 ? "warning" : "danger";
  const bar = tone === "danger" ? "#fb7185" : tone === "warning" ? "#fbbf24" : "#34d399";
  return (
    <div className="min-w-0 flex-1 rounded-xl border border-[var(--hair,var(--border))] bg-surface-2 px-3 py-2">
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-[11px] font-semibold uppercase tracking-wide text-muted">{label}</span>
        <span className="text-xs font-semibold tabular-nums text-text">
          {unlimited ? "unlimited" : `${used} / ${cap}`}
        </span>
      </div>
      {!unlimited && (
        <div className="mt-1.5 h-1.5 w-full overflow-hidden rounded-full bg-border">
          <div className="h-full rounded-full transition-all" style={{ width: `${pct}%`, background: bar }} />
        </div>
      )}
    </div>
  );
}

// Edit the two daily caps via the shared system config (0 = unlimited). Inline, in a small popup.
function CapsEditor({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const cfg = useQuery({ queryKey: qk.systemConfig(), queryFn: api.getSystemConfig });
  const [searches, setSearches] = useState<string | null>(null);
  const [downloads, setDownloads] = useState<string | null>(null);
  const v = cfg.data?.values;
  const sVal = searches ?? String(v?.stock_searches_per_day ?? 0);
  const dVal = downloads ?? String(v?.stock_downloads_per_day ?? 0);
  const save = useMutation({
    mutationFn: () => api.putSystemConfig({
      stock_searches_per_day: Math.max(0, parseInt(sVal) || 0),
      stock_downloads_per_day: Math.max(0, parseInt(dVal) || 0),
    }),
    onSuccess: (d) => {
      qc.setQueryData(qk.systemConfig(), d);
      qc.invalidateQueries({ queryKey: qk.stockSummary() });
      onClose();
    },
  });
  return (
    <Modal width="max-w-sm" onClose={onClose} title="Daily stocking caps">
      <p className="mb-3 text-sm text-muted">
        Throttle how much the stocker does per day across all batches. <b>0 = unlimited.</b>
      </p>
      <div className="space-y-3">
        <label className="block text-xs text-muted">Searches per day
          <input className={`${inputCls} mt-1`} type="number" min={0} value={sVal}
            onChange={(e) => setSearches(e.target.value)} />
        </label>
        <label className="block text-xs text-muted">Downloads per day
          <input className={`${inputCls} mt-1`} type="number" min={0} value={dVal}
            onChange={(e) => setDownloads(e.target.value)} />
        </label>
      </div>
      <div className="mt-4 flex justify-end gap-2">
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
        <Button variant="primary" disabled={save.isPending || !cfg.data} onClick={() => save.mutate()}>
          {save.isPending ? "Saving…" : "Save caps"}
        </Button>
      </div>
    </Modal>
  );
}

// Config header: stock-dir status + editable directory + the daily-caps usage gauges.
function StockHeader() {
  const qc = useQueryClient();
  const summary = useQuery({
    queryKey: qk.stockSummary(),
    queryFn: api.getStockSummary,
    refetchInterval: 5000,
  });
  const [dir, setDir] = useState<string | null>(null);
  const [editCaps, setEditCaps] = useState(false);
  const d = summary.data;
  const value = dir ?? d?.stock_dir ?? "";
  const caps = d?.daily_caps;
  const save = useMutation({
    mutationFn: () => api.setStockConfig(value.trim() || null),
    onSuccess: () => { setDir(null); qc.invalidateQueries({ queryKey: qk.stockSummary() }); },
  });

  return (
    <Card className="mb-3 p-4">
      <p className="mb-3 text-sm text-muted">
        Pre-download catalog works through the <b>Prowlarr → SABnzbd</b> pipeline and keep them on disk,
        so when a user acquires one it's served instantly. Needs the pipeline configured under{" "}
        <span className="text-text">Settings → Integrations</span>.
      </p>
      {d && !d.pipeline_configured && (
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

      {/* Daily caps + today's usage. */}
      <div className="mt-4 flex items-center justify-between gap-2">
        <span className="text-xs font-semibold uppercase tracking-wide text-muted">Daily limits</span>
        <Button size="sm" variant="ghost" onClick={() => setEditCaps(true)}>Edit caps</Button>
      </div>
      <div className="mt-2 flex flex-wrap gap-2">
        <UsageGauge label="Searches today" used={caps?.searches_used_today ?? 0} cap={caps?.searches_per_day ?? 0} />
        <UsageGauge label="Downloads today" used={caps?.downloads_used_today ?? 0} cap={caps?.downloads_per_day ?? 0} />
      </div>
      {editCaps && <CapsEditor onClose={() => setEditCaps(false)} />}
    </Card>
  );
}

function QueueForm() {
  const qc = useQueryClient();
  const summary = useQuery({ queryKey: qk.stockSummary(), queryFn: api.getStockSummary });
  const [name, setName] = useState<string>("");             // operator's batch name (optional)
  const [media, setMedia] = useState<string>("");           // "" = all categories
  const [cat, setCat] = useState<string>("");               // "kind:slug" genre/theme, "" = all
  const [sort, setSort] = useState<string>("popularity");
  const [limit, setLimit] = useState<string>("200");
  const [variant, setVariant] = useState<"ebook" | "audiobook" | "both">("ebook");
  const [entireCatalog, setEntireCatalog] = useState(false);
  const [excludeWeb, setExcludeWeb] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const cats = useQuery({
    queryKey: qk.catalogCategories(media),
    queryFn: () => api.catalogCategories(media || undefined),
    enabled: !entireCatalog,
  });

  const queue = useMutation({
    mutationFn: () => {
      const [dimension, value] = !entireCatalog && cat ? cat.split(":") : [undefined, undefined];
      return api.queueStock({
        name: name.trim() || undefined,
        media: entireCatalog ? undefined : (media || undefined),
        dimension, value,
        sort,
        limit: Math.max(1, Math.min(5000, parseInt(limit) || 200)),
        variant,
        entire_catalog: entireCatalog,
        exclude_web_index: excludeWeb,
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
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
      <label className="text-xs text-muted">Name
        <input className={`${inputCls} mt-1`} placeholder="e.g. Top Sci-Fi"
          value={name} onChange={(e) => setName(e.target.value)} />
      </label>
      {/* Whole-catalog stock hides the narrowing media/genre filters (they'd be ignored). */}
      {!entireCatalog && (
        <>
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
        </>
      )}
      <div className="col-span-full grid gap-2 sm:grid-cols-2">
        <div className="flex items-center justify-between gap-2 rounded-xl border border-[var(--hair,var(--border))] bg-surface-2 px-3 py-2">
          <span className="text-xs">
            <span className="block font-semibold text-text">Stock the entire catalog</span>
            <span className="block text-muted">Ignore the filters; cap still applies</span>
          </span>
          <Toggle checked={entireCatalog} onChange={setEntireCatalog} label="" />
        </div>
        <div className="flex items-center justify-between gap-2 rounded-xl border border-[var(--hair,var(--border))] bg-surface-2 px-3 py-2">
          <span className="text-xs">
            <span className="block font-semibold text-text">Exclude web-crawled titles</span>
            <span className="block text-muted">Skip groups that are crawl-only</span>
          </span>
          <Toggle checked={excludeWeb} onChange={setExcludeWeb} label="" />
        </div>
      </div>
      <div className="col-span-full">
        <Disclosure title="More options" subtitle="Sort, format & cap">
          <div className="grid gap-3 sm:grid-cols-3">
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
              <input className={`${inputCls} mt-1`} type="number" min={1} max={5000}
                value={limit} onChange={(e) => setLimit(e.target.value)} />
            </label>
          </div>
        </Disclosure>
      </div>
      <div className="col-span-full">
        <Button variant="primary" disabled={!ready || queue.isPending} onClick={() => queue.mutate()}>
          {queue.isPending ? "Queuing…" : entireCatalog ? "Queue whole catalog" : "Queue selection"}
        </Button>
        {!ready && <span className="ml-2 text-xs text-muted">Set a stock directory + pipeline first.</span>}
      </div>
      {note && <p className="col-span-full text-sm text-muted">{note}</p>}
    </div>
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
  if (jobs.isLoading) return <Spinner label="Loading…" />;
  if (rows.length === 0)
    return <EmptyState title="No stock batches yet" hint="Queue a selection above — give it a name to track it here." />;
  return (
    <div className="space-y-2">
      {rows.map((j) => <StockJobCard key={j.id ?? "ungrouped"} job={j} onOpen={onOpen} />)}
    </div>
  );
}

function StockJobCard({ job, onOpen }: { job: StockJob; onOpen: (id: number) => void }) {
  const id = job.id ?? 0;
  const pct = Math.round(job.progress * 100);
  return (
    <button
      className="block w-full rounded-xl border border-[var(--hair,var(--border))] bg-surface p-3 text-left transition hover:border-accent/60 hover:bg-surface-2"
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

// Read-only: which list subscriptions currently feed the stock pool (to_stock).
function FeedingLists() {
  const summary = useQuery({ queryKey: qk.stockSummary(), queryFn: api.getStockSummary });
  const lists = summary.data?.feeding_lists ?? [];
  if (lists.length === 0) return null;
  return (
    <Card className="mt-3 p-4">
      <h3 className="mb-1 text-sm font-semibold text-text">Lists feeding stock</h3>
      <p className="mb-3 text-xs text-muted">
        List subscriptions set to stock — new titles are auto-stocked as they appear. Managed under List imports below.
      </p>
      <div className="space-y-2">
        {lists.map((l) => (
          <div key={l.id} className="flex items-center gap-3 rounded-xl border border-[var(--hair,var(--border))] bg-surface p-2.5">
            <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[9px] bg-surface-2 text-muted">📋</span>
            <div className="min-w-0 flex-1">
              <div className="truncate text-sm font-medium text-text">{l.display_name}</div>
              <div className="truncate text-xs text-muted">
                {[l.provider, l.list_name].filter(Boolean).join(" · ")}
                {l.auto_added > 0 ? ` · ${l.auto_added} auto-stocked` : ""}
              </div>
            </div>
            {l.variant !== "ebook" && (
              <StatusChip tone="violet">{l.variant === "both" ? "ebook + audio" : "audiobook"}</StatusChip>
            )}
          </div>
        ))}
      </div>
    </Card>
  );
}

/** The whole Stocking surface, embedded in Sources & Acquisitions (admin-only). */
export default function StockManager({ className = "" }: { className?: string }) {
  const [openJob, setOpenJob] = useState<number | null>(null);
  const summary = useQuery({ queryKey: qk.stockSummary(), queryFn: api.getStockSummary });
  const d = summary.data;
  return (
    <section className={className}>
      <div className="mb-4 flex items-center gap-2.5">
        <h2 className="font-display text-[22px] font-semibold text-text">Stocking</h2>
        {d && <Badge tone={d.configured ? "green" : "amber"}>{d.configured ? "ready" : "not ready"}</Badge>}
        {d && d.total > 0 && <span className="text-sm text-muted">{d.total.toLocaleString()} in pool</span>}
      </div>

      <StockHeader />

      <Disclosure title="Queue a batch" subtitle="Pick what to stock — or stock the whole catalog">
        <QueueForm />
      </Disclosure>

      <Card className="p-4">
        <h3 className="mb-3 text-sm font-semibold text-text">Stock batches</h3>
        <StockJobsList onOpen={setOpenJob} />
      </Card>

      <FeedingLists />

      {openJob !== null && <StockJobModal id={openJob} onClose={() => setOpenJob(null)} />}
    </section>
  );
}
