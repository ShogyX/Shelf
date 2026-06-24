// Acquire-time "where does this land?" prompt. Replaces the always-present ShelfDestination
// picker: every hook / acquire / add call asks once, at the moment of acquiring, via pickShelf().
// pickShelf() resolves to a shelf id (number), null ("Library only"), or undefined (cancelled —
// the caller must ABORT). When the user has no shelves it resolves null immediately (no modal).
//
// useAcquirePrompt() is the same prompt PLUS a format choice (ebook / audiobook / both) for catalog
// acquire, so one prompt collects destination + format. It resolves { shelfId, format } | undefined.
import React, { createContext, useCallback, useContext, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { qk } from "../api/queryKeys";
import { Button, Modal, SegmentedControl } from "./ui";

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

export type AcquireFormat = "ebook" | "audiobook" | "both";

export interface PickShelfOpts {
  defaultShelfId?: number | null;
  // Acquire-only (admins): show a "save to operator stock" choice. Picking it fires onStock and
  // resolves the promise to `undefined` (the normal library flow aborts) — so callers that don't
  // opt in are completely unaffected and the return type stays a shelf id.
  allowStock?: boolean;
  onStock?: () => void;
  // Catalog acquire: also offer ebook / audiobook / both. Shows the format selector + forces the
  // modal (so the format is always a deliberate choice, even when the user has no shelves).
  pickFormat?: boolean;
  defaultFormat?: AcquireFormat;
  // In stock — reword the modal for the instant-add case ("Add to library" vs "Acquire").
  inStock?: boolean;
}
export interface AcquirePick {
  shelfId: number | null;
  format: AcquireFormat;
}

type PickInternal = (opts?: PickShelfOpts) => Promise<AcquirePick | undefined>;
type PickShelf = (opts?: PickShelfOpts) => Promise<number | null | undefined>;
type PickAcquire = (opts?: PickShelfOpts) => Promise<AcquirePick | undefined>;

const ShelfPromptCtx = createContext<{ pickShelf: PickShelf; pickAcquire: PickAcquire }>({
  pickShelf: async () => null,
  pickAcquire: async () => ({ shelfId: null, format: "ebook" }),
});

export const useShelfPrompt = (): PickShelf => useContext(ShelfPromptCtx).pickShelf;
export const useAcquirePrompt = (): PickAcquire => useContext(ShelfPromptCtx).pickAcquire;

export function ShelfPromptProvider({ children }: { children: React.ReactNode }) {
  const qc = useQueryClient();
  const [opts, setOpts] = useState<PickShelfOpts | null>(null);
  const resolver = useRef<((v: AcquirePick | undefined) => void) | null>(null);

  const pickInternal = useCallback<PickInternal>(
    async (o) => {
      // Await the shelves so we never flash an empty prompt then skip. If the user has none AND
      // there's no stock/format choice to offer, resolve to "Library only" immediately with no modal.
      const shelves = await qc.ensureQueryData({
        queryKey: qk.bookshelves(),
        queryFn: api.listBookshelves,
      });
      if (shelves.length === 0 && !o?.allowStock && !o?.pickFormat)
        return { shelfId: null, format: o?.defaultFormat ?? "ebook" };
      return new Promise<AcquirePick | undefined>((resolve) => {
        resolver.current = resolve;
        setOpts(o ?? {});
      });
    },
    [qc],
  );

  const pickShelf = useCallback<PickShelf>(
    async (o) => {
      const r = await pickInternal(o);
      return r === undefined ? undefined : r.shelfId;
    },
    [pickInternal],
  );
  const pickAcquire = useCallback<PickAcquire>(
    (o) => pickInternal({ ...o, pickFormat: true }),
    [pickInternal],
  );

  const settle = (v: AcquirePick | undefined) => {
    setOpts(null);
    resolver.current?.(v);
    resolver.current = null;
  };

  return (
    <ShelfPromptCtx.Provider value={{ pickShelf, pickAcquire }}>
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
  onSettle: (v: AcquirePick | undefined) => void;
}) {
  const { data: shelves = [] } = useQuery({ queryKey: qk.bookshelves(), queryFn: api.listBookshelves });
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

  const [choice, setChoice] = useState<number | null | "stock">(initial);
  const [format, setFormat] = useState<AcquireFormat>(opts.defaultFormat ?? "ebook");
  const [remember, setRemember] = useState(false);

  const confirm = () => {
    if (choice === "stock") {
      // Operator action, not a library destination: fire it and abort the normal shelf flow.
      opts.onStock?.();
      onSettle(undefined);
      return;
    }
    if (remember) saveLastPick(choice);
    onSettle({ shelfId: choice, format });
  };

  const FORMATS: { value: AcquireFormat; label: string }[] = [
    { value: "ebook", label: "📖 Book" },
    { value: "audiobook", label: "🎧 Audiobook" },
    { value: "both", label: "Both" },
  ];

  return (
    <Modal
      title={opts.pickFormat ? (opts.inStock ? "Add to library" : "Acquire") : opts.allowStock ? "Acquire — choose destination" : "Save to shelf"}
      onClose={() => onSettle(undefined)}
      footer={
        <>
          <Button variant="ghost" onClick={() => onSettle(undefined)}>
            Cancel
          </Button>
          <Button variant="primary" onClick={confirm} autoFocus>
            {choice === "stock" ? "Save to stock" : opts.inStock ? "Add" : "Acquire"}
          </Button>
        </>
      }
    >
      {opts.pickFormat && (
        <div className="mb-4">
          <div className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-muted">Format</div>
          <SegmentedControl<AcquireFormat>
            value={format}
            onChange={setFormat}
            options={FORMATS}
            ariaLabel="Format"
            className="w-full [&>button]:flex-1"
          />
          {opts.inStock && (
            <p className="mt-2 text-xs text-muted">
              In-stock formats are added instantly; anything not in stock is queued to fetch.
            </p>
          )}
        </div>
      )}
      <div className="space-y-1">
        {opts.pickFormat && (
          <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted">Destination</div>
        )}
        <label className="flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm transition hover:bg-surface-2">
          <input
            type="radio"
            name="shelf-pick"
            className="accent-[var(--accent)]"
            checked={choice == null}
            onChange={() => setChoice(null)}
          />
          <span className="text-text">Library only</span>
        </label>
        {shelves.map((s) => (
          <label
            key={s.id}
            className="flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm transition hover:bg-surface-2"
          >
            <input
              type="radio"
              name="shelf-pick"
              className="accent-[var(--accent)]"
              checked={choice === s.id}
              onChange={() => setChoice(s.id)}
            />
            <span className="text-text">{s.name}</span>
          </label>
        ))}
      </div>
      {opts.allowStock && (
        <label className="mt-2 flex items-start gap-2.5 rounded-xl border border-[var(--hair,var(--border))] bg-surface-2/40 px-2.5 py-2.5 text-sm transition hover:bg-surface-2">
          <input
            type="radio"
            name="shelf-pick"
            className="mt-0.5 accent-[var(--accent)]"
            checked={choice === "stock"}
            onChange={() => setChoice("stock")}
          />
          <span>
            <span className="font-medium text-text">📦 Save to operator stock</span>
            <span className="mt-0.5 block text-xs text-muted">
              Pre-fetched into the shared pool — every user can then add it to their library instantly.
            </span>
          </span>
        </label>
      )}
      <label className="mt-3 flex items-center gap-2 border-t border-[var(--hair,var(--border))] pt-3 text-xs text-muted">
        <input type="checkbox" className="accent-[var(--accent)]" checked={remember} onChange={(e) => setRemember(e.target.checked)} />
        Remember my choice
      </label>
    </Modal>
  );
}
