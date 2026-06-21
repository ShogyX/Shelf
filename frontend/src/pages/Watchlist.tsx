import { useMemo, useState } from "react";
import { QueryClient, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, MissingRequest, MissingSource, RescanStatus, Subscription } from "../api/client";
import { qk } from "../api/queryKeys";
import { Badge, Button, Card, EmptyState, Disclosure, Select, Toggle } from "../components/ui";
import Cover from "../components/Cover";
import { SeriesModal } from "../components/catalog/CatalogCard";
import { useApp } from "../store";
import { useIsAdmin } from "../auth";
import { useConfirm } from "../components/confirm";

// ---------------------------------------------------------------------------------------------
// Label + tone maps (moved verbatim from the old Missing.tsx, which this page replaces).
// ---------------------------------------------------------------------------------------------
const STATUS_TONE: Record<MissingRequest["status"], "green" | "amber" | "violet" | "default"> = {
  unavailable: "amber",
  resolved: "green",
  open: "default",
  searching: "violet", // in-progress tone, consistent with Jobs/Stock (UI-L6)
  planned: "violet",   // "system handles it, wait" — same family as searching
};
const STATUS_LABEL: Record<MissingRequest["status"], string> = {
  open: "Queued",
  searching: "Searching",
  unavailable: "Unavailable",
  resolved: "Resolved",
  planned: "Planned",
};

const REASON_LABEL: Record<string, string> = {
  no_match: "No match found",
  all_broken: "All sources broken",
  rate_limited: "Rate limited",
  blocked: "Blocked",
  unverified: "Couldn't verify",
  timeout: "Timed out",
  error: "Error",
};
const reasonLabel = (r: string | null) => (r ? REASON_LABEL[r] ?? r : "—");

/** Friendly relative phrasing for the next scheduled re-check (e.g. "~in 3 days", "~today"). */
function relativeDate(iso: string | null): string | null {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  if (isNaN(t)) return null;
  const days = Math.round((t - Date.now()) / 86_400_000);
  if (days <= 0) return "~due now";
  if (days === 1) return "~tomorrow";
  if (days < 14) return `~in ${days} days`;
  return `~${new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" })}`;
}

/** Absolute date matching the page's other date phrasing (e.g. "Jun 18"). */
function shortDate(iso: string | null): string | null {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  if (isNaN(t)) return null;
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

/** Year of a planned title's release date, for the "🕘 Planned · 2026" tag. */
function planYear(iso: string | null): string {
  if (!iso) return "";
  const t = new Date(iso).getTime();
  if (isNaN(t)) return "";
  return ` · ${new Date(iso).getFullYear()}`;
}

const STATUS_OPTIONS = [
  { value: "", label: "Any status" },
  { value: "open", label: "Queued" },
  { value: "searching", label: "Searching" },
  { value: "unavailable", label: "Unavailable" },
  { value: "resolved", label: "Resolved" },
];
const REASON_OPTIONS = [
  { value: "", label: "Any reason" },
  ...Object.entries(REASON_LABEL).map(([value, label]) => ({ value, label })),
];
// The list is grouped by author, so the meaningful orderings are over GROUPS (not flat title/series).
const SORT_OPTIONS = [
  { value: "attention", label: "Needs attention" },
  { value: "author", label: "Author" },
  { value: "newest", label: "Newest" },
];

const SOURCE_LABEL: Record<MissingSource["source"], string> = {
  torrent: "Torrent",
  pipeline: "Usenet pipeline",
  libgen: "Anna's Archive",
};
const SOURCE_STATUS_LABEL: Record<MissingSource["status"], string> = {
  pending: "Queued",
  searching: "Searching…",
  no_match: "No match",
  exhausted: "Exhausted",
  unavailable: "Unavailable",
  matched: "Matched",
  skipped: "Skipped",
};
const SOURCE_STATUS_TONE: Record<MissingSource["status"], "green" | "amber" | "violet" | "default"> = {
  pending: "default",
  searching: "violet",
  no_match: "default",
  exhausted: "amber",
  unavailable: "amber",
  matched: "green",
  skipped: "default",
};
// A one-letter dot per durable source for the dense title row (T U A).
const SOURCE_DOT: Record<MissingSource["source"], string> = { torrent: "T", pipeline: "U", libgen: "A" };

/** Info affordance surfacing the per-source last-search state (result, date, sources tried).
 *  Mirrors ui.tsx InfoHint's toggle/hover pattern but renders a structured per-source list. */
function SourcesInfo({ sources }: { sources: MissingSource[] }) {
  const [open, setOpen] = useState(false);
  return (
    <span className="relative inline-flex align-middle">
      <button
        type="button"
        aria-label="Per-source search details"
        aria-expanded={open}
        className="inline-flex h-[18px] w-[18px] items-center justify-center rounded-full border border-border text-[11px] font-semibold leading-none text-muted transition hover:border-text hover:text-text"
        onClick={() => setOpen((v) => !v)}
        onMouseEnter={() => setOpen(true)}
        onMouseLeave={() => setOpen(false)}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
      >ℹ</button>
      {open && (
        <span
          role="tooltip"
          className="absolute right-0 top-6 z-50 w-72 rounded-lg border border-border bg-surface p-2.5 text-left text-xs font-normal leading-snug text-text shadow-lg"
        >
          <span className="mb-1.5 block text-[10px] font-semibold uppercase tracking-wide text-muted">
            Last search by source
          </span>
          <span className="block space-y-2">
            {sources.map((s) => {
              const when = shortDate(s.last_attempt_at);
              const retry = s.status === "unavailable" ? relativeDate(s.next_retry_at) : null;
              return (
                <span key={s.source} className="block">
                  <span className="flex items-center justify-between gap-2">
                    <span className="font-medium text-text">{SOURCE_LABEL[s.source] ?? s.source}</span>
                    <Badge tone={SOURCE_STATUS_TONE[s.status] ?? "default"}>
                      {SOURCE_STATUS_LABEL[s.status] ?? s.status}
                    </Badge>
                  </span>
                  <span className="mt-0.5 block text-muted">
                    {s.reason && <span>{reasonLabel(s.reason)} · </span>}
                    {when ? <span>{when}</span> : <span>not searched yet</span>}
                    {retry && <span> · retry {retry}</span>}
                  </span>
                </span>
              );
            })}
          </span>
        </span>
      )}
    </span>
  );
}

/** Three durable-source dots (T U A) tinted by their last status — the compact form of SourcesInfo. */
function SourceDots({ sources }: { sources: MissingSource[] }) {
  const dotTone: Record<string, string> = {
    matched: "border-green-500/50 text-green-600 dark:text-green-400",
    searching: "border-violet-500/50 text-violet-600 dark:text-violet-300",
    exhausted: "border-amber-500/50 text-amber-600 dark:text-amber-400",
    unavailable: "border-amber-500/50 text-amber-600 dark:text-amber-400",
  };
  return (
    <span className="inline-flex items-center gap-0.5">
      {sources.map((s) => (
        <span
          key={s.source}
          title={`${SOURCE_LABEL[s.source] ?? s.source}: ${SOURCE_STATUS_LABEL[s.status] ?? s.status}`}
          className={`inline-flex h-[15px] w-[15px] items-center justify-center rounded-full border text-[9px] font-semibold leading-none ${
            dotTone[s.status] ?? "border-border text-muted"
          }`}
        >
          {SOURCE_DOT[s.source] ?? "?"}
        </span>
      ))}
    </span>
  );
}

// ---------------------------------------------------------------------------------------------
// Grouping: missing rows → Author → (Series sub-groups + standalone titles); followed-but-empty
// authors/series still render as headers. Matching of a Subscription to a group is by kind +
// display_name (case-insensitive), done entirely client-side.
// ---------------------------------------------------------------------------------------------
const UNGROUPED = " ungrouped"; // sorts/keys last; never a real author name

interface SeriesGroup {
  name: string;
  rows: MissingRequest[];
  sub?: Subscription; // matched series follow (if any)
}
interface AuthorGroup {
  name: string;        // author name, or UNGROUPED sentinel
  standalone: MissingRequest[]; // titles under this author with no series
  series: SeriesGroup[];
  sub?: Subscription;  // matched author follow (if any)
}

const norm = (s: string | null | undefined) => (s ?? "").trim().toLowerCase();

// ---------------------------------------------------------------------------------------------
// Optimistic follow/unfollow on the shared subscriptions cache. The header Toggle reads `following`
// from a group's matched Subscription (by kind + normalized display_name), so flipping the cache
// flips every toggle instantly. Both return a `restore()` to roll back on error; onSettled then
// invalidates to reconcile (the synthetic row's temp id/key gets replaced by the server's).
// ---------------------------------------------------------------------------------------------
async function optimisticFollow(qc: QueryClient, kind: "author" | "series", displayName: string) {
  await qc.cancelQueries({ queryKey: qk.subscriptions() }); // await: stop an in-flight refetch clobbering the optimistic write
  const prev = qc.getQueryData<Subscription[]>(qk.subscriptions());
  const optimistic: Subscription = {
    id: -Date.now(), kind, key: norm(displayName), display_name: displayName,
    active: true, auto_request: true, auto_added: 0, last_checked_at: null, created_at: null,
  };
  // Avoid a duplicate if a matching follow is somehow already present.
  const exists = (prev ?? []).some((s) => s.kind === kind && norm(s.display_name) === norm(displayName));
  qc.setQueryData<Subscription[]>(qk.subscriptions(), exists ? prev : [...(prev ?? []), optimistic]);
  return { restore: () => qc.setQueryData(qk.subscriptions(), prev) };
}

async function optimisticUnfollow(qc: QueryClient, id: number) {
  await qc.cancelQueries({ queryKey: qk.subscriptions() }); // await: stop an in-flight refetch clobbering the optimistic write
  const prev = qc.getQueryData<Subscription[]>(qk.subscriptions());
  qc.setQueryData<Subscription[]>(qk.subscriptions(), (prev ?? []).filter((s) => s.id !== id));
  return { restore: () => qc.setQueryData(qk.subscriptions(), prev) };
}

function buildGroups(rows: MissingRequest[], subs: Subscription[]): AuthorGroup[] {
  const authorSubs = new Map<string, Subscription>();
  const seriesSubs = new Map<string, Subscription>();
  for (const s of subs) {
    if (s.kind === "author") authorSubs.set(norm(s.display_name), s);
    else seriesSubs.set(norm(s.display_name), s);
  }

  const byAuthor = new Map<string, AuthorGroup>();
  const author = (name: string | null): AuthorGroup => {
    const key = name && name.trim() ? name : UNGROUPED;
    let g = byAuthor.get(norm(key));
    if (!g) {
      const asub = authorSubs.get(norm(key));
      // Prefer the follow's canonical display name over the first row's author casing.
      g = { name: asub?.display_name ?? key, standalone: [], series: [], sub: asub };
      byAuthor.set(norm(key), g);
    }
    return g;
  };

  for (const r of rows) {
    const g = author(r.author);
    if (r.series && r.series.trim()) {
      let sg = g.series.find((x) => norm(x.name) === norm(r.series));
      if (!sg) {
        const ssub = seriesSubs.get(norm(r.series));
        sg = { name: ssub?.display_name ?? r.series, rows: [], sub: ssub };
        g.series.push(sg);
      }
      sg.rows.push(r);
    } else {
      g.standalone.push(r);
    }
  }

  // Inject followed-but-empty authors so they still show as headers.
  for (const s of subs) {
    if (s.kind !== "author") continue;
    if (!byAuthor.has(norm(s.display_name)))
      byAuthor.set(norm(s.display_name), { name: s.display_name, standalone: [], series: [], sub: s });
  }
  // Inject followed-but-empty series. A series follow without a matching wanted row has no known
  // author — surface it under the Ungrouped bucket so it isn't lost.
  const seenSeries = new Set<string>();
  for (const g of byAuthor.values()) for (const sg of g.series) seenSeries.add(norm(sg.name));
  for (const s of subs) {
    if (s.kind !== "series" || seenSeries.has(norm(s.display_name))) continue;
    const g = author(null);
    g.series.push({ name: s.display_name, rows: [], sub: s });
    seenSeries.add(norm(s.display_name));
  }

  return [...byAuthor.values()];
}

// A row is "actionable" if it's a released title needing attention (unavailable / searched out).
const isActionable = (r: MissingRequest) => r.status === "unavailable";
const isPlanned = (r: MissingRequest) => r.status === "planned";

function groupRows(g: AuthorGroup): MissingRequest[] {
  return [...g.standalone, ...g.series.flatMap((s) => s.rows)];
}

// ---------------------------------------------------------------------------------------------
// Title cover-card — one cell of the poster-wall grid. Replaces the old dense TitleRow.
//
// Layout follows the ui-ux-pro-max "content-first cover grid": a fixed 2:3 poster (aspect-ratio
// reserves space → no CLS), a truncated title, one compact status signal, and a per-source detail
// affordance. The dense-row details (source dots + ℹ popover, attempts/next-recheck, admin recheck)
// stay ACCESSIBLE via a one-line footer under the cover + an ℹ overlay on the poster, so the card
// stays scannable without growing tall.
// ---------------------------------------------------------------------------------------------
function TitleCard({ r, isAdmin }: { r: MissingRequest; isAdmin: boolean }) {
  const qc = useQueryClient();
  const toast = useApp((s) => s.toast);
  const recheck = useMutation({
    mutationFn: () => api.recheckMissing(r.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.missing() });
      qc.invalidateQueries({ queryKey: qk.missingStats() });
      toast(`Re-checking “${r.title}”`, "success");
    },
    onError: (e) => toast((e as Error).message, "error"),
  });
  const isGoodreads = r.origin === "goodreads";
  const planned = isPlanned(r);
  const next = relativeDate(r.next_check_at);
  const hasSources = !planned && !isGoodreads && !!r.sources && r.sources.length > 0;

  return (
    <div className="flex flex-col gap-1.5">
      {/* Poster: 2:3 cover with the per-source ℹ popover overlaid top-right. */}
      <div className="relative aspect-[2/3] w-full overflow-hidden rounded-lg border border-border bg-surface-2">
        <Cover title={r.title} author={r.author} coverUrl={r.cover_url} small />
        {r.series_position != null && (
          <span className="absolute left-1 top-1 rounded bg-black/55 px-1.5 py-0.5 text-[10px] font-semibold leading-none text-white tabular-nums">
            #{r.series_position}
          </span>
        )}
        {hasSources && (
          <span className="absolute right-1 top-1 rounded-full bg-black/70 text-white">
            <SourcesInfo sources={r.sources!} />
          </span>
        )}
      </div>

      {/* Title — truncated to two lines, full text on hover. */}
      <div className="line-clamp-2 text-xs font-medium leading-snug text-text" title={r.title}>
        {r.title}
      </div>

      {/* Compact status signal: planned/goodreads/status — exactly one badge. */}
      <div className="flex flex-wrap items-center gap-1">
        {isGoodreads ? (
          <Badge tone="violet">🕘 Goodreads</Badge>
        ) : planned ? (
          <Badge tone="violet">🕘 Planned{planYear(r.release_date)}</Badge>
        ) : (
          <Badge tone={STATUS_TONE[r.status]}>{STATUS_LABEL[r.status]}</Badge>
        )}
      </div>

      {/* Footer line: the per-title detail from the old dense row, kept accessible + compact. */}
      <div className="flex items-center gap-1.5 text-[11px] text-muted">
        {planned ? (
          <span>waiting for release</span>
        ) : isGoodreads ? (
          <span>waiting on hook</span>
        ) : (
          <>
            {hasSources && <SourceDots sources={r.sources!} />}
            <span className="min-w-0 truncate">
              {r.status === "unavailable" && r.failure_reason
                ? reasonLabel(r.failure_reason)
                : next && r.status !== "resolved"
                ? `next ${next}`
                : `${r.attempts} ${r.attempts === 1 ? "try" : "tries"}`}
            </span>
            {isAdmin && (
              <Button
                size="icon"
                variant="ghost"
                className="ml-auto"
                title="Recheck now"
                aria-label="Recheck now"
                disabled={recheck.isPending}
                onClick={() => recheck.mutate()}
              >
                {recheck.isPending ? "…" : "↻"}
              </Button>
            )}
          </>
        )}
      </div>
    </div>
  );
}

/** Responsive poster-wall: auto-fill columns at ~7.5rem min (fewer columns on mobile, more on
 *  desktop), 8px-rhythm gaps. Covers never overflow because each cell sets its own aspect-ratio. */
function TitleGrid({ rows, isAdmin }: { rows: MissingRequest[]; isAdmin: boolean }) {
  return (
    <div className="grid grid-cols-[repeat(auto-fill,minmax(7.5rem,1fr))] gap-3 sm:gap-4">
      {rows.map((r) => (
        <TitleCard key={`m-${r.id}`} r={r} isAdmin={isAdmin} />
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------------------------
// Group header + body (one author group, with its series sub-groups).
// ---------------------------------------------------------------------------------------------
function GroupBlock({
  g,
  isAdmin,
  open,
  onToggle,
}: {
  g: AuthorGroup;
  isAdmin: boolean;
  open: boolean;
  onToggle: () => void;
}) {
  const qc = useQueryClient();
  const toast = useApp((s) => s.toast);
  const confirm = useConfirm();
  const isUngrouped = g.name === UNGROUPED;
  const rows = groupRows(g);
  const wantedCount = rows.filter((r) => !isPlanned(r)).length;
  const plannedCount = rows.filter(isPlanned).length;

  // A representative catalog_work_id lets us follow the AUTHOR from the header (subscribe by catalog_id).
  const repCatalogId = rows.find((r) => r.catalog_work_id != null)?.catalog_work_id ?? null;

  const follow = useMutation({
    mutationFn: () =>
      api.follow({ kind: "author", catalog_id: repCatalogId ?? undefined }),
    // Optimistic: the toggle reads `following` from the subscriptions cache (matched by kind +
    // display_name), so slip in a synthetic sub to flip it instantly; onSettled reconciles to the
    // server's real row.
    onMutate: () => optimisticFollow(qc, "author", g.name),
    onError: (e, _v, ctx) => { ctx?.restore(); toast((e as Error).message, "error"); },
    onSuccess: () => toast(`Following ${g.name} — new releases auto-fetch`, "success"),
    onSettled: () => qc.invalidateQueries({ queryKey: qk.subscriptions() }),
  });
  const unfollow = useMutation({
    mutationFn: (id: number) => api.unfollow(id),
    onMutate: (id) => optimisticUnfollow(qc, id),
    onError: (e, _v, ctx) => { ctx?.restore(); toast((e as Error).message, "error"); },
    onSuccess: () => toast(`Unfollowed ${g.name}`, "success"),
    onSettled: () => qc.invalidateQueries({ queryKey: qk.subscriptions() }),
  });
  const rescan = useMutation({
    mutationFn: () => api.rescanWanted({ author: g.name }),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: qk.rescanStatus() });
      toast(`Queued ${res.queued} ${res.queued === 1 ? "title" : "titles"} for rescan`, "success");
    },
    onError: (e) => toast((e as Error).message, "error"),
  });

  async function toggleFollow(on: boolean) {
    if (on) {
      if (repCatalogId == null) {
        toast("Can't follow this author yet — no catalog match.", "error");
        return;
      }
      follow.mutate();
    } else if (g.sub) {
      if (
        await confirm({
          title: "Unfollow",
          message: `Stop following ${g.name}? New titles won't be auto-fetched.`,
          confirmText: "Unfollow",
        })
      )
        unfollow.mutate(g.sub.id);
    }
  }

  const following = !!g.sub;  // a subscription's presence = following (auto_request paused still = followed)
  const followBusy = follow.isPending || unfollow.isPending;

  return (
    <div>
      <div className="flex items-center gap-2 bg-surface-2 px-3 py-2">
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={open}
          aria-label={open ? "Collapse" : "Expand"}
          className="shrink-0 text-muted transition hover:text-text"
        >
          <span className={`inline-block transition-transform ${open ? "rotate-90" : ""}`}>▶</span>
        </button>
        <span className="min-w-0 flex-1 truncate font-semibold text-text">
          {isUngrouped ? "Ungrouped / Other" : g.name}
        </span>
        {!isUngrouped && <Badge tone="amber">author</Badge>}
        {wantedCount > 0 && (
          <span className="shrink-0 text-xs text-muted">
            {wantedCount} wanted{plannedCount > 0 ? ` · ${plannedCount} planned` : ""}
          </span>
        )}
        {wantedCount === 0 && plannedCount > 0 && (
          <span className="shrink-0 text-xs text-muted">{plannedCount} planned</span>
        )}
        {/* The single follow control for the whole author group. */}
        {!isUngrouped && (
          <span title={following ? "Following — auto-fetch on" : "Follow this author"}>
            <Toggle checked={following} onChange={(on) => !followBusy && toggleFollow(on)} />
          </span>
        )}
        {isAdmin && !isUngrouped && wantedCount > 0 && (
          <Button
            size="sm"
            variant="outline"
            disabled={rescan.isPending}
            onClick={() => rescan.mutate()}
          >
            {rescan.isPending ? "Queuing…" : "Rescan"}
          </Button>
        )}
      </div>

      {open && (
        <div className="border-t border-border/60">
          {g.standalone.length > 0 && (
            <div className="px-4 py-3">
              <TitleGrid rows={g.standalone} isAdmin={isAdmin} />
            </div>
          )}
          {g.series.map((sg) => (
            <SeriesSubGroup key={`s-${sg.name}`} sg={sg} isAdmin={isAdmin} />
          ))}
          {rows.length === 0 && (
            <div className="px-4 py-3 text-xs text-muted">
              Following — new releases auto-fetch. Nothing outstanding.
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function SeriesSubGroup({ sg, isAdmin }: { sg: SeriesGroup; isAdmin: boolean }) {
  const qc = useQueryClient();
  const toast = useApp((s) => s.toast);
  const confirm = useConfirm();
  const [showSeries, setShowSeries] = useState(false);
  const wantedCount = sg.rows.filter((r) => !isPlanned(r)).length;
  const plannedCount = sg.rows.filter(isPlanned).length;
  const repCatalogId = sg.rows.find((r) => r.catalog_work_id != null)?.catalog_work_id ?? null;

  const follow = useMutation({
    mutationFn: () => api.follow({ kind: "series", series_name: sg.name }),
    onMutate: () => optimisticFollow(qc, "series", sg.name),
    onError: (e, _v, ctx) => { ctx?.restore(); toast((e as Error).message, "error"); },
    onSuccess: () => toast(`Following “${sg.name}” — new releases auto-fetch`, "success"),
    onSettled: () => qc.invalidateQueries({ queryKey: qk.subscriptions() }),
  });
  const unfollow = useMutation({
    mutationFn: (id: number) => api.unfollow(id),
    onMutate: (id) => optimisticUnfollow(qc, id),
    onError: (e, _v, ctx) => { ctx?.restore(); toast((e as Error).message, "error"); },
    onSuccess: () => toast(`Unfollowed “${sg.name}”`, "success"),
    onSettled: () => qc.invalidateQueries({ queryKey: qk.subscriptions() }),
  });
  const rescan = useMutation({
    mutationFn: () => api.rescanWanted({ series: sg.name }),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: qk.rescanStatus() });
      toast(`Queued ${res.queued} ${res.queued === 1 ? "title" : "titles"} for rescan`, "success");
    },
    onError: (e) => toast((e as Error).message, "error"),
  });

  const following = !!sg.sub;  // subscription presence = following (paused auto_request still = followed)
  const followBusy = follow.isPending || unfollow.isPending;

  async function toggleFollow(on: boolean) {
    if (on) follow.mutate();
    else if (sg.sub) {
      if (
        await confirm({
          title: "Unfollow",
          message: `Stop following “${sg.name}”? New titles won't be auto-fetched.`,
          confirmText: "Unfollow",
        })
      )
        unfollow.mutate(sg.sub.id);
    }
  }

  return (
    <div>
      <div className="flex items-center gap-2 px-3 py-1.5 pl-6">
        {/* SeriesModal chip — label only; lazy roster/counts live inside the modal. */}
        {repCatalogId != null ? (
          <button
            type="button"
            onClick={() => setShowSeries(true)}
            className="inline-flex items-center rounded-full border border-border px-2 py-0.5 text-[11px] font-medium text-violet-600 transition hover:border-text hover:text-text dark:text-violet-300"
            title="View the whole series"
          >
            Series · {sg.name}
          </button>
        ) : (
          <Badge tone="violet">Series · {sg.name}</Badge>
        )}
        {wantedCount > 0 && (
          <span className="shrink-0 text-xs text-muted">
            {wantedCount} wanted{plannedCount > 0 ? ` · ${plannedCount} planned` : ""}
          </span>
        )}
        {wantedCount === 0 && plannedCount > 0 && (
          <span className="shrink-0 text-xs text-muted">{plannedCount} planned</span>
        )}
        <span className="ml-auto" title={following ? "Following — auto-fetch on" : "Follow this series"}>
          <Toggle checked={following} onChange={(on) => !followBusy && toggleFollow(on)} />
        </span>
        {isAdmin && wantedCount > 0 && (
          <Button size="sm" variant="outline" disabled={rescan.isPending} onClick={() => rescan.mutate()}>
            {rescan.isPending ? "Queuing…" : "Rescan"}
          </Button>
        )}
      </div>
      {sg.rows.length > 0 && (
        <div className="px-4 pb-3 pl-6">
          <TitleGrid rows={sg.rows} isAdmin={isAdmin} />
        </div>
      )}
      {sg.rows.length === 0 && (
        <div className="px-4 py-2 pl-8 text-xs text-muted">
          Following — new releases auto-fetch. Nothing outstanding.
        </div>
      )}
      {showSeries && repCatalogId != null && (
        <SeriesModal catalogId={repCatalogId} seriesName={sg.name} onClose={() => setShowSeries(false)} />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------------------------
// Summary strip (admin): stats + Rescan all + the live progress strip.
// ---------------------------------------------------------------------------------------------
function SummaryStrip() {
  const qc = useQueryClient();
  const toast = useApp((s) => s.toast);
  const confirm = useConfirm();
  const stats = useQuery({ queryKey: qk.missingStats(), queryFn: api.missingStats });
  const status = useQuery({
    queryKey: qk.rescanStatus(),
    queryFn: api.getRescanStatus,
    refetchInterval: (q) => (q.state.data?.active ? 1500 : false),
  });

  const rescanAll = useMutation({
    mutationFn: () => api.rescanWanted({ scope: "all" }),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: qk.rescanStatus() });
      status.refetch();
      toast(`Queued ${res.queued} ${res.queued === 1 ? "title" : "titles"} for rescan`, "success");
    },
    onError: (e) => toast((e as Error).message, "error"),
  });

  const s = stats.data;
  const byStatus = s ? Object.entries(s.by_status).filter(([, n]) => n > 0) : [];
  const nextDue = s ? relativeDate(s.next_due_at) : null;
  const rs = status.data;
  const pct = rs && rs.total > 0 ? Math.round((rs.done / rs.total) * 100) : 0;

  async function onRescanAll() {
    const n = s?.total_unavailable ?? 0;
    if (
      n > 10 &&
      !(await confirm({
        title: "Rescan all",
        message: `Queue ${n} titles? They run in batches, a few at a time.`,
        confirmText: "Queue rescan",
      }))
    )
      return;
    rescanAll.mutate();
  }

  if (!s) return null;
  return (
    <Card className="mb-4 p-4">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2 text-sm">
        <span><span className="font-semibold text-text">{s.total}</span> <span className="text-muted">tracked</span></span>
        <span><span className="font-semibold text-text">{s.total_unavailable}</span> <span className="text-muted">unavailable</span></span>
        {nextDue && <span className="text-muted">next re-check {nextDue}</span>}
        <Button
          size="sm"
          variant="outline"
          className="ml-auto"
          disabled={rescanAll.isPending}
          onClick={onRescanAll}
        >
          {rescanAll.isPending ? "Queuing…" : "Rescan all"}
        </Button>
      </div>

      {byStatus.length > 0 && (
        <div className="mt-3 flex flex-wrap items-center gap-1.5">
          {byStatus.map(([k, n]) => (
            <Badge key={k} tone={STATUS_TONE[k as MissingRequest["status"]] ?? "default"}>
              {STATUS_LABEL[k as MissingRequest["status"]] ?? k}: {n}
            </Badge>
          ))}
        </div>
      )}

      {rs?.active && (
        <div className="mt-3">
          <div className="mb-1 text-xs text-muted">
            ⟳ Rescanning · {rs.done} of {rs.total} done · {rs.queued} queued
          </div>
          <div className="h-1.5 w-full overflow-hidden rounded-full bg-surface-2">
            <div className="h-full rounded-full bg-accent transition-all" style={{ width: `${pct}%` }} />
          </div>
        </div>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------------------------------------
// Page.
// ---------------------------------------------------------------------------------------------
export default function Watchlist() {
  const isAdmin = useIsAdmin();
  const [sort, setSort] = useState("attention");
  const [status, setStatus] = useState("");
  const [reason, setReason] = useState("");
  const [followedOnly, setFollowedOnly] = useState(false);
  const [hidePlanned, setHidePlanned] = useState(false);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());

  const params = {
    ...(isAdmin ? { status: status || undefined, reason: reason || undefined } : {}),
  };
  const missingQ = useQuery({
    queryKey: qk.missing(isAdmin ? status : "", isAdmin ? reason : "", ""),
    queryFn: () => api.listMissing(params),
  });
  const subsQ = useQuery({ queryKey: qk.subscriptions(), queryFn: api.listSubscriptions });

  const loading = missingQ.isLoading || subsQ.isLoading;
  const error = missingQ.error || subsQ.error;

  const groups = useMemo(() => {
    let rows = missingQ.data ?? [];
    if (hidePlanned) rows = rows.filter((r) => !isPlanned(r));
    let gs = buildGroups(rows, subsQ.data ?? []);
    if (followedOnly) gs = gs.filter((g) => g.sub || g.series.some((s) => s.sub));

    const score = (g: AuthorGroup) => {
      const rows = groupRows(g);
      if (sort === "attention") return -rows.filter(isActionable).length;
      return 0;
    };
    const cmp: Record<string, (a: AuthorGroup, b: AuthorGroup) => number> = {
      attention: (a, b) => score(a) - score(b) || a.name.localeCompare(b.name),
      author: (a, b) => a.name.localeCompare(b.name),
      series: (a, b) => a.name.localeCompare(b.name),
      title: (a, b) => a.name.localeCompare(b.name),
      newest: (a, b) => {
        const ai = Math.max(0, ...groupRows(a).map((r) => r.id));
        const bi = Math.max(0, ...groupRows(b).map((r) => r.id));
        return bi - ai;
      },
    };
    gs.sort(cmp[sort] ?? cmp.author);
    // Ungrouped bucket always last.
    gs.sort((a, b) => Number(a.name === UNGROUPED) - Number(b.name === UNGROUPED));
    return gs;
  }, [missingQ.data, subsQ.data, sort, followedOnly, hidePlanned]);

  const allKeys = useMemo(() => groups.map((g) => norm(g.name)), [groups]);
  const isOpen = (g: AuthorGroup) => !collapsed.has(norm(g.name));
  const toggle = (g: AuthorGroup) =>
    setCollapsed((prev) => {
      const next = new Set(prev);
      const k = norm(g.name);
      next.has(k) ? next.delete(k) : next.add(k);
      return next;
    });
  const expandAll = () => setCollapsed(new Set());
  const collapseAll = () => setCollapsed(new Set(allKeys));

  const controls = (
    <div className="flex flex-wrap items-end gap-3">
      <div className="min-w-[12rem]">
        <Select label="Sort" value={sort} onChange={setSort} options={SORT_OPTIONS} />
      </div>
      {isAdmin && (
        <>
          <div className="min-w-[10rem]">
            <Select label="Status" value={status} onChange={setStatus} options={STATUS_OPTIONS} />
          </div>
          <div className="min-w-[10rem]">
            <Select label="Reason" value={reason} onChange={setReason} options={REASON_OPTIONS} />
          </div>
        </>
      )}
      <Toggle checked={followedOnly} onChange={setFollowedOnly} label="Followed only" />
      <Toggle checked={hidePlanned} onChange={setHidePlanned} label="Hide planned" />
      <div className="ml-auto flex items-center gap-2">
        <Button size="sm" variant="ghost" onClick={expandAll}>Expand all</Button>
        <Button size="sm" variant="ghost" onClick={collapseAll}>Collapse all</Button>
      </div>
    </div>
  );

  return (
    <main className="mx-auto max-w-3xl px-4 py-8">
      <h1 className="mb-1 text-2xl font-semibold">Watchlist</h1>
      <p className="mb-6 text-sm text-muted">
        Titles you want and the authors and series you follow — grouped together. Follows auto-fetch new
        releases; wanted titles keep being searched until they turn up.
      </p>

      {isAdmin && <SummaryStrip />}

      {/* Controls: inline on ≥sm, behind a Disclosure on mobile. */}
      <div className="mb-4 hidden sm:block">{controls}</div>
      <div className="mb-4 sm:hidden">
        <Disclosure title="Sort & filter">{controls}</Disclosure>
      </div>

      {loading ? (
        <Card className="divide-y divide-border">
          {Array.from({ length: 4 }).map((_, i) => (
            <div key={i} className="animate-pulse">
              <div className="flex items-center gap-2 bg-surface-2 px-3 py-3">
                <div className="h-4 w-40 rounded bg-border" />
                <div className="ml-auto h-4 w-16 rounded bg-border" />
              </div>
              <div className="space-y-2 px-4 py-3">
                <div className="h-3 w-3/4 rounded bg-border" />
                <div className="h-3 w-1/2 rounded bg-border" />
              </div>
            </div>
          ))}
        </Card>
      ) : error ? (
        <p className="text-sm text-red-500">{(error as Error).message}</p>
      ) : groups.length === 0 ? (
        <EmptyState
          title={followedOnly ? "No follows yet" : "Your watchlist is empty"}
          hint={
            followedOnly
              ? "Open a title in the Catalog and use “Follow author” or “Follow series” to get new releases automatically."
              : isAdmin
              ? "Nothing is currently tracked as wanted, and no authors or series are followed."
              : "Everything you've requested was found, and you're not following anyone yet."
          }
        />
      ) : (
        <Card className="divide-y divide-border">
          {groups.map((g) => (
            <GroupBlock key={norm(g.name)} g={g} isAdmin={isAdmin} open={isOpen(g)} onToggle={() => toggle(g)} />
          ))}
        </Card>
      )}
    </main>
  );
}
