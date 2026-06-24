// Hand-rolled SVG charts for Settings → Insights (no chart dependency). All colours come from the
// caller (the semantic status palette / theme tokens). Tiny, dependency-free, theme-retinting.
import { useId } from "react";

/** A tiny inline sparkline for KPI tiles. Renders full-width so it hugs the tile under the value. */
export function Sparkline({ values, color, width = 120, height = 30 }: {
  values: number[]; color: string; width?: number; height?: number;
}) {
  if (values.length < 2) return <svg width="100%" height={height} style={{ display: "block" }} />;
  const max = Math.max(...values), min = Math.min(...values);
  const span = max - min || 1;
  const X = (i: number) => (i * width) / (values.length - 1);
  const Y = (v: number) => height - 3 - ((v - min) / span) * (height - 8);
  const d = values.map((v, i) => `${i ? "L" : "M"}${X(i).toFixed(1)} ${Y(v).toFixed(1)}`).join(" ");
  return (
    <svg width="100%" height={height} viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" style={{ display: "block" }} aria-hidden>
      <path d={d} fill="none" stroke={color} strokeWidth={2.2} strokeLinecap="round" strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

/** An area chart (line + gradient fill) that fills its container width. */
export function AreaChart({ values, color, height = 130 }: {
  values: number[]; color: string; height?: number;
}) {
  const gid = useId();
  const W = 520;
  if (values.length < 2) return <div style={{ height }} />;
  const max = Math.max(...values, 1), min = 0, n = values.length;
  const X = (i: number) => (i * W) / (n - 1);
  const Y = (v: number) => height - 6 - ((v - min) / (max - min || 1)) * (height - 16);
  const line = values.map((v, i) => `${i ? "L" : "M"}${X(i).toFixed(1)} ${Y(v).toFixed(1)}`).join(" ");
  const fill = `${line} L${W} ${height} L0 ${height} Z`;
  return (
    <svg width="100%" height={height} viewBox={`0 0 ${W} ${height}`} preserveAspectRatio="none" style={{ display: "block" }} aria-hidden>
      <defs>
        <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity={0.32} />
          <stop offset="100%" stopColor={color} stopOpacity={0} />
        </linearGradient>
      </defs>
      <path d={fill} fill={`url(#${gid})`} />
      <path d={line} fill="none" stroke={color} strokeWidth={2.5} strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}

/** A donut ring with a centre label (e.g. acquisition health: imported / failed / in-flight). */
export function Donut({ segments, centerLabel, centerSub, size = 140 }: {
  segments: { value: number; color: string }[];
  centerLabel: React.ReactNode;
  centerSub?: React.ReactNode;
  size?: number;
}) {
  const r = (size - 36) / 2;
  const cx = size / 2, cy = size / 2;
  const C = 2 * Math.PI * r;
  const total = segments.reduce((s, x) => s + x.value, 0) || 1;
  let off = 0;
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} aria-hidden>
      <circle cx={cx} cy={cy} r={r} fill="none" stroke="var(--surface-2)" strokeWidth={15} />
      {segments.map((s, i) => {
        const len = (C * s.value) / total;
        const el = (
          <circle key={i} cx={cx} cy={cy} r={r} fill="none" stroke={s.color} strokeWidth={15}
            strokeDasharray={`${len} ${C - len}`} strokeDashoffset={-off} strokeLinecap="butt"
            transform={`rotate(-90 ${cx} ${cy})`} />
        );
        off += len;
        return el;
      })}
      <text x={cx} y={cy - 2} textAnchor="middle" fontSize={28} fontWeight={700} fill="var(--text)">{centerLabel}</text>
      {centerSub && <text x={cx} y={cy + 20} textAnchor="middle" fontSize={12} fill="var(--muted)">{centerSub}</text>}
    </svg>
  );
}

/** A list of labelled horizontal bars (where-downloads-come-from, why-fetches-failed). */
export function HBars({ items }: {
  items: { label: React.ReactNode; value: React.ReactNode; pct: number; color: string }[];
}) {
  return (
    <div>
      {items.map((b, i) => (
        <div key={i} className="mb-3.5 last:mb-0">
          <div className="mb-1.5 flex justify-between text-[13px]">
            <span className="font-semibold text-text">{b.label}</span>
            <span className="text-muted [font-variant-numeric:tabular-nums]">{b.value}</span>
          </div>
          <div className="h-2.5 overflow-hidden rounded-full bg-surface-2">
            <div className="h-full rounded-full" style={{ width: `${Math.min(100, b.pct)}%`, background: b.color }} />
          </div>
        </div>
      ))}
    </div>
  );
}
