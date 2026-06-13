import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, CatalogRow, IndexLayout } from "../../api/client";
import { useApp } from "../../store";
import { useIsAdmin } from "../../auth";
import { Badge, Button, Card, InfoHint, Spinner } from "../ui";
import {
  EMPTY_LAYOUT, effectiveLayout, laneKey, lanesForCategory, layoutToPrefs,
  moveCategory, moveLane, orderedCategories, toggleCategory, toggleLaneHidden,
} from "./layout";

const ctlBtn =
  "rounded border border-border px-1.5 py-0.5 text-[11px] leading-none text-muted " +
  "hover:bg-surface-2 disabled:opacity-30 disabled:hover:bg-transparent";

function MatrixRow({ label, indent, hidden, bold, upDis, downDis, onUp, onDown, onToggle, disabled }: {
  label: string; indent?: boolean; hidden: boolean; bold?: boolean;
  upDis: boolean; downDis: boolean; onUp: () => void; onDown: () => void; onToggle: () => void;
  disabled?: boolean;
}) {
  return (
    <div className={`flex items-center gap-2 px-2.5 py-1.5 ${indent ? "pl-8" : ""} ${hidden ? "opacity-45" : ""}`}>
      <span className={`flex-1 truncate text-sm ${bold ? "font-semibold uppercase tracking-wide text-muted" : "text-text"}`}>
        {label}
      </span>
      {!disabled && (
        <>
          <button className={ctlBtn} disabled={upDis} onClick={onUp} title="Move up" aria-label="Move up">▲</button>
          <button className={ctlBtn} disabled={downDis} onClick={onDown} title="Move down" aria-label="Move down">▼</button>
          <button className={`${ctlBtn} w-12 text-center`} onClick={onToggle} title={hidden ? "Show" : "Hide"}>
            {hidden ? "Show" : "Hide"}
          </button>
        </>
      )}
    </div>
  );
}

/** The editable matrix of categories + their genres. `rows` is the EDITING user's permission-filtered
 *  catalog (so the matrix can only ever contain content they're allowed to see). */
function LayoutMatrix({ rows, value, onChange, disabled }: {
  rows: CatalogRow[]; value: IndexLayout; onChange: (l: IndexLayout) => void; disabled?: boolean;
}) {
  const cats = orderedCategories(rows, value);
  if (cats.length === 0) {
    return <p className="text-sm text-muted">No discovered categories yet — index a site first.</p>;
  }
  return (
    <div className={`mt-3 overflow-hidden rounded-lg border border-border ${disabled ? "pointer-events-none opacity-60" : ""}`}>
      {cats.map((cat, ci) => {
        const catHidden = value.hiddenCategories.includes(cat);
        const lanes = lanesForCategory(rows, cat, value);
        return (
          <div key={cat} className="border-b border-border last:border-0">
            <MatrixRow
              label={cat} bold hidden={catHidden} disabled={disabled}
              upDis={ci === 0} downDis={ci === cats.length - 1}
              onUp={() => onChange(moveCategory(rows, value, cat, -1))}
              onDown={() => onChange(moveCategory(rows, value, cat, 1))}
              onToggle={() => onChange(toggleCategory(value, cat))}
            />
            {lanes.map((row, li) => {
              const k = laneKey(row);
              const laneHidden = value.hiddenLanes.includes(k);
              return (
                <MatrixRow
                  key={k} label={row.label} indent disabled={disabled}
                  hidden={catHidden || laneHidden}
                  upDis={li === 0} downDis={li === lanes.length - 1}
                  onUp={() => onChange(moveLane(rows, value, cat, k, -1))}
                  onDown={() => onChange(moveLane(rows, value, cat, k, 1))}
                  onToggle={() => onChange(toggleLaneHidden(value, k))}
                />
              );
            })}
          </div>
        );
      })}
    </div>
  );
}

export default function LayoutSettings() {
  const isAdmin = useIsAdmin();
  const { prefs, setPrefs } = useApp();
  const qc = useQueryClient();
  const rowsQ = useQuery({ queryKey: ["catalog-rows"], queryFn: () => api.catalogRows() });
  const globalQ = useQuery({ queryKey: ["index-layout"], queryFn: () => api.getIndexLayout() });

  const rows = rowsQ.data ?? [];
  const globalDefault = globalQ.data ?? EMPTY_LAYOUT;
  const custom = !!prefs.indexLayoutCustom;
  const personal = effectiveLayout(prefs, globalDefault);

  // Admin's draft of the GLOBAL default — explicit save (it affects everyone).
  const [draft, setDraft] = useState<IndexLayout | null>(null);
  const gdraft = draft ?? globalDefault;
  const saveGlobal = useMutation({
    mutationFn: () => api.putIndexLayout(gdraft),
    onSuccess: (d) => { qc.setQueryData(["index-layout"], d); setDraft(null); },
  });

  if (rowsQ.isLoading || globalQ.isLoading) {
    return <Card className="mb-4 p-4"><Spinner label="Loading layout…" /></Card>;
  }

  return (
    <>
      <Card className="mb-4 p-4">
        <h2 className="flex items-center gap-1.5 font-semibold">
          Your index layout
          <InfoHint text={<>Reorder and hide the Index page's media categories and genre rows.
            This is a display preference only — it can't reveal categories or 18+ content your
            account isn't permitted to see; it only arranges what you already have access to.</>} />
        </h2>
        {custom ? (
          <>
            <p className="mt-1 mb-1 text-sm text-muted">
              Personal layout — overrides the shared default. Changes save automatically.
            </p>
            <LayoutMatrix rows={rows} value={personal} onChange={(l) => setPrefs(layoutToPrefs(l))} />
            <div className="mt-3">
              <Button size="sm" onClick={() => setPrefs({ indexLayoutCustom: false })}>
                Reset to shared default
              </Button>
            </div>
          </>
        ) : (
          <>
            <p className="mt-1 mb-1 text-sm text-muted">
              You're following the shared default layout (below). Customize to make it your own.
            </p>
            <LayoutMatrix rows={rows} value={globalDefault} onChange={() => {}} disabled />
            <div className="mt-3">
              <Button variant="primary" size="sm" onClick={() => setPrefs(layoutToPrefs(globalDefault))}>
                Customize my layout
              </Button>
            </div>
          </>
        )}
      </Card>

      {isAdmin && (
        <Card className="p-4">
          <h2 className="flex items-center gap-1.5 font-semibold">
            Global default layout
            <Badge tone="violet">admin</Badge>
            <InfoHint text={<>The default arrangement for every user who hasn't customized their own.
              It's applied ON TOP of each user's permission-filtered catalog, so hiding/showing or
              reordering here can never expose a category or 18+ genre a user isn't allowed to see —
              restricted content is simply absent for them.</>} />
          </h2>
          <p className="mt-1 mb-1 text-sm text-muted">
            Sets the order + show/hide every user starts from. They can still override it with their own.
          </p>
          <LayoutMatrix rows={rows} value={gdraft} onChange={setDraft} />
          <div className="mt-3 flex items-center gap-2">
            <Button variant="primary" size="sm" disabled={!draft || saveGlobal.isPending} onClick={() => saveGlobal.mutate()}>
              {saveGlobal.isPending ? "Saving…" : "Save default for everyone"}
            </Button>
            {draft && <Button size="sm" onClick={() => setDraft(null)}>Discard</Button>}
            {saveGlobal.isError && <span className="text-sm text-red-500">{(saveGlobal.error as Error).message}</span>}
            {saveGlobal.isSuccess && !draft && <Badge tone="green">saved</Badge>}
          </div>
        </Card>
      )}
    </>
  );
}
