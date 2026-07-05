// The Wanted page: a user's requested titles + the series/authors they track, driven by the new
// /api/wanted/* endpoints. A regular user sees only their own; an admin gets a scope toggle to view
// the whole instance (per-user breakdown, requester info, recheck/rescan controls). Built fresh on
// the shared design-system primitives — none of the deprecated Watchlist code.
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  Tracked,
  WantedRequest,
  WantedScope,
  WantedState,
  WantedStateCounts,
  WantedTrackedList,
  WantedTrackingCounts,
  WantedUserBreakdown,
} from "../api/client";
import { qk } from "../api/queryKeys";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  InfoHint,
  PageHeader,
  SegmentedControl,
  Skeleton,
  StatTile,
  StatusChip,
  StatusTone,
} from "../components/ui";
import Cover from "../components/Cover";
import { Rail } from "../components/Rail";
import { CoverCard } from "../components/CoverCard";
import { LanguageBadge } from "../components/LanguageBadge";
import { useApp } from "../store";
import { useIsAdmin } from "../auth";
import { useConfirm } from "../components/confirm";

// ---------------------------------------------------------------------------------------------
// State → chip tone/label + tile mapping (the spec's colour scheme).
// ---------------------------------------------------------------------------------------------
const STATE_TONE: Record<WantedState, StatusTone> = {
  requested: "neutral",
  searching: "warning",
  downloading: "violet",
  available: "success",
  unavailable: "danger",
  upcoming: "info",
};
const STATE_LABEL: Record<WantedState, string> = {
  requested: "wanted.stateRequested",
  searching: "wanted.stateSearching",
  downloading: "wanted.stateDownloading",
  available: "wanted.stateAvailable",
  unavailable: "wanted.stateUnavailable",
  upcoming: "wanted.stateUpcoming",
};

// ---------------------------------------------------------------------------------------------
// Tracking: state → chip tone/label (shared by the Tracking rail's tiles).
// ---------------------------------------------------------------------------------------------
const TRACK_TONE: Record<Tracked["state"], StatusTone> = {
  up_to_date: "success",
  gathering: "violet",
  paused: "neutral",
};
const TRACK_LABEL: Record<Tracked["state"], string> = {
  up_to_date: "wanted.trackStateUpToDate",
  gathering: "wanted.trackStateGathering",
  paused: "wanted.trackStatePaused",
};

// ---------------------------------------------------------------------------------------------
// Summary tiles from an overview's counts. The user view shows a compact 4; admin shows a wider set.
// ---------------------------------------------------------------------------------------------
function SummaryTiles({ counts, tracking, wide }: {
  counts: WantedStateCounts;
  tracking: WantedTrackingCounts;
  wide: boolean;
}) {
  const { t } = useTranslation();
  if (!wide) {
    return (
      <div className="grid grid-cols-2 gap-3.5 sm:grid-cols-4">
        <StatTile value={counts.requested} label={t("wanted.tileRequested")} tone="neutral" icon="＋" />
        <StatTile value={counts.downloading} label={t("wanted.tileDownloading")} tone="violet" icon="↓" />
        <StatTile value={counts.available} label={t("wanted.tileAvailable")} tone="success" icon="✓" />
        <StatTile value={counts.unavailable} label={t("wanted.tileUnavailable")} tone="danger" icon="!" />
      </div>
    );
  }
  return (
    <div className="grid grid-cols-2 gap-3.5 sm:grid-cols-3 lg:grid-cols-6">
      <StatTile value={counts.requested} label={t("wanted.tileRequested")} tone="neutral" icon="＋" />
      <StatTile value={counts.searching} label={t("wanted.tileSearching")} tone="warning" icon="⌕" />
      <StatTile value={counts.downloading} label={t("wanted.tileDownloading")} tone="violet" icon="↓" />
      <StatTile value={counts.available} label={t("wanted.tileAvailable")} tone="success" icon="✓" />
      <StatTile value={counts.unavailable} label={t("wanted.tileUnavailable")} tone="danger" icon="!" />
      <StatTile value={tracking.total} label={t("wanted.tileTracking")} tone="accent" icon="☆" />
    </div>
  );
}

// ---------------------------------------------------------------------------------------------
// Admin: per-user breakdown table.
// ---------------------------------------------------------------------------------------------
function UserBreakdown({ rows }: { rows: WantedUserBreakdown[] }) {
  const { t } = useTranslation();
  if (rows.length === 0) {
    return <EmptyState title={t("wanted.emptyBreakdownTitle")} hint={t("wanted.emptyBreakdownHint")} />;
  }
  return (
    <section>
      <h2 className="mb-3 font-display text-lg font-semibold text-text">{t("wanted.breakdownHeading")}</h2>
      <Card className="overflow-x-auto">
        <table className="w-full min-w-[560px] text-sm">
          <thead>
            <tr className="border-b border-[var(--hair,var(--border))] text-left text-xs font-semibold uppercase tracking-wide text-muted">
              <th className="px-4 py-2.5">{t("wanted.colUser")}</th>
              <th className="px-3 py-2.5 text-right">{t("wanted.colRequests")}</th>
              <th className="px-3 py-2.5 text-right">{t("wanted.stateSearching")}</th>
              <th className="px-3 py-2.5 text-right">{t("wanted.colAvailable")}</th>
              <th className="px-3 py-2.5 text-right">{t("wanted.stateUnavailable")}</th>
              <th className="px-3 py-2.5 text-right">{t("wanted.colTracking")}</th>
              <th className="px-4 py-2.5 text-right">{t("wanted.tileAutoAdded")}</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-[var(--hair,var(--border))] [font-variant-numeric:tabular-nums]">
            {rows.map((u) => (
              <tr key={u.user_id ?? u.username} className="text-text">
                <td className="px-4 py-2.5 font-medium">{u.username}</td>
                <td className="px-3 py-2.5 text-right font-medium">{u.requests.total}</td>
                <td className="px-3 py-2.5 text-right text-muted">{u.requests.searching}</td>
                <td className="px-3 py-2.5 text-right text-muted">{u.requests.available}</td>
                <td className="px-3 py-2.5 text-right text-muted">{u.requests.unavailable}</td>
                <td className="px-3 py-2.5 text-right">{u.tracking.total}</td>
                <td className="px-4 py-2.5 text-right text-muted">{u.tracking.auto_added_total}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
    </section>
  );
}

// ---------------------------------------------------------------------------------------------
// Admin: the rescan-all control + live progress strip.
// ---------------------------------------------------------------------------------------------
function RescanControl() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useApp((s) => s.toast);
  const confirm = useConfirm();

  const status = useQuery({
    queryKey: qk.wantedRescanStatus(),
    queryFn: api.getWantedRescanStatus,
    refetchInterval: (query) => (query.state.data?.active ? 1500 : false),
  });
  const rescanAll = useMutation({
    mutationFn: () => api.rescanWanted({ all: true }),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: qk.wantedRescanStatus() });
      status.refetch();
      toast(t("wanted.queuedRescan", { count: res.queued }), "success");
    },
    onError: (e) => toast((e as Error).message, "error"),
  });

  async function onRescanAll() {
    if (await confirm({
      title: t("wanted.rescanAll"),
      message: t("wanted.rescanAllMessage"),
      confirmText: t("wanted.rescanAll"),
    })) rescanAll.mutate();
  }

  const rs = status.data;
  const pct = rs && rs.total > 0 ? Math.round((rs.done / rs.total) * 100) : 0;

  return (
    <>
      <Button size="sm" variant="outline" disabled={rescanAll.isPending} onClick={onRescanAll}>
        {rescanAll.isPending ? t("wanted.queuing") : `↻ ${t("wanted.rescanAll")}`}
      </Button>
      {rs?.active && (
        <div className="mt-4 rounded-2xl border border-[var(--hair,var(--border))] bg-surface p-4">
          <div className="mb-1.5 text-xs text-muted">
            {t("wanted.rescanningProgress", { done: rs.done, total: rs.total, queued: rs.queued })}
          </div>
          <div className="h-1.5 w-full overflow-hidden rounded-full bg-surface-2">
            <div className="h-full rounded-full bg-accent transition-all" style={{ width: `${pct}%` }} />
          </div>
        </div>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------------------------
// Dashboard: a compact card for one imported/tracked external reading list (provider + progress).
// Shared by the admin "Tracked lists" rail and the user "Imported lists" section.
// ---------------------------------------------------------------------------------------------
function ListProgressCard({ name, provider, total, done, pending }: {
  name: string;
  provider: string;
  total: number;
  done: number;
  pending: number;
}) {
  const { t } = useTranslation();
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;
  return (
    <Card className="flex flex-col gap-2 p-3.5">
      <div className="flex items-center gap-2">
        <Badge tone="violet">{provider}</Badge>
        <span className="truncate text-[14px] font-semibold text-text" title={name}>{name}</span>
      </div>
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-surface-2">
        <div className="h-full rounded-full bg-accent transition-all" style={{ width: `${pct}%` }} />
      </div>
      <div className="flex items-center justify-between text-[11.5px] text-muted [font-variant-numeric:tabular-nums]">
        <span>{t("wanted.listProgress", { done, total })}</span>
        {pending > 0 && <span>{t("wanted.listPending", { count: pending })}</span>}
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------------------------------------
// Dashboard: the Overseerr-style landscape "request card" — a blurred cover behind a crisp poster
// thumbnail on the right, with year / title / requester / status stacked over the left of the art.
// ---------------------------------------------------------------------------------------------
function RequestRailCard({ r }: { r: WantedRequest }) {
  const { t } = useTranslation();
  const navigate = useNavigate();

  // Year label: prefer the release date (upcoming), else when it was first requested.
  const dated = r.release_date ?? r.first_requested_at;
  const year = dated ? new Date(dated).getFullYear() : null;
  const requester = r.requesters?.[0] ?? null;

  // Available + imported → open it in the library; otherwise search Discover for the title.
  const open = () =>
    r.state === "available" && r.work_id != null
      ? navigate(`/read/${r.work_id}`)
      : navigate(`/discover?q=${encodeURIComponent(r.title)}`);

  return (
    <button
      type="button"
      onClick={open}
      title={r.title}
      className="group relative h-36 w-[340px] shrink-0 snap-start overflow-hidden rounded-2xl border border-[var(--hair,var(--border))] text-left shadow-[0_6px_18px_rgba(0,0,0,0.28)] transition-transform duration-200 [transition-timing-function:var(--ease)] hover:-translate-y-1"
    >
      {/* Blurred cover backdrop + a left-to-right darkening gradient for text legibility. */}
      <div className="absolute inset-0 scale-110 opacity-40 blur-xl">
        <Cover title={r.title} author={r.author} coverUrl={r.cover_url} />
      </div>
      <div className="absolute inset-0 bg-gradient-to-r from-black/85 via-black/60 to-black/20" />

      {/* Crisp poster thumbnail, pinned right. */}
      <div className="absolute inset-y-0 right-0 flex w-24 items-center pr-3">
        <div className="aspect-[2/3] w-full overflow-hidden rounded-lg border border-white/10 shadow-[0_4px_14px_rgba(0,0,0,0.45)]">
          <Cover title={r.title} author={r.author} coverUrl={r.cover_url} small />
        </div>
      </div>

      {/* Left overlay column: year, title, media/lang, requester, status. */}
      <div className="relative z-10 flex h-full w-[60%] flex-col gap-1 p-3.5">
        {year != null && <div className="text-[11px] font-medium text-white/60">{year}</div>}
        <div className="line-clamp-2 text-sm font-semibold leading-snug text-white">{r.title}</div>
        <div className="flex flex-wrap items-center gap-1">
          {r.variant === "audiobook" && <Badge tone="violet">{t("wanted.audiobook")}</Badge>}
          <LanguageBadge language={r.language} />
        </div>
        {requester && (
          <div className="flex items-center gap-1.5">
            <span className="grid h-6 w-6 shrink-0 place-items-center rounded-full bg-accent text-[11px] font-bold text-accent-fg">
              {(requester === "system" ? "S" : requester.charAt(0)).toUpperCase()}
            </span>
            <span className="truncate text-[11.5px] text-white/80">
              {requester === "system" ? t("wanted.requestedBySystem") : requester}
            </span>
          </div>
        )}
        <div className="mt-auto flex items-center gap-1.5">
          <span className="text-[11px] text-white/60">{t("wanted.statusLabel")}</span>
          <StatusChip tone={STATE_TONE[r.state]}>{t(STATE_LABEL[r.state])}</StatusChip>
        </div>
      </div>
    </button>
  );
}

// ---------------------------------------------------------------------------------------------
// Dashboard: a compact tile for one followed series/author (no Overseerr equivalent — kept clean).
// ---------------------------------------------------------------------------------------------
function TrackTile({ tk }: { tk: Tracked }) {
  const { t } = useTranslation();
  const isSeries = tk.kind === "series";
  return (
    <div className="w-52 shrink-0 snap-start rounded-xl border border-[var(--hair,var(--border))] bg-surface p-3">
      <div className="flex items-center gap-2">
        <span className="text-base leading-none" aria-hidden>{isSeries ? "📚" : "✍️"}</span>
        <span
          className="truncate text-[13.5px] font-semibold text-text"
          title={tk.display_name}
        >
          {tk.display_name}
        </span>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted">
        <StatusChip tone={TRACK_TONE[tk.state]}>{t(TRACK_LABEL[tk.state])}</StatusChip>
        {tk.username && <span className="truncate">{tk.username}</span>}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------------------------
// The Overseerr-style dashboard of poster/request rails. scope="me" = the caller's own requests/
// tracking/lists/upcoming (no per-user breakdown); scope="global" (admin) = whole-instance + breakdown.
// ---------------------------------------------------------------------------------------------
function WantedDashboard({ scope }: { scope: "me" | "global" }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const q = useQuery({
    queryKey: qk.wantedDashboard(scope),
    queryFn: () => api.getWantedDashboard(scope),
  });

  if (q.isLoading) {
    return (
      <div className="space-y-8">
        <Skeleton className="h-40 w-full rounded-2xl" />
        <Skeleton className="h-64 w-full rounded-2xl" />
        <Skeleton className="h-64 w-full rounded-2xl" />
      </div>
    );
  }
  if (q.error) return <p className="text-sm text-red-500">{(q.error as Error).message}</p>;
  const d = q.data;
  if (!d) return null;

  return (
    <div>
      {/* Recent requests — landscape request cards (shows an EmptyState rather than self-hiding). */}
      {d.recent_requests.length === 0 ? (
        <section className="mt-8 first:mt-7">
          <h2 className="mb-3.5 px-1 font-display text-[23px] font-semibold tracking-tight text-text">
            {t("wanted.railRecentRequests")}
          </h2>
          <EmptyState title={t("wanted.emptyRequestsTitle")} hint={t("wanted.emptyRequestsHint")} />
        </section>
      ) : (
        <Rail title={t("wanted.railRecentRequests")}>
          {d.recent_requests.map((r) => (
            <RequestRailCard key={r.id} r={r} />
          ))}
        </Rail>
      )}

      {/* Recently added ebooks → open in library. */}
      <Rail title={t("wanted.railRecentEbooks")}>
        {d.recent_ebooks.map((w) => (
          <CoverCard
            key={w.work_id}
            title={w.title}
            author={w.author}
            coverUrl={w.cover_url}
            language={w.language}
            kind="book"
            to={`/read/${w.work_id}`}
          />
        ))}
      </Rail>

      {/* Recently added audiobooks — click routes to Discover (owning the ebook gates listening). */}
      <Rail title={t("wanted.railRecentAudiobooks")}>
        {d.recent_audiobooks.map((w) => (
          <CoverCard
            key={w.work_id}
            title={w.title}
            author={w.author}
            coverUrl={w.cover_url}
            language={w.language}
            kind="audio"
            onClick={() => navigate(`/discover?q=${encodeURIComponent(w.title)}`)}
          />
        ))}
      </Rail>

      {/* Tracked lists (imported external reading lists). */}
      <Rail title={t("wanted.railTrackedLists")}>
        {d.tracked_lists.map((l: WantedTrackedList) => (
          <div key={l.id} className="w-72 shrink-0 snap-start">
            <ListProgressCard
              name={l.display_name}
              provider={l.provider}
              total={l.total}
              done={l.done}
              pending={l.pending}
            />
          </div>
        ))}
      </Rail>

      {/* Tracking (followed series/authors). */}
      <Rail title={t("wanted.railTracking")}>
        {d.tracking.map((tk) => (
          <TrackTile key={tk.id} tk={tk} />
        ))}
      </Rail>

      {/* Upcoming tracked titles — landscape request cards. */}
      <Rail title={t("wanted.railUpcoming")}>
        {d.upcoming.map((r) => (
          <RequestRailCard key={r.id} r={r} />
        ))}
      </Rail>

      {/* User requests — per-user table (admin whole-instance only; a "me" view has no breakdown). */}
      {scope === "global" && (
        <section className="mt-8">
          <UserBreakdown rows={d.user_requests} />
        </section>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------------------------
// Page.
// ---------------------------------------------------------------------------------------------
export default function Wanted() {
  const { t } = useTranslation();
  const isAdmin = useIsAdmin();
  // Admins can flip between their own view and the whole instance; users are pinned to "me".
  const [scope, setScope] = useState<WantedScope>("me");
  const effectiveScope: WantedScope = isAdmin ? scope : "me";

  const overview = useQuery({
    queryKey: qk.wantedOverview(effectiveScope),
    queryFn: () => api.wantedOverview(effectiveScope),
  });

  const counts = overview.data?.requests;
  const tracking = overview.data?.tracking;
  const instanceView = isAdmin && effectiveScope === "global";

  return (
    <main className="mx-auto max-w-6xl px-4 py-8">
      <PageHeader
        title={<span className="flex items-center gap-1.5">{t("wanted.title")}<InfoHint text={t("wanted.pageHint")} /></span>}
        actions={
          <div className="flex items-center gap-2">
            {isAdmin && instanceView && <RescanControl />}
            {isAdmin && (
              <SegmentedControl<WantedScope>
                value={scope}
                onChange={setScope}
                ariaLabel={t("wanted.title")}
                options={[
                  { value: "me", label: t("wanted.scopeMine") },
                  { value: "global", label: t("wanted.scopeInstance") },
                ]}
              />
            )}
          </div>
        }
      />

      {overview.isLoading ? (
        <div className="grid grid-cols-2 gap-3.5 sm:grid-cols-4">
          {Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-[104px] rounded-2xl" />)}
        </div>
      ) : overview.error ? (
        <p className="text-sm text-red-500">{t("wanted.errorLoading")}</p>
      ) : counts && tracking ? (
        <div className="space-y-8">
          <SummaryTiles counts={counts} tracking={tracking} wide={instanceView} />
          {/* Overseerr-style rail dashboard for BOTH views. Key on scope so switching My view ↔
              Whole instance resets cleanly. */}
          <WantedDashboard key={effectiveScope} scope={instanceView ? "global" : "me"} />
        </div>
      ) : null}
    </main>
  );
}
