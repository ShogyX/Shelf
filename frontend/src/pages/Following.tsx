import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, Subscription } from "../api/client";
import { qk } from "../api/queryKeys";
import { Badge, Button, Card, EmptyState, Spinner } from "../components/ui";
import { useApp } from "../store";
import { useConfirm } from "../components/confirm";

/** Absolute "since" date matching the app's other date phrasing (e.g. "Jun 18"). */
function shortDate(iso: string | null): string | null {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  if (isNaN(t)) return null;
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

function Row({ s }: { s: Subscription }) {
  const qc = useQueryClient();
  const toast = useApp((x) => x.toast);
  const confirm = useConfirm();

  const toggle = useMutation({
    mutationFn: (auto: boolean) => api.patchSubscription(s.id, { auto_request: auto }),
    onSuccess: (next) => {
      qc.invalidateQueries({ queryKey: qk.subscriptions() });
      toast(next.auto_request ? "Auto-fetch on — new titles arrive automatically" : "Auto-fetch off", "success");
    },
    onError: (e) => toast((e as Error).message, "error"),
  });
  const remove = useMutation({
    mutationFn: () => api.unfollow(s.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.subscriptions() });
      toast(`Unfollowed ${s.display_name}`, "success");
    },
    onError: (e) => toast((e as Error).message, "error"),
  });

  const since = shortDate(s.created_at);
  return (
    <Card className="flex items-start justify-between gap-3 p-4">
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-medium text-text">{s.display_name}</span>
          <Badge tone={s.kind === "author" ? "amber" : "violet"}>{s.kind}</Badge>
          {!s.active && <Badge>paused</Badge>}
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
          {since && <span>following since {since}</span>}
          <span>{s.auto_added} auto-added</span>
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <label className="flex cursor-pointer items-center gap-1.5 text-xs text-muted" title="Auto-fetch new titles as they appear">
          <input
            type="checkbox"
            className="h-4 w-4 accent-[var(--accent)]"
            checked={s.auto_request}
            disabled={toggle.isPending}
            onChange={(e) => toggle.mutate(e.target.checked)}
          />
          Auto-fetch
        </label>
        <Button
          size="sm"
          variant="outline"
          disabled={remove.isPending}
          onClick={async () => {
            if (await confirm({
              title: "Unfollow",
              message: `Stop following ${s.kind === "author" ? s.display_name : `“${s.display_name}”`}? New titles won't be auto-fetched.`,
              confirmText: "Unfollow",
            }))
              remove.mutate();
          }}
        >
          {remove.isPending ? "Removing…" : "Unfollow"}
        </Button>
      </div>
    </Card>
  );
}

export default function Following() {
  const q = useQuery({ queryKey: qk.subscriptions(), queryFn: api.listSubscriptions });
  const rows = q.data ?? [];
  return (
    <main className="mx-auto max-w-3xl px-4 py-8">
      <h1 className="mb-1 text-2xl font-semibold">Following</h1>
      <p className="mb-6 text-sm text-muted">
        Authors and series you follow. New titles are auto-fetched into your library — turn off
        Auto-fetch on any follow to just keep it on your list.
      </p>

      {q.isLoading ? (
        <Spinner label="Loading…" />
      ) : q.isError ? (
        <p className="text-sm text-red-500">{(q.error as Error).message}</p>
      ) : rows.length === 0 ? (
        <EmptyState
          title="Not following anyone yet"
          hint="Open a title in the Catalog and use “Follow author” or “Follow series” to get new releases automatically."
        />
      ) : (
        <div className="space-y-2">
          {rows.map((s) => (
            <Row key={s.id} s={s} />
          ))}
        </div>
      )}
    </main>
  );
}
