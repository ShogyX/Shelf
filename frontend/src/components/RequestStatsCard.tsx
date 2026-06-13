import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, RequestStats } from "../api/client";
import { Card, InfoHint, Spinner } from "./ui";

// Stable colors per category so the chart legend + bars stay consistent.
const CAT_COLOR: Record<string, string> = {
  crawl: "#6366f1", metadata: "#10b981", integration: "#f59e0b", libgen: "#8b5cf6",
  image: "#06b6d4", export: "#ec4899", solver: "#ef4444", other: "#94a3b8",
};
const color = (c: string) => CAT_COLOR[c] ?? "#94a3b8";
const fmt = (n: number) => n.toLocaleString();

function StackedTrend({ stats }: { stats: RequestStats }) {
  const series = stats.series;
  if (series.length === 0) return <p className="text-xs text-muted">No requests recorded yet.</p>;
  const cats = stats.categories.length ? stats.categories : ["other"];
  const max = Math.max(1, ...series.map((s) => s.total));
  const W = 720, H = 140, pad = 4;
  const bw = Math.max(1, (W - pad * 2) / series.length - 1);
  const labelEvery = Math.ceil(series.length / 8);
  return (
    <div className="overflow-x-auto">
      <svg viewBox={`0 0 ${W} ${H + 18}`} className="w-full" style={{ minWidth: 480 }}
        role="img" aria-label="Requests per hour, stacked by category">
        {series.map((s, i) => {
          const x = pad + i * ((W - pad * 2) / series.length);
          let y = H;
          return (
            <g key={s.bucket}>
              {cats.map((c) => {
                const v = s.by_category[c] || 0;
                if (!v) return null;
                const h = (v / max) * (H - 4);
                y -= h;
                return <rect key={c} x={x} y={y} width={bw} height={h} fill={color(c)} rx={0.5}>
                  <title>{`${s.bucket}  ${c}: ${fmt(v)}`}</title>
                </rect>;
              })}
              {i % labelEvery === 0 && (
                <text x={x + bw / 2} y={H + 12} textAnchor="middle"
                  className="fill-muted" style={{ fontSize: 8 }}>
                  {s.bucket.slice(11, 16)}
                </text>
              )}
            </g>
          );
        })}
      </svg>
    </div>
  );
}

function Rate({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-lg border border-border bg-bg px-3 py-2">
      <div className="text-lg font-semibold tabular-nums">{value}</div>
      <div className="text-[11px] uppercase tracking-wide text-muted">{label}</div>
    </div>
  );
}

export default function RequestStatsCard() {
  const [hours, setHours] = useState(48);
  const q = useQuery({
    queryKey: ["request-stats", hours],
    queryFn: () => api.getRequestStats(hours),
    refetchInterval: 15000,
  });
  const s = q.data;

  return (
    <Card className="mb-4 p-4">
      <div className="mb-1 flex items-center justify-between gap-2">
        <h2 className="flex items-center gap-1.5 font-semibold">
          External requests
          <InfoHint text="Every outbound HTTP request the app makes — crawling, metadata APIs, integrations (Prowlarr/SABnzbd), ingestion, cover/image fetches and Cloudflare solvers — counted by destination host and category. Updated every ~30s." />
        </h2>
        <select className="rounded-lg border border-border bg-bg px-2 py-1 text-xs"
          value={hours} onChange={(e) => setHours(Number(e.target.value))}>
          <option value={6}>Last 6h</option>
          <option value={24}>Last 24h</option>
          <option value={48}>Last 48h</option>
          <option value={168}>Last 7d</option>
        </select>
      </div>

      {q.isLoading ? <Spinner label="Loading request stats…" /> : !s ? (
        <p className="text-sm text-muted">No request data yet.</p>
      ) : (
        <>
          <div className="mb-3 grid grid-cols-2 gap-2 sm:grid-cols-5">
            <Rate label="req / sec" value={s.rates.per_second} />
            <Rate label="req / min" value={s.rates.per_minute} />
            <Rate label="req / hour" value={fmt(Math.round(s.rates.per_hour))} />
            <Rate label="req / day" value={fmt(s.rates.per_day)} />
            <Rate label={`total (${s.window_hours}h)`} value={fmt(s.total)} />
          </div>

          <div className="mb-1 flex items-center gap-1.5 text-sm font-medium">
            Requests over time
            <InfoHint text="Hourly request counts, stacked by category. Hover a bar segment for the exact count." />
          </div>
          <StackedTrend stats={s} />
          {/* legend */}
          <div className="mb-4 mt-1 flex flex-wrap gap-x-3 gap-y-1">
            {s.by_category.map((c) => (
              <span key={c.category} className="flex items-center gap-1 text-[11px] text-muted">
                <span className="inline-block h-2.5 w-2.5 rounded-sm" style={{ background: color(c.category) }} />
                {c.category} <span className="tabular-nums">{fmt(c.count)}</span>
              </span>
            ))}
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <div className="mb-1 text-sm font-medium">By category</div>
              <table className="w-full text-xs">
                <tbody>
                  {s.by_category.length === 0 && <tr><td className="text-muted">—</td></tr>}
                  {s.by_category.slice().sort((a, b) => b.count - a.count).map((c) => (
                    <tr key={c.category} className="border-t border-border/60">
                      <td className="py-1">
                        <span className="mr-1.5 inline-block h-2 w-2 rounded-sm align-middle"
                          style={{ background: color(c.category) }} />{c.category}
                      </td>
                      <td className="py-1 text-right tabular-nums">{fmt(c.count)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div>
              <div className="mb-1 flex items-center gap-1.5 text-sm font-medium">
                Top destinations
                <InfoHint text="The external hosts the app talked to most in this window." />
              </div>
              <table className="w-full text-xs">
                <tbody>
                  {s.by_host.length === 0 && <tr><td className="text-muted">—</td></tr>}
                  {s.by_host.slice(0, 12).map((h) => (
                    <tr key={h.host} className="border-t border-border/60">
                      <td className="truncate py-1" title={h.host}>{h.host}</td>
                      <td className="py-1 text-right tabular-nums">{fmt(h.count)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}
    </Card>
  );
}
