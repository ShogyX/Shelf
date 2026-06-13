import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, RequestStats } from "../api/client";
import { Card, InfoHint, Spinner } from "./ui";

// Stable colors per category + per outcome.
const CAT_COLOR: Record<string, string> = {
  crawl: "#6366f1", metadata: "#10b981", integration: "#f59e0b", libgen: "#8b5cf6",
  image: "#06b6d4", export: "#ec4899", solver: "#ef4444", other: "#94a3b8",
};
const OUTCOME_COLOR: Record<string, string> = {
  success: "#10b981", blocked: "#f59e0b", timeout: "#f97316", error: "#ef4444",
};
const color = (c: string) => CAT_COLOR[c] ?? "#94a3b8";
const ocolor = (o: string) => OUTCOME_COLOR[o] ?? "#94a3b8";
const fmt = (n: number) => n.toLocaleString();
const clock = (b: string) => b.slice(11, 16);       // "HH:MM" from "YYYY-MM-DDTHH:00"
const dayLabel = (b: string) => b.slice(5, 10);     // "MM-DD"

/** Line chart: X = clock time (hourly buckets), Y = requests/hour. One line per outcome plus a
 *  faint Total line, with labelled axes + gridlines. */
function TrendLines({ stats }: { stats: RequestStats }) {
  const s = stats.series;
  if (s.length === 0) return <p className="text-xs text-muted">No requests recorded yet.</p>;
  const W = 760, H = 200, padL = 38, padR = 8, padT = 8, padB = 22;
  const iw = W - padL - padR, ih = H - padT - padB;
  const max = Math.max(1, ...s.map((p) => p.total));
  const n = s.length;
  const xAt = (i: number) => padL + (n === 1 ? iw / 2 : (i / (n - 1)) * iw);
  const yAt = (v: number) => padT + ih - (v / max) * ih;
  const lineFor = (vals: number[]) => vals.map((v, i) => `${i ? "L" : "M"}${xAt(i).toFixed(1)},${yAt(v).toFixed(1)}`).join(" ");

  const outs = stats.outcomes.length ? stats.outcomes : ["success"];
  const yTicks = 4;
  const xEvery = Math.max(1, Math.ceil(n / 10));
  // Mark a date when the day rolls over (so a multi-day window is readable).
  let lastDay = "";

  return (
    <div className="overflow-x-auto">
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" style={{ minWidth: 520 }}
        role="img" aria-label="Requests per hour over time, by outcome">
        {/* Y gridlines + labels */}
        {Array.from({ length: yTicks + 1 }, (_, k) => {
          const v = Math.round((max * k) / yTicks);
          const y = yAt(v);
          return (
            <g key={k}>
              <line x1={padL} y1={y} x2={W - padR} y2={y} stroke="currentColor" className="text-border" strokeWidth={0.5} />
              <text x={padL - 4} y={y + 3} textAnchor="end" className="fill-muted" style={{ fontSize: 8 }}>{fmt(v)}</text>
            </g>
          );
        })}
        {/* X axis ticks (clock time) */}
        {s.map((p, i) => {
          if (i % xEvery !== 0 && i !== n - 1) return null;
          const x = xAt(i);
          const d = dayLabel(p.bucket);
          const showDay = d !== lastDay; lastDay = d;
          return (
            <g key={p.bucket}>
              <text x={x} y={H - 11} textAnchor="middle" className="fill-muted" style={{ fontSize: 8 }}>{clock(p.bucket)}</text>
              {showDay && <text x={x} y={H - 2} textAnchor="middle" className="fill-muted" style={{ fontSize: 7, opacity: 0.7 }}>{d}</text>}
            </g>
          );
        })}
        {/* Total line (faint) */}
        <path d={lineFor(s.map((p) => p.total))} fill="none" stroke="currentColor" className="text-muted" strokeWidth={1} strokeDasharray="3 2" opacity={0.5} />
        {/* One line per outcome */}
        {outs.map((o) => (
          <path key={o} d={lineFor(s.map((p) => p.by_outcome[o] || 0))} fill="none"
            stroke={ocolor(o)} strokeWidth={1.6} strokeLinejoin="round" />
        ))}
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

          {/* Outcome summary tiles */}
          <div className="mb-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
            {s.by_outcome.map((o) => (
              <div key={o.outcome} className="rounded-lg border border-border bg-bg px-3 py-2">
                <div className="flex items-center gap-1.5 text-lg font-semibold tabular-nums">
                  <span className="inline-block h-2.5 w-2.5 rounded-full" style={{ background: ocolor(o.outcome) }} />
                  {fmt(o.count)}
                </div>
                <div className="text-[11px] uppercase tracking-wide text-muted">{o.outcome}</div>
              </div>
            ))}
          </div>

          <div className="mb-1 flex items-center gap-1.5 text-sm font-medium">
            Requests over time
            <InfoHint text="Requests per hour (Y) over clock time (X). One line per outcome — success,
              blocked (anti-bot / rate-limit), timeout, error — plus a dashed Total." />
          </div>
          <TrendLines stats={s} />
          {/* outcome legend */}
          <div className="mb-4 mt-1 flex flex-wrap gap-x-3 gap-y-1">
            {s.outcomes.map((o) => (
              <span key={o} className="flex items-center gap-1 text-[11px] text-muted">
                <span className="inline-block h-2.5 w-2.5 rounded-sm" style={{ background: ocolor(o) }} />{o}
              </span>
            ))}
            <span className="flex items-center gap-1 text-[11px] text-muted">
              <span className="inline-block h-0.5 w-3 align-middle" style={{ background: "currentColor", opacity: 0.5 }} />total
            </span>
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
