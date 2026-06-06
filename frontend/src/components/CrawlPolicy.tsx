import { CrawlPolicy } from "../api/client";

type V = Partial<CrawlPolicy>;

// Editable per-title crawl policy: request speed and allowed hours.
// Empty input = use the source default (null). There is no daily request cap.
export function CrawlPolicyFields({
  value,
  onChange,
}: {
  value: V;
  onChange: (v: V) => void;
}) {
  const num = (s: string): number | null => (s.trim() === "" ? null : Number(s));
  const set = (k: keyof CrawlPolicy, s: string) => onChange({ ...value, [k]: num(s) });
  const show = (n: number | null | undefined) => (n == null ? "" : String(n));
  const input =
    "mt-1 w-full rounded-lg border border-border bg-bg px-2 py-1.5 text-sm";

  return (
    <div className="grid gap-3 sm:grid-cols-2">
      <label className="text-sm">
        <span className="text-muted">Seconds between requests</span>
        <input
          type="number"
          min={0}
          step="0.5"
          value={show(value.crawl_interval_s)}
          placeholder="source default"
          onChange={(e) => set("crawl_interval_s", e.target.value)}
          className={input}
        />
      </label>
      <label className="text-sm">
        <span className="text-muted">Run from (hour, UTC 0–23)</span>
        <input
          type="number"
          min={0}
          max={23}
          value={show(value.crawl_window_start)}
          placeholder="anytime"
          onChange={(e) => set("crawl_window_start", e.target.value)}
          className={input}
        />
      </label>
      <label className="text-sm">
        <span className="text-muted">Run until (hour, UTC 0–23)</span>
        <input
          type="number"
          min={0}
          max={23}
          value={show(value.crawl_window_end)}
          placeholder="anytime"
          onChange={(e) => set("crawl_window_end", e.target.value)}
          className={input}
        />
      </label>
    </div>
  );
}

export function policyFrom(w: {
  crawl_interval_s: number | null;
  crawl_window_start: number | null;
  crawl_window_end: number | null;
}): V {
  return {
    crawl_interval_s: w.crawl_interval_s,
    crawl_window_start: w.crawl_window_start,
    crawl_window_end: w.crawl_window_end,
  };
}
