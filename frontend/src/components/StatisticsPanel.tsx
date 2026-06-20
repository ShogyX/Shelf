// Settings → Statistics: outbound-request telemetry, VirusTotal API usage, and acquisition-pipeline
// outcomes (where fetches succeeded + why they failed), all on one page.
import { useQuery } from "@tanstack/react-query";
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
  const q = useQuery({ queryKey: ["vt-usage"], queryFn: api.getVirusTotalUsage });
  const d = q.data;
  return (
    <Card className="mb-4 p-4">
      <h2 className="mb-3 flex items-center gap-1.5 font-semibold">
        VirusTotal API usage
        <InfoHint text="Torrent-grabbed files are SHA-256 hashed and checked against VirusTotal before import. Counts over the last 30 days." />
      </h2>
      {q.isLoading ? (
        <Spinner label="Loading…" />
      ) : !d ? (
        <p className="text-sm text-muted">Connect VirusTotal under Integrations to see usage.</p>
      ) : d.total === 0 ? (
        <p className="text-sm text-muted">No lookups yet — run a Test on the VirusTotal integration to verify it works.</p>
      ) : (
        <div className="grid gap-x-8 sm:grid-cols-2">
          <StatRow label="Lookups (30d)" value={d.total} />
          <StatRow label="In the last 24h" value={d.last_24h} />
          <StatRow label="Rate-limited (quota)" value={d.by_outcome.blocked || 0}
            tone={(d.by_outcome.blocked || 0) > 0 ? "text-amber-600" : "text-text"} />
          <StatRow label="Errors" value={d.by_outcome.error || 0}
            tone={(d.by_outcome.error || 0) > 0 ? "text-red-500" : "text-text"} />
        </div>
      )}
      {d?.queue && (
        <div className="mt-3 border-t border-border pt-3">
          <div className="mb-1 flex items-center gap-2 text-xs font-medium uppercase tracking-wide text-muted">
            Scan queue
            {d.queue.waiting_on_quota && (
              <Badge tone="amber">
                waiting on VT quota{d.queue.next_slot_at ? ` · next slot ${fmtSlot(d.queue.next_slot_at)}` : ""}
              </Badge>
            )}
          </div>
          <div className="grid gap-x-8 sm:grid-cols-2">
            <StatRow label="Parked (awaiting scan)" value={d.queue.depth}
              tone={d.queue.depth > 0 ? "text-amber-600" : "text-text"} />
            <div className="flex items-baseline justify-between gap-3 py-0.5 text-sm">
              <span className="text-muted">Parked size</span>
              <span className="font-semibold tabular-nums text-text">{fmtBytes(d.queue.parked_bytes)}</span>
            </div>
            <StatRow label="Blocked (rate-limit hits)" value={d.queue.blocked}
              tone={d.queue.blocked > 0 ? "text-red-500" : "text-text"} />
          </div>
        </div>
      )}
    </Card>
  );
}

const ROUTE_LABEL: Record<string, string> = {
  usenet: "Usenet (Prowlarr → SABnzbd)",
  torrent: "Torrent (Prowlarr → qBittorrent)",
  "anna's archive": "Anna's Archive",
  librivox: "LibriVox (public-domain audiobooks)",
};

function PipelineStatsCard() {
  const q = useQuery({ queryKey: ["pipeline-stats"], queryFn: api.getPipelineStats });
  const d = q.data;
  return (
    <Card className="mb-4 p-4">
      <h2 className="mb-3 flex items-center gap-1.5 font-semibold">
        Pipeline fetches
        <InfoHint text="How acquisition went: where downloads succeeded (usenet / torrent / Anna's Archive / LibriVox), web-crawl hooks, and — for titles that couldn't be obtained — why." />
      </h2>
      {q.isLoading ? (
        <Spinner label="Loading…" />
      ) : !d ? (
        <p className="text-sm text-muted">No pipeline data yet.</p>
      ) : (
        <div className="space-y-4">
          {/* Downloads by route — where in the pipeline the fetch succeeded vs failed. */}
          <div>
            <div className="mb-1 text-xs font-medium uppercase tracking-wide text-muted">
              Downloads by route
            </div>
            {d.downloads.by_route.length === 0 && d.web_fetch.hooked === 0 ? (
              <p className="text-sm text-muted">No downloads recorded yet.</p>
            ) : (
              <div className="overflow-hidden rounded-lg border border-border">
                <table className="w-full text-sm">
                  <thead className="bg-surface-2 text-xs text-muted">
                    <tr>
                      <th className="px-3 py-1.5 text-left font-medium">Route</th>
                      <th className="px-3 py-1.5 text-right font-medium">Succeeded</th>
                      <th className="px-3 py-1.5 text-right font-medium">Failed</th>
                      <th className="px-3 py-1.5 text-right font-medium">In flight</th>
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
                        <td className="px-3 py-1.5">Web crawl (hooked from a source)</td>
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
              <Badge tone="green">{fmt(d.downloads.totals.imported)} downloaded</Badge>
              <Badge tone="red">{fmt(d.downloads.totals.failed)} failed</Badge>
              {d.downloads.totals.active > 0 && <Badge>{fmt(d.downloads.totals.active)} in flight</Badge>}
              <Badge tone="green">{fmt(d.web_fetch.hooked)} via web crawl</Badge>
            </div>
          </div>

          {/* Requested-title outcomes (the missing-content ledger). */}
          <div>
            <div className="mb-1 text-xs font-medium uppercase tracking-wide text-muted">
              Requested titles
            </div>
            <div className="grid gap-x-8 sm:grid-cols-2">
              <StatRow label="Obtained" value={d.requests.resolved} tone="text-green-600" />
              <StatRow label="Unavailable" value={d.requests.unavailable} tone="text-amber-600" />
              <StatRow label="Searching" value={d.requests.searching} />
              <StatRow label="Open" value={d.requests.open} />
            </div>
          </div>

          {/* Why unavailable titles couldn't be fetched. */}
          {d.failure_reasons.length > 0 && (
            <div>
              <div className="mb-1 text-xs font-medium uppercase tracking-wide text-muted">
                Why fetches failed
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
                Source search queue{d.sources.due_now > 0 ? ` · ${fmt(d.sources.due_now)} due now` : ""}
              </div>
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-xs text-muted">
                    <th className="px-3 py-1 text-left font-medium">Source</th>
                    <th className="px-3 py-1 text-right font-medium">Searched</th>
                    <th className="px-3 py-1 text-right font-medium">Queued</th>
                    <th className="px-3 py-1 text-right font-medium">In flight</th>
                  </tr>
                </thead>
                <tbody>
                  {d.sources.by_source.map((s) => (
                    <tr key={s.source} className="border-t border-border/50">
                      <td className="px-3 py-1.5">
                        {s.source === "pipeline" ? "Usenet" : s.source === "libgen" ? "Anna's Archive" : "Torrent"}
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
              <div className="mb-1 text-xs font-medium uppercase tracking-wide text-muted">Following</div>
              <div className="grid gap-x-8 sm:grid-cols-3">
                <StatRow label="Authors" value={d.following.authors} />
                <StatRow label="Series" value={d.following.series} />
                <StatRow label="Auto-added" value={d.following.auto_added} tone="text-green-600" />
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
