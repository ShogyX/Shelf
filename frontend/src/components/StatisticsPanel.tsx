// Settings → Statistics: outbound-request telemetry, VirusTotal API usage, and acquisition-pipeline
// outcomes (where fetches succeeded + why they failed), all on one page.
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
import { api } from "../api/client";
import { Badge, Card, InfoHint, Spinner } from "./ui";
import RequestStatsCard from "./RequestStatsCard";

const fmt = (n: number) => n.toLocaleString();

const fmtBytes = (n: number) => {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${units[i]}`;
};

const fmtSlot = (iso: string) => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
};

function StatRow({ label, value, tone }: { label: string; value: number; tone?: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-0.5 text-sm">
      <span className="text-muted">{label}</span>
      <span className={`font-semibold tabular-nums ${tone ?? "text-text"}`}>{fmt(value)}</span>
    </div>
  );
}

function VirusTotalStatsCard() {
  const { t } = useTranslation();
  const q = useQuery({ queryKey: ["vt-usage"], queryFn: api.getVirusTotalUsage });
  const d = q.data;
  return (
    <Card className="mb-4 p-4">
      <h2 className="mb-3 flex items-center gap-1.5 font-semibold">
        {t("stats.vt.title")}
        <InfoHint text={t("stats.vt.infoHint")} />
      </h2>
      {q.isLoading ? (
        <Spinner label={t("stats.loading")} />
      ) : !d ? (
        <p className="text-sm text-muted">{t("stats.vt.connectHint")}</p>
      ) : d.total === 0 ? (
        <p className="text-sm text-muted">{t("stats.vt.noLookups")}</p>
      ) : (
        <div className="grid gap-x-8 sm:grid-cols-2">
          <StatRow label={t("stats.vt.lookups30d")} value={d.total} />
          <StatRow label={t("stats.vt.last24h")} value={d.last_24h} />
          <StatRow label={t("stats.vt.rateLimited")} value={d.by_outcome.blocked || 0}
            tone={(d.by_outcome.blocked || 0) > 0 ? "text-amber-600" : "text-text"} />
          <StatRow label={t("stats.vt.errors")} value={d.by_outcome.error || 0}
            tone={(d.by_outcome.error || 0) > 0 ? "text-red-500" : "text-text"} />
        </div>
      )}
      {d?.queue && (
        <div className="mt-3 border-t border-border pt-3">
          <div className="mb-1 flex items-center gap-2 text-xs font-medium uppercase tracking-wide text-muted">
            {t("stats.vt.scanQueue")}
            {d.queue.waiting_on_quota && (
              <Badge tone="amber">
                {t("stats.vt.waitingOnQuota")}{d.queue.next_slot_at ? t("stats.vt.nextSlot", { time: fmtSlot(d.queue.next_slot_at) }) : ""}
              </Badge>
            )}
          </div>
          <div className="grid gap-x-8 sm:grid-cols-2">
            <StatRow label={t("stats.vt.parked")} value={d.queue.depth}
              tone={d.queue.depth > 0 ? "text-amber-600" : "text-text"} />
            <div className="flex items-baseline justify-between gap-3 py-0.5 text-sm">
              <span className="text-muted">{t("stats.vt.parkedSize")}</span>
              <span className="font-semibold tabular-nums text-text">{fmtBytes(d.queue.parked_bytes)}</span>
            </div>
            <StatRow label={t("stats.vt.blockedHits")} value={d.queue.blocked}
              tone={d.queue.blocked > 0 ? "text-red-500" : "text-text"} />
          </div>
        </div>
      )}
    </Card>
  );
}

const buildRouteLabel = (t: TFunction): Record<string, string> => ({
  usenet: t("stats.route.usenet"),
  torrent: t("stats.route.torrent"),
  "anna's archive": t("stats.route.annas"),
  librivox: t("stats.route.librivox"),
});

function PipelineStatsCard() {
  const { t } = useTranslation();
  const ROUTE_LABEL = buildRouteLabel(t);
  const q = useQuery({ queryKey: ["pipeline-stats"], queryFn: api.getPipelineStats });
  const d = q.data;
  return (
    <Card className="mb-4 p-4">
      <h2 className="mb-3 flex items-center gap-1.5 font-semibold">
        {t("stats.pipeline.title")}
        <InfoHint text={t("stats.pipeline.infoHint")} />
      </h2>
      {q.isLoading ? (
        <Spinner label={t("stats.loading")} />
      ) : !d ? (
        <p className="text-sm text-muted">{t("stats.pipeline.noData")}</p>
      ) : (
        <div className="space-y-4">
          {/* Downloads by route — where in the pipeline the fetch succeeded vs failed. */}
          <div>
            <div className="mb-1 text-xs font-medium uppercase tracking-wide text-muted">
              {t("stats.pipeline.downloadsByRoute")}
            </div>
            {d.downloads.by_route.length === 0 && d.web_fetch.hooked === 0 ? (
              <p className="text-sm text-muted">{t("stats.pipeline.noDownloads")}</p>
            ) : (
              <div className="overflow-hidden rounded-lg border border-border">
                <table className="w-full text-sm">
                  <thead className="bg-surface-2 text-xs text-muted">
                    <tr>
                      <th className="px-3 py-1.5 text-left font-medium">{t("stats.pipeline.colRoute")}</th>
                      <th className="px-3 py-1.5 text-right font-medium">{t("stats.pipeline.colSucceeded")}</th>
                      <th className="px-3 py-1.5 text-right font-medium">{t("stats.pipeline.colFailed")}</th>
                      <th className="px-3 py-1.5 text-right font-medium">{t("stats.pipeline.colInFlight")}</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {d.downloads.by_route.map((r) => (
                      <tr key={r.route}>
                        <td className="px-3 py-1.5">{ROUTE_LABEL[r.route] ?? r.route}</td>
                        <td className="px-3 py-1.5 text-right tabular-nums text-green-600">{fmt(r.imported)}</td>
                        <td className="px-3 py-1.5 text-right tabular-nums text-red-500">{fmt(r.failed)}</td>
                        <td className="px-3 py-1.5 text-right tabular-nums text-muted">{fmt(r.active)}</td>
                      </tr>
                    ))}
                    {d.web_fetch.hooked > 0 && (
                      <tr>
                        <td className="px-3 py-1.5">{t("stats.pipeline.webCrawlRoute")}</td>
                        <td className="px-3 py-1.5 text-right tabular-nums text-green-600">{fmt(d.web_fetch.hooked)}</td>
                        <td className="px-3 py-1.5 text-right text-muted">—</td>
                        <td className="px-3 py-1.5 text-right text-muted">—</td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            )}
            <div className="mt-1.5 flex flex-wrap gap-2 text-xs">
              <Badge tone="green">{t("stats.pipeline.downloaded", { count: fmt(d.downloads.totals.imported) })}</Badge>
              <Badge tone="red">{t("stats.pipeline.failedBadge", { count: fmt(d.downloads.totals.failed) })}</Badge>
              {d.downloads.totals.active > 0 && <Badge>{t("stats.pipeline.inFlightBadge", { count: fmt(d.downloads.totals.active) })}</Badge>}
              <Badge tone="green">{t("stats.pipeline.viaWebCrawl", { count: fmt(d.web_fetch.hooked) })}</Badge>
            </div>
          </div>

          {/* Requested-title outcomes (the missing-content ledger). */}
          <div>
            <div className="mb-1 text-xs font-medium uppercase tracking-wide text-muted">
              {t("stats.pipeline.requestedTitles")}
            </div>
            <div className="grid gap-x-8 sm:grid-cols-2">
              <StatRow label={t("stats.pipeline.obtained")} value={d.requests.resolved} tone="text-green-600" />
              <StatRow label={t("stats.pipeline.unavailable")} value={d.requests.unavailable} tone="text-amber-600" />
              <StatRow label={t("stats.pipeline.searching")} value={d.requests.searching} />
              <StatRow label={t("stats.pipeline.open")} value={d.requests.open} />
            </div>
          </div>

          {/* Why unavailable titles couldn't be fetched. */}
          {d.failure_reasons.length > 0 && (
            <div>
              <div className="mb-1 text-xs font-medium uppercase tracking-wide text-muted">
                {t("stats.pipeline.whyFailed")}
              </div>
              <ul className="space-y-1.5">
                {d.failure_reasons.map((f) => (
                  <li key={f.reason} className="flex items-baseline gap-2 text-sm">
                    <span className="shrink-0 font-semibold tabular-nums">{fmt(f.count)}</span>
                    <span className="text-muted">{f.label}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* Wave B: per-source search-queue state (how the cascade is progressing per source). */}
          {d.sources && d.sources.by_source.length > 0 && (
            <div>
              <div className="mb-1 text-xs font-medium uppercase tracking-wide text-muted">
                {t("stats.pipeline.sourceQueue")}{d.sources.due_now > 0 ? t("stats.pipeline.dueNow", { count: fmt(d.sources.due_now) }) : ""}
              </div>
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-xs text-muted">
                    <th className="px-3 py-1 text-left font-medium">{t("stats.pipeline.colSource")}</th>
                    <th className="px-3 py-1 text-right font-medium">{t("stats.pipeline.colSearched")}</th>
                    <th className="px-3 py-1 text-right font-medium">{t("stats.pipeline.colQueued")}</th>
                    <th className="px-3 py-1 text-right font-medium">{t("stats.pipeline.colInFlight")}</th>
                  </tr>
                </thead>
                <tbody>
                  {d.sources.by_source.map((s) => (
                    <tr key={s.source} className="border-t border-border/50">
                      <td className="px-3 py-1.5">
                        {s.source === "pipeline" ? t("stats.pipeline.routeUsenet") : s.source === "libgen" ? t("stats.pipeline.routeAnnas") : t("stats.pipeline.routeTorrent")}
                      </td>
                      <td className="px-3 py-1.5 text-right tabular-nums">{fmt(s.searched)}</td>
                      <td className="px-3 py-1.5 text-right tabular-nums text-amber-600">{fmt(s.queued)}</td>
                      <td className="px-3 py-1.5 text-right tabular-nums">{fmt(s.in_flight)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {/* Wave E: follow subscriptions + how many titles the follow tick auto-added. */}
          {d.following && d.following.authors + d.following.series > 0 && (
            <div>
              <div className="mb-1 text-xs font-medium uppercase tracking-wide text-muted">{t("stats.pipeline.following")}</div>
              <div className="grid gap-x-8 sm:grid-cols-3">
                <StatRow label={t("stats.pipeline.authors")} value={d.following.authors} />
                <StatRow label={t("stats.pipeline.series")} value={d.following.series} />
                <StatRow label={t("stats.pipeline.autoAdded")} value={d.following.auto_added} tone="text-green-600" />
              </div>
            </div>
          )}
        </div>
      )}
    </Card>
  );
}

export default function StatisticsPanel() {
  return (
    <>
      <PipelineStatsCard />
      <VirusTotalStatsCard />
      <RequestStatsCard />
    </>
  );
}
