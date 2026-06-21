// External reading-list imports: a user imports a list/library from another service (AniList,
// Goodreads, Open Library, Hardcover, MyAnimeList, Amazon wishlist), curates which titles to keep +
// the media variant, then subscribes. Shelf then monitors the list and auto-fetches newly-added
// titles. This page hosts both the first-time add flow (with a curatable preview) and the manage
// section for existing imports. Reachable from the Watchlist tab area (sibling of Following).
import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  ApiError,
  Bookshelf,
  ListConfirmItem,
  ListPreview,
  ListProvider,
  ListSubscription,
  ListVariant,
} from "../api/client";
import { qk } from "../api/queryKeys";
import { Badge, Button, Card, EmptyState, inputCls, Modal, Select, Spinner, Toggle } from "../components/ui";
import Cover from "../components/Cover";
import { useApp } from "../store";
import { useConfirm } from "../components/confirm";

// ---------------------------------------------------------------------------------------------
// Shared variant picker (Book / Audiobook / Both) — same option set + Select chrome used by the
// Stock "Format" picker, so the per-list variant choice reads identically across the app.
// ---------------------------------------------------------------------------------------------
const VARIANT_OPTIONS = [
  { value: "ebook", label: "Book" },
  { value: "audiobook", label: "Audiobook" },
  { value: "both", label: "Both" },
];
const variantLabel = (v: ListVariant) =>
  v === "both" ? "Book + Audiobook" : v === "audiobook" ? "Audiobook" : "Book";

function VariantPicker({ value, onChange, label = "Format" }:
  { value: ListVariant; onChange: (v: ListVariant) => void; label?: string }) {
  return (
    <Select
      label={label}
      value={value}
      onChange={(v) => onChange(v as ListVariant)}
      options={VARIANT_OPTIONS}
    />
  );
}

// Per-provider hint for the list-identity field.
const REF_HINT: Record<string, string> = {
  anilist: "your AniList username",
  goodreads: "your Goodreads numeric user-id or profile URL",
  openlibrary: "your Open Library username",
  hardcover: "your Hardcover username",
  mal: "your MyAnimeList username",
  amazon_wishlist: "your PUBLIC Amazon wishlist URL",
};
const REF_PLACEHOLDER: Record<string, string> = {
  anilist: "username",
  goodreads: "12345678 or https://www.goodreads.com/user/show/12345678",
  openlibrary: "username",
  hardcover: "username",
  mal: "username",
  amazon_wishlist: "https://www.amazon.com/hz/wishlist/ls/XXXXXXXX",
};

function providerLabel(providers: ListProvider[] | undefined, key: string): string {
  return providers?.find((p) => p.key === key)?.label ?? key;
}

function shelfName(shelves: Bookshelf[] | undefined, id: number | null): string | null {
  if (id == null) return null;
  return shelves?.find((s) => s.id === id)?.name ?? null;
}

function relTime(iso: string | null): string {
  if (!iso) return "never";
  const t = new Date(iso).getTime();
  if (isNaN(t)) return "never";
  const mins = Math.round((Date.now() - t) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.round(hrs / 24);
  if (days < 30) return `${days}d ago`;
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

// ---------------------------------------------------------------------------------------------
// Add flow — a modal: pick provider → enter identity → preview → curate → confirm.
// ---------------------------------------------------------------------------------------------

// A previewed row's local editable state (selection + corrected title/author + optional override).
interface Row {
  title: string;
  author: string | null;
  cover_url: string | null;
  matchTitle: string | null;       // upstream/local catalog match title (display only)
  matchCatalogId: number | null;   // resolved catalog id, if any
  resolved: boolean;               // upstream metadata resolution has run for this (title, author)
  selected: boolean;
  variant: ListVariant | "";  // "" = use the global variant
}

function PreviewRow({ row, resolving, onChange }:
  { row: Row; resolving: boolean; onChange: (r: Row) => void }) {
  const [editing, setEditing] = useState(false);
  return (
    <div className={`flex items-start gap-3 py-2.5 ${row.selected ? "" : "opacity-55"}`}>
      <input
        type="checkbox"
        checked={row.selected}
        onChange={(e) => onChange({ ...row, selected: e.target.checked })}
        aria-label={`Include ${row.title}`}
        className="mt-1 h-4 w-4 shrink-0 accent-[var(--accent)]"
      />
      <div className="h-14 w-10 shrink-0 overflow-hidden rounded border border-border bg-surface-2">
        <Cover title={row.title} author={row.author} coverUrl={row.cover_url} small />
      </div>
      <div className="min-w-0 flex-1">
        {editing ? (
          <div className="grid gap-1.5 sm:grid-cols-2">
            <input
              className={inputCls}
              value={row.title}
              placeholder="Title"
              aria-label="Title"
              onChange={(e) => onChange({ ...row, title: e.target.value })}
            />
            <input
              className={inputCls}
              value={row.author ?? ""}
              placeholder="Author"
              aria-label="Author"
              onChange={(e) => onChange({ ...row, author: e.target.value || null })}
            />
          </div>
        ) : (
          <>
            <div className="truncate text-sm font-medium text-text" title={row.title}>{row.title}</div>
            {row.author && <div className="truncate text-xs text-muted">{row.author}</div>}
          </>
        )}
        <div className="mt-1 flex flex-wrap items-center gap-1.5">
          {resolving ? (
            <span className="inline-flex items-center gap-1.5 text-xs text-muted">
              <span className="h-3 w-3 animate-spin rounded-full border-2 border-border border-t-accent" />
              resolving…
            </span>
          ) : row.matchTitle ? (
            <Badge tone="green">matched · {row.matchTitle}</Badge>
          ) : row.resolved ? (
            <Badge tone="amber">no match found</Badge>
          ) : (
            <Badge tone="amber">will search when added</Badge>
          )}
          <button
            type="button"
            onClick={() => setEditing((v) => !v)}
            className="text-xs text-muted underline-offset-2 hover:text-text hover:underline"
          >
            {editing ? "Done" : "Edit title/author"}
          </button>
        </div>
      </div>
    </div>
  );
}

function AddListModal({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useApp((s) => s.toast);

  const providersQ = useQuery({ queryKey: qk.listImportProviders(), queryFn: api.listProviders });
  const shelvesQ = useQuery({ queryKey: qk.bookshelves(), queryFn: api.listBookshelves });
  const providers = providersQ.data?.providers ?? [];

  const [provider, setProvider] = useState<string>("");
  const [listRef, setListRef] = useState("");
  const [listName, setListName] = useState(""); // sub-list, "" when provider has none
  const [displayName, setDisplayName] = useState("");
  const [variant, setVariant] = useState<ListVariant>("ebook");
  const [targetShelf, setTargetShelf] = useState<string>(""); // "" = none
  const [autoSeries, setAutoSeries] = useState(false);
  const [autoFollowSeries, setAutoFollowSeries] = useState(false);
  const [rows, setRows] = useState<Row[] | null>(null); // populated after a preview
  const [previewErr, setPreviewErr] = useState<string | null>(null);

  // --- Upstream metadata resolution gate -------------------------------------------------------
  // After a preview, we resolve the SELECTED titles against upstream APIs (in chunks; server-capped
  // at 30/req) before the import can be finalized. While any selected row is unresolved the Add
  // button stays disabled. A row's identity is its (title, author) — corrected titles re-resolve.
  const rowKey = (r: { title: string; author: string | null }) => `${r.title}␟${r.author ?? ""}`;
  const resolvingRef = useRef<Set<string>>(new Set()); // keys currently in-flight (a chunk)
  const [resolveTick, setResolveTick] = useState(0);   // bump to re-render the resolving indicator
  const [resolveWarn, setResolveWarn] = useState(false); // a chunk failed → subtle warning

  const selectedUnresolved = (rows ?? []).filter((r) => r.selected && !r.resolved);
  const resolving = selectedUnresolved.length > 0;
  const selResolved = (rows ?? []).filter((r) => r.selected && r.resolved).length;
  const selTotal = (rows ?? []).filter((r) => r.selected).length;
  const isRowResolving = (r: Row) => resolvingRef.current.has(rowKey(r));

  // Drive the chunk loop: whenever there are selected unresolved rows and nothing is in-flight,
  // take the next ~10 and resolve them. Runs again as selection/edits create new unresolved rows.
  useEffect(() => {
    if (!rows || resolvingRef.current.size > 0) return;
    const batch = rows.filter((r) => r.selected && !r.resolved).slice(0, 10);
    if (batch.length === 0) return;
    const keys = batch.map(rowKey);
    keys.forEach((k) => resolvingRef.current.add(k));
    setResolveTick((t) => t + 1);
    let cancelled = false;
    (async () => {
      let byKey: Map<string, { matchTitle: string | null; matchCatalogId: number | null }> | null = null;
      try {
        const out = await api.resolveList(batch.map((r) => ({ title: r.title, author: r.author })));
        byKey = new Map(
          out.map((o) => [rowKey({ title: o.title, author: o.author }),
            { matchTitle: o.match_title, matchCatalogId: o.match_catalog_id }]),
        );
      } catch {
        setResolveWarn(true); // skip/continue — still let the user finish
      } finally {
        if (!cancelled) {
          keys.forEach((k) => resolvingRef.current.delete(k));
          setRows((prev) =>
            prev
              ? prev.map((r) => {
                  if (!keys.includes(rowKey(r))) return r;
                  const hit = byKey?.get(rowKey(r));
                  return {
                    ...r,
                    resolved: true,
                    ...(hit ? { matchTitle: hit.matchTitle, matchCatalogId: hit.matchCatalogId } : {}),
                  };
                })
              : prev,
          );
          setResolveTick((t) => t + 1);
        }
      }
    })();
    return () => { cancelled = true; };
    // resolveTick is the loop trigger after each chunk completes; rows drives new-selection resolves.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows, resolveTick]);

  // Default the provider (and its first sub-list) once they load.
  const current = providers.find((p) => p.key === provider);
  useEffect(() => {
    if (!provider && providers.length) {
      setProvider(providers[0].key);
      setListName(providers[0].lists[0] ?? "");
    }
  }, [provider, providers]);

  // Keep the display-name default in sync with provider/ref until the user types their own.
  const [dnTouched, setDnTouched] = useState(false);
  const defaultDisplay = useMemo(() => {
    const lbl = providerLabel(providers, provider);
    const ref = listRef.trim();
    return ref ? `${lbl} — ${ref}` : lbl;
  }, [providers, provider, listRef]);
  const effectiveDisplay = dnTouched ? displayName : defaultDisplay;

  function onProviderChange(key: string) {
    setProvider(key);
    setRows(null);
    setPreviewErr(null);
    const p = providers.find((x) => x.key === key);
    setListName(p?.lists[0] ?? "");
  }

  const preview = useMutation({
    mutationFn: () =>
      api.previewList({
        provider,
        list_ref: listRef.trim(),
        list_name: current?.lists.length ? listName || undefined : undefined,
      }),
    onSuccess: (p: ListPreview) => {
      setPreviewErr(null);
      setRows(
        p.items.map((it) => ({
          title: it.title,
          author: it.author,
          cover_url: it.cover_url,
          matchTitle: it.match_title,
          matchCatalogId: it.match_catalog_id,
          resolved: false, // upstream resolution still runs (preview is a quick LOCAL match only)
          selected: true,
          variant: "" as const,
        })),
      );
    },
    onError: (e) => {
      setRows(null);
      setPreviewErr((e as Error).message);
    },
  });

  const confirm = useMutation({
    mutationFn: () => {
      const items: ListConfirmItem[] = (rows ?? []).map((r) => ({
        title: r.title.trim(),
        author: r.author?.trim() || null,
        selected: r.selected,
        ...(r.variant ? { variant: r.variant } : {}),
      }));
      return api.createImport({
        provider,
        list_ref: listRef.trim(),
        list_name: current?.lists.length ? listName || undefined : undefined,
        display_name: effectiveDisplay.trim(),
        variant,
        target_shelf_id: targetShelf ? Number(targetShelf) : undefined,
        auto_series: autoSeries,
        auto_follow_series: autoFollowSeries,
        items,
      });
    },
    onSuccess: (sub: ListSubscription) => {
      qc.invalidateQueries({ queryKey: qk.listImports() });
      const n = (rows ?? []).filter((r) => r.selected).length;
      toast(`Added “${sub.display_name}” — ${n} title${n === 1 ? "" : "s"} fetching`, "success");
      onClose();
    },
    onError: (e) => {
      const msg = e instanceof ApiError && e.status === 409 ? "You've already added this list." : (e as Error).message;
      toast(msg, "error");
    },
  });

  // A title/author edit changes the row's identity → mark it unresolved so the gate re-resolves it.
  const setRow = (i: number, r: Row) =>
    setRows((prev) =>
      prev
        ? prev.map((x, j) => {
            if (j !== i) return x;
            const identityChanged = r.title !== x.title || (r.author ?? "") !== (x.author ?? "");
            return identityChanged ? { ...r, resolved: false } : r;
          })
        : prev,
    );
  const selectAll = (on: boolean) => setRows((prev) => (prev ? prev.map((r) => ({ ...r, selected: on })) : prev));
  const selectedCount = rows?.filter((r) => r.selected).length ?? 0;

  const canPreview = !!provider && listRef.trim().length > 0 && !preview.isPending;
  const canConfirm =
    !!rows && selectedCount > 0 && !resolving && effectiveDisplay.trim().length > 0 && !confirm.isPending;

  return (
    <Modal
      variant="fullscreen-sheet"
      width="max-w-2xl"
      onClose={onClose}
      title="Import a reading list"
      footer={
        <div className="flex items-center justify-between gap-2">
          <span className="flex items-center gap-2 text-xs text-muted">
            {resolving ? (
              <>
                <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-border border-t-accent" />
                Fetching metadata… {selResolved}/{selTotal}
              </>
            ) : (
              rows ? `${selectedCount} of ${rows.length} selected` : "Preview a list to continue"
            )}
            {resolveWarn && !resolving && (
              <span className="text-amber-600 dark:text-amber-400">· some titles couldn't be resolved</span>
            )}
          </span>
          <div className="flex gap-2">
            <Button size="sm" variant="ghost" onClick={onClose}>Cancel</Button>
            <Button size="sm" variant="primary" disabled={!canConfirm} onClick={() => confirm.mutate()}>
              {confirm.isPending
                ? "Adding…"
                : resolving
                  ? `Fetching metadata… ${selResolved}/${selTotal}`
                  : "Add & start fetching"}
            </Button>
          </div>
        </div>
      }
    >
      {providersQ.isLoading ? (
        <Spinner label="Loading providers…" />
      ) : (
        <div className="space-y-4">
          {/* Identity */}
          <div className="grid gap-3 sm:grid-cols-2">
            <Select
              label="Service"
              value={provider}
              onChange={onProviderChange}
              options={providers.map((p) => ({ value: p.key, label: p.label }))}
            />
            {!!current?.lists.length && (
              <Select
                label="List"
                value={listName}
                onChange={(v) => { setListName(v); setRows(null); }}
                options={current.lists.map((l) => ({ value: l, label: l }))}
              />
            )}
            <label className="block sm:col-span-2">
              <div className="mb-1 text-xs text-muted">
                List identity <span className="text-muted/80">— {REF_HINT[provider] ?? "username or list URL"}</span>
              </div>
              <input
                className={inputCls}
                value={listRef}
                placeholder={REF_PLACEHOLDER[provider] ?? ""}
                spellCheck={false}
                onChange={(e) => { setListRef(e.target.value); setRows(null); }}
              />
            </label>
          </div>

          <div>
            <Button variant="outline" disabled={!canPreview} onClick={() => preview.mutate()}>
              {preview.isPending ? "Reading list…" : rows ? "Re-preview" : "Preview"}
            </Button>
            {previewErr && <p className="mt-2 text-sm text-red-500">{previewErr}</p>}
          </div>

          {/* Settings + curated preview, shown once a preview succeeds */}
          {rows && (
            <>
              <div className="grid gap-3 border-t border-border pt-4 sm:grid-cols-2">
                <label className="block sm:col-span-2">
                  <div className="mb-1 text-xs text-muted">Display name</div>
                  <input
                    className={inputCls}
                    value={effectiveDisplay}
                    onChange={(e) => { setDnTouched(true); setDisplayName(e.target.value); }}
                  />
                </label>
                <VariantPicker value={variant} onChange={setVariant} />
                <Select
                  label="Add to bookshelf (optional)"
                  value={targetShelf}
                  onChange={setTargetShelf}
                  options={[
                    { value: "", label: "None" },
                    ...(shelvesQ.data ?? []).map((s) => ({ value: String(s.id), label: s.name })),
                  ]}
                />
                <div className="space-y-2 sm:col-span-2">
                  <div className="flex items-center justify-between gap-3">
                    <span className="text-sm text-text">Also fetch the rest of each title's series</span>
                    <Toggle checked={autoSeries} onChange={setAutoSeries} />
                  </div>
                  <div className="flex items-center justify-between gap-3">
                    <span className="text-sm text-text">Follow each series for new volumes</span>
                    <Toggle checked={autoFollowSeries} onChange={setAutoFollowSeries} />
                  </div>
                  <p className="text-xs text-muted">
                    Applies per fetched title that's part of a series. The first fills in earlier and later
                    volumes now; following keeps future volumes coming even after they leave the list.
                  </p>
                </div>
              </div>

              <div className="border-t border-border pt-3">
                <div className="mb-1 flex items-center justify-between gap-2">
                  <span className="text-sm font-medium text-text">{rows.length} titles</span>
                  <div className="flex gap-2">
                    <Button size="sm" variant="ghost" onClick={() => selectAll(true)}>Select all</Button>
                    <Button size="sm" variant="ghost" onClick={() => selectAll(false)}>Deselect all</Button>
                  </div>
                </div>
                <p className="mb-1 text-xs text-muted">
                  Unchecked titles are remembered but never fetched. Checked titles start fetching now.
                </p>
                {rows.length === 0 ? (
                  <EmptyState title="That list looks empty" hint="Nothing was returned for this list." />
                ) : (
                  <div className="divide-y divide-border">
                    {rows.map((r, i) => (
                      <PreviewRow key={`${i}-${r.title}`} row={r} resolving={isRowResolving(r)}
                        onChange={(nr) => setRow(i, nr)} />
                    ))}
                  </div>
                )}
              </div>
            </>
          )}
        </div>
      )}
    </Modal>
  );
}

// ---------------------------------------------------------------------------------------------
// Edit settings of an existing import (variant / target shelf / name / active).
// ---------------------------------------------------------------------------------------------
function EditImportModal({ sub, onClose }: { sub: ListSubscription; onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useApp((s) => s.toast);
  const shelvesQ = useQuery({ queryKey: qk.bookshelves(), queryFn: api.listBookshelves });

  const [displayName, setDisplayName] = useState(sub.display_name);
  const [variant, setVariant] = useState<ListVariant>(sub.variant);
  const [targetShelf, setTargetShelf] = useState<string>(sub.target_shelf_id != null ? String(sub.target_shelf_id) : "");
  const [autoSeries, setAutoSeries] = useState(sub.auto_series);
  const [autoFollowSeries, setAutoFollowSeries] = useState(sub.auto_follow_series);
  const [active, setActive] = useState(sub.active);

  const save = useMutation({
    mutationFn: () =>
      api.patchImport(sub.id, {
        display_name: displayName.trim() || undefined,
        variant,
        target_shelf_id: targetShelf ? Number(targetShelf) : null,
        auto_series: autoSeries,
        auto_follow_series: autoFollowSeries,
        active,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.listImports() });
      toast("Saved", "success");
      onClose();
    },
    onError: (e) => toast((e as Error).message, "error"),
  });

  return (
    <Modal
      onClose={onClose}
      title="Import settings"
      footer={
        <>
          <Button size="sm" variant="ghost" onClick={onClose}>Cancel</Button>
          <Button size="sm" variant="primary" disabled={save.isPending} onClick={() => save.mutate()}>
            {save.isPending ? "Saving…" : "Save"}
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <label className="block">
          <div className="mb-1 text-xs text-muted">Display name</div>
          <input className={inputCls} value={displayName} onChange={(e) => setDisplayName(e.target.value)} />
        </label>
        <VariantPicker value={variant} onChange={setVariant} />
        <Select
          label="Bookshelf"
          value={targetShelf}
          onChange={setTargetShelf}
          options={[
            { value: "", label: "None" },
            ...(shelvesQ.data ?? []).map((s) => ({ value: String(s.id), label: s.name })),
          ]}
        />
        <div className="space-y-2 border-t border-border pt-2">
          <div className="flex items-center justify-between gap-3">
            <span className="text-sm text-text">Also fetch the rest of each title's series</span>
            <Toggle checked={autoSeries} onChange={setAutoSeries} />
          </div>
          <div className="flex items-center justify-between gap-3">
            <span className="text-sm text-text">Follow each series for new volumes</span>
            <Toggle checked={autoFollowSeries} onChange={setAutoFollowSeries} />
          </div>
          <p className="text-xs text-muted">
            Applies per fetched title that's part of a series. Following keeps future volumes coming even
            after they leave the list.
          </p>
        </div>
        <div className="flex items-center justify-between pt-1">
          <span className="text-sm text-text">Active</span>
          <Toggle checked={active} onChange={setActive} />
        </div>
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------------------------------------
// Netflix-style horizontally-scrolling strip of the list's current title covers. Lazy: the /items
// query only runs once the strip is opened (re-fetching a big list — e.g. an Amazon wishlist — can
// take a few seconds), so we never fetch every list on page load.
// ---------------------------------------------------------------------------------------------
function ListCoverStrip({ id }: { id: number }) {
  const q = useQuery({
    queryKey: qk.listImportItems(id),
    queryFn: () => api.listItems(id),
    staleTime: 5 * 60 * 1000, // covers don't change minute-to-minute; avoid re-fetching on every open
  });

  if (q.isLoading) return <div className="px-4 pb-3"><Spinner label="Loading titles…" /></div>;
  if (q.error) return <p className="px-4 pb-3 text-xs text-red-500">{(q.error as Error).message}</p>;

  const items = q.data?.items ?? [];
  if (items.length === 0) {
    return <p className="px-4 pb-3 text-xs text-muted">No titles on this list right now.</p>;
  }
  return (
    <div className="flex gap-3 overflow-x-auto px-4 pb-3">
      {items.map((it, i) => (
        <div key={`${i}-${it.title}`} className="w-[72px] shrink-0">
          <div className="h-[104px] w-[72px] overflow-hidden rounded border border-border bg-surface-2">
            <Cover title={it.title} author={it.author} coverUrl={it.cover_url} small />
          </div>
          <div className="mt-1 line-clamp-2 text-[11px] leading-snug text-muted" title={it.title}>
            {it.title}
          </div>
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------------------------
// One row in the manage list.
// ---------------------------------------------------------------------------------------------
function ImportRow({
  sub,
  providers,
  shelves,
  onEdit,
}: {
  sub: ListSubscription;
  providers: ListProvider[] | undefined;
  shelves: Bookshelf[] | undefined;
  onEdit: () => void;
}) {
  const qc = useQueryClient();
  const toast = useApp((s) => s.toast);
  const confirm = useConfirm();

  const invalidate = () => qc.invalidateQueries({ queryKey: qk.listImports() });

  const sync = useMutation({
    mutationFn: () => api.syncImport(sub.id),
    onSuccess: (s) => {
      invalidate();
      toast(s.last_error ? `Checked with an error: ${s.last_error}` : `Checked “${s.display_name}”`,
        s.last_error ? "error" : "success");
    },
    onError: (e) => toast((e as Error).message, "error"),
  });
  const toggleActive = useMutation({
    mutationFn: (active: boolean) => api.patchImport(sub.id, { active }),
    onSuccess: invalidate,
    onError: (e) => toast((e as Error).message, "error"),
  });
  const remove = useMutation({
    mutationFn: () => api.deleteImport(sub.id),
    onSuccess: () => { invalidate(); toast(`Removed “${sub.display_name}”`, "success"); },
    onError: (e) => toast((e as Error).message, "error"),
  });

  async function onDelete() {
    if (await confirm({
      title: "Remove import",
      message: `Stop monitoring “${sub.display_name}”? Already-fetched titles stay; no new ones will be added.`,
      confirmText: "Remove",
      danger: true,
    })) remove.mutate();
  }

  const shelf = shelfName(shelves, sub.target_shelf_id);

  return (
    <div className={sub.active ? "" : "opacity-60"}>
      <div className="flex flex-col gap-2 px-4 py-3 sm:flex-row sm:items-center sm:gap-4">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="truncate font-medium text-text">{sub.display_name}</span>
            <Badge>{providerLabel(providers, sub.provider)}</Badge>
            {sub.list_name && <Badge tone="violet">{sub.list_name}</Badge>}
            <Badge tone="amber">{variantLabel(sub.variant)}</Badge>
            {sub.auto_series && (
              <span title="Also fetches the rest of each title's series"><Badge tone="violet">+ series</Badge></span>
            )}
            {sub.auto_follow_series && (
              <span title="Follows each series for new volumes"><Badge tone="violet">following series</Badge></span>
            )}
            {!sub.active && <Badge>paused</Badge>}
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-muted">
            <span>checked {relTime(sub.last_checked_at)}</span>
            {sub.auto_added > 0 && <span>{sub.auto_added} auto-added</span>}
            {shelf && <span>→ {shelf}</span>}
          </div>
          {sub.last_error && <div className="mt-1 text-xs text-red-500">⚠ {sub.last_error}</div>}
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          <span title={sub.active ? "Active — paused if off" : "Paused"}>
            <Toggle checked={sub.active} onChange={(on) => toggleActive.mutate(on)} />
          </span>
          <Button size="sm" variant="outline" disabled={sync.isPending || !sub.active}
            onClick={() => sync.mutate()} title={sub.active ? "Re-check now" : "Paused"}>
            {sync.isPending ? "Checking…" : "Check now"}
          </Button>
          <Button size="sm" variant="ghost" onClick={onEdit}>Edit</Button>
          <Button size="icon" variant="ghost" aria-label="Remove" title="Remove" onClick={onDelete}>✕</Button>
        </div>
      </div>
      <ListCoverStrip id={sub.id} />
    </div>
  );
}

// ---------------------------------------------------------------------------------------------
// Page.
// ---------------------------------------------------------------------------------------------
export default function ListImports() {
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<ListSubscription | null>(null);

  const importsQ = useQuery({ queryKey: qk.listImports(), queryFn: api.listImports });
  const providersQ = useQuery({ queryKey: qk.listImportProviders(), queryFn: api.listProviders });
  const shelvesQ = useQuery({ queryKey: qk.bookshelves(), queryFn: api.listBookshelves });

  const subs = importsQ.data ?? [];

  return (
    <main className="mx-auto max-w-3xl px-4 py-8">
      <div className="mb-1 flex items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold">List imports</h1>
        <Button variant="primary" onClick={() => setAdding(true)}>Import a list</Button>
      </div>
      <p className="mb-6 text-sm text-muted">
        Import a reading list or library from AniList, Goodreads, Open Library, Hardcover, MyAnimeList,
        or an Amazon wishlist. Shelf keeps watching it — new titles you add there are fetched here
        automatically.
      </p>

      {importsQ.isLoading ? (
        <Spinner label="Loading…" />
      ) : importsQ.error ? (
        <p className="text-sm text-red-500">{(importsQ.error as Error).message}</p>
      ) : subs.length === 0 ? (
        <EmptyState
          title="No imports yet"
          hint="Import a list to pull in its titles and keep it in sync as you add more."
          action={<Button variant="primary" onClick={() => setAdding(true)}>Import a list</Button>}
        />
      ) : (
        <Card className="divide-y divide-border">
          {subs.map((sub) => (
            <ImportRow
              key={sub.id}
              sub={sub}
              providers={providersQ.data?.providers}
              shelves={shelvesQ.data}
              onEdit={() => setEditing(sub)}
            />
          ))}
        </Card>
      )}

      {adding && <AddListModal onClose={() => setAdding(false)} />}
      {editing && <EditImportModal sub={editing} onClose={() => setEditing(null)} />}
    </main>
  );
}
