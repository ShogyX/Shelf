import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, Source } from "../api/client";
import { Badge, Button, Card, Modal, Spinner, Toggle } from "../components/ui";
import { useConfirm } from "../components/confirm";
import { useIsAdmin } from "../auth";
import { useEffect, useState } from "react";

/** One source as a COMPACT row: name + badges + base_url, with the enable Toggle and a ⚙ that
 *  opens the per-source config modal. Source management is admin-only — non-admins see the same
 *  row READ-ONLY (no enable toggle, no ⚙), with status shown as static badges. */
function SourceRow({ source }: { source: Source }) {
  const qc = useQueryClient();
  const confirm = useConfirm();
  const isAdmin = useIsAdmin();
  const [configuring, setConfiguring] = useState(false);

  const update = useMutation({
    mutationFn: (patch: Partial<Source>) => api.updateSource(source.id, patch),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["sources"] }),
  });

  return (
    <Card className="flex items-center justify-between gap-3 p-3">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="font-medium">{source.display_name}</h3>
          <Badge tone="violet">{source.license_basis}</Badge>
          <Badge tone={source.tos_permitted ? "green" : "red"}>
            {source.tos_permitted ? "enabled" : "off"}
          </Badge>
          {source.supports_auth && (
            <Badge tone={source.has_auth ? "green" : "amber"}>
              {source.has_auth ? "token set" : "no token"}
            </Badge>
          )}
        </div>
        <p className="mt-0.5 truncate text-xs text-muted">{source.base_url ?? "no network (local)"}</p>
      </div>
      {isAdmin ? (
        <div className="flex shrink-0 items-center gap-2">
          <Toggle
            checked={source.tos_permitted}
            label={source.tos_permitted ? "Permitted" : "Disabled"}
            onChange={async (v) => {
              if (v && !(await confirm({
                title: "Enable source",
                message: `Enable “${source.display_name}”? Only do this for sources you are permitted to read.`,
                confirmText: "Enable",
              }))) return;
              update.mutate({ tos_permitted: v });
            }}
          />
          <Button
            size="sm"
            variant="ghost"
            aria-label={`Configure ${source.display_name}`}
            onClick={() => setConfiguring(true)}
          >
            ⚙
          </Button>
        </div>
      ) : (
        <div className="shrink-0">
          <Badge tone={source.tos_permitted ? "green" : "red"}>
            {source.tos_permitted ? "ingest allowed" : "gate closed"}
          </Badge>
        </div>
      )}
      {configuring && (
        <SourceConfigModal source={source} update={update} onClose={() => setConfiguring(false)} />
      )}
    </Card>
  );
}

function SourceConfigModal({
  source,
  update,
  onClose,
}: {
  source: Source;
  update: ReturnType<typeof useMutation<Source, Error, Partial<Source>>>;
  onClose: () => void;
}) {
  const confirm = useConfirm();
  // NB: named intervalS (not setInterval) so it doesn't shadow the global window.setInterval.
  const [intervalS, setIntervalS] = useState(source.min_request_interval_s);
  const [token, setToken] = useState("");
  const [tokenSaved, setTokenSaved] = useState(false);
  // Re-sync from the server value if it changes (e.g. another tab edited it, or a refetch).
  useEffect(() => setIntervalS(source.min_request_interval_s), [source.min_request_interval_s]);

  return (
    <Modal title={source.display_name} onClose={onClose}>
      <div className="space-y-4">
        <label className="block text-xs text-muted">
          Min interval (s)
          <input
            type="number"
            min={0}
            step={0.5}
            value={Number.isFinite(intervalS) ? intervalS : ""}
            onChange={(e) => {
              const v = parseFloat(e.target.value);
              setIntervalS(Number.isFinite(v) ? Math.max(0, v) : NaN);   // empty input → NaN, shown blank
            }}
            onBlur={() => {
              // Never PATCH NaN (the cleared-input bug): fall back to 0 and re-sync the field.
              const v = Number.isFinite(intervalS) ? intervalS : 0;
              setIntervalS(v);
              if (v !== source.min_request_interval_s) update.mutate({ min_request_interval_s: v });
            }}
            className="mt-1 w-full rounded-lg border border-border bg-bg px-2 py-1 text-sm text-text"
          />
          <span className="mt-1 block text-[11px] text-muted">
            Gathering is paced only by this interval — there is no daily cap.
          </span>
        </label>

        <div className="text-xs text-muted">
          robots.txt
          <div className="mt-1">
            <Toggle
              checked={source.robots_respected}
              label={source.robots_respected ? "respected" : "ignored"}
              onChange={async (v) => {
                if (!v && !(await confirm({
                  title: "Ignore robots.txt?",
                  message: "Only for dev/troubleshooting on sources you are permitted to read.",
                  danger: true,
                  confirmText: "Ignore robots.txt",
                }))) return;
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
              onChange={async (v) => {
                if (v && !(await confirm({
                  title: "Use a headless browser?",
                  message: "Slower and heavier — use for JS-heavy sites you are permitted to read.",
                  confirmText: "Enable",
                }))) return;
                update.mutate({ render_js: v });
              }}
            />
          </div>
        </div>

        {/* Members-only sources (e.g. J-Novel): provide an access token to fetch content you own. */}
        {source.supports_auth && (
          <div className="border-t border-border pt-3">
            <div className="mb-1 flex items-center gap-2 text-xs text-muted">
              Access token (members-only content)
              <Badge tone={source.has_auth ? "green" : "amber"}>
                {source.has_auth ? "saved" : "not set"}
              </Badge>
            </div>
            <div className="flex items-center gap-2">
              <input
                type="password"
                autoComplete="off"
                value={token}
                onChange={(e) => { setToken(e.target.value); setTokenSaved(false); }}
                placeholder={source.has_auth ? "•••••••• (leave blank to keep)" : "paste your account access token"}
                className="w-full rounded-lg border border-border bg-bg px-2 py-1 text-sm text-text"
              />
              <Button
                size="sm"
                variant="primary"
                disabled={!token.trim() || update.isPending}
                onClick={() => update.mutate({ auth_token: token.trim() } as Partial<Source>,
                  { onSuccess: () => { setToken(""); setTokenSaved(true); } })}
              >
                Save
              </Button>
              {source.has_auth && (
                <Button
                  size="sm"
                  variant="ghost"
                  title="Remove the stored token"
                  disabled={update.isPending}
                  onClick={() => update.mutate({ auth_token: "" } as Partial<Source>)}
                >
                  Clear
                </Button>
              )}
            </div>
            {tokenSaved && <p className="mt-1 text-[11px] text-green-600">Token saved.</p>}
            <p className="mt-1 text-[11px] text-muted">
              Stored on the server and never returned by the API. Used only to fetch content your
              account is entitled to. For J-Novel, this is your account access token.
            </p>
          </div>
        )}
      </div>
    </Modal>
  );
}

// Non-network / non-working sources that don't belong in this management view: the in-memory demo,
// the paid/gated J-Novel adapter, the two local-file sources (their own "Import files" / "Watched
// folders" tabs), and the legacy "x" placeholder. Only real, ingestable network sources are listed.
const HIDDEN_SOURCE_KEYS = new Set(["memory", "jnovel", "local_folder", "local_import", "lf"]);

/** The Sources tab body (rendered inside the merged Add page). */
export function SourcesTab() {
  const sources = useQuery({ queryKey: ["sources"], queryFn: api.listSources });
  const visible = (sources.data ?? []).filter((s) => !HIDDEN_SOURCE_KEYS.has(s.key));

  return (
    <div>
      <p className="mb-6 text-sm text-muted">
        Each source carries a compliance declaration. The engine refuses to ingest any source that is
        not explicitly permitted — toggle a source on only for content you have the right to read.
      </p>

      {sources.isLoading && <Spinner label="Loading sources…" />}
      <div className="space-y-2">
        {visible.map((s) => (
          <SourceRow key={s.id} source={s} />
        ))}
      </div>
    </div>
  );
}
