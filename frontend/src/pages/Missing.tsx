import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, MissingRequest, MissingSource } from "../api/client";
import { qk } from "../api/queryKeys";
import { Badge, Button, Card, EmptyState, Select, Spinner } from "../components/ui";
import { SeriesModal } from "../components/catalog/CatalogCard";
import { useApp } from "../store";
import { useIsAdmin } from "../auth";

const STATUS_TONE: Record<MissingRequest["status"], "green" | "amber" | "violet" | "default"> = {
  unavailable: "amber",
  resolved: "green",
  open: "default",
  searching: "violet",  // in-progress tone, consistent with Jobs/Stock (UI-L6)
};
const STATUS_LABEL: Record<MissingRequest["status"], string> = {
  open: "Open",
  searching: "Searching",
  unavailable: "Unavailable",
  resolved: "Resolved",
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

const STATUS_OPTIONS = [
  { value: "", label: "Any status" },
  { value: "open", label: "Open" },
  { value: "searching", label: "Searching" },
  { value: "unavailable", label: "Unavailable" },
  { value: "resolved", label: "Resolved" },
];
const REASON_OPTIONS = [
  { value: "", label: "Any reason" },
  ...Object.entries(REASON_LABEL).map(([value, label]) => ({ value, label })),
];
const ORIGIN_OPTIONS = [
  { value: "", label: "Any source" },
  { value: "request", label: "Requests" },
  { value: "series", label: "From a series" },
  { value: "goodreads", label: "Goodreads (waiting on hook)" },
];
const SORT_OPTIONS = [
  { value: "newest", label: "Newest first" },
  { value: "author", label: "Author" },
  { value: "series", label: "Series" },
  { value: "title", label: "Title" },
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

/** Absolute date matching the page's other date phrasing (e.g. "Jun 18"). */
function shortDate(iso: string | null): string | null {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  if (isNaN(t)) return null;
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

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

function StatsSummary() {
  const q = useQuery({ queryKey: qk.missingStats(), queryFn: api.missingStats });
  if (!q.data) return null;
  const s = q.data;
  const byReason = Object.entries(s.by_reason).filter(([, n]) => n > 0);
  const byStatus = Object.entries(s.by_status).filter(([, n]) => n > 0);
  const nextDue = relativeDate(s.next_due_at);
  return (
    <Card className="mb-4 p-4">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2 text-sm">
        <span><span className="font-semibold text-text">{s.total}</span> <span className="text-muted">tracked</span></span>
        <span><span className="font-semibold text-text">{s.total_unavailable}</span> <span className="text-muted">unavailable</span></span>
        {nextDue && <span className="text-muted">next re-check {nextDue}</span>}
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
      {byReason.length > 0 && (
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          {byReason.map(([k, n]) => (
            <Badge key={k}>{reasonLabel(k)}: {n}</Badge>
          ))}
        </div>
      )}
    </Card>
  );
}

function Row({ r, isAdmin }: { r: MissingRequest; isAdmin: boolean }) {
  const isGoodreads = r.origin === "goodreads";
  const isSeries = r.origin === "series";
  const [showSeries, setShowSeries] = useState(false);
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
  const next = relativeDate(r.next_check_at);
  return (
    <Card className="flex items-start justify-between gap-3 p-4">
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-medium text-text">{r.title}</span>
          {isGoodreads ? (
            <Badge tone="violet">Goodreads · waiting on hook</Badge>
          ) : (
            <>
              <Badge tone={STATUS_TONE[r.status]}>{STATUS_LABEL[r.status]}</Badge>
              {isSeries && <Badge tone="violet">from series</Badge>}
            </>
          )}
          {/* Label-only chip — opens the existing SeriesModal (lazy roster + owned/total counts live
              INSIDE the modal; no detect_series / count on load). */}
          {!isGoodreads && r.catalog_work_id != null && r.series && (
            <button
              type="button"
              onClick={() => setShowSeries(true)}
              className="inline-flex items-center rounded-full border border-border px-2 py-0.5 text-[11px] font-medium text-muted transition hover:border-text hover:text-text"
              title="View the whole series"
            >
              Series · {r.series}{r.series_position ? ` · #${r.series_position}` : ""}
            </button>
          )}
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
          {r.author && <span>by {r.author}</span>}
          {isGoodreads ? (
            <span>queued from a Goodreads shelf — auto-hooked when it appears in the index</span>
          ) : (
            <>
              {isSeries && r.origin_detail && <span>from series “{r.origin_detail}”</span>}
              {r.failure_reason && <span>{reasonLabel(r.failure_reason)}</span>}
              <span>{r.attempts} {r.attempts === 1 ? "attempt" : "attempts"}</span>
              {r.last_provider && <span>via {r.last_provider}</span>}
              {next && r.status !== "resolved" && <span>next re-check {next}</span>}
              {isAdmin && r.requester_count != null && (
                <span title={(r.requesters ?? []).join(", ")}>
                  {r.requester_count} {r.requester_count === 1 ? "requester" : "requesters"}
                  {r.requesters && r.requesters.length > 0 && `: ${r.requesters.join(", ")}`}
                </span>
              )}
            </>
          )}
        </div>
        {showSeries && r.catalog_work_id != null && (
          <SeriesModal
            catalogId={r.catalog_work_id}
            seriesName={r.series ?? null}
            onClose={() => setShowSeries(false)}
          />
        )}
      </div>
      {!isGoodreads && (
        <div className="flex shrink-0 items-center gap-2">
          {r.sources && r.sources.length > 0 && <SourcesInfo sources={r.sources} />}
          {isAdmin && (
            <Button size="sm" variant="outline" disabled={recheck.isPending} onClick={() => recheck.mutate()}>
              {recheck.isPending ? "Re-checking…" : "Recheck now"}
            </Button>
          )}
        </div>
      )}
    </Card>
  );
}

export default function Missing() {
  const isAdmin = useIsAdmin();
  const [status, setStatus] = useState("");
  const [reason, setReason] = useState("");
  const [origin, setOrigin] = useState("");
  const [sort, setSort] = useState("newest");

  // Status/reason filters are admin-only; sort is a user feature, so it applies for everyone.
  const params = {
    ...(isAdmin ? { status: status || undefined, reason: reason || undefined } : {}),
    sort,
  };
  const q = useQuery({
    queryKey: qk.missing(isAdmin ? status : "", isAdmin ? reason : "", sort),
    queryFn: () => api.listMissing(params),
  });
  // Source is filtered client-side (goodreads rows are a read-time union, not a backend query param).
  const rows = (q.data ?? []).filter((r) => !origin || (r.origin ?? "request") === origin);

  return (
    <main className="mx-auto max-w-3xl px-4 py-8">
      <h1 className="mb-1 text-2xl font-semibold">Wanted</h1>
      <p className="mb-6 text-sm text-muted">
        {isAdmin
          ? "Titles Shelf couldn't find, across every request."
          : "Titles you asked for that Shelf hasn't been able to find yet."}
      </p>

      {isAdmin && (
        <>
          <StatsSummary />
          <div className="mb-4 grid gap-3 sm:grid-cols-3">
            <Select label="Status" value={status} onChange={setStatus} options={STATUS_OPTIONS} />
            <Select label="Reason" value={reason} onChange={setReason} options={REASON_OPTIONS} />
            <Select label="Source" value={origin} onChange={setOrigin} options={ORIGIN_OPTIONS} />
          </div>
        </>
      )}

      {/* Sort is a user feature (R7-9) — shown to everyone, not gated behind admin. */}
      <div className="mb-4 max-w-[14rem]">
        <Select label="Sort" value={sort} onChange={setSort} options={SORT_OPTIONS} />
      </div>

      {q.isLoading ? (
        <Spinner label="Loading…" />
      ) : q.isError ? (
        <p className="text-sm text-red-500">{(q.error as Error).message}</p>
      ) : rows.length === 0 ? (
        <EmptyState
          title="Nothing missing"
          hint={
            isAdmin
              ? "No titles match — nothing is currently tracked as unfound."
              : "Everything you've requested was found."
          }
        />
      ) : (
        <div className="space-y-2">
          {rows.map((r) => (
            <Row key={`${r.origin ?? "request"}-${r.id}`} r={r} isAdmin={isAdmin} />
          ))}
        </div>
      )}
    </main>
  );
}
