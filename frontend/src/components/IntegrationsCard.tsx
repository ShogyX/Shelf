import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, Integration, IntegrationTest } from "../api/client";
import { Badge, Button, Card, Spinner, Toggle } from "./ui";

const input = "w-full rounded-lg border border-border bg-bg px-3 py-2 text-sm";

export default function IntegrationsCard() {
  const qc = useQueryClient();
  const integs = useQuery({ queryKey: ["integrations"], queryFn: api.listIntegrations });

  const [kind, setKind] = useState<"readarr" | "kapowarr">("readarr");
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [autoMap, setAutoMap] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["integrations"] });
    qc.invalidateQueries({ queryKey: ["catalog"] });
    qc.invalidateQueries({ queryKey: ["catalog-stats"] });
  };

  const add = useMutation({
    mutationFn: () =>
      api.addIntegration({
        kind,
        base_url: baseUrl.trim(),
        api_key: apiKey.trim(),
        auto_map_folders: autoMap,
      }),
    onSuccess: () => {
      setBaseUrl("");
      setApiKey("");
      setError(null);
      invalidate();
    },
    onError: (e) => setError((e as Error).message),
  });

  return (
    <Card className="mb-4 p-4">
      <h2 className="mb-1 font-semibold">Integrations</h2>
      <p className="mb-3 text-sm text-muted">
        Connect <b>Readarr</b> (books / novels) or <b>Kapowarr</b> (comics). Shelf fills the index
        with their libraries + metadata, and finds the files they download via your watched folders.
      </p>

      <div className="grid gap-2 sm:grid-cols-2">
        <select
          value={kind}
          onChange={(e) => setKind(e.target.value as "readarr" | "kapowarr")}
          className={input}
        >
          <option value="readarr">Readarr — books / novels</option>
          <option value="kapowarr">Kapowarr — comics</option>
        </select>
        <input
          value={baseUrl}
          onChange={(e) => setBaseUrl(e.target.value)}
          placeholder={kind === "readarr" ? "http://host:8787" : "http://host:5656"}
          className={input}
        />
        <input
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
          placeholder="API key"
          type="password"
          className={input}
        />
        <div className="flex items-center">
          <Toggle checked={autoMap} onChange={setAutoMap} label="Auto-map download folders" />
        </div>
      </div>
      <div className="mt-2 flex justify-end">
        <Button
          variant="primary"
          disabled={!baseUrl.trim() || !apiKey.trim() || add.isPending}
          onClick={() => add.mutate()}
        >
          {add.isPending ? "Connecting…" : "Connect"}
        </Button>
      </div>
      {error && <p className="mt-2 text-sm text-red-500">{error}</p>}

      {integs.isLoading && <div className="mt-3"><Spinner label="Loading integrations…" /></div>}
      <div className="mt-4 space-y-2">
        {integs.data?.map((i) => (
          <IntegrationRow key={i.id} integ={i} onChanged={invalidate} />
        ))}
      </div>
    </Card>
  );
}

function IntegrationRow({ integ, onChanged }: { integ: Integration; onChanged: () => void }) {
  const qc = useQueryClient();
  const [test, setTest] = useState<IntegrationTest | null>(null);

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["integrations"] });
    onChanged();
  };

  const testM = useMutation({
    mutationFn: () => api.testIntegration(integ.id),
    onSuccess: (r) => {
      setTest(r);
      refresh();
    },
  });
  const syncM = useMutation({
    mutationFn: () => api.syncIntegration(integ.id),
    onSuccess: () => refresh(),
  });
  const toggle = useMutation({
    mutationFn: (enabled: boolean) => api.updateIntegration(integ.id, { enabled }),
    onSuccess: () => refresh(),
  });
  const del = useMutation({
    mutationFn: () => api.deleteIntegration(integ.id),
    onSuccess: () => refresh(),
  });

  return (
    <div className="rounded-lg border border-border p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="truncate font-medium">{integ.name}</span>
            <Badge tone="violet">{integ.kind}</Badge>
            {!integ.enabled && <Badge>disabled</Badge>}
          </div>
          <div className="truncate text-xs text-muted">{integ.base_url}</div>
          <div className="text-xs text-muted">
            {integ.catalog_count} in catalog
            {integ.root_folder ? ` · ${integ.root_folder}` : ""}
            {integ.last_sync_at ? ` · synced ${new Date(integ.last_sync_at).toLocaleString()}` : ""}
          </div>
          {integ.last_error && <div className="text-xs text-red-500">⚠ {integ.last_error}</div>}
        </div>
        <div className="flex shrink-0 items-center gap-1">
          <Button size="sm" variant="ghost" disabled={testM.isPending} onClick={() => testM.mutate()}>
            {testM.isPending ? "Testing…" : "Test"}
          </Button>
          <Button size="sm" variant="ghost" disabled={syncM.isPending} onClick={() => syncM.mutate()}>
            {syncM.isPending ? "Syncing…" : "Sync now"}
          </Button>
          <Toggle checked={integ.enabled} onChange={(v) => toggle.mutate(v)} label="" />
          <Button
            size="sm"
            variant="danger"
            onClick={() => confirm(`Disconnect ${integ.name}?`) && del.mutate()}
          >
            ✕
          </Button>
        </div>
      </div>
      {test && (
        <div className={`mt-2 text-xs ${test.ok ? "text-green-600" : "text-red-500"}`}>
          {test.ok
            ? `✓ ${test.app ?? "Connected"}${test.version ? ` v${test.version}` : ""}` +
              (test.root_folders.length ? ` · folders: ${test.root_folders.join(", ")}` : "")
            : `✗ ${test.error}`}
        </div>
      )}
    </div>
  );
}
