import { useState } from "react";
import { useTranslation } from "react-i18next";
import { coverSrc } from "./Cover";
import { cleanText } from "../lib/text";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, IndexSite, IndexedPage } from "../api/client";
import { qk } from "../api/queryKeys";
import { Badge, Button, Card, InfoHint, Modal, Spinner } from "./ui";
import { useShelfPrompt } from "./ShelfPrompt";

export function fmtDuration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

export type Tone = "green" | "amber" | "violet" | "red" | "default";

// Per-source media-kind restriction: a 3-way choice mapped to the backend's allowed_media_kinds.
// `labelKey` is resolved through i18n at render time (options can't call hooks at module scope).
type MediaKindChoice = "all" | "text" | "comic";
const MEDIA_KIND_OPTIONS: { value: MediaKindChoice; labelKey: string }[] = [
  { value: "all", labelKey: "sources.mediaAll" },
  { value: "text", labelKey: "sources.mediaNovelsOnly" },
  { value: "comic", labelKey: "sources.mediaComicsOnly" },
];
function mediaKindValue(kinds: string[] | null | undefined): MediaKindChoice {
  if (!kinds || kinds.length === 0) return "all";
  if (kinds.length === 1 && kinds[0] === "text") return "text";
  if (kinds.length === 1 && kinds[0] === "comic") return "comic";
  return "all"; // any unexpected subset reads as unrestricted
}
function mediaKindPayload(v: MediaKindChoice): string[] | null {
  return v === "all" ? null : [v];
}

/** A crawl is open-ended (it can't know its end), so show WHAT it's doing rather than a % bar:
 *  running · cooling down (backing off after a block) · finished · paused · error. */
// `label` is an i18n key (resolved by the caller via t()); an unknown status falls back to the raw
// backend string, which has no key and is passed through as-is.
export function siteStatus(site: IndexSite): { label: string; tone: Tone } {
  if (site.status === "active") {
    const cooling =
      site.cooldown_until && new Date(site.cooldown_until).getTime() > Date.now();
    return cooling ? { label: "sources.statusCoolingDown", tone: "amber" } : { label: "sources.statusRunning", tone: "violet" };
  }
  if (site.status === "done") return { label: "sources.statusFinished", tone: "green" };
  if (site.status === "paused") return { label: "sources.statusPaused", tone: "default" };
  if (site.status === "removed") return { label: "sources.statusRemoved", tone: "default" };
  if (site.status === "failed") return { label: "sources.statusError", tone: "red" };
  return { label: site.status, tone: "default" };
}

// `label` is an i18n key — the caller resolves it via t() (this helper can't hold a hook).
export function healthBadge(h: string): { tone: Tone; label: string } | null {
  switch (h) {
    case "ok":
      return { tone: "green", label: "sources.healthComplete" };
    case "incomplete":
      return { tone: "amber", label: "sources.healthIncomplete" };
    case "no_chapters":
      return { tone: "red", label: "sources.healthNoChapters" };
    case "unreachable":
      return { tone: "red", label: "sources.healthUnreachable" };
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
  const { t } = useTranslation();
  const stats = useQuery({
    queryKey: qk.indexStats(),
    queryFn: api.indexStats,
    refetchInterval: (q) => (q.state.data && q.state.data.sites_active > 0 ? 2500 : false),
  });
  const d = stats.data;
  if (!d) return null;
  return (
    <Card className="mb-4 p-4">
      <div className="mb-3 grid grid-cols-2 gap-4 sm:grid-cols-4">
        <Stat label={t("sources.titlesFound")} value={(d.titles_found ?? 0).toLocaleString()} />
        <Stat
          label={t("sources.requestsMade")}
          value={(d.requests_made ?? 0).toLocaleString()}
          hint={t("sources.requestsMadeHint")}
        />
        <Stat
          label={t("sources.timeSpent")}
          value={fmtDuration(d.time_spent_seconds ?? 0)}
          hint={t("sources.timeSpentHint")}
        />
        <Stat label={t("sources.wordsIndexed")} value={(d.words_indexed ?? 0).toLocaleString()} />
      </div>
      <div className="flex flex-wrap items-center gap-2 border-t border-border pt-3 text-xs">
        <span className="text-muted">{t("sources.sites")}</span>
        {d.sites_active > 0 && <Badge tone="violet">{t("sources.sitesInProgress", { count: d.sites_active })}</Badge>}
        {d.sites_done > 0 && <Badge tone="green">{t("sources.sitesComplete", { count: d.sites_done })}</Badge>}
        {d.sites_paused > 0 && <Badge tone="amber">{t("sources.sitesAborted", { count: d.sites_paused })}</Badge>}
        {d.sites_failed > 0 && <Badge tone="red">{t("sources.sitesError", { count: d.sites_failed })}</Badge>}
        <span className="ml-auto text-muted">
          {t("sources.pagesSummary", {
            fetched: d.pages_fetched.toLocaleString(),
            queued: d.pages_pending.toLocaleString(),
            failed: d.pages_failed.toLocaleString(),
          })}
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
  const { t } = useTranslation();
  const qc = useQueryClient();
  const pickShelf = useShelfPrompt();
  const [open, setOpen] = useState(false);
  const [editingIdle, setEditingIdle] = useState(false);
  const [confirmDel, setConfirmDel] = useState(false);
  const [idleVal, setIdleVal] = useState<number>(site.stop_after_idle_pages || 200);
  const removed = site.status === "removed";

  const pages = useQuery({
    queryKey: qk.indexPages(site.id),
    queryFn: () => api.listIndexPages(site.id, undefined, 200),
    enabled: open,
  });

  const act = (fn: () => Promise<unknown>) => async () => {
    await fn();
    qc.invalidateQueries({ queryKey: qk.indexSites() });
  };

  // Remove / permanently-delete also touch the catalog (a purge drops catalog entries; even a
  // soft remove changes the site list), so refresh those views and close the confirm panel.
  const del = (fn: () => Promise<unknown>) => async () => {
    await fn();
    setConfirmDel(false);
    qc.invalidateQueries({ queryKey: qk.indexSites() });
    qc.invalidateQueries({ queryKey: qk.catalog() });
    qc.invalidateQueries({ queryKey: qk.catalogStats() });
  };

  const saveIdle = useMutation({
    mutationFn: () => api.updateIndexSite(site.id, { stop_after_idle_pages: idleVal }),
    onSuccess: () => {
      setEditingIdle(false);
      qc.invalidateQueries({ queryKey: qk.indexSites() });
    },
  });

  // Which media kinds this source is allowed to satisfy. null/[] = all; else a 1-kind subset.
  const mediaValue = mediaKindValue(site.allowed_media_kinds);
  const saveMedia = useMutation({
    mutationFn: (v: MediaKindChoice) =>
      api.updateIndexSite(site.id, { allowed_media_kinds: mediaKindPayload(v) }),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.indexSites() }),
  });

  const hookAll = useMutation({
    mutationFn: (shelfId?: number) => api.hookIndexSite(site.id, shelfId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.works() });
      qc.invalidateQueries({ queryKey: qk.indexPages(site.id) });
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
              return <Badge tone={st.tone}>{t(st.label)}</Badge>;
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
              title={t("sources.restoreHint")}
              onClick={act(() => api.resumeIndexSite(site.id))}
            >
              {t("sources.restore")}
            </Button>
          ) : site.status === "active" ? (
            <Button size="sm" variant="ghost" onClick={act(() => api.pauseIndexSite(site.id))}>
              {t("sources.pause")}
            </Button>
          ) : (
            <Button size="sm" variant="ghost" onClick={act(() => api.resumeIndexSite(site.id))}>
              {t("sources.resume")}
            </Button>
          )}
          <Button size="sm" variant="ghost" onClick={() => setOpen((o) => !o)}>
            {open ? t("sources.hide") : t("sources.browse")}
          </Button>
          <Button
            size="sm"
            variant="outline"
            title={t("sources.hookAllHint")}
            disabled={site.pages_fetched === 0 || hookAll.isPending || hookAll.isSuccess}
            onClick={async () => {
              const id = await pickShelf();
              if (id === undefined) return; // cancelled → abort
              hookAll.mutate(id ?? undefined);
            }}
          >
            {hookAll.isPending ? t("sources.adding") : hookAll.isSuccess ? t("sources.added") : t("sources.addLibrary")}
          </Button>
          <Button
            size="sm"
            variant="danger"
            title={removed ? t("sources.deletePermanently") : t("sources.removeKeepContent")}
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
                {t("sources.deleteConfirmBody")}
              </div>
              <div className="flex gap-2">
                <Button
                  size="sm"
                  variant="danger"
                  onClick={del(() => api.deleteIndexSite(site.id, { purge: true }))}
                >
                  {t("sources.deletePermanently")}
                </Button>
                <Button size="sm" variant="ghost" onClick={() => setConfirmDel(false)}>
                  {t("common.cancel")}
                </Button>
              </div>
            </>
          ) : (
            <>
              <div className="mb-2 text-text">
                {t("sources.removeConfirmPre")} <span className="font-medium">{t("sources.removeConfirmKept")}</span> {t("sources.removeConfirmPost")}
              </div>
              <div className="flex flex-wrap gap-2">
                <Button
                  size="sm"
                  variant="outline"
                  onClick={del(() => api.deleteIndexSite(site.id))}
                >
                  {t("sources.removeKeep")}
                </Button>
                <Button
                  size="sm"
                  variant="danger"
                  onClick={del(() => api.deleteIndexSite(site.id, { purge: true }))}
                >
                  {t("sources.deletePermanently")}
                </Button>
                <Button size="sm" variant="ghost" onClick={() => setConfirmDel(false)}>
                  {t("common.cancel")}
                </Button>
              </div>
            </>
          )}
        </div>
      )}

      <div className="mt-3">
        <div className="flex justify-between text-xs text-muted">
          <span>
            {t("sources.pagesCount", { fetched: site.pages_fetched, total: site.max_pages ? site.pages_total : "∞" })}
            {site.pages_pending > 0 && ` · ${t("sources.pagesQueuedSuffix", { count: site.pages_pending })}`}
            {site.pages_failed > 0 && ` · ${t("sources.pagesFailedSuffix", { count: site.pages_failed })}`}
          </span>
          <span>{t("sources.wordsCount", { count: site.words.toLocaleString() })}</span>
        </div>
        {/* No progress bar: an index crawl is open-ended — it can't know how much content a
            site has, so a fill % would be meaningless. The status badge conveys what it's doing. */}
        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-muted">
          <span>{t("sources.titlesFoundCount", { count: site.titles_found ?? 0 })}</span>
          <span>· {t("sources.requestsCount", { count: (site.requests ?? 0).toLocaleString() })}</span>
          <span>· {fmtDuration(site.duration_seconds ?? 0)}</span>
          {/* Editable idle threshold: pages with nothing new before the crawl stops looking for
              MORE pages (it still finishes its queue, so no found content is left un-indexed). */}
          {editingIdle ? (
            <span className="flex items-center gap-1">
              · {t("sources.stopAfter")}
              <input
                type="number"
                min={1}
                value={idleVal}
                onChange={(e) => setIdleVal(Math.max(1, Number(e.target.value) || 1))}
                className="w-16 rounded border border-border bg-bg px-1 py-0.5 text-xs"
              />
              {t("sources.idlePages")}
              <Button size="sm" variant="ghost" disabled={saveIdle.isPending} onClick={() => saveIdle.mutate()}>
                {saveIdle.isPending ? "…" : t("sources.saveInline")}
              </Button>
              <button className="underline" onClick={() => setEditingIdle(false)}>{t("sources.cancelInline")}</button>
            </span>
          ) : (
            <button
              className="underline decoration-dotted"
              title={t("sources.idleThresholdHint")}
              onClick={() => {
                setIdleVal(site.stop_after_idle_pages || 200);
                setEditingIdle(true);
              }}
            >
              · {t("sources.stopsAfterIdle", { count: site.stop_after_idle_pages || 200 })}
              {site.pages_since_new_title ? ` ${t("sources.idleNow", { count: site.pages_since_new_title })}` : ""} ✎
            </button>
          )}
          {/* Restrict which media kinds this source is used for. Excludes it from searches of
              other media types (e.g. a comic-only site is skipped for novel requests). */}
          <span className="flex items-center gap-1">
            · {t("sources.usedFor")}
            <select
              value={mediaValue}
              disabled={saveMedia.isPending}
              onChange={(e) => saveMedia.mutate(e.target.value as MediaKindChoice)}
              className="rounded border border-border bg-bg px-1 py-0.5 text-xs text-text"
            >
              {MEDIA_KIND_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>{t(o.labelKey)}</option>
              ))}
            </select>
            <InfoHint
              text={t("sources.usedForHint")}
            />
          </span>
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
              ` · ${t("sources.errorsInARow", { count: site.consecutive_errors })}`}
          </div>
        )}
      </div>

      {open && (
        <div className="mt-3 max-h-80 overflow-y-auto rounded-lg border border-border">
          {pages.isLoading ? (
            <div className="p-3"><Spinner label={t("sources.loadingPages")} /></div>
          ) : (pages.data?.length ?? 0) === 0 ? (
            <p className="p-3 text-sm text-muted">{t("sources.noPagesYet")}</p>
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
  const { t } = useTranslation();
  return (
    <li className="flex items-center justify-between gap-2 px-3 py-2 hover:bg-surface-2">
      <button
        onClick={onOpen}
        className="flex min-w-0 flex-1 gap-3 text-left"
        disabled={page.status !== "fetched"}
      >
        {page.cover_url && (
          <img
            src={coverSrc(page.cover_url) ?? ""}
            alt=""
            loading="lazy"
            className="h-16 w-11 shrink-0 rounded border border-border object-cover"
            onError={(e) => (e.currentTarget.style.display = "none")}
          />
        )}
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-medium text-text">{page.title || page.url}</div>
          {page.author && <div className="truncate text-xs text-muted">{t("common.byAuthor", { author: page.author })}</div>}
          {page.description ? (
            <div className="line-clamp-2 text-xs text-muted">{cleanText(page.description)}</div>
          ) : (
            <div className="truncate text-xs text-muted">{page.url}</div>
          )}
          {/* Why this page failed / was skipped / is deferred — the kind-prefixed cause. */}
          {page.status !== "fetched" && page.last_error && (
            <div className="truncate text-xs text-red-500" title={page.last_error}>
              ⚠ {page.last_error}
              {page.attempts ? ` ${t("sources.attempt", { count: page.attempts })}` : ""}
            </div>
          )}
        </div>
      </button>
      <div className="flex shrink-0 items-center gap-2">
        {page.status !== "fetched" && (
          <Badge tone={page.status === "failed" ? "red" : "amber"}>{page.status}</Badge>
        )}
        {page.hooked_work_id && <Badge tone="green">{t("sources.inLibrary")}</Badge>}
      </div>
    </li>
  );
}

/** Modal that reads a single indexed page in-app, with a non-blocking "add to library". */
export function PageReader({ pageId, onClose }: { pageId: number; onClose: () => void }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const pickShelf = useShelfPrompt();
  const page = useQuery({ queryKey: qk.indexPage(pageId), queryFn: () => api.getIndexPage(pageId) });
  const hook = useMutation({
    mutationFn: (shelfId?: number) => api.hookIndexPage(pageId, shelfId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.works() });
      qc.invalidateQueries({ queryKey: qk.indexPages() });
      qc.invalidateQueries({ queryKey: qk.indexPage(pageId) });
    },
  });

  return (
    <Modal
      variant="fullscreen-sheet"
      width="max-w-3xl"
      onClose={onClose}
      title={
        <div className="min-w-0">
          <div className="truncate font-medium">{page.data?.title || t("sources.reading")}</div>
          {page.data && (
            <a
              href={page.data.url}
              target="_blank"
              rel="noreferrer"
              className="block truncate text-xs font-normal text-muted underline"
            >
              {page.data.domain || page.data.url}
            </a>
          )}
        </div>
      }
      footer={
        <div className="flex justify-end">
          <Button
            size="sm"
            variant="primary"
            disabled={!page.data || hook.isPending || !!page.data.hooked_work_id}
            onClick={async () => {
              const id = await pickShelf();
              if (id === undefined) return; // cancelled → abort
              hook.mutate(id ?? undefined);
            }}
          >
            {page.data?.hooked_work_id ? t("sources.inLibrary") : hook.isPending ? t("sources.adding") : t("sources.addToLibrary")}
          </Button>
        </div>
      }
    >
      {page.isLoading ? (
        <Spinner label={t("common.loading")} />
      ) : (
        <>
          {(page.data?.cover_url || page.data?.description) && (
            <div className="mb-5 flex gap-4 rounded-xl border border-border bg-surface-2/50 p-4">
              {page.data?.cover_url && (
                <img
                  src={coverSrc(page.data.cover_url) ?? ""}
                  alt=""
                  className="h-32 w-24 shrink-0 rounded-md border border-border object-cover"
                  onError={(e) => (e.currentTarget.style.display = "none")}
                />
              )}
              <div className="min-w-0">
                {page.data?.author && <div className="text-sm text-muted">{t("common.byAuthor", { author: page.data.author })}</div>}
                {page.data?.site_name && <div className="text-xs text-muted">{page.data.site_name}</div>}
                {page.data?.description && (
                  <p className="mt-1 text-sm text-text">{cleanText(page.data.description)}</p>
                )}
              </div>
            </div>
          )}
          <article
            className="reader-prose mx-auto"
            dangerouslySetInnerHTML={{ __html: page.data?.html || `<p>${t("sources.noContent")}</p>` }}
          />
        </>
      )}
    </Modal>
  );
}
