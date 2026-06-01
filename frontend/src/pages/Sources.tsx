import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, Source } from "../api/client";
import { Badge, Button, Card, Spinner, Toggle } from "../components/ui";
import { useState } from "react";

function SourceRow({ source }: { source: Source }) {
  const qc = useQueryClient();
  const [interval, setInterval] = useState(source.min_request_interval_s);
  const [daily, setDaily] = useState(source.max_daily_requests);

  const update = useMutation({
    mutationFn: (patch: Partial<Source>) => api.updateSource(source.id, patch),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["sources"] }),
  });

  return (
    <Card className="p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <h3 className="font-semibold">{source.display_name}</h3>
            <Badge tone="violet">{source.license_basis}</Badge>
          </div>
          <p className="text-xs text-muted">{source.base_url ?? "no network (local)"}</p>
        </div>
        <Toggle
          checked={source.tos_permitted}
          label={source.tos_permitted ? "Permitted" : "Disabled"}
          onChange={(v) => {
            if (v && !confirm(`Enable "${source.display_name}"? Only do this for sources you are permitted to read.`)) return;
            update.mutate({ tos_permitted: v });
          }}
        />
      </div>

      <div className="mt-4 grid grid-cols-2 gap-4 sm:grid-cols-4">
        <label className="text-xs text-muted">
          Min interval (s)
          <input
            type="number"
            min={0}
            step={0.5}
            value={interval}
            onChange={(e) => setInterval(parseFloat(e.target.value))}
            onBlur={() => update.mutate({ min_request_interval_s: interval })}
            className="mt-1 w-full rounded-lg border border-border bg-bg px-2 py-1 text-sm text-text"
          />
        </label>
        <label className="text-xs text-muted">
          Max daily requests
          <input
            type="number"
            min={0}
            value={daily}
            onChange={(e) => setDaily(parseInt(e.target.value))}
            onBlur={() => update.mutate({ max_daily_requests: daily })}
            className="mt-1 w-full rounded-lg border border-border bg-bg px-2 py-1 text-sm text-text"
          />
        </label>
        <div className="text-xs text-muted">
          robots.txt
          <div className="mt-1">
            <Toggle
              checked={source.robots_respected}
              label={source.robots_respected ? "respected" : "ignored"}
              onChange={(v) => {
                if (!v && !confirm(
                  `Ignore robots.txt for "${source.display_name}"?\n\nOnly for dev/troubleshooting on sources you are permitted to read.`
                )) return;
                update.mutate({ robots_respected: v });
              }}
            />
          </div>
        </div>
        <div className="text-xs text-muted">
          Headless browser
          <div className="mt-1">
            <Toggle
              checked={source.render_js}
              label={source.render_js ? "render JS" : "plain HTTP"}
              onChange={(v) => {
                if (v && !confirm(
                  `Render "${source.display_name}" with a headless browser?\n\n` +
                  `Slower and heavier — use for JS-heavy sites you are permitted to read.`
                )) return;
                update.mutate({ render_js: v });
              }}
            />
          </div>
        </div>
        <div className="text-xs text-muted">
          Status
          <div className="mt-1">
            <Badge tone={source.tos_permitted ? "green" : "red"}>
              {source.tos_permitted ? "ingest allowed" : "gate closed"}
            </Badge>
          </div>
        </div>
      </div>
    </Card>
  );
}

export default function Sources() {
  const sources = useQuery({ queryKey: ["sources"], queryFn: api.listSources });

  return (
    <main className="mx-auto max-w-3xl px-4 py-8">
      <h1 className="mb-1 text-2xl font-semibold">Sources</h1>
      <p className="mb-6 text-sm text-muted">
        Each source carries a compliance declaration. The engine refuses to ingest any source that is
        not explicitly permitted — toggle a source on only for content you have the right to read.
      </p>
      {sources.isLoading && <Spinner label="Loading sources…" />}
      <div className="space-y-3">
        {sources.data?.map((s) => (
          <SourceRow key={s.id} source={s} />
        ))}
      </div>
    </main>
  );
}
