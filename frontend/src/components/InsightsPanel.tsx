// Settings → Insights: the acquisition/library dashboard rebuilt as charts (was the raw-table
// "Statistics" panel). All data is live from the stats endpoints; charts are hand-rolled SVG.
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { qk } from "../api/queryKeys";
import { STATUS_HEX } from "./ui";
import { AreaChart, Donut, HBars, Sparkline } from "./charts";

function fmtAcquire(s: number | null): string {
  if (s == null) return "—";
  if (s < 90) return `${Math.round(s)}s`;
  if (s < 5400) return `${(s / 60).toFixed(1)}m`;
  return `${(s / 3600).toFixed(1)}h`;
}
const pct = (v: number | null) => (v == null ? "—" : `${Math.round(v * 100)}%`);

const ROUTE_COLOR: Record<string, string> = {
  usenet: STATUS_HEX.success, torrent: STATUS_HEX.accent,
  "anna's archive": STATUS_HEX.info, librivox: STATUS_HEX.warning,
};

function Kpi({ value, label, color, spark }: { value: React.ReactNode; label: string; color: string; spark: number[] }) {
  return (
    <div className="rounded-2xl border border-[var(--hair,var(--border))] bg-surface p-4">
      <div className="text-[28px] font-bold tracking-tight [font-variant-numeric:tabular-nums]" style={{ color }}>{value}</div>
      <div className="my-1.5 text-xs font-semibold text-muted">{label}</div>
      <Sparkline values={spark} color={color} />
    </div>
  );
}

function Panel({ title, hint, children, className = "" }: { title: string; hint?: React.ReactNode; children: React.ReactNode; className?: string }) {
  return (
    <div className={`rounded-2xl border border-[var(--hair,var(--border))] bg-surface p-[22px] ${className}`}>
      <div className="mb-4 flex items-baseline justify-between">
        <h3 className="font-display text-[18px] font-semibold text-text">{title}</h3>
        {hint && <span className="text-[12.5px] text-muted">{hint}</span>}
      </div>
      {children}
    </div>
  );
}

export default function InsightsPanel() {
  const overview = useQuery({ queryKey: qk.statsOverview(), queryFn: api.statsOverview });
  const pipeline = useQuery({ queryKey: qk.pipelineStats(), queryFn: api.getPipelineStats });
  const acq = useQuery({ queryKey: qk.statsAcquisitions(14), queryFn: () => api.statsAcquisitions(14) });
  const growth = useQuery({ queryKey: qk.statsLibraryGrowth(90), queryFn: () => api.statsLibraryGrowth(90) });

  const o = overview.data;
  const totals = pipeline.data?.downloads.totals;
  const imported = totals?.imported ?? 0, failed = totals?.failed ?? 0, active = totals?.active ?? 0;
  const denom = imported + failed + active || 1;

  const sourceBars = (pipeline.data?.downloads.by_route ?? [])
    .slice()
    .sort((a, b) => b.imported - a.imported)
    .map((r) => ({
      label: r.route.replace(/\b\w/g, (c) => c.toUpperCase()),
      value: `${r.imported} · ${r.hit_rate == null ? "—" : `${Math.round(r.hit_rate * 100)}% hit`}`,
      pct: imported ? (r.imported / imported) * 100 : 0,
      color: ROUTE_COLOR[r.route] ?? STATUS_HEX.accent,
    }));
  const failReasons = pipeline.data?.failure_reasons ?? [];
  const failMax = Math.max(1, ...failReasons.map((f) => f.count));

  return (
    <div className="space-y-[18px]">
      {/* KPI tiles */}
      <div className="grid grid-cols-2 gap-3.5 lg:grid-cols-4">
        <Kpi value={o?.downloaded_30d ?? "—"} label="Downloaded · 30d" color={STATUS_HEX.success} spark={o?.spark.downloaded ?? []} />
        <Kpi value={pct(o?.success_rate ?? null)} label="Success rate" color={STATUS_HEX.accent} spark={(o?.spark.success ?? []).map((v) => v * 100)} />
        <Kpi value={fmtAcquire(o?.avg_acquire_s ?? null)} label="Avg. acquire time" color={STATUS_HEX.info} spark={o?.spark.acquire_s ?? []} />
        <Kpi value={(o?.titles_in_library ?? 0).toLocaleString()} label="Titles in library" color={STATUS_HEX.warning} spark={o?.spark.titles ?? []} />
      </div>

      <div className="grid gap-3.5 lg:grid-cols-[300px_1fr]">
        <Panel title="Acquisition health">
          <div className="flex justify-center py-1">
            <Donut
              segments={[
                { value: imported, color: STATUS_HEX.success },
                { value: failed, color: STATUS_HEX.danger },
                { value: active, color: STATUS_HEX.violet },
              ]}
              centerLabel={`${Math.round((imported / denom) * 100)}%`}
              centerSub="imported"
            />
          </div>
          {[["Imported", imported, STATUS_HEX.success], ["Failed", failed, STATUS_HEX.danger], ["In flight", active, STATUS_HEX.violet]].map(
            ([label, val, c]) => (
              <div key={label as string} className="flex items-center gap-2.5 py-1 text-[13px]">
                <span className="h-2.5 w-2.5 rounded" style={{ background: c as string }} />
                <span className="flex-1 text-muted">{label}</span>
                <span className="font-bold [font-variant-numeric:tabular-nums]">{(val as number).toLocaleString()}</span>
              </div>
            ),
          )}
        </Panel>

        <Panel title="Acquisitions over time" hint="Last 14 days">
          <div className="flex min-h-[130px] items-end">
            <AreaChart values={(acq.data?.days ?? []).map((d) => d.imported)} color="var(--accent)" />
          </div>
        </Panel>
      </div>

      <div className="grid gap-3.5 lg:grid-cols-2">
        <Panel title="Where downloads come from">
          {sourceBars.length ? <HBars items={sourceBars} /> : <p className="text-sm text-muted">No downloads yet.</p>}
        </Panel>
        <Panel title="Why fetches failed">
          {failReasons.length ? (
            <HBars items={failReasons.map((f) => ({
              // Short, consistent labels from the reason code (the verbose sentence is overkill in a bar).
              label: (f.reason || "error").replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()),
              value: f.count, pct: (f.count / failMax) * 100, color: STATUS_HEX.danger,
            }))} />
          ) : <p className="text-sm text-muted">Nothing has failed — clean run.</p>}
        </Panel>
      </div>

      <Panel title="Library growth" hint={growth.data ? `${growth.data.total.toLocaleString()} titles` : undefined}>
        <div className="flex min-h-[130px] items-end">
          <AreaChart values={(growth.data?.days ?? []).map((d) => d.total)} color={STATUS_HEX.info} />
        </div>
      </Panel>
    </div>
  );
}
