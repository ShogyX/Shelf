// External reading-list imports: a user imports a list/library from another service (AniList,
// Goodreads, Open Library, Hardcover, MyAnimeList, Amazon wishlist), curates which titles to keep +
// the media variant, then subscribes. Shelf then monitors the list and auto-fetches newly-added
// titles. This page hosts both the first-time add flow (with a curatable preview) and the manage
// section for existing imports. Reachable from the Watchlist tab area (sibling of Following).
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  ApiError,
  Bookshelf,
  ListConfirmItem,
  ListMode,
  ListPreview,
  ListProvider,
  ListSubscription,
  ListVariant,
} from "../api/client";
import { qk } from "../api/queryKeys";
import { Badge, Button, Card, EmptyState, inputCls, Modal, Select, Spinner, Toggle } from "../components/ui";
import Cover from "../components/Cover";
import { useApp } from "../store";
import { useIsAdmin } from "../auth";
import { useConfirm } from "../components/confirm";

// ---------------------------------------------------------------------------------------------
// Shared variant picker (Book / Audiobook / Both) — same option set + Select chrome used by the
// Stock "Format" picker, so the per-list variant choice reads identically across the app.
// ---------------------------------------------------------------------------------------------
const buildVariantOptions = (t: TFunction) => [
  { value: "ebook", label: t("listimports.variantBook") },
  { value: "audiobook", label: t("listimports.variantAudiobook") },
  { value: "both", label: t("listimports.variantBoth") },
];
const variantLabel = (t: TFunction, v: ListVariant) =>
  v === "both" ? t("listimports.variantBookAudiobook") : v === "audiobook" ? t("listimports.variantAudiobook") : t("listimports.variantBook");

function VariantPicker({ value, onChange, label }:
  { value: ListVariant; onChange: (v: ListVariant) => void; label?: string }) {
  const { t } = useTranslation();
  return (
    <Select
      label={label ?? t("listimports.format")}
      value={value}
      onChange={(v) => onChange(v as ListVariant)}
      options={buildVariantOptions(t)}
    />
  );
}

// Per-provider hint for the list-identity field.
const buildRefHint = (t: TFunction): Record<string, string> => ({
  anilist: t("listimports.refHint.anilist"),
  goodreads: t("listimports.refHint.goodreads"),
  openlibrary: t("listimports.refHint.openlibrary"),
  hardcover: t("listimports.refHint.hardcover"),
  mal: t("listimports.refHint.mal"),
  amazon_wishlist: t("listimports.refHint.amazon_wishlist"),
});
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

// Sentinel shelf-select value for "create a new shelf for this list".
const NEW_SHELF = "__new__";

/** Resolve the import's target shelf id: an existing shelf, or — when "New shelf" is chosen — create
 *  one (named for the list) and return its id. null = no shelf (lands in the main library). */
async function resolveTargetShelf(sel: string, newName: string): Promise<number | null> {
  if (sel === NEW_SHELF) {
    const name = newName.trim();
    if (!name) return null;
    return (await api.createBookshelf({ name })).id;
  }
  return sel ? Number(sel) : null;
}

function relTime(t: TFunction, iso: string | null): string {
  if (!iso) return t("listimports.never");
  const ms = new Date(iso).getTime();
  if (isNaN(ms)) return t("listimports.never");
  const mins = Math.round((Date.now() - ms) / 60000);
  if (mins < 1) return t("listimports.justNow");
  if (mins < 60) return t("listimports.minutesAgo", { count: mins });
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return t("listimports.hoursAgo", { count: hrs });
  const days = Math.round(hrs / 24);
  if (days < 30) return t("listimports.daysAgo", { count: days });
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
  mediaKind: string;          // text | comic — drives strict content-type matching vs crawled sources
}

function PreviewRow({ row, resolving, onChange }:
  { row: Row; resolving: boolean; onChange: (r: Row) => void }) {
  const { t } = useTranslation();
  const [editing, setEditing] = useState(false);
  return (
    <div className={`flex items-start gap-3 py-2.5 ${row.selected ? "" : "opacity-55"}`}>
      <input
        type="checkbox"
        checked={row.selected}
        onChange={(e) => onChange({ ...row, selected: e.target.checked })}
        aria-label={t("listimports.row.includeAria", { title: row.title })}
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
              placeholder={t("listimports.row.title")}
              aria-label={t("listimports.row.title")}
              onChange={(e) => onChange({ ...row, title: e.target.value })}
            />
            <input
              className={inputCls}
              value={row.author ?? ""}
              placeholder={t("listimports.row.author")}
              aria-label={t("listimports.row.author")}
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
              {t("listimports.row.resolving")}
            </span>
          ) : row.matchTitle ? (
            <Badge tone="green">{t("listimports.row.matched", { title: row.matchTitle })}</Badge>
          ) : row.resolved ? (
            <Badge tone="amber">{t("listimports.row.noMatch")}</Badge>
          ) : (
            <Badge tone="amber">{t("listimports.row.willSearch")}</Badge>
          )}
          <button
            type="button"
            onClick={() => setEditing((v) => !v)}
            className="text-xs text-muted underline-offset-2 hover:text-text hover:underline"
          >
            {editing ? t("listimports.row.done") : t("listimports.row.editTitleAuthor")}
          </button>
        </div>
      </div>
    </div>
  );
}

// Lists larger than this skip hand-curation and ingest in the background (matches the server default).
const BIG_LIST_THRESHOLD = 25;

export function AddListModal({ onClose }: { onClose: () => void }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useApp((s) => s.toast);

  const isAdmin = useIsAdmin();
  const providersQ = useQuery({ queryKey: qk.listImportProviders(), queryFn: api.listProviders });
  const shelvesQ = useQuery({ queryKey: qk.bookshelves(), queryFn: api.listBookshelves });
  const stockQ = useQuery({ queryKey: qk.stockSummary(), queryFn: api.getStockSummary, enabled: isAdmin });
  const allowStock = isAdmin && !!stockQ.data?.configured;
  const providers = providersQ.data?.providers ?? [];

  const [provider, setProvider] = useState<string>("");
  const [listRef, setListRef] = useState("");
  const [listName, setListName] = useState(""); // sub-list, "" when provider has none
  const [displayName, setDisplayName] = useState("");
  const [variant, setVariant] = useState<ListVariant>("ebook");
  const [mode, setMode] = useState<ListMode>("download"); // download files vs catalogue-only
  const [targetShelf, setTargetShelf] = useState<string>(""); // "" = none, NEW_SHELF = create one
  const [newShelf, setNewShelf] = useState("");
  const [toStock, setToStock] = useState(false); // admin: fetch into shared operator stock, not a library
  const [autoSeries, setAutoSeries] = useState(false);
  const [autoFollowSeries, setAutoFollowSeries] = useState(false);
  const [rows, setRows] = useState<Row[] | null>(null); // populated after a preview
  // Big lists (a sample, or > BIG_LIST_THRESHOLD titles) skip per-item curation + the resolve gate and
  // ingest the WHOLE list in the background — curating 76k titles by hand isn't practical.
  const [big, setBig] = useState(false);
  const [listTotal, setListTotal] = useState(0);
  const [truncated, setTruncated] = useState(false); // listTotal is a lower bound (sample capped)
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
    if (big || !rows || resolvingRef.current.size > 0) return; // big lists resolve in the background, not here
    const batch = rows.filter((r) => r.selected && !r.resolved).slice(0, 10);
    if (batch.length === 0) return;
    const keys = batch.map(rowKey);
    keys.forEach((k) => resolvingRef.current.add(k));
    setResolveTick((t) => t + 1);
    let cancelled = false;
    (async () => {
      let byKey: Map<string, { matchTitle: string | null; matchCatalogId: number | null }> | null = null;
      try {
        const out = await api.resolveList(
          batch.map((r) => ({ title: r.title, author: r.author, media_kind: r.mediaKind })));
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
      const isBig = p.truncated || p.total > BIG_LIST_THRESHOLD || p.items.length > BIG_LIST_THRESHOLD;
      setBig(isBig);
      setTruncated(p.truncated);
      setListTotal(p.truncated ? p.total : p.items.length);
      if (isBig) setMode("catalog"); // safe default for a large list — don't mass-download by surprise
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
          mediaKind: it.media_kind || "text",
        })),
      );
    },
    onError: (e) => {
      setRows(null);
      setPreviewErr((e as Error).message);
    },
  });

  const confirm = useMutation({
    mutationFn: async () => {
      // Big lists: the server fetches + ingests the WHOLE list itself (no giant per-item payload).
      const items: ListConfirmItem[] | undefined = big
        ? undefined
        : (rows ?? []).map((r) => ({
            title: r.title.trim(),
            author: r.author?.trim() || null,
            selected: r.selected,
            ...(r.variant ? { variant: r.variant } : {}),
          }));
      const downloadToStock = mode === "download" && toStock;
      const targetId = downloadToStock ? null : await resolveTargetShelf(targetShelf, newShelf);
      return api.createImport({
        provider,
        list_ref: listRef.trim(),
        list_name: current?.lists.length ? listName || undefined : undefined,
        display_name: effectiveDisplay.trim(),
        variant,
        mode,
        target_shelf_id: targetId ?? undefined,
        to_stock: downloadToStock,
        auto_series: autoSeries,
        auto_follow_series: autoFollowSeries,
        import_all: big,
        items,
      });
    },
    onSuccess: (sub: ListSubscription) => {
      qc.invalidateQueries({ queryKey: qk.listImports() });
      const verb = mode === "catalog" ? t("listimports.add.toastVerbCataloguing") : t("listimports.add.toastVerbFetching");
      const n = big ? listTotal : (rows ?? []).filter((r) => r.selected).length;
      const count = big && truncated ? `${listTotal}+` : `${n}`;
      toast(t("listimports.add.toastAdded", { name: sub.display_name, count: t("listimports.add.titlesCount", { count }), verb }), "success");
      onClose();
    },
    onError: (e) => {
      const msg = e instanceof ApiError && e.status === 409 ? t("listimports.add.already") : (e as Error).message;
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

  // A "new shelf" choice needs a name — only relevant when downloading into a library (not stock/catalog).
  const needsShelfName =
    mode === "download" && !toStock && targetShelf === NEW_SHELF && !newShelf.trim();
  const canPreview = !!provider && listRef.trim().length > 0 && !preview.isPending;
  const canConfirm =
    !!rows && (big || selectedCount > 0) && (big || !resolving) && effectiveDisplay.trim().length > 0 &&
    !needsShelfName && !confirm.isPending;

  return (
    <Modal
      variant="fullscreen-sheet"
      width="max-w-2xl"
      onClose={onClose}
      title={t("listimports.add.title")}
      footer={
        <div className="flex items-center justify-between gap-2">
          <span className="flex items-center gap-2 text-xs text-muted">
            {big ? (
              t("listimports.add.bigSummary", {
                total: truncated ? `${listTotal}+` : listTotal,
                mode: mode === "catalog" ? t("listimports.add.modeCatalogueOnly") : t("listimports.add.modeDownload"),
              })
            ) : resolving ? (
              <>
                <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-border border-t-accent" />
                {t("listimports.add.fetchingMeta", { done: selResolved, total: selTotal })}
              </>
            ) : (
              rows ? t("listimports.add.selectedOfTotal", { selected: selectedCount, total: rows.length }) : t("listimports.add.previewToContinue")
            )}
            {resolveWarn && !resolving && !big && (
              <span className="text-amber-600 dark:text-amber-400">{t("listimports.add.someUnresolved")}</span>
            )}
          </span>
          <div className="flex gap-2">
            <Button size="sm" variant="ghost" onClick={onClose}>{t("common.cancel")}</Button>
            <Button size="sm" variant="primary" disabled={!canConfirm} onClick={() => confirm.mutate()}>
              {confirm.isPending
                ? t("listimports.add.adding")
                : !big && resolving
                  ? t("listimports.add.fetchingMeta", { done: selResolved, total: selTotal })
                  : mode === "catalog"
                    ? t("listimports.add.addCatalogue")
                    : t("listimports.add.addDownload")}
            </Button>
          </div>
        </div>
      }
    >
      {providersQ.isLoading ? (
        <Spinner label={t("listimports.add.loadingProviders")} />
      ) : (
        <div className="space-y-4">
          {/* Identity */}
          <div className="grid gap-3 sm:grid-cols-2">
            <Select
              label={t("listimports.add.service")}
              value={provider}
              onChange={onProviderChange}
              options={providers.map((p) => ({ value: p.key, label: p.label }))}
            />
            {!!current?.lists.length && (
              <Select
                label={t("listimports.add.list")}
                value={listName}
                onChange={(v) => { setListName(v); setRows(null); }}
                options={current.lists.map((l) => ({ value: l, label: l }))}
              />
            )}
            <label className="block sm:col-span-2">
              <div className="mb-1 text-xs text-muted">
                {t("listimports.add.listIdentity")} <span className="text-muted/80">— {buildRefHint(t)[provider] ?? t("listimports.refHint.fallback")}</span>
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
              {preview.isPending ? t("listimports.add.readingList") : rows ? t("listimports.add.rePreview") : t("listimports.add.preview")}
            </Button>
            {previewErr && <p className="mt-2 text-sm text-red-500">{previewErr}</p>}
          </div>

          {/* Settings + curated preview, shown once a preview succeeds */}
          {rows && (
            <>
              <div className="grid gap-3 border-t border-border pt-4 sm:grid-cols-2">
                <label className="block sm:col-span-2">
                  <div className="mb-1 text-xs text-muted">{t("listimports.add.displayName")}</div>
                  <input
                    className={inputCls}
                    value={effectiveDisplay}
                    onChange={(e) => { setDnTouched(true); setDisplayName(e.target.value); }}
                  />
                </label>

                {/* The core choice: download each title's file, or just catalogue it for Discovery. */}
                <div className="sm:col-span-2">
                  <div className="mb-1 text-xs text-muted">{t("listimports.add.whenIngested")}</div>
                  <div className="inline-flex rounded-[11px] border border-[var(--hair-strong,var(--border))] bg-surface-2 p-0.5" role="group" aria-label={t("listimports.add.ingestModeAria")}>
                    {([["download", t("listimports.add.download")], ["catalog", t("listimports.add.catalogueOnly")]] as const).map(([m, label]) => (
                      <button
                        key={m}
                        type="button"
                        aria-pressed={mode === m}
                        onClick={() => setMode(m)}
                        className={`rounded-[9px] px-3 py-1.5 text-xs font-semibold transition ${
                          mode === m ? "bg-accent text-accent-fg shadow-sm" : "text-[var(--text-soft,var(--muted))] hover:text-text"
                        }`}
                      >
                        {label}
                      </button>
                    ))}
                  </div>
                  <p className="mt-1 text-xs text-muted">
                    {mode === "catalog"
                      ? t("listimports.add.catalogueHint")
                      : t("listimports.add.downloadHint")}
                  </p>
                </div>

                {mode === "download" && (
                  <>
                    <VariantPicker value={variant} onChange={setVariant} />
                    {toStock ? (
                      <div className="flex items-end pb-1 text-xs text-muted">
                        {t("listimports.add.toStockHint")}
                      </div>
                    ) : (
                      <div>
                        <Select
                          label={t("listimports.add.addToBookshelf")}
                          value={targetShelf}
                          onChange={setTargetShelf}
                          options={[
                            { value: "", label: t("listimports.add.noneMainLibrary") },
                            ...(shelvesQ.data ?? []).map((s) => ({ value: String(s.id), label: s.name })),
                            { value: NEW_SHELF, label: t("listimports.add.newShelfOption") },
                          ]}
                        />
                        {targetShelf === NEW_SHELF && (
                          <input
                            className={`${inputCls} mt-2`}
                            value={newShelf}
                            onChange={(e) => setNewShelf(e.target.value)}
                            placeholder={t("listimports.add.newShelfPlaceholder", { name: effectiveDisplay || t("listimports.add.importsDefault") })}
                          />
                        )}
                      </div>
                    )}
                    {allowStock && (
                      <div className="flex items-center justify-between gap-3 sm:col-span-2">
                        <span className="text-sm text-text">
                          {t("listimports.add.sendToStock")}
                          <span className="block text-xs text-muted">{t("listimports.add.sendToStockHint")}</span>
                        </span>
                        <Toggle checked={toStock} onChange={setToStock} />
                      </div>
                    )}
                    <div className="space-y-2 sm:col-span-2">
                      <div className="flex items-center justify-between gap-3">
                        <span className="text-sm text-text">{t("listimports.add.alsoFetchSeries")}</span>
                        <Toggle checked={autoSeries} onChange={setAutoSeries} />
                      </div>
                      <div className="flex items-center justify-between gap-3">
                        <span className="text-sm text-text">{t("listimports.add.followSeries")}</span>
                        <Toggle checked={autoFollowSeries} onChange={setAutoFollowSeries} />
                      </div>
                      <p className="text-xs text-muted">
                        {t("listimports.add.seriesHint")}
                      </p>
                    </div>
                  </>
                )}
              </div>

              <div className="border-t border-border pt-3">
                {big ? (
                  <div className="rounded-xl border border-[var(--hair,var(--border))] bg-surface-2 p-4">
                    <div className="text-sm font-medium text-text">
                      {t("listimports.add.largeList", { total: truncated ? `${listTotal}+` : listTotal })}
                    </div>
                    <p className="mt-1 text-xs leading-snug text-muted">
                      {t("listimports.add.largeListHint", { verb: mode === "catalog" ? t("listimports.add.verbCatalogues") : t("listimports.add.verbDownloads") })}
                    </p>
                  </div>
                ) : (
                  <>
                    <div className="mb-1 flex items-center justify-between gap-2">
                      <span className="text-sm font-medium text-text">{t("listimports.add.titlesCount", { count: rows.length })}</span>
                      <div className="flex gap-2">
                        <Button size="sm" variant="ghost" onClick={() => selectAll(true)}>{t("listimports.add.selectAll")}</Button>
                        <Button size="sm" variant="ghost" onClick={() => selectAll(false)}>{t("listimports.add.deselectAll")}</Button>
                      </div>
                    </div>
                    <p className="mb-1 text-xs text-muted">
                      {t("listimports.add.uncheckedHint", { verb: mode === "catalog" ? t("listimports.add.verbCataloguing") : t("listimports.add.verbFetching") })}
                    </p>
                    {rows.length === 0 ? (
                      <EmptyState title={t("listimports.add.emptyListTitle")} hint={t("listimports.add.emptyListHint")} />
                    ) : (
                      <div className="divide-y divide-border">
                        {rows.map((r, i) => (
                          <PreviewRow key={`${i}-${r.title}`} row={r} resolving={isRowResolving(r)}
                            onChange={(nr) => setRow(i, nr)} />
                        ))}
                      </div>
                    )}
                  </>
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
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useApp((s) => s.toast);
  const isAdmin = useIsAdmin();
  const shelvesQ = useQuery({ queryKey: qk.bookshelves(), queryFn: api.listBookshelves });
  const stockQ = useQuery({ queryKey: qk.stockSummary(), queryFn: api.getStockSummary, enabled: isAdmin });
  const allowStock = isAdmin && !!stockQ.data?.configured;

  const [displayName, setDisplayName] = useState(sub.display_name);
  const [variant, setVariant] = useState<ListVariant>(sub.variant);
  const [targetShelf, setTargetShelf] = useState<string>(sub.target_shelf_id != null ? String(sub.target_shelf_id) : "");
  const [newShelf, setNewShelf] = useState("");
  const [toStock, setToStock] = useState(sub.to_stock);
  const [autoSeries, setAutoSeries] = useState(sub.auto_series);
  const [autoFollowSeries, setAutoFollowSeries] = useState(sub.auto_follow_series);
  const [active, setActive] = useState(sub.active);

  const save = useMutation({
    mutationFn: async () =>
      api.patchImport(sub.id, {
        display_name: displayName.trim() || undefined,
        variant,
        target_shelf_id: toStock ? null : await resolveTargetShelf(targetShelf, newShelf),
        to_stock: toStock,
        auto_series: autoSeries,
        auto_follow_series: autoFollowSeries,
        active,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.listImports() });
      toast(t("listimports.edit.saved"), "success");
      onClose();
    },
    onError: (e) => toast((e as Error).message, "error"),
  });

  return (
    <Modal
      onClose={onClose}
      title={t("listimports.edit.title")}
      footer={
        <>
          <Button size="sm" variant="ghost" onClick={onClose}>{t("common.cancel")}</Button>
          <Button size="sm" variant="primary"
            disabled={save.isPending || (!toStock && targetShelf === NEW_SHELF && !newShelf.trim())}
            onClick={() => save.mutate()}>
            {save.isPending ? t("listimports.edit.saving") : t("listimports.edit.save")}
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <label className="block">
          <div className="mb-1 text-xs text-muted">{t("listimports.edit.displayName")}</div>
          <input className={inputCls} value={displayName} onChange={(e) => setDisplayName(e.target.value)} />
        </label>
        <VariantPicker value={variant} onChange={setVariant} />
        {!toStock && (
          <div>
            <Select
              label={t("listimports.edit.bookshelf")}
              value={targetShelf}
              onChange={setTargetShelf}
              options={[
                { value: "", label: t("listimports.edit.noneMainLibrary") },
                ...(shelvesQ.data ?? []).map((s) => ({ value: String(s.id), label: s.name })),
                { value: NEW_SHELF, label: t("listimports.edit.newShelfOption") },
              ]}
            />
            {targetShelf === NEW_SHELF && (
              <input
                className={`${inputCls} mt-2`}
                value={newShelf}
                onChange={(e) => setNewShelf(e.target.value)}
                placeholder={t("listimports.edit.newShelfPlaceholder", { name: sub.display_name })}
              />
            )}
          </div>
        )}
        {allowStock && (
          <div className="flex items-center justify-between gap-3">
            <span className="text-sm text-text">
              {t("listimports.edit.sendToStock")}
              <span className="block text-xs text-muted">{t("listimports.edit.sendToStockHint")}</span>
            </span>
            <Toggle checked={toStock} onChange={setToStock} />
          </div>
        )}
        <div className="space-y-2 border-t border-border pt-2">
          <div className="flex items-center justify-between gap-3">
            <span className="text-sm text-text">{t("listimports.edit.alsoFetchSeries")}</span>
            <Toggle checked={autoSeries} onChange={setAutoSeries} />
          </div>
          <div className="flex items-center justify-between gap-3">
            <span className="text-sm text-text">{t("listimports.edit.followSeries")}</span>
            <Toggle checked={autoFollowSeries} onChange={setAutoFollowSeries} />
          </div>
          <p className="text-xs text-muted">
            {t("listimports.edit.seriesHint")}
          </p>
        </div>
        <div className="flex items-center justify-between pt-1">
          <span className="text-sm text-text">{t("listimports.edit.active")}</span>
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
  const { t } = useTranslation();
  const q = useQuery({
    queryKey: qk.listImportItems(id),
    queryFn: () => api.listItems(id),
    staleTime: 5 * 60 * 1000, // covers don't change minute-to-minute; avoid re-fetching on every open
  });

  if (q.isLoading) return <div className="px-4 pb-3"><Spinner label={t("listimports.strip.loadingTitles")} /></div>;
  if (q.error) return <p className="px-4 pb-3 text-xs text-red-500">{(q.error as Error).message}</p>;

  const items = q.data?.items ?? [];
  if (items.length === 0) {
    return <p className="px-4 pb-3 text-xs text-muted">{t("listimports.strip.noTitles")}</p>;
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
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useApp((s) => s.toast);
  const confirm = useConfirm();

  const invalidate = () => qc.invalidateQueries({ queryKey: qk.listImports() });

  const sync = useMutation({
    mutationFn: () => api.syncImport(sub.id),
    onSuccess: (s) => {
      invalidate();
      toast(s.last_error ? t("listimports.manageRow.checkedError", { error: s.last_error }) : t("listimports.manageRow.checkedOk", { name: s.display_name }),
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
    onSuccess: () => { invalidate(); toast(t("listimports.manageRow.removed", { name: sub.display_name }), "success"); },
    onError: (e) => toast((e as Error).message, "error"),
  });

  async function onDelete() {
    if (await confirm({
      title: t("listimports.manageRow.removeConfirmTitle"),
      message: t("listimports.manageRow.removeConfirmMessage", { name: sub.display_name }),
      confirmText: t("listimports.manageRow.remove"),
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
            {sub.mode === "catalog"
              ? <span title={t("listimports.manageRow.catalogueTitle")}><Badge tone="violet">{t("listimports.manageRow.catalogue")}</Badge></span>
              : <Badge tone="amber">{variantLabel(t, sub.variant)}</Badge>}
            {sub.to_stock && (
              <span title={t("listimports.manageRow.toStockTitle")}><Badge tone="green">{t("listimports.manageRow.toStock")}</Badge></span>
            )}
            {sub.auto_series && (
              <span title={t("listimports.manageRow.seriesTitle")}><Badge tone="violet">{t("listimports.manageRow.series")}</Badge></span>
            )}
            {sub.auto_follow_series && (
              <span title={t("listimports.manageRow.followingTitle")}><Badge tone="violet">{t("listimports.manageRow.following")}</Badge></span>
            )}
            {!sub.active && <Badge>{t("listimports.manageRow.paused")}</Badge>}
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-muted">
            <span>{t("listimports.manageRow.checked", { time: relTime(t, sub.last_checked_at) })}</span>
            {sub.auto_added > 0 && <span>{sub.auto_added} {sub.mode === "catalog" ? t("listimports.manageRow.catalogued") : t("listimports.manageRow.autoAdded")}</span>}
            {shelf && <span>→ {shelf}</span>}
          </div>
          {sub.total > 0 && (
            <div className="mt-1.5 max-w-sm">
              <div className="text-[11px] text-muted">
                {t("listimports.manageRow.resolved", { done: sub.done.toLocaleString(), total: sub.total.toLocaleString() })}
                {sub.pending > 0 ? t("listimports.manageRow.ingesting") : t("listimports.manageRow.complete")}
              </div>
              <div className="mt-0.5 h-1 w-full overflow-hidden rounded-full bg-surface-2">
                <div className="h-full rounded-full bg-accent transition-all"
                  style={{ width: `${Math.min(100, Math.round((sub.done / Math.max(1, sub.total)) * 100))}%` }} />
              </div>
            </div>
          )}
          {sub.last_error && <div className="mt-1 text-xs text-red-500">⚠ {sub.last_error}</div>}
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          <span title={sub.active ? t("listimports.manageRow.activeTitle") : t("listimports.manageRow.pausedTitle")}>
            <Toggle checked={sub.active} onChange={(on) => toggleActive.mutate(on)} />
          </span>
          <Button size="sm" variant="outline" disabled={sync.isPending || !sub.active}
            onClick={() => sync.mutate()} title={sub.active ? t("listimports.manageRow.recheckTitle") : t("listimports.manageRow.pausedTitle")}>
            {sync.isPending ? t("listimports.manageRow.checking") : t("listimports.manageRow.checkNow")}
          </Button>
          <Button size="sm" variant="ghost" onClick={onEdit}>{t("listimports.manageRow.edit")}</Button>
          <Button size="icon" variant="ghost" aria-label={t("listimports.manageRow.removeAria")} title={t("listimports.manageRow.removeAria")} onClick={onDelete}>✕</Button>
        </div>
      </div>
      <ListCoverStrip id={sub.id} />
    </div>
  );
}

// ---------------------------------------------------------------------------------------------
// Manage section — the existing-imports list + "Import a list" trigger + edit modal. Extracted so
// it can live standalone (/imports redirect target had its own page) OR be embedded as a section in
// the merged Sources page. All import mutations live in ImportRow/AddListModal/EditImportModal —
// this component only owns the add/edit open-state.
// ---------------------------------------------------------------------------------------------
export function ListImportsManager({ className = "" }: { className?: string }) {
  const { t } = useTranslation();
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<ListSubscription | null>(null);

  const importsQ = useQuery({
    queryKey: qk.listImports(), queryFn: api.listImports,
    // While any active import is still ingesting, poll so the progress bar advances on its own.
    refetchInterval: (q) =>
      (q.state.data ?? []).some((s) => s.active && s.pending > 0) ? 5000 : false,
  });
  const providersQ = useQuery({ queryKey: qk.listImportProviders(), queryFn: api.listProviders });
  const shelvesQ = useQuery({ queryKey: qk.bookshelves(), queryFn: api.listBookshelves });

  const subs = importsQ.data ?? [];

  return (
    <section className={className}>
      <div className="mb-1 flex items-center justify-between gap-3">
        <h2 className="font-display text-[22px] font-semibold text-text">{t("listimports.manage.title")}</h2>
        <Button variant="primary" onClick={() => setAdding(true)}>{t("listimports.manage.importAList")}</Button>
      </div>
      <p className="mb-4 text-sm text-muted">
        {t("listimports.manage.intro")}
      </p>

      {importsQ.isLoading ? (
        <Spinner label={t("listimports.manage.loading")} />
      ) : importsQ.error ? (
        <p className="text-sm text-red-500">{(importsQ.error as Error).message}</p>
      ) : subs.length === 0 ? (
        <EmptyState
          title={t("listimports.manage.emptyTitle")}
          hint={t("listimports.manage.emptyHint")}
          action={<Button variant="primary" onClick={() => setAdding(true)}>{t("listimports.manage.importAList")}</Button>}
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
    </section>
  );
}
