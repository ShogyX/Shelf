// Acquire-time "where does this land?" prompt. Replaces the always-present ShelfDestination
// picker: every hook / acquire / add call asks once, at the moment of acquiring, via pickShelf().
// pickShelf() resolves to a shelf id (number), null ("Library only"), or undefined (cancelled —
// the caller must ABORT). When the user has no shelves it resolves null immediately (no modal).
import React, { createContext, useCallback, useContext, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { Button, Modal } from "./ui";

const LAST_PICK_KEY = "shelf-last-pick";

function loadLastPick(): number | null | undefined {
  if (typeof localStorage === "undefined") return undefined;
  const v = localStorage.getItem(LAST_PICK_KEY);
  if (v == null) return undefined; // never chosen "remember"
  return v === "" ? null : Number(v) || null;
}
function saveLastPick(id: number | null) {
  if (typeof localStorage === "undefined") return;
  localStorage.setItem(LAST_PICK_KEY, id == null ? "" : String(id));
}

export interface PickShelfOpts {
  defaultShelfId?: number | null;
}
type PickShelf = (opts?: PickShelfOpts) => Promise<number | null | undefined>;

const ShelfPromptCtx = createContext<PickShelf>(async () => null);

export const useShelfPrompt = (): PickShelf => useContext(ShelfPromptCtx);

export function ShelfPromptProvider({ children }: { children: React.ReactNode }) {
  const qc = useQueryClient();
  const [opts, setOpts] = useState<PickShelfOpts | null>(null);
  const resolver = useRef<((v: number | null | undefined) => void) | null>(null);

  const pickShelf = useCallback<PickShelf>(
    async (o) => {
      // Await the shelves so we never flash an empty prompt then skip. If the user has none,
      // resolve to "Library only" immediately with no modal.
      const shelves = await qc.ensureQueryData({
        queryKey: ["bookshelves"],
        queryFn: api.listBookshelves,
      });
      if (shelves.length === 0) return null;
      return new Promise<number | null | undefined>((resolve) => {
        resolver.current = resolve;
        setOpts(o ?? {});
      });
    },
    [qc],
  );

  const settle = (v: number | null | undefined) => {
    setOpts(null);
    resolver.current?.(v);
    resolver.current = null;
  };

  return (
    <ShelfPromptCtx.Provider value={pickShelf}>
      {children}
      {opts && <ShelfPromptModal opts={opts} onSettle={settle} />}
    </ShelfPromptCtx.Provider>
  );
}

function ShelfPromptModal({
  opts,
  onSettle,
}: {
  opts: PickShelfOpts;
  onSettle: (v: number | null | undefined) => void;
}) {
  const { data: shelves = [] } = useQuery({ queryKey: ["bookshelves"], queryFn: api.listBookshelves });
  const valid = (id: number | null | undefined): id is number | null =>
    id == null || shelves.some((s) => s.id === id);

  // Preselect precedence: opts.defaultShelfId > remembered-last > null (Library only). A stale
  // default/remembered id that no longer exists falls back to null.
  const remembered = loadLastPick();
  const initial =
    opts.defaultShelfId !== undefined && valid(opts.defaultShelfId)
      ? opts.defaultShelfId
      : remembered !== undefined && valid(remembered)
        ? remembered
        : null;

  const [choice, setChoice] = useState<number | null>(initial);
  const [remember, setRemember] = useState(false);

  const confirm = () => {
    if (remember) saveLastPick(choice);
    onSettle(choice);
  };

  return (
    <Modal
      title="Save to shelf"
      onClose={() => onSettle(undefined)}
      footer={
        <>
          <Button variant="ghost" onClick={() => onSettle(undefined)}>
            Cancel
          </Button>
          <Button variant="primary" onClick={confirm} autoFocus>
            Add
          </Button>
        </>
      }
    >
      <div className="space-y-1">
        <label className="flex items-center gap-2 rounded-lg px-2 py-1.5 text-sm hover:bg-surface-2">
          <input
            type="radio"
            name="shelf-pick"
            checked={choice == null}
            onChange={() => setChoice(null)}
          />
          <span>Library only</span>
        </label>
        {shelves.map((s) => (
          <label
            key={s.id}
            className="flex items-center gap-2 rounded-lg px-2 py-1.5 text-sm hover:bg-surface-2"
          >
            <input
              type="radio"
              name="shelf-pick"
              checked={choice === s.id}
              onChange={() => setChoice(s.id)}
            />
            <span>{s.name}</span>
          </label>
        ))}
      </div>
      <label className="mt-3 flex items-center gap-2 border-t border-border pt-3 text-xs text-muted">
        <input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} />
        Remember my choice
      </label>
    </Modal>
  );
}
