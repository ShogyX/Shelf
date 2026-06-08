// Library stocking (admin): pre-fetch catalog works through Prowlarr/SABnzbd so they're instantly
// available when a user acquires them. Configure a stock directory, queue a filtered selection
// (media category / genre / theme / popularity, capped), and watch the pool fill in the background.
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, MEDIA_CATEGORIES, StockItem } from "../api/client";
import { Badge, Button, Card, EmptyState, Spinner } from "../components/ui";
import { useConfirm } from "../components/confirm";

const input = "rounded-lg border border-border bg-bg px-3 py-2 text-sm";

const STATUS_TONE: Record<string, "green" | "amber" | "violet" | "default"> = {
  stocked: "green",
  downloading: "violet",
  searching: "violet",
  pending: "default",
  unavailable: "amber",
  failed: "amber",
};
const STATUS_ORDER = ["stocked", "downloading", "searching", "pending", "unavailable", "failed"];

function StockConfigCard() {
  const qc = useQueryClient();
  const summary = useQuery({
    queryKey: ["stock-summary"],
    queryFn: api.getStockSummary,
    refetchInterval: 5000,
  });
  const [dir, setDir] = useState<string | null>(null);
  const d = summary.data;
  const value = dir ?? d?.stock_dir ?? "";
  const save = useMutation({
    mutationFn: () => api.setStockConfig(value.trim() || null),
    onSuccess: () => { setDir(null); qc.invalidateQueries({ queryKey: ["stock-summary"] }); },
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
          <input className={`${input} flex-1`} placeholder="/mnt/NAS-Pool/media/Stock"
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
  const summary = useQuery({ queryKey: ["stock-summary"], queryFn: api.getStockSummary });
  const [media, setMedia] = useState<string>("");           // "" = all categories
  const [cat, setCat] = useState<string>("");               // "kind:slug" genre/theme, "" = all
  const [sort, setSort] = useState<string>("popularity");
  const [limit, setLimit] = useState<string>("200");
  const [note, setNote] = useState<string | null>(null);

  const cats = useQuery({
    queryKey: ["catalog-categories", media],
    queryFn: () => api.catalogCategories(media || undefined),
  });

  const queue = useMutation({
    mutationFn: () => {
      const [dimension, value] = cat ? cat.split(":") : [undefined, undefined];
      return api.queueStock({
        media: media || undefined,
        dimension, value,
        sort,
        limit: Math.max(1, Math.min(2000, parseInt(limit) || 200)),
      });
    },
    onSuccess: (r) => {
      setNote(`Queued ${r.queued} new item${r.queued === 1 ? "" : "s"} (skipped ${r.skipped} already stocked, ${r.selected} matched).`);
      qc.invalidateQueries({ queryKey: ["stock-summary"] });
      qc.invalidateQueries({ queryKey: ["stock-list"] });
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
      <div className="flex flex-wrap items-end gap-2">
        <label className="text-xs text-muted">Media
          <select className={`${input} mt-1 block`} value={media}
            onChange={(e) => { setMedia(e.target.value); setCat(""); }}>
            <option value="">All</option>
            {MEDIA_CATEGORIES.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        </label>
        <label className="text-xs text-muted">Genre / theme
          <select className={`${input} mt-1 block`} value={cat} onChange={(e) => setCat(e.target.value)}>
            <option value="">Any</option>
            {(cats.data?.categories ?? []).map((c) => (
              <option key={`${c.kind}:${c.slug}`} value={`${c.kind}:${c.slug}`}>
                {c.label} ({c.kind}, {c.count})
              </option>
            ))}
          </select>
        </label>
        <label className="text-xs text-muted">Sort
          <select className={`${input} mt-1 block`} value={sort} onChange={(e) => setSort(e.target.value)}>
            <option value="popularity">Most popular</option>
            <option value="new">Newest</option>
            <option value="title">Title A–Z</option>
          </select>
        </label>
        <label className="text-xs text-muted">Cap
          <input className={`${input} mt-1 block w-24`} type="number" min={1} max={2000}
            value={limit} onChange={(e) => setLimit(e.target.value)} />
        </label>
        <Button variant="primary" disabled={!ready || queue.isPending} onClick={() => queue.mutate()}>
          {queue.isPending ? "Queuing…" : "Queue selection"}
        </Button>
      </div>
      {note && <p className="mt-2 text-sm text-muted">{note}</p>}
    </Card>
  );
}

function StatusSummary() {
  const summary = useQuery({
    queryKey: ["stock-summary"], queryFn: api.getStockSummary, refetchInterval: 5000,
  });
  const counts = summary.data?.counts ?? {};
  const shown = STATUS_ORDER.filter((s) => counts[s]);
  if (!shown.length) return null;
  return (
    <div className="mb-3 flex flex-wrap gap-2">
      {shown.map((s) => (
        <Badge key={s} tone={STATUS_TONE[s] ?? "default"}>{s}: {counts[s]}</Badge>
      ))}
    </div>
  );
}

function StockTable() {
  const qc = useQueryClient();
  const [status, setStatus] = useState<string>("");
  const list = useQuery({
    queryKey: ["stock-list", status],
    queryFn: () => api.listStock({ status: status || undefined, limit: 300 }),
    refetchInterval: 5000,
  });
  const inval = () => {
    qc.invalidateQueries({ queryKey: ["stock-list"] });
    qc.invalidateQueries({ queryKey: ["stock-summary"] });
  };
  const del = useMutation({ mutationFn: (id: number) => api.deleteStock(id), onSuccess: inval });
  const clear = useMutation({ mutationFn: (s: string) => api.clearStock(s), onSuccess: inval });

  const rows = list.data ?? [];
  return (
    <Card className="p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h2 className="font-semibold">Stock pool</h2>
        <div className="flex items-center gap-2">
          <select className={input} value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">All statuses</option>
            {STATUS_ORDER.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
          <Button size="sm" variant="ghost" onClick={() => clear.mutate("unavailable")}>Clear unavailable</Button>
          <Button size="sm" variant="ghost" onClick={() => clear.mutate("failed")}>Clear failed</Button>
        </div>
      </div>
      <StatusSummary />
      {list.isLoading ? <Spinner label="Loading…" /> : rows.length === 0 ? (
        <EmptyState title="Nothing stocked yet" hint="Queue a selection above to start filling the stock pool." />
      ) : (
        <div className="divide-y divide-border">
          {rows.map((it) => <StockRow key={it.id} it={it} onDelete={() => del.mutate(it.id)} />)}
        </div>
      )}
    </Card>
  );
}

function StockRow({ it, onDelete }: { it: StockItem; onDelete: () => void }) {
  const confirm = useConfirm();
  const mb = it.size ? `${(it.size / 1_000_000).toFixed(1)} MB` : "";
  return (
    <div className="flex items-center justify-between gap-2 py-2">
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <span className="truncate text-sm font-medium">{it.title}</span>
          <Badge tone="default">{it.media_label}</Badge>
          <Badge tone={STATUS_TONE[it.status] ?? "default"}>{it.status}</Badge>
        </div>
        <div className="truncate text-xs text-muted">
          {[it.author, mb, it.error].filter(Boolean).join(" · ")}
        </div>
      </div>
      <Button size="sm" variant="danger" title="Remove from stock"
        onClick={async () => {
          if (await confirm({ message: `Remove “${it.title}” from stock?`, danger: true, confirmText: "Remove" }))
            onDelete();
        }}>
        ✕
      </Button>
    </div>
  );
}

export default function Stock() {
  return (
    <main className="mx-auto max-w-4xl px-4 py-8">
      <h1 className="mb-1 text-2xl font-semibold">Library stocking</h1>
      <p className="mb-6 text-sm text-muted">
        Operator pre-fetch of catalog works through the usenet pipeline — stocked titles are served
        instantly to users, with no per-user download.
      </p>
      <StockConfigCard />
      <QueueCard />
      <StockTable />
    </main>
  );
}
