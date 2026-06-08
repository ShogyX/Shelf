import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, IndexSite, IndexedPage } from "../api/client";
import { Badge, Button, Card, Spinner } from "./ui";
import { useApp } from "../store";

export function fmtDuration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

export type Tone = "green" | "amber" | "violet" | "red" | "default";

/** A crawl is open-ended (it can't know its end), so show WHAT it's doing rather than a % bar:
 *  running · cooling down (backing off after a block) · finished · paused · error. */
export function siteStatus(site: IndexSite): { label: string; tone: Tone } {
  if (site.status === "active") {
    const cooling =
      site.cooldown_until && new Date(site.cooldown_until).getTime() > Date.now();
    return cooling ? { label: "cooling down", tone: "amber" } : { label: "running", tone: "violet" };
  }
  if (site.status === "done") return { label: "finished", tone: "green" };
  if (site.status === "paused") return { label: "paused", tone: "default" };
  if (site.status === "removed") return { label: "removed", tone: "default" };
  if (site.status === "failed") return { label: "error", tone: "red" };
  return { label: site.status, tone: "default" };
}

export function healthBadge(h: string): { tone: Tone; label: string } | null {
  switch (h) {
    case "ok":
      return { tone: "green", label: "complete" };
    case "incomplete":
      return { tone: "amber", label: "incomplete" };
    case "no_chapters":
      return { tone: "red", label: "no chapters" };
    case "unreachable":
      return { tone: "red", label: "unreachable" };
    default:
      return null; // "unknown" → no badge
  }
}

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="flex flex-col" title={hint}>
      <span className="text-lg font-semibold tabular-nums text-text">{value}</span>
      <span className="text-xs text-muted">{label}</span>
    </div>
  );
}

/** Aggregate crawl observability: titles found, requests, time, and site status mix.
 *  Rendered on the Jobs page (crawl progress lives alongside the backfill jobs). */
export function CrawlStats() {
  const stats = useQuery({
    queryKey: ["index-stats"],
    queryFn: api.indexStats,
    refetchInterval: (q) => (q.state.data && q.state.data.sites_active > 0 ? 2500 : false),
  });
  const d = stats.data;
  if (!d) return null;
  return (
    <Card className="mb-4 p-4">
      <div className="mb-3 grid grid-cols-2 gap-4 sm:grid-cols-4">
        <Stat label="Titles found" value={(d.titles_found ?? 0).toLocaleString()} />
        <Stat
          label="Requests made"
          value={(d.requests_made ?? 0).toLocaleString()}
          hint="Pages requested (fetched + failed)"
        />
        <Stat
          label="Time spent"
          value={fmtDuration(d.time_spent_seconds ?? 0)}
          hint="Total crawl time, summed across all sites (parallel crawls each count)"
        />
        <Stat label="Words indexed" value={(d.words_indexed ?? 0).toLocaleString()} />
      </div>
      <div className="flex flex-wrap items-center gap-2 border-t border-border pt-3 text-xs">
        <span className="text-muted">Sites:</span>
        {d.sites_active > 0 && <Badge tone="violet">{d.sites_active} in-progress</Badge>}
        {d.sites_done > 0 && <Badge tone="green">{d.sites_done} complete</Badge>}
        {d.sites_paused > 0 && <Badge tone="amber">{d.sites_paused} aborted</Badge>}
        {d.sites_failed > 0 && <Badge tone="red">{d.sites_failed} error</Badge>}
        <span className="ml-auto text-muted">
          {d.pages_fetched.toLocaleString()} fetched · {d.pages_pending.toLocaleString()} queued ·{" "}
          {d.pages_failed.toLocaleString()} failed
        </span>
      </div>
    </Card>
  );
}

/** One indexed site: crawl progress, controls, editable idle-stop threshold, page browser.
 *  Lives on the Jobs page (the indexing crawl jobs). */
export function SiteCard({
  site,
  onOpenPage,
}: {
  site: IndexSite;
  onOpenPage: (id: number) => void;
}) {
  const qc = useQueryClient();
  const destShelfId = useApp((s) => s.destShelfId);
  const [open, setOpen] = useState(false);
  const [editingIdle, setEditingIdle] = useState(false);
  const [confirmDel, setConfirmDel] = useState(false);
  const [idleVal, setIdleVal] = useState<number>(site.stop_after_idle_pages || 200);
  const removed = site.status === "removed";

  const pages = useQuery({
    queryKey: ["index-pages", site.id],
    queryFn: () => api.listIndexPages(site.id, undefined, 200),
    enabled: open,
  });

  const act = (fn: () => Promise<unknown>) => async () => {
    await fn();
    qc.invalidateQueries({ queryKey: ["index-sites"] });
  };

  // Remove / permanently-delete also touch the catalog (a purge drops catalog entries; even a
  // soft remove changes the site list), so refresh those views and close the confirm panel.
  const del = (fn: () => Promise<unknown>) => async () => {
    await fn();
    setConfirmDel(false);
    qc.invalidateQueries({ queryKey: ["index-sites"] });
    qc.invalidateQueries({ queryKey: ["catalog"] });
    qc.invalidateQueries({ queryKey: ["catalog-stats"] });
  };

  const saveIdle = useMutation({
    mutationFn: () => api.updateIndexSite(site.id, { stop_after_idle_pages: idleVal }),
    onSuccess: () => {
      setEditingIdle(false);
      qc.invalidateQueries({ queryKey: ["index-sites"] });
    },
  });

  const hookAll = useMutation({
    mutationFn: () => api.hookIndexSite(site.id, destShelfId ?? undefined),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["works"] });
      qc.invalidateQueries({ queryKey: ["index-pages", site.id] });
    },
  });

  return (
    <Card className="p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="truncate font-medium">{site.title || site.domain}</span>
            {(() => {
              const st = siteStatus(site);
              return <Badge tone={st.tone}>{st.label}</Badge>;
            })()}
          </div>
          <a
            href={site.root_url}
            target="_blank"
            rel="noreferrer"
            className="block truncate text-xs text-muted underline"
          >
            {site.root_url}
          </a>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-1">
          {removed ? (
            <Button
              size="sm"
              variant="outline"
              title="Resume crawling from where it left off — already-indexed pages aren't re-fetched"
              onClick={act(() => api.resumeIndexSite(site.id))}
            >
              Restore
            </Button>
          ) : site.status === "active" ? (
            <Button size="sm" variant="ghost" onClick={act(() => api.pauseIndexSite(site.id))}>
              Pause
            </Button>
          ) : (
            <Button size="sm" variant="ghost" onClick={act(() => api.resumeIndexSite(site.id))}>
              Resume
            </Button>
          )}
          <Button size="sm" variant="ghost" onClick={() => setOpen((o) => !o)}>
            {open ? "Hide" : "Browse"}
          </Button>
          <Button
            size="sm"
            variant="outline"
            title="Add every fetched page to your library as one work"
            disabled={site.pages_fetched === 0 || hookAll.isPending || hookAll.isSuccess}
            onClick={() => hookAll.mutate()}
          >
            {hookAll.isPending ? "Adding…" : hookAll.isSuccess ? "Added ✓" : "+ Library"}
          </Button>
          <Button
            size="sm"
            variant="danger"
            title={removed ? "Delete permanently" : "Remove (keeps indexed content)"}
            onClick={() => setConfirmDel((v) => !v)}
          >
            ✕
          </Button>
        </div>
      </div>

      {confirmDel && (
        <div className="mt-3 rounded-lg border border-red-500/30 bg-red-500/5 p-2.5 text-sm">
          {removed ? (
            <>
              <div className="mb-2 text-text">
                Permanently delete this source and all its indexed pages, catalog entries and
                search records? This can't be undone. (To bring it back instead, use Restore.)
              </div>
              <div className="flex gap-2">
                <Button
                  size="sm"
                  variant="danger"
                  onClick={del(() => api.deleteIndexSite(site.id, { purge: true }))}
                >
                  Delete permanently
                </Button>
                <Button size="sm" variant="ghost" onClick={() => setConfirmDel(false)}>
                  Cancel
                </Button>
              </div>
            </>
          ) : (
            <>
              <div className="mb-2 text-text">
                Remove this source? Crawling stops, but the indexed pages, catalog entries and
                search records are <span className="font-medium">kept</span> — re-add the URL (or
                hit Restore) later to resume without re-crawling. Or delete everything permanently.
              </div>
              <div className="flex flex-wrap gap-2">
                <Button
                  size="sm"
                  variant="outline"
                  onClick={del(() => api.deleteIndexSite(site.id))}
                >
                  Remove (keep content)
                </Button>
                <Button
                  size="sm"
                  variant="danger"
                  onClick={del(() => api.deleteIndexSite(site.id, { purge: true }))}
                >
                  Delete permanently
                </Button>
                <Button size="sm" variant="ghost" onClick={() => setConfirmDel(false)}>
                  Cancel
                </Button>
              </div>
            </>
          )}
        </div>
      )}

      <div className="mt-3">
        <div className="flex justify-between text-xs text-muted">
          <span>
            {site.pages_fetched} / {site.max_pages ? site.pages_total : "∞"} pages
            {site.pages_pending > 0 && ` · ${site.pages_pending} queued`}
            {site.pages_failed > 0 && ` · ${site.pages_failed} failed`}
          </span>
          <span>{site.words.toLocaleString()} words</span>
        </div>
        {/* No progress bar: an index crawl is open-ended — it can't know how much content a
            site has, so a fill % would be meaningless. The status badge conveys what it's doing. */}
        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-muted">
          <span>{site.titles_found ?? 0} title{(site.titles_found ?? 0) === 1 ? "" : "s"} found</span>
          <span>· {(site.requests ?? 0).toLocaleString()} requests</span>
          <span>· {fmtDuration(site.duration_seconds ?? 0)}</span>
          {/* Editable idle threshold: pages with nothing new before the crawl stops looking for
              MORE pages (it still finishes its queue, so no found content is left un-indexed). */}
          {editingIdle ? (
            <span className="flex items-center gap-1">
              · stop after
              <input
                type="number"
                min={1}
                value={idleVal}
                onChange={(e) => setIdleVal(Math.max(1, Number(e.target.value) || 1))}
                className="w-16 rounded border border-border bg-bg px-1 py-0.5 text-xs"
              />
              idle pages
              <Button size="sm" variant="ghost" disabled={saveIdle.isPending} onClick={() => saveIdle.mutate()}>
                {saveIdle.isPending ? "…" : "save"}
              </Button>
              <button className="underline" onClick={() => setEditingIdle(false)}>cancel</button>
            </span>
          ) : (
            <button
              className="underline decoration-dotted"
              title="After this many consecutive pages with nothing new (no title and no new link), stop discovering more pages — the crawl still finishes whatever is already queued"
              onClick={() => {
                setIdleVal(site.stop_after_idle_pages || 200);
                setEditingIdle(true);
              }}
            >
              · stops after {site.stop_after_idle_pages || 200} idle pages
              {site.pages_since_new_title ? ` (${site.pages_since_new_title} now)` : ""} ✎
            </button>
          )}
        </div>
        {/* Plain-language diagnostic: WHY the crawl is in this state (stopped / paused / cooling
            / failing) so the operator isn't left guessing. */}
        {site.status_reason && (
          <div
            className={`mt-2 rounded-md px-2 py-1.5 text-xs ${
              site.pages_fetched === 0 && site.pages_failed > 0
                ? "bg-red-500/10 text-red-600"
                : (site.cooldown_until && new Date(site.cooldown_until).getTime() > Date.now())
                  ? "bg-amber-500/10 text-amber-700"
                  : "bg-surface-2 text-muted"
            }`}
            title={site.last_error ?? undefined}
          >
            {site.status_reason}
            {site.consecutive_errors > 0 && site.status === "active" &&
              ` · ${site.consecutive_errors} error${site.consecutive_errors === 1 ? "" : "s"} in a row`}
          </div>
        )}
      </div>

      {open && (
        <div className="mt-3 max-h-80 overflow-y-auto rounded-lg border border-border">
          {pages.isLoading ? (
            <div className="p-3"><Spinner label="Loading pages…" /></div>
          ) : (pages.data?.length ?? 0) === 0 ? (
            <p className="p-3 text-sm text-muted">No pages yet.</p>
          ) : (
            <ul className="divide-y divide-border">
              {pages.data!.map((p) => (
                <PageRow key={p.id} page={p} onOpen={() => onOpenPage(p.id)} />
              ))}
            </ul>
          )}
        </div>
      )}
    </Card>
  );
}

function PageRow({ page, onOpen }: { page: IndexedPage; onOpen: () => void }) {
  return (
    <li className="flex items-center justify-between gap-2 px-3 py-2 hover:bg-surface-2">
      <button
        onClick={onOpen}
        className="flex min-w-0 flex-1 gap-3 text-left"
        disabled={page.status !== "fetched"}
      >
        {page.cover_url && (
          <img
            src={page.cover_url}
            alt=""
            loading="lazy"
            className="h-16 w-11 shrink-0 rounded border border-border object-cover"
            onError={(e) => (e.currentTarget.style.display = "none")}
          />
        )}
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-medium text-text">{page.title || page.url}</div>
          {page.author && <div className="truncate text-xs text-muted">by {page.author}</div>}
          {page.description ? (
            <div className="line-clamp-2 text-xs text-muted">{page.description}</div>
          ) : (
            <div className="truncate text-xs text-muted">{page.url}</div>
          )}
          {/* Why this page failed / was skipped / is deferred — the kind-prefixed cause. */}
          {page.status !== "fetched" && page.last_error && (
            <div className="truncate text-xs text-red-500" title={page.last_error}>
              ⚠ {page.last_error}
              {page.attempts ? ` (attempt ${page.attempts})` : ""}
            </div>
          )}
        </div>
      </button>
      <div className="flex shrink-0 items-center gap-2">
        {page.status !== "fetched" && (
          <Badge tone={page.status === "failed" ? "red" : "amber"}>{page.status}</Badge>
        )}
        {page.hooked_work_id && <Badge tone="green">in library</Badge>}
      </div>
    </li>
  );
}

/** Modal that reads a single indexed page in-app, with a non-blocking "add to library". */
export function PageReader({ pageId, onClose }: { pageId: number; onClose: () => void }) {
  const qc = useQueryClient();
  const destShelfId = useApp((s) => s.destShelfId);
  const page = useQuery({ queryKey: ["index-page", pageId], queryFn: () => api.getIndexPage(pageId) });
  // Close on Escape (touch/keyboard parity with the ✕ button + backdrop tap).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  const hook = useMutation({
    mutationFn: () => api.hookIndexPage(pageId, destShelfId ?? undefined),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["works"] });
      qc.invalidateQueries({ queryKey: ["index-pages"] });
      qc.invalidateQueries({ queryKey: ["index-page", pageId] });
    },
  });

  return (
    <div
      className="fixed inset-0 z-50 flex justify-center overflow-y-auto bg-black/50 p-0 sm:p-6"
      onClick={onClose}
    >
      <div
        className="relative h-full w-full max-w-3xl overflow-y-auto bg-surface sm:h-auto sm:rounded-2xl sm:shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="sticky top-0 z-10 flex items-center justify-between gap-2 border-b border-border bg-surface/95 px-4 py-3 backdrop-blur">
          <div className="min-w-0">
            <div className="truncate font-medium">{page.data?.title || "Reading…"}</div>
            {page.data && (
              <a
                href={page.data.url}
                target="_blank"
                rel="noreferrer"
                className="truncate text-xs text-muted underline"
              >
                {page.data.domain || page.data.url}
              </a>
            )}
          </div>
          <div className="flex shrink-0 items-center gap-1">
            <Button
              size="sm"
              variant="primary"
              disabled={!page.data || hook.isPending || !!page.data.hooked_work_id}
              onClick={() => hook.mutate()}
            >
              {page.data?.hooked_work_id ? "In library" : hook.isPending ? "Adding…" : "Add to library"}
            </Button>
            <Button size="sm" variant="ghost" onClick={onClose}>
              ✕
            </Button>
          </div>
        </div>
        <div className="px-5 py-6">
          {page.isLoading ? (
            <Spinner label="Loading…" />
          ) : (
            <>
              {(page.data?.cover_url || page.data?.description) && (
                <div className="mb-5 flex gap-4 rounded-xl border border-border bg-surface-2/50 p-4">
                  {page.data?.cover_url && (
                    <img
                      src={page.data.cover_url}
                      alt=""
                      className="h-32 w-24 shrink-0 rounded-md border border-border object-cover"
                      onError={(e) => (e.currentTarget.style.display = "none")}
                    />
                  )}
                  <div className="min-w-0">
                    {page.data?.author && <div className="text-sm text-muted">by {page.data.author}</div>}
                    {page.data?.site_name && <div className="text-xs text-muted">{page.data.site_name}</div>}
                    {page.data?.description && (
                      <p className="mt-1 text-sm text-text">{page.data.description}</p>
                    )}
                  </div>
                </div>
              )}
              <article
                className="reader-prose mx-auto"
                dangerouslySetInnerHTML={{ __html: page.data?.html || "<p>(no content)</p>" }}
              />
            </>
          )}
        </div>
      </div>
    </div>
  );
}
