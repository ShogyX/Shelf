import { Navigate, useSearchParams } from "react-router-dom";
import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query";
import { api, Bookshelf, MetaCandidate, Work } from "../api/client";
import { qk } from "../api/queryKeys";
import { useEffect, useState } from "react";
import { Button, FormField, inputCls, Modal, StatusChip, useEdgeFlip } from "../components/ui";
import Cover from "../components/Cover";
import type { Tone } from "../components/IndexShared";
import { useApp } from "../store";
import LibraryHome from "../components/LibraryHome";

// One clear, friendly state per title (computed server-side as work.library_status). Exported so the
// shared LibraryGrid renders the exact same per-work status badge.
export const STATUS_BADGE: Record<string, { label: string; tone: Tone; icon: string; help: string }> = {
  paused: { label: "Paused", tone: "default", icon: "⏸",
    help: "Automatic updates are off — Resume to gather new chapters again." },
  gathering: { label: "Gathering", tone: "amber", icon: "↓",
    help: "Downloading chapters now." },
  ongoing: { label: "Ongoing", tone: "violet", icon: "●",
    help: "Caught up — new chapters are gathered as the series releases them." },
  complete: { label: "Complete", tone: "green", icon: "✓",
    help: "The series has finished and every chapter is gathered." },
  incomplete: { label: "Incomplete", tone: "red", icon: "!",
    help: "Some chapters are missing or couldn't be fetched." },
};

/** Per-work control to toggle which bookshelves the work is on. */
export function ShelfMenu({ work, shelves }: { work: Work; shelves: Bookshelf[] }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const { ref, style } = useEdgeFlip<HTMLDivElement>(open, 192, "left"); // 192 = w-48; clamp into the viewport
  const on = new Set(work.shelf_ids);
  const toggle = useMutation({
    mutationFn: ({ shelfId, add }: { shelfId: number; add: boolean }) =>
      add ? api.addWorkToShelf(shelfId, work.id) : api.removeWorkFromShelf(shelfId, work.id),
    // Optimistic: flip this work's shelf_ids across every cached works list so the checkbox AND the
    // "🗂 Shelves (N)" count update instantly. onSettled invalidation reconciles (incl. shelf-filtered
    // lists, where a removed work should drop out — left to the refetch, not done optimistically).
    onMutate: async ({ shelfId, add }) => {
      await qc.cancelQueries({ queryKey: qk.works() });
      const prev = qc.getQueriesData<Work[]>({ queryKey: qk.works() });
      for (const [key, list] of prev) {
        if (!list) continue;
        qc.setQueryData<Work[]>(
          key,
          list.map((w) =>
            w.id !== work.id
              ? w
              : {
                  ...w,
                  shelf_ids: add
                    ? [...new Set([...w.shelf_ids, shelfId])]
                    : w.shelf_ids.filter((id) => id !== shelfId),
                },
          ),
        );
      }
      return { prev };
    },
    onError: (_e, _v, ctx) => {
      for (const [key, list] of ctx?.prev ?? []) qc.setQueryData(key, list);
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: qk.works() });
      qc.invalidateQueries({ queryKey: qk.bookshelves() });
    },
  });
  if (shelves.length === 0) return null;
  return (
    <div ref={ref} className="relative">
      <Button size="sm" variant="outline" title="Add to a bookshelf" onClick={() => setOpen((o) => !o)}>
        🗂 Shelves{on.size ? ` (${on.size})` : ""}
      </Button>
      {open && (
        <>
          <div className="fixed inset-0 z-10" onClick={() => setOpen(false)} />
          <div
            style={style}
            className="absolute left-0 top-full z-20 w-48 max-w-[calc(100vw-1rem)] rounded-lg border border-border bg-surface p-1 shadow-xl"
          >
            {shelves.map((s) => (
              <label
                key={s.id}
                className="flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 text-sm hover:bg-surface-2"
              >
                <input
                  type="checkbox"
                  checked={on.has(s.id)}
                  disabled={toggle.isPending}
                  onChange={(e) => toggle.mutate({ shelfId: s.id, add: e.target.checked })}
                />
                <span className="truncate">{s.name}</span>
              </label>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

export default function Library() {
  // Search text comes from ?q= (driven by the single nav search box). When a search is active the
  // cinematic home isn't the right surface — the dense, manage-everything grid is — so we hand off to
  // /library/browse, which renders the full grid + multi-select for the same ?q=. (Browse is also in
  // NavSearch's `searchable` routes, so the nav box keeps live-updating ?q= there.)
  const [sp] = useSearchParams();
  const q = (sp.get("q") ?? "").trim();
  if (q) return <Navigate to={`/library/browse?q=${encodeURIComponent(q)}`} replace />;

  // Home: ONLY the cinematic hero + rails (incl. per-bookshelf rails). The full grid, multi-select
  // and shelf CRUD now live on /library/browse and Settings → Bookshelves respectively.
  return <LibraryHome />;
}

/** Correct a library work's metadata: edit title/author/series/cover directly, or search a metadata
 *  provider and apply a match. Saves via PATCH /works/{id}. */
export function FixMetadataDialog({ work, onClose }: { work: Work; onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useApp((s) => s.toast);
  const [title, setTitle] = useState(work.title);
  const [author, setAuthor] = useState(work.author ?? "");
  const [series, setSeries] = useState(work.series ?? "");
  const [seriesPos, setSeriesPos] = useState(work.series_position != null ? String(work.series_position) : "");
  const [coverUrl, setCoverUrl] = useState(work.cover_url ?? "");
  const [q, setQ] = useState(work.title);
  const [candidates, setCandidates] = useState<MetaCandidate[] | null>(null);
  // Provenance: where this title was fetched (source/file) + the catalog/import metadata used — so a
  // wrong match is diagnosable. source_work_ref is editable to fix the fetching source.
  const prov = useQuery({ queryKey: ["work-provenance", work.id], queryFn: () => api.getWorkProvenance(work.id) });
  const [sourceRef, setSourceRef] = useState<string | null>(null); // null = not yet seeded from provenance
  useEffect(() => { if (prov.data && sourceRef === null) setSourceRef(prov.data.source_ref ?? ""); }, [prov.data, sourceRef]);

  const search = useMutation({
    mutationFn: () => api.searchWorkMetadata(work.id, q.trim(), author.trim() || undefined),
    onSuccess: (rows) => setCandidates(rows),
    onError: (e) => toast((e as Error).message, "error"),
  });
  const save = useMutation({
    mutationFn: () => api.updateWorkMetadata(work.id, {
      title: title.trim(),
      author: author.trim() || null,
      series: series.trim() || null,
      series_position: seriesPos.trim() ? Number(seriesPos) : null,
      cover_url: coverUrl.trim() || null,
      ...(sourceRef !== null ? { source_work_ref: sourceRef.trim() || null } : {}),
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.works() });
      qc.invalidateQueries({ queryKey: qk.work(work.id) });
      qc.invalidateQueries({ queryKey: qk.continue() });
      toast(`Updated “${title.trim()}”`, "success");
      onClose();
    },
    onError: (e) => toast((e as Error).message, "error"),
  });
  const applyCandidate = (c: MetaCandidate) => {
    setTitle(c.title);
    if (c.author) setAuthor(c.author);
    if (c.cover_url) setCoverUrl(c.cover_url);
  };

  return (
    <Modal
      variant="fullscreen-sheet"
      width="max-w-lg"
      title="Fix metadata"
      onClose={onClose}
      footer={
        <div className="flex w-full justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button variant="primary" disabled={save.isPending || !title.trim()} onClick={() => {
            if (seriesPos.trim() && !Number.isFinite(Number(seriesPos))) {
              toast("Vol # must be a number.", "error");
              return;
            }
            save.mutate();
          }}>
            {save.isPending ? "Saving…" : "Save"}
          </Button>
        </div>
      }
    >
      <div className="space-y-4">
        {/* Provenance — where this title was fetched, the catalog metadata used, and what was
            originally requested. Surfaces a wrong match (e.g. a file/source that doesn't match the
            requested title) so the user can correct the metadata and the fetching source. */}
        {prov.data && (prov.data.source_name || prov.data.source_ref || prov.data.filename || prov.data.catalog_title || prov.data.request_title) && (
          <div className="rounded-2xl border border-[var(--hair-strong,var(--border))] bg-surface-2/40 p-3 text-xs">
            <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-[var(--text-soft,var(--muted))]">Where this came from</div>
            <div className="space-y-1.5">
              {(prov.data.source_name || prov.data.source_ref) && (
                <div className="flex gap-2">
                  <span className="w-20 shrink-0 text-muted">Source</span>
                  <span className="min-w-0 flex-1 break-words text-text">
                    {prov.data.source_name || "—"}{prov.data.source_ref ? ` · ${prov.data.source_ref}` : ""}
                    {prov.data.source_url && (
                      <a href={prov.data.source_url} target="_blank" rel="noreferrer" className="ml-1 text-accent underline">open</a>
                    )}
                  </span>
                </div>
              )}
              {prov.data.filename && (
                <div className="flex gap-2">
                  <span className="w-20 shrink-0 text-muted">File</span>
                  <span className="min-w-0 flex-1 break-words text-text">{prov.data.filename}</span>
                </div>
              )}
              {prov.data.catalog_title && (
                <div className="flex gap-2">
                  <span className="w-20 shrink-0 text-muted">Catalog</span>
                  <span className="min-w-0 flex-1 break-words text-text">
                    {prov.data.catalog_title}{prov.data.catalog_author ? ` · ${prov.data.catalog_author}` : ""}
                    {prov.data.catalog_domain ? ` · ${prov.data.catalog_domain}` : ""}
                  </span>
                </div>
              )}
              {prov.data.request_title && (
                <div className="flex gap-2">
                  <span className="w-20 shrink-0 text-muted">Requested</span>
                  <span className="min-w-0 flex-1 break-words text-text">
                    {prov.data.request_title}{prov.data.request_author ? ` · ${prov.data.request_author}` : ""}
                    {(prov.data.request_detail || prov.data.request_origin) ? ` · via ${prov.data.request_detail || prov.data.request_origin}` : ""}
                  </span>
                </div>
              )}
            </div>
          </div>
        )}
        <div className="flex gap-3">
          <div className="h-28 w-20 shrink-0 overflow-hidden rounded-lg border border-[var(--hair-strong,var(--border))]">
            <Cover title={title || work.title} author={author} coverUrl={coverUrl || null} small />
          </div>
          <div className="min-w-0 flex-1">
            <FormField label="Title">
              <input className={inputCls} value={title} onChange={(e) => setTitle(e.target.value)} />
            </FormField>
            <FormField label="Author">
              <input className={inputCls} value={author} onChange={(e) => setAuthor(e.target.value)} placeholder="(unknown)" />
            </FormField>
          </div>
        </div>
        <div className="flex gap-2">
          <div className="flex-1">
            <FormField label="Series">
              <input className={inputCls} value={series} onChange={(e) => setSeries(e.target.value)} placeholder="(none)" />
            </FormField>
          </div>
          <div className="w-24">
            <FormField label="Vol #">
              <input className={inputCls} value={seriesPos} onChange={(e) => setSeriesPos(e.target.value)} inputMode="decimal" placeholder="–" />
            </FormField>
          </div>
        </div>
        <FormField label="Cover URL">
          <input className={inputCls} value={coverUrl} onChange={(e) => setCoverUrl(e.target.value)} placeholder="https://…  (blank = generated cover)" />
        </FormField>
        {prov.data && (prov.data.source_name || prov.data.source_ref) && (
          <FormField label={<>Source reference{prov.data.source_name ? <span className="opacity-70"> ({prov.data.source_name})</span> : null}</>}>
            <input className={inputCls} value={sourceRef ?? ""} onChange={(e) => setSourceRef(e.target.value)}
              placeholder="this title's ref / URL on the source — fix a wrong fetching source" />
          </FormField>
        )}

        <div className="border-t border-[var(--hair,var(--border))] pt-3">
          <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-[var(--text-soft,var(--muted))]">Search a metadata provider</div>
          <div className="flex gap-2">
            <input
              className={inputCls}
              value={q}
              onChange={(e) => setQ(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") search.mutate(); }}
              placeholder="Title to search…"
            />
            <Button variant="outline" disabled={search.isPending || !q.trim()} onClick={() => search.mutate()}>
              {search.isPending ? "Searching…" : "Search"}
            </Button>
          </div>
          {candidates && candidates.length === 0 && (
            <p className="mt-2 text-xs leading-snug text-[var(--text-soft,var(--muted))]">
              No matches — or no metadata providers are enabled (Settings → Integrations). You can still edit the fields above by hand.
            </p>
          )}
          {candidates && candidates.length > 0 && (
            <div className="mt-2 space-y-1.5">
              {candidates.map((c) => (
                <div key={`${c.provider}:${c.ref}`} className="flex items-center gap-2.5 rounded-xl border border-[var(--hair-strong,var(--border))] bg-surface p-2">
                  <div className="h-14 w-10 shrink-0 overflow-hidden rounded border border-[var(--hair,var(--border))]">
                    <Cover title={c.title} author={c.author} coverUrl={c.cover_url} small />
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-sm font-medium text-text">{c.title}</div>
                    <div className="mt-0.5 flex items-center gap-1.5 truncate text-xs text-[var(--text-soft,var(--muted))]">
                      <span className="truncate">{c.author ?? "Unknown"}{c.year ? ` · ${c.year}` : ""}</span>
                      <StatusChip tone="neutral">{c.provider}</StatusChip>
                    </div>
                  </div>
                  <Button size="sm" variant="ghost" onClick={() => applyCandidate(c)}>Use</Button>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </Modal>
  );
}
