import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { qk } from "../api/queryKeys";
import { Badge, Button } from "./ui";

/** Shows the metadata-provider links + related titles (prequel/sequel/spin-off) for a work,
 *  with a one-click "queue all related" so they auto-hook once found in the index.
 *  Renders nothing when the work has no metadata links. */
export default function RelatedTitles({ workId }: { workId: number }) {
  const qc = useQueryClient();
  const links = useQuery({
    queryKey: qk.workMetadata(workId),
    queryFn: () => api.workMetadataLinks(workId),
  });
  const linkList = links.data ?? [];
  const related = useQuery({
    queryKey: qk.workRelated(workId),
    queryFn: () => api.workRelated(workId),
    enabled: linkList.length > 0, // no links → no related titles to fetch
  });

  const refreshLinks = () => {
    qc.invalidateQueries({ queryKey: qk.workMetadata(workId) });
    qc.invalidateQueries({ queryKey: qk.workRelated(workId) });
  };
  const queue = useMutation({
    mutationFn: () => api.queueRelated(workId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.workRelated(workId) });
      qc.invalidateQueries({ queryKey: qk.queuedHooks() });
    },
  });
  const confirm = useMutation({
    mutationFn: (id: number) => api.confirmMetadataLink(id),
    onSuccess: refreshLinks,
  });
  const unlink = useMutation({
    mutationFn: (id: number) => api.deleteMetadataLink(id),
    onSuccess: refreshLinks,
  });

  if (linkList.length === 0) return null; // not linked to any provider → nothing to show

  const rel = related.data?.related ?? [];
  const queueable = rel.filter((r) => !r.in_library && !r.queued_status);

  return (
    <div className="border-b border-border px-4 py-3">
      <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">
        Metadata source
      </div>
      <div className="space-y-1">
        {linkList.map((l) => (
          <div key={l.id} className="space-y-0.5">
            <div className="flex items-center gap-2 text-sm">
              {l.url ? (
                <a href={l.url} target="_blank" rel="noreferrer" className="min-w-0 truncate text-accent hover:underline">
                  {l.matched_title ?? l.provider}
                </a>
              ) : (
                <span className="min-w-0 truncate">{l.matched_title ?? l.provider}</span>
              )}
              <span className="ml-auto flex shrink-0 items-center gap-1">
                {l.status !== "confirmed" && (
                  <Button
                    size="sm"
                    variant="ghost"
                    title="Confirm this is the right match (locks it from re-scoring)"
                    disabled={confirm.isPending}
                    onClick={() => confirm.mutate(l.id)}
                  >
                    ✓
                  </Button>
                )}
                <Button
                  size="sm"
                  variant="ghost"
                  title="Wrong match — unlink this provider"
                  disabled={unlink.isPending}
                  onClick={() => unlink.mutate(l.id)}
                >
                  ✕
                </Button>
              </span>
            </div>
            <div className="flex flex-wrap items-center gap-1.5">
              <Badge tone="amber">{l.provider}</Badge>
              {l.status === "confirmed" && <Badge tone="green">confirmed</Badge>}
              {l.expected_chapters != null ? (
                <span className="text-xs text-muted">
                  {l.expected_chapters} chapters released
                </span>
              ) : l.total_units != null ? (
                <span className="text-xs text-muted">
                  {l.total_units} {l.unit_kind ?? "units"}
                </span>
              ) : null}
              {l.major_discrepancy && l.chapter_discrepancy != null && (
                <span
                  title={
                    l.chapter_discrepancy > 0
                      ? `Provider lists ${l.chapter_discrepancy} more chapters than we've gathered`
                      : `We have ${-l.chapter_discrepancy} more chapters than the provider lists`
                  }
                >
                  <Badge tone="red">
                    ⚠ {l.chapter_discrepancy > 0 ? `missing ${l.chapter_discrepancy}` : `+${-l.chapter_discrepancy} ahead`}
                  </Badge>
                </span>
              )}
            </div>
          </div>
        ))}
      </div>

      {rel.length > 0 && (
        <div className="mt-3">
          <div className="mb-1 flex items-center justify-between gap-2">
            <span className="text-xs font-semibold uppercase tracking-wide text-muted">
              Related titles
            </span>
            {queueable.length > 0 && (
              <Button size="sm" variant="ghost" disabled={queue.isPending} onClick={() => queue.mutate()}>
                {queue.isPending ? "Queuing…" : `Queue all (${queueable.length})`}
              </Button>
            )}
          </div>
          <div className="space-y-1">
            {rel.map((r, i) => (
              <div key={`${r.title}-${i}`} className="flex items-center gap-2 text-sm">
                <span className="min-w-0 flex-1 truncate">{r.title}</span>
                <Badge tone="violet">{r.relation}</Badge>
                {r.in_library ? (
                  <Badge tone="green">in library</Badge>
                ) : r.queued_status ? (
                  <Badge tone="amber">{r.queued_status}</Badge>
                ) : null}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
