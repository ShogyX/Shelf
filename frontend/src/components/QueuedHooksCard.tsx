import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, QueuedHook } from "../api/client";
import { Badge, Button, Card, Spinner } from "./ui";

const statusTone: Record<string, "default" | "green" | "amber" | "red"> = {
  pending: "amber",
  hooked: "green",
  failed: "red",
};

export default function QueuedHooksCard() {
  const qc = useQueryClient();
  const hooks = useQuery({ queryKey: ["queued-hooks"], queryFn: () => api.listQueuedHooks() });

  const invalidate = () => qc.invalidateQueries({ queryKey: ["queued-hooks"] });

  const process = useMutation({
    mutationFn: () => api.processQueuedHooks(),
    onSuccess: () => invalidate(),
  });
  const del = useMutation({
    mutationFn: (id: number) => api.deleteQueuedHook(id),
    onSuccess: () => invalidate(),
  });

  const items = hooks.data ?? [];
  if (!hooks.isLoading && items.length === 0) return null; // nothing queued → hide the card

  const pending = items.filter((h) => h.status === "pending").length;

  return (
    <Card className="mb-4 p-4">
      <div className="mb-1 flex items-center justify-between gap-2">
        <h2 className="font-semibold">Auto-hook queue</h2>
        <Button
          size="sm"
          variant="ghost"
          disabled={process.isPending || pending === 0}
          onClick={() => process.mutate()}
        >
          {process.isPending ? "Checking…" : "Check now"}
        </Button>
      </div>
      <p className="mb-3 text-sm text-muted">
        Related titles (prequels / sequels / spin-offs) and your Goodreads want-to-read shelf,
        queued to hook automatically once they appear in the index from an enabled source.
        {pending > 0 ? ` ${pending} waiting.` : ""}
      </p>

      {hooks.isLoading && <Spinner label="Loading queue…" />}
      <div className="space-y-1.5">
        {items.map((h) => (
          <QueuedRow key={h.id} hook={h} onDelete={() => del.mutate(h.id)} />
        ))}
      </div>
    </Card>
  );
}

function QueuedRow({ hook, onDelete }: { hook: QueuedHook; onDelete: () => void }) {
  return (
    <div className="flex items-center justify-between gap-2 rounded-lg border border-border p-2.5">
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <span className="truncate text-sm font-medium">{hook.title}</span>
          <Badge tone={statusTone[hook.status] ?? "default"}>{hook.status}</Badge>
          <Badge tone="violet">{hook.relation ?? hook.reason}</Badge>
        </div>
        <div className="truncate text-xs text-muted">
          {hook.author ? `${hook.author} · ` : ""}
          {hook.reason === "goodreads" ? "Goodreads shelf" : "related title"}
          {hook.detail ? ` · ${hook.detail}` : ""}
        </div>
      </div>
      <Button size="sm" variant="danger" onClick={onDelete}>
        ✕
      </Button>
    </div>
  );
}
