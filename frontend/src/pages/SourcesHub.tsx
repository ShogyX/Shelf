// Sources & Acquisitions — the merged operator surface (redesign): live stat tiles, active jobs
// (crawl backfills + pipeline downloads, with the verifying state), indexed sources, watched folders,
// and list imports. Composes the existing data hooks + the already-built JobRow / CrawlStats / SiteCard.
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api, DownloadJob, Work } from "../api/client";
import { qk } from "../api/queryKeys";
import { useIsAdmin } from "../auth";
import { Button, Disclosure, EmptyState, InfoHint, StatTile, StatusChip, Spinner } from "../components/ui";
import { CrawlStats, PageReader, SiteCard } from "../components/IndexShared";
import StockManager from "../components/StockManager";
import { JobRow } from "./Jobs";
import { ListImportsManager } from "./ListImports";

const ACTIVE_DL = new Set(["queued", "searching", "downloading", "completed", "retry", "deferred"]);
// Terminal crawl-job statuses — done backfills/refreshes + failures. Excluded from the Active list
// (the pruner trims them server-side); failures stay inspectable under the History disclosure.
const TERMINAL_JOB = new Set(["done", "failed"]);

// Display-only card for an in-flight pipeline download. (A true "cancel" must abort the SAB/qBit
// transfer, not just delete the row — deferred to a backend abort endpoint; ponytail: no fake cancel.)
function DownloadCard({ d }: { d: DownloadJob }) {
  const { t } = useTranslation();
  const state = d.verifying ? "verifying" : d.status;
  const tone: "warning" | "danger" | "success" | "accent" | "neutral" =
    d.verifying ? "warning" : state === "failed" ? "danger"
      : state === "imported" ? "success" : ACTIVE_DL.has(state) ? "accent" : "neutral";
  const sizeGb = d.size ? `${(d.size / 1e9).toFixed(1)} GB` : null;
  const barColor = state === "failed" ? "#fb7185" : "var(--accent)";
  return (
    <div className="rounded-2xl border border-[var(--hair,var(--border))] bg-surface p-4">
      <div className="mb-2.5 flex items-center gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2.5">
            <span className="truncate text-[14.5px] font-bold text-text">{d.title}</span>
            <StatusChip tone={tone}>
              {d.verifying ? t("sources.verifying") : state.replace(/\b\w/, (c) => c.toUpperCase())}
            </StatusChip>
          </div>
          <div className="mt-0.5 truncate text-[12.5px] text-muted">
            {[d.indexer, d.release_title, sizeGb].filter(Boolean).join(" · ") || d.grab_kind}
          </div>
          {d.error && <p className="mt-1 truncate text-xs text-[#fb7185]" title={d.error}>⚠ {d.error}</p>}
        </div>
        <span className="shrink-0 text-sm font-bold tabular-nums text-muted">{d.percent}%</span>
      </div>
      <div className="h-[7px] overflow-hidden rounded-full bg-surface-2">
        <div className="h-full rounded-full transition-all" style={{ width: `${d.percent}%`, background: barColor }} />
      </div>
    </div>
  );
}

export default function SourcesHub() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const isAdmin = useIsAdmin();
  const [openPage, setOpenPage] = useState<number | null>(null);
  // Bound how many job rows render at once — on a busy instance there can be hundreds of active +
  // historical jobs, which is what made this page "incredibly long". "Show more" reveals the rest.
  const ACTIVE_CAP = 12;
  const [showAllActive, setShowAllActive] = useState(false);
  const [histShown, setHistShown] = useState(40);

  const jobs = useQuery({
    queryKey: qk.jobs(), queryFn: api.listJobs,
    refetchInterval: (q) => (q.state.data ?? []).some((j) => j.status === "running" || j.status === "scheduled") ? 4000 : false,
  });
  // Only the IN-FLIGHT downloads — terminal (imported/failed) ones were fetched but never rendered
  // here (crawl history lives under the Jobs disclosure), so active-only trims the payload sharply.
  const downloads = useQuery({
    queryKey: qk.downloads(), queryFn: () => api.listDownloads("active", 1000),
    refetchInterval: (q) => (q.state.data ?? []).some((d) => ACTIVE_DL.has(d.status) || d.verifying) ? 3000 : false,
  });
  const sites = useQuery({
    queryKey: qk.indexSites(), queryFn: api.listIndexSites,
    refetchInterval: (q) => (q.state.data ?? []).some((s) => s.status === "active") ? 2500 : false,
  });
  const works = useQuery({ queryKey: qk.works("", null), queryFn: () => api.listWorks() });
  const catStats = useQuery({ queryKey: qk.catalogStats(), queryFn: api.catalogStats });
  const folders = useQuery({ queryKey: qk.folders(), queryFn: api.listFolders });

  const workById = new Map<number, Work>((works.data ?? []).map((w) => [w.id, w]));
  const crawlsRunning = (jobs.data ?? []).filter((j) => j.status === "running").length
    + (sites.data ?? []).filter((s) => s.status === "active").length;
  const dlActive = (downloads.data ?? []).filter((d) => ACTIVE_DL.has(d.status) || d.verifying);
  // Active crawl jobs vs. terminal history (done/failed). Failures must stay inspectable, so they
  // move to the History disclosure rather than vanishing from the page.
  const activeJobs = (jobs.data ?? []).filter((j) => !TERMINAL_JOB.has(j.status));
  const historyJobs = (jobs.data ?? []).filter((j) => TERMINAL_JOB.has(j.status));
  const pagesQueued = (sites.data ?? []).reduce((n, s) => n + (s.pages_pending ?? 0), 0);

  return (
    <main className="page-in mx-auto max-w-6xl px-4 py-8 sm:px-6">
      <div className="mb-1 flex items-center gap-2.5">
        <h1 className="font-display text-3xl font-semibold tracking-tight text-text sm:text-4xl">{t("sources.title")}</h1>
        <InfoHint text={t("sources.titleHint")} />
        <Button variant="primary" className="ml-auto" onClick={() => navigate("/add")}>{t("sources.addSource")}</Button>
      </div>
      <p className="mb-6 text-sm text-muted">{t("sources.subtitle")}</p>

      {/* Stat tiles */}
      <div className="mb-8 grid grid-cols-2 gap-3.5 lg:grid-cols-4">
        <StatTile value={crawlsRunning} label={t("sources.crawlsRunning")} tone="accent" />
        {/* ≥1000 means we hit the request cap — show an honest "999+" rather than a fake exact. */}
        <StatTile value={dlActive.length >= 1000 ? "999+" : dlActive.length}
                  label={t("sources.downloadsInFlight")} tone="success" />
        <StatTile value={pagesQueued.toLocaleString()} label={t("sources.pagesQueued")} tone="warning" />
        <StatTile value={(catStats.data?.titles ?? 0).toLocaleString()} label={t("sources.titlesIndexed")} tone="info" />
      </div>

      {/* Active jobs: pipeline downloads + crawl backfills (terminal jobs move to History below) */}
      <h2 className="font-display mb-4 text-[22px] font-semibold text-text">{t("sources.activeJobs")}</h2>
      {downloads.isLoading || jobs.isLoading ? (
        <Spinner label={t("sources.loadingJobs")} />
      ) : dlActive.length === 0 && activeJobs.length === 0 ? (
        <EmptyState title={t("sources.nothingFetching")} hint={t("sources.nothingFetchingHint")} />
      ) : (
        <div className="space-y-3">
          {(showAllActive ? dlActive : dlActive.slice(0, ACTIVE_CAP))
            .map((d) => <DownloadCard key={`d-${d.id}`} d={d} />)}
          {(showAllActive ? activeJobs : activeJobs.slice(0, ACTIVE_CAP))
            .map((job) => <JobRow key={`j-${job.id}`} job={job} work={workById.get(job.work_id)} />)}
          {(() => {
            const hidden = Math.max(0, dlActive.length - ACTIVE_CAP) + Math.max(0, activeJobs.length - ACTIVE_CAP);
            return !showAllActive && hidden > 0 ? (
              <button onClick={() => setShowAllActive(true)}
                className="w-full rounded-xl border border-[var(--hair,var(--border))] bg-surface py-2 text-sm font-medium text-muted hover:bg-surface-2">
                {t("sources.showMore")} ({hidden})
              </button>
            ) : null;
          })()}
        </div>
      )}

      {/* History: finished + failed crawl jobs, kept inspectable (failures never silently vanish). */}
      {historyJobs.length > 0 && (
        <div className="mt-4">
          <Disclosure
            title={t("sources.historyTitle", { count: historyJobs.length })}
            subtitle={t("sources.historySubtitle")}
          >
            <div className="space-y-3">
              {historyJobs.slice(0, histShown).map((job) => <JobRow key={`h-${job.id}`} job={job} work={workById.get(job.work_id)} />)}
              {historyJobs.length > histShown && (
                <button onClick={() => setHistShown((n) => n + 50)}
                  className="w-full rounded-xl border border-[var(--hair,var(--border))] bg-surface py-2 text-sm font-medium text-muted hover:bg-surface-2">
                  {t("sources.showMore")} ({historyJobs.length - histShown})
                </button>
              )}
            </div>
          </Disclosure>
        </div>
      )}

      {/* Indexed sources */}
      <h2 className="font-display mb-4 mt-9 text-[22px] font-semibold text-text">{t("sources.indexedSources")}</h2>
      <CrawlStats />
      {(sites.data?.length ?? 0) > 0 ? (
        <div className="space-y-3">
          {sites.data!.map((s) => <SiteCard key={s.id} site={s} onOpenPage={setOpenPage} />)}
        </div>
      ) : (
        <EmptyState title={t("sources.noIndexedSources")} hint={t("sources.noIndexedSourcesHint")} />
      )}

      {/* Watched folders */}
      {(folders.data?.length ?? 0) > 0 && (
        <>
          <h2 className="font-display mb-4 mt-9 text-[22px] font-semibold text-text">{t("sources.watchedFolders")}</h2>
          <div className="space-y-2.5">
            {folders.data!.map((f) => (
              <div key={f.id} className="flex items-center gap-3 rounded-2xl border border-[var(--hair,var(--border))] bg-surface p-4">
                <span className="flex h-[34px] w-[34px] shrink-0 items-center justify-center rounded-[9px] bg-surface-2 text-muted">📁</span>
                <div className="min-w-0 flex-1">
                  <div className="truncate font-mono text-sm font-medium text-text">{f.path}</div>
                  <div className="text-xs text-muted">{t("add.folderStats", { works: f.works, files: f.file_count })}</div>
                </div>
                <StatusChip tone={f.enabled ? "success" : "neutral"}>{f.enabled ? t("sources.enabled") : t("sources.paused")}</StatusChip>
              </div>
            ))}
          </div>
        </>
      )}

      {/* Library stocking (admin) — operator pre-fetch of catalog works through the usenet pipeline so
          stocked titles serve instantly. Folded in from the old standalone /stock page. */}
      {isAdmin && <StockManager className="mt-9" />}

      {/* List imports — the full manage surface (add / edit / delete / toggle / check-now), merged
          in from the old /imports page so everything Shelf is fetching lives on one operator page. */}
      <ListImportsManager className="mt-9" />

      {openPage != null && <PageReader pageId={openPage} onClose={() => setOpenPage(null)} />}
    </main>
  );
}
