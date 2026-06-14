import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, CatalogRow, IndexLayout } from "../../api/client";
import { Badge, Button, Card, InfoHint, Spinner } from "../ui";
import {
  EMPTY_LAYOUT, laneKey, lanesForCategory, moveCategory, moveLane,
  orderedCategories, toggleCategory, toggleLaneHidden,
} from "./layout";

const ctlBtn =
  "rounded border border-border px-1.5 py-0.5 text-[11px] leading-none text-muted " +
  "hover:bg-surface-2 disabled:opacity-30 disabled:hover:bg-transparent";

function MatrixRow({ label, indent, hidden, bold, upDis, downDis, onUp, onDown, onToggle }: {
  label: string; indent?: boolean; hidden: boolean; bold?: boolean;
  upDis: boolean; downDis: boolean; onUp: () => void; onDown: () => void; onToggle: () => void;
}) {
  return (
    <div className={`flex items-center gap-2 px-2.5 py-1.5 ${indent ? "pl-8" : ""} ${hidden ? "opacity-45" : ""}`}>
      <span className={`flex-1 truncate text-sm ${bold ? "font-semibold uppercase tracking-wide text-muted" : "text-text"}`}>
        {label}
      </span>
      <button className={ctlBtn} disabled={upDis} onClick={onUp} title="Move up" aria-label="Move up">▲</button>
      <button className={ctlBtn} disabled={downDis} onClick={onDown} title="Move down" aria-label="Move down">▼</button>
      <button className={`${ctlBtn} w-12 text-center`} onClick={onToggle} title={hidden ? "Show" : "Hide"}>
        {hidden ? "Show" : "Hide"}
      </button>
    </div>
  );
}

/** Editable matrix of categories + their genres. `rows` is the editing user's permission-filtered
 *  catalog (admin → all), so the matrix can only ever contain content that user may see. */
function LayoutMatrix({ rows, value, onChange }: {
  rows: CatalogRow[]; value: IndexLayout; onChange: (l: IndexLayout) => void;
}) {
  const cats = orderedCategories(rows, value);
  if (cats.length === 0) {
    return <p className="text-sm text-muted">No discovered categories yet — index a site first.</p>;
  }
  return (
    <div className="mt-3 overflow-hidden rounded-lg border border-border">
      {cats.map((cat, ci) => {
        const catHidden = value.hiddenCategories.includes(cat);
        const lanes = lanesForCategory(rows, cat, value);
        return (
          <div key={cat} className="border-b border-border last:border-0">
            <MatrixRow
              label={cat} bold hidden={catHidden}
              upDis={ci === 0} downDis={ci === cats.length - 1}
              onUp={() => onChange(moveCategory(rows, value, cat, -1))}
              onDown={() => onChange(moveCategory(rows, value, cat, 1))}
              onToggle={() => onChange(toggleCategory(value, cat))}
            />
            {lanes.map((row, li) => {
              const k = laneKey(row);
              return (
                <MatrixRow
                  key={k} label={row.label} indent
                  hidden={catHidden || value.hiddenLanes.includes(k)}
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

/** Admin-only: edit the GLOBAL DEFAULT index layout. Individual users tweak their own layout
 *  inline on the Index page ("Edit layout"); this just sets the default everyone starts from. */
export default function LayoutSettings() {
  const qc = useQueryClient();
  const rowsQ = useQuery({ queryKey: ["catalog-rows"], queryFn: () => api.catalogRows() });
  const globalQ = useQuery({ queryKey: ["index-layout"], queryFn: () => api.getIndexLayout() });
  const [draft, setDraft] = useState<IndexLayout | null>(null);
  // value must be computed before useMutation (which closes over it) — and all hooks must run on
  // every render, so this and the mutation stay ABOVE the loading early-return.
  const value = draft ?? globalQ.data ?? EMPTY_LAYOUT;
  const save = useMutation({
    mutationFn: () => api.putIndexLayout(value),
    onSuccess: (d) => { qc.setQueryData(["index-layout"], d); setDraft(null); },
  });

  if (rowsQ.isLoading || globalQ.isLoading) {
    return <Card className="mb-4 p-4"><Spinner label="Loading layout…" /></Card>;
  }
  const rows = rowsQ.data ?? [];

  return (
    <Card className="mb-4 p-4">
      <h2 className="flex items-center gap-1.5 font-semibold">
        Global default index layout
        <Badge tone="violet">admin</Badge>
        <InfoHint text={<>The default arrangement (category + genre order, and what's hidden) every
          user starts from. Each user can still override it for themselves via “Edit layout” on the
          Index page. This is applied ON TOP of each user's permission-filtered catalog, so reordering
          or hiding here can never expose a category or 18+ genre a user isn't allowed to see —
          restricted content is simply absent for them.</>} />
      </h2>
      <p className="mt-1 mb-1 text-sm text-muted">
        Reorder with ▲▼ and Hide/Show each category &amp; genre, then save it as the default for everyone.
      </p>
      <LayoutMatrix rows={rows} value={value} onChange={setDraft} />
      <div className="mt-3 flex items-center gap-2">
        <Button variant="primary" size="sm" disabled={!draft || save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? "Saving…" : "Save default for everyone"}
        </Button>
        {draft && <Button size="sm" onClick={() => setDraft(null)}>Discard</Button>}
        {save.isError && <span className="text-sm text-red-500">{(save.error as Error).message}</span>}
        {save.isSuccess && !draft && <Badge tone="green">saved</Badge>}
      </div>
    </Card>
  );
}
