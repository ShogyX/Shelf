// The library work detail "sheet" (Wave 5): a cinematic cover/hero + tabbed Overview / Chapters /
// Sources / Details for a single library title. Opened by clicking a work's poster anywhere in the
// library (grid cards, home rails). Reuses the existing per-work primitives (ShelfMenu, SendDialog,
// FixMetadataDialog, RelatedTitles) rather than rebuilding them.
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, WorkDetail } from "../api/client";
import { qk } from "../api/queryKeys";
import { useApp } from "../store";
import { useAudio } from "../audioStore";
import Cover, { coverSrc } from "./Cover";
import RelatedTitles from "./RelatedTitles";
import SendDialog from "./SendDialog";
import { ShelfMenu, FixMetadataDialog } from "../pages/Library";
import {
  Badge, Button, Chip, EmptyState, Modal, OverflowMenu, SegmentedControl, Spinner, StatusChip,
  type StatusTone,
} from "./ui";

// library_status → a StatusChip tone (the redesign's semantic palette).
const STATUS_TONE: Record<string, StatusTone> = {
  paused: "neutral", gathering: "warning", ongoing: "violet",
  complete: "success", incomplete: "danger",
};

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  const mb = n / (1024 * 1024);
  if (mb < 1) return `${(n / 1024).toFixed(0)} KB`;
  if (mb < 1024) return `${mb.toFixed(1)} MB`;
  return `${(mb / 1024).toFixed(2)} GB`;
}
function fmtDate(s: string): string {
  const d = new Date(s);
  return isNaN(d.getTime()) ? s : d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

type Tab = "overview" | "chapters" | "sources" | "details";

export default function WorkDetailModal({ workId, onClose }: { workId: number; onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useApp((s) => s.toast);
  const navigate = useNavigate();
  const [tab, setTab] = useState<Tab>("overview");
  const [showSend, setShowSend] = useState(false);
  const [showFix, setShowFix] = useState(false);

  const { data: work, isLoading } = useQuery({ queryKey: qk.work(workId), queryFn: () => api.getWork(workId) });
  const { data: shelves = [] } = useQuery({ queryKey: qk.bookshelves(), queryFn: api.listBookshelves });
  const { data: subs = [] } = useQuery({ queryKey: qk.subscriptions(), queryFn: api.listSubscriptions });

  const enrich = useMutation({
    mutationFn: () => api.enrichWork(workId),
    onSuccess: () => { qc.invalidateQueries({ queryKey: qk.work(workId) }); toast("Metadata refreshed.", "success"); },
    onError: (e) => toast((e as Error).message, "error"),
  });
  const check = useMutation({
    mutationFn: () => api.checkWorkUpdates(workId),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: qk.work(workId) });
      qc.invalidateQueries({ queryKey: qk.works() });
      if (!r.checked) toast("This title's source doesn't get new chapters.");
      else if (r.error) toast(`Update check failed: ${r.error}`, "error");
      else if (r.new_chapters > 0) toast(`Found ${r.new_chapters} new chapter${r.new_chapters === 1 ? "" : "s"} — gathering now.`, "success");
      else toast(r.metadata_changed ? "Metadata refreshed; no new chapters." : "Already up to date.");
    },
    onError: (e) => toast((e as Error).message, "error"),
  });
  const repair = useMutation({
    mutationFn: () => api.repairWork(workId),
    onSuccess: (rep) => {
      qc.invalidateQueries({ queryKey: qk.work(workId) });
      qc.invalidateQueries({ queryKey: qk.works() });
      toast(`Diagnosis: ${rep.health}. ${rep.detail ?? ""} — ${rep.actions.length ? rep.actions.join("; ") : "no fixable issues found"}.`);
    },
    onError: (e) => toast((e as Error).message, "error"),
  });
  const pause = useMutation({
    mutationFn: () => api.pauseWork(workId),
    onSuccess: () => { qc.invalidateQueries({ queryKey: qk.work(workId) }); qc.invalidateQueries({ queryKey: qk.works() }); toast("Paused — automatic updates are off for this title."); },
    onError: (e) => toast((e as Error).message, "error"),
  });
  const resume = useMutation({
    mutationFn: () => api.resumeWork(workId),
    onSuccess: () => { qc.invalidateQueries({ queryKey: qk.work(workId) }); qc.invalidateQueries({ queryKey: qk.works() }); toast("Resumed — checking for new chapters.", "success"); },
    onError: (e) => toast((e as Error).message, "error"),
  });
  const remove = useMutation({
    mutationFn: () => api.deleteWork(workId),
    onSuccess: () => {
      for (const key of [qk.works(), qk.continue(), qk.continueListening(), qk.bookshelves()])
        qc.invalidateQueries({ queryKey: key });
      onClose();
    },
    onError: (e) => toast((e as Error).message, "error"),
  });

  // Follow series/author — reflect already-following state from the subscriptions list. Best-effort:
  // matches on display_name (the only field correlating a library work to a follow). A follow made
  // elsewhere with a differently-normalized name may not light up "Following", but the backend dedupes
  // on its normalized key, so re-following is idempotent (never a duplicate).
  const seriesSub = work?.series ? subs.find((s) => s.kind === "series" && s.display_name === work.series) : undefined;
  const authorSub = work?.author ? subs.find((s) => s.kind === "author" && s.display_name === work.author) : undefined;
  const followMut = useMutation({
    mutationFn: (v: { kind: "series" | "author" }) =>
      v.kind === "series"
        ? api.follow({ kind: "series", series_name: work!.series! })
        : api.follow({ kind: "author", author_name: work!.author! }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: qk.subscriptions() }); toast("Following — new titles will be gathered automatically.", "success"); },
    onError: (e) => toast((e as Error).message, "error"),
  });
  const unfollowMut = useMutation({
    mutationFn: (id: number) => api.unfollow(id),
    onSuccess: () => { qc.invalidateQueries({ queryKey: qk.subscriptions() }); toast("Unfollowed."); },
    onError: (e) => toast((e as Error).message, "error"),
  });

  const tabs: { value: Tab; label: string }[] = [
    { value: "overview", label: "Overview" },
    { value: "chapters", label: "Chapters" },
    { value: "sources", label: "Sources" },
    { value: "details", label: "Details" },
  ];

  return (
    <Modal title={work ? (work.series || work.author || "Title") : "Loading…"} onClose={onClose} variant="fullscreen-sheet" width="max-w-3xl">
      {isLoading || !work ? (
        <div className="py-12"><Spinner label="Loading…" /></div>
      ) : (
        <div className="space-y-5">
          <Hero
            work={work}
            onRead={() => navigate(work.last_chapter_id ? `/read/${workId}/${work.last_chapter_id}` : `/read/${workId}`)}
            onListen={work.audiobook_work_id ? () => useAudio.getState().playWork(work.audiobook_work_id!) : undefined}
            shelves={shelves}
            menu={
              <OverflowMenu
                label={`More actions for ${work.title}`}
                items={[
                  // Follow series/author — set-and-forget, folded into the menu (off the action row).
                  work.series && {
                    label: seriesSub ? "✓ Following series" : "+ Follow series",
                    disabled: followMut.isPending || unfollowMut.isPending,
                    onClick: () => seriesSub ? unfollowMut.mutate(seriesSub.id) : followMut.mutate({ kind: "series" }),
                  },
                  work.author && {
                    label: authorSub ? "✓ Following author" : "+ Follow author",
                    disabled: followMut.isPending || unfollowMut.isPending,
                    onClick: () => authorSub ? unfollowMut.mutate(authorSub.id) : followMut.mutate({ kind: "author" }),
                  },
                  { label: "📤 Send / export", onClick: () => setShowSend(true) },
                  { label: enrich.isPending ? "Refreshing…" : "↻ Refresh metadata", disabled: enrich.isPending, onClick: () => enrich.mutate() },
                  { label: "✎ Edit metadata", onClick: () => setShowFix(true) },
                  { label: check.isPending ? "Checking…" : "⟳ Check for updates", disabled: check.isPending, onClick: () => check.mutate() },
                  (work.health === "incomplete" || work.library_status === "incomplete") && {
                    label: repair.isPending ? "Fixing…" : "🩺 Repair", disabled: repair.isPending, onClick: () => repair.mutate(),
                  },
                  work.library_status === "paused"
                    ? { label: resume.isPending ? "Resuming…" : "▶ Resume", disabled: resume.isPending, onClick: () => resume.mutate() }
                    : work.hooked && work.status === "ongoing" && { label: pause.isPending ? "Pausing…" : "⏸ Pause", disabled: pause.isPending, onClick: () => pause.mutate() },
                  { label: "Remove from library", danger: true, onClick: () => remove.mutate() },
                ]}
              />
            }
          />

          <SegmentedControl<Tab> value={tab} onChange={setTab} options={tabs} ariaLabel="Detail section" />

          {tab === "overview" && <OverviewTab work={work} workId={workId} />}
          {tab === "chapters" && <ChaptersTab work={work} workId={workId} onPick={(cid) => navigate(`/read/${workId}/${cid}`)} />}
          {tab === "sources" && <SourcesTab work={work} workId={workId} onRepair={() => repair.mutate()} onCheck={() => check.mutate()} repairBusy={repair.isPending} checkBusy={check.isPending} />}
          {tab === "details" && <DetailsTab work={work} onRefresh={() => enrich.mutate()} onEdit={() => setShowFix(true)} refreshBusy={enrich.isPending} />}
        </div>
      )}

      {showSend && work && <SendDialog workId={workId} title={work.title} onClose={() => setShowSend(false)} />}
      {showFix && work && <FixMetadataDialog work={work} onClose={() => setShowFix(false)} />}
    </Modal>
  );
}

function Hero({
  work, onRead, onListen, shelves, menu,
}: {
  work: WorkDetail;
  onRead: () => void;
  onListen?: () => void;
  shelves: import("../api/client").Bookshelf[];
  menu: React.ReactNode;
}) {
  const reading = work.library_status === "gathering";
  return (
    <div className="flex flex-col gap-5 sm:flex-row">
      <div className="mx-auto w-36 shrink-0 sm:mx-0">
        <div className="aspect-[2/3] w-36 overflow-hidden rounded-[13px] border border-[var(--hair,var(--border))] shadow-[var(--pop-shadow)]">
          {coverSrc(work.cover_url) ? (
            <img src={coverSrc(work.cover_url)!} alt="" className="h-full w-full object-cover" />
          ) : (
            <Cover title={work.title} author={work.author} small />
          )}
        </div>
      </div>

      <div className="min-w-0 flex-1">
        <h2 className="font-display text-[28px] font-semibold leading-[1.1] text-text sm:text-4xl">{work.title}</h2>
        {work.author && <div className="mt-1.5 text-sm font-semibold text-[var(--text-soft,var(--muted))]">{work.author}</div>}

        {/* Meta chips: one accent pill (status) + the ★ rating; the quiet facts stay neutral. */}
        <div className="mt-3 flex flex-wrap items-center gap-1.5">
          <StatusChip tone={STATUS_TONE[work.library_status] ?? "neutral"}>{work.library_status}</StatusChip>
          {work.rating != null && <StatusChip tone="neutral">★ {work.rating.toFixed(1)}</StatusChip>}
          {work.year != null && <Badge>{work.year}</Badge>}
          <Badge>{work.media_kind === "comic" ? "Comic" : work.media_kind === "audio" ? "Audiobook" : "Book"}</Badge>
          {work.series && <Badge>{work.series}{work.series_position != null ? ` · #${work.series_position}` : ""}</Badge>}
          {work.page_count != null && <Badge>{work.page_count} pp</Badge>}
        </div>

        {/* Primary actions */}
        <div className="mt-4 flex flex-wrap items-center gap-2">
          <Button variant="primary" onClick={onRead}>{reading ? "Read" : work.scroll_fraction > 0 || work.last_chapter_id ? "Continue" : "Read"}</Button>
          {onListen && <Button variant="outline" onClick={onListen}>🎧 Listen</Button>}
          <ShelfMenu work={work} shelves={shelves} />
          {menu}
        </div>
      </div>
    </div>
  );
}

function OverviewTab({ work, workId }: { work: WorkDetail; workId: number }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div className="space-y-4">
      {work.description ? (
        <div>
          <p className={`whitespace-pre-line text-sm leading-relaxed text-[var(--text-soft,var(--muted))] ${expanded ? "" : "line-clamp-6"}`}>
            {work.description}
          </p>
          {work.description.length > 280 && (
            <button onClick={() => setExpanded((v) => !v)} className="mt-1 text-xs font-semibold text-accent hover:underline">
              {expanded ? "Show less" : "Show more"}
            </button>
          )}
        </div>
      ) : (
        <p className="text-sm text-muted">No description yet.</p>
      )}
      <RelatedTitles workId={workId} />
    </div>
  );
}

function ChaptersTab({ work, workId, onPick }: { work: WorkDetail; workId: number; onPick: (chapterId: number) => void }) {
  const { data: chapters = [], isLoading } = useQuery({ queryKey: qk.chaptersAll(workId), queryFn: () => api.listAllChapters(workId) });
  if (isLoading) return <div className="py-6"><Spinner label="Loading chapters…" /></div>;
  if (chapters.length === 0) return <EmptyState title="No chapters yet" hint="Chapters appear here as they're gathered." />;
  return (
    <div>
      <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">
        {work.chapters_fetched} / {Math.max(work.chapters_total, work.chapters_fetched)} gathered
      </div>
      <div className="max-h-[46vh] divide-y divide-[var(--hair,var(--border))] overflow-y-auto rounded-xl border border-[var(--hair,var(--border))]">
        {chapters.map((c) => (
          <button
            key={c.id}
            onClick={() => c.has_content && onPick(c.id)}
            disabled={!c.has_content}
            className="flex w-full items-center gap-3 px-3 py-2 text-left text-sm transition hover:bg-surface-2 disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:bg-transparent"
          >
            <span className="w-10 shrink-0 text-xs tabular-nums text-muted">{c.number}</span>
            <span className="min-w-0 flex-1 truncate text-text">{c.title || `Chapter ${c.number}`}</span>
            <span className="shrink-0 text-xs text-muted">{c.has_content ? "→" : "…"}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

function SourcesTab({
  work, workId, onRepair, onCheck, repairBusy, checkBusy,
}: {
  work: WorkDetail; workId: number;
  onRepair: () => void; onCheck: () => void; repairBusy: boolean; checkBusy: boolean;
}) {
  const { data: prov } = useQuery({ queryKey: ["work-provenance", workId], queryFn: () => api.getWorkProvenance(workId) });
  const healthy = work.health === "ok";
  return (
    <div className="space-y-4">
      {/* Provenance */}
      {prov && (prov.source_name || prov.source_ref || prov.filename || prov.catalog_title) && (
        <div className="rounded-xl border border-[var(--hair,var(--border))] bg-surface-2/40 p-3 text-xs">
          <div className="mb-1.5 font-semibold uppercase tracking-wide text-muted">Where this came from</div>
          <div className="space-y-1">
            {(prov.source_name || prov.source_ref) && (
              <div className="flex gap-2">
                <span className="w-20 shrink-0 text-muted">Source</span>
                <span className="min-w-0 flex-1 break-words text-text">
                  {prov.source_name || "—"}{prov.source_ref ? ` · ${prov.source_ref}` : ""}
                  {prov.source_url && <a href={prov.source_url} target="_blank" rel="noreferrer" className="ml-1 text-accent underline">open</a>}
                </span>
              </div>
            )}
            {prov.filename && (
              <div className="flex gap-2"><span className="w-20 shrink-0 text-muted">File</span><span className="min-w-0 flex-1 break-words text-text">{prov.filename}</span></div>
            )}
            {prov.catalog_title && (
              <div className="flex gap-2">
                <span className="w-20 shrink-0 text-muted">Catalog</span>
                <span className="min-w-0 flex-1 break-words text-text">{prov.catalog_title}{prov.catalog_author ? ` · ${prov.catalog_author}` : ""}{prov.catalog_domain ? ` · ${prov.catalog_domain}` : ""}</span>
              </div>
            )}
          </div>
        </div>
      )}

      {/* Health */}
      <div className="rounded-xl border border-[var(--hair,var(--border))] p-3">
        <div className="mb-2 flex items-center gap-2">
          <span className="text-xs font-semibold uppercase tracking-wide text-muted">Health</span>
          <StatusChip tone={healthy ? "success" : work.health === "incomplete" ? "danger" : "neutral"}>{work.health}</StatusChip>
        </div>
        {work.health_detail && <p className="mb-2 text-sm text-[var(--text-soft,var(--muted))]">{work.health_detail}</p>}
        <div className="flex flex-wrap gap-2">
          <Button size="sm" variant="outline" disabled={checkBusy} onClick={onCheck}>{checkBusy ? "Checking…" : "⟳ Check for updates"}</Button>
          {!healthy && <Button size="sm" variant="outline" disabled={repairBusy} onClick={onRepair}>{repairBusy ? "Fixing…" : "🩺 Repair"}</Button>}
        </div>
      </div>

      {/* Metadata links + related (confirm/unlink lives inside RelatedTitles) */}
      <RelatedTitles workId={workId} />
    </div>
  );
}

function DetailsTab({ work, onRefresh, onEdit, refreshBusy }: { work: WorkDetail; onRefresh: () => void; onEdit: () => void; refreshBusy: boolean }) {
  const isbns = Array.isArray((work.identifiers as Record<string, unknown> | null)?.isbn)
    ? ((work.identifiers as Record<string, unknown>).isbn as unknown[]).map(String)
    : [];
  const crawl = work.crawl_interval_s
    ? `every ${Math.round(work.crawl_interval_s / 3600)}h${work.crawl_window_start != null && work.crawl_window_end != null ? ` · ${work.crawl_window_start}:00–${work.crawl_window_end}:00` : ""}`
    : null;

  const rows: { label: string; value: React.ReactNode }[] = [];
  if (work.rating != null)
    rows.push({ label: "Rating", value: `${work.rating.toFixed(1)} ★${work.rating_count != null ? ` · ${work.rating_count.toLocaleString()} ratings` : ""}` });
  if (work.year != null) rows.push({ label: "Year", value: work.year });
  if (work.genres && work.genres.length > 0)
    rows.push({ label: "Genres", value: <span className="flex flex-wrap gap-1.5">{work.genres.map((g) => <Chip key={g}>{g}</Chip>)}</span> });
  if (work.publisher) rows.push({ label: "Publisher", value: work.publisher });
  if (work.narrator) rows.push({ label: "Narrator", value: work.narrator });
  if (work.language) rows.push({ label: "Language", value: work.language });
  if (work.page_count != null) rows.push({ label: "Pages", value: work.page_count });
  rows.push({ label: "Status", value: work.library_status });
  if (isbns.length > 0) rows.push({ label: isbns.length > 1 ? "ISBNs" : "ISBN", value: isbns.join(", ") });
  if (work.created_at) rows.push({ label: "Added", value: fmtDate(work.created_at) });
  if (work.local_size != null) rows.push({ label: "Size", value: fmtBytes(work.local_size) });
  if (crawl) rows.push({ label: "Update schedule", value: crawl });

  return (
    <div className="space-y-4">
      <dl className="grid grid-cols-1 gap-x-6 gap-y-2.5 sm:grid-cols-2">
        {rows.map((r) => (
          <div key={r.label} className="flex flex-col gap-0.5 border-b border-[var(--hair,var(--border))] pb-2 last:border-0 sm:flex-row sm:items-baseline sm:gap-3">
            <dt className="shrink-0 text-xs font-semibold uppercase tracking-wide text-muted sm:w-28">{r.label}</dt>
            <dd className="min-w-0 text-sm text-text">{r.value}</dd>
          </div>
        ))}
      </dl>
      <div className="flex flex-wrap gap-2">
        <Button size="sm" variant="outline" disabled={refreshBusy} onClick={onRefresh}>{refreshBusy ? "Refreshing…" : "↻ Refresh metadata"}</Button>
        <Button size="sm" variant="outline" onClick={onEdit}>✎ Edit metadata</Button>
      </div>
    </div>
  );
}
