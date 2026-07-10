// The reusable library poster grid: series-collapsing + per-work cards (status badges, progress,
// Read/Listen, Shelves popover, and the ⋯ overflow of Send/Fix/maintenance/Remove), plus the
// optional per-card multi-select checkbox. Extracted verbatim from Library's old `management` grid so
// both the Library home (q-search case) and the new /library/browse page render identical cards.
//
// Self-contained: it owns its own per-card mutations (delete/repair/check/resume/pause) and the
// dialogs they open (WorkDetailModal / SendDialog / FixMetadataDialog / SeriesLibraryModal). The
// shared per-work primitives (ShelfMenu, FixMetadataDialog) + the STATUS_BADGE map stay exported from
// pages/Library so WorkDetailModal's existing import is untouched.
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, Bookshelf, SeriesBook, Work } from "../api/client";
import { qk } from "../api/queryKeys";
import { Badge, Button, Card, OverflowMenu, Spinner, useDialogFocus } from "./ui";
import { useConfirm } from "./confirm";
import Cover, { coverSrc } from "./Cover";
import SendDialog from "./SendDialog";
import { useApp } from "../store";
import { useAudio } from "../audioStore";
import WorkDetailModal from "./WorkDetailModal";
import { ShelfMenu, FixMetadataDialog, STATUS_BADGE } from "../pages/Library";

/** The poster grid shared by Library and Browse. Multi-select rendering (the per-card checkbox) is
 *  driven by `selecting`/`selected`/`onToggleSelect`; everything else (cards, series collapsing,
 *  per-work actions) is identical to the old in-Library grid. */
export default function LibraryGrid({
  works,
  shelves,
  selecting,
  selected,
  onToggleSelect,
}: {
  works: Work[];
  shelves: Bookshelf[];
  selecting: boolean;
  selected: Set<number>;
  onToggleSelect: (id: number) => void;
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useApp((s) => s.toast);
  const confirm = useConfirm();
  const navigate = useNavigate();
  const [sendWork, setSendWork] = useState<Work | null>(null);
  const [fixWork, setFixWork] = useState<Work | null>(null);
  const [detailId, setDetailId] = useState<number | null>(null); // work whose detail sheet is open

  const del = useMutation({
    mutationFn: (id: number) => api.deleteWork(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.works() }),
  });

  const repair = useMutation({
    mutationFn: (id: number) => api.repairWork(id),
    onSuccess: (rep) => {
      qc.invalidateQueries({ queryKey: qk.works() });
      const acted = rep.actions.length ? rep.actions.join("; ") : t("libgrid.repair.noFixable");
      toast(t("libgrid.repair.diagnosis", { health: rep.health, detail: rep.detail ?? "", actions: acted }));
    },
    onError: (e) => toast((e as Error).message, "error"),
  });

  const checkOne = useMutation({
    mutationFn: (id: number) => api.checkWorkUpdates(id),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: qk.works() });
      if (!r.checked) toast(t("libgrid.check.noNewChaptersSource"));
      else if (r.error) toast(t("libgrid.check.failed", { error: r.error }), "error");
      else if (r.new_chapters > 0)
        toast(t("libgrid.check.foundNew", { count: r.new_chapters }), "success");
      else toast(r.metadata_changed ? t("libgrid.check.metadataRefreshed") : t("libgrid.check.upToDate"));
    },
    onError: (e) => toast((e as Error).message, "error"),
  });

  const resumeOne = useMutation({
    mutationFn: (id: number) => api.resumeWork(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.works() });
      toast(t("libgrid.resumed"), "success");
    },
    onError: (e) => toast((e as Error).message, "error"),
  });

  const pauseOne = useMutation({
    mutationFn: (id: number) => api.pauseWork(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.works() });
      toast(t("libgrid.paused"));
    },
    onError: (e) => toast((e as Error).message, "error"),
  });

  return (
    <>
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6">
        {buildGridItems(works).map((it) => {
          if (it.kind === "series")
            return <SeriesLibraryCard key={`series:${it.name}`} name={it.name} books={it.books} />;
          const w = it.work;
          // A title may also have a shared audiobook (the "listen" format): audiobook_work_id points
          // at that separate audio Work, which lives in stock — not the library — and is offered as a
          // download alongside Read, so the user sees ONE title and picks ebook or audiobook.
          const audiobookId = w.audiobook_work_id;
          // No overflow-hidden on the Card: the OverflowMenu dropdown must escape it. The cover
          // wrappers clip themselves (+ rounded-t-xl keeps the card's rounded top).
          return (
            <Card key={w.id} className="group relative hover-lift">
              {selecting && (
                <label className="absolute left-2 top-2 z-10 flex h-7 w-7 cursor-pointer items-center justify-center rounded-md border border-border bg-surface/90 shadow">
                  <input
                    type="checkbox"
                    checked={selected.has(w.id)}
                    onChange={() => onToggleSelect(w.id)}
                  />
                </label>
              )}
              {selecting ? (
                <button className="block w-full text-left" onClick={() => onToggleSelect(w.id)}>
                  <div className="aspect-[2/3] w-full overflow-hidden rounded-t-xl">
                    <Cover title={w.title} author={w.author} coverUrl={w.cover_url} />
                  </div>
                </button>
              ) : (
                <button type="button" onClick={() => setDetailId(w.id)} className="block w-full text-left" title={t("libgrid.openTitle", { title: w.title })}>
                  <div className="aspect-[2/3] w-full overflow-hidden rounded-t-xl">
                    <Cover title={w.title} author={w.author} coverUrl={w.cover_url} />
                  </div>
                </button>
              )}
              <div className="space-y-1 p-3">
                <button type="button" onClick={() => setDetailId(w.id)} className="w-full text-left font-medium leading-tight hover:underline line-clamp-2">
                  {w.title}
                </button>
                <div className="text-xs text-muted line-clamp-1">{w.author ?? t("libgrid.unknownAuthor")}</div>
                <div className="flex flex-wrap items-center gap-1.5 pt-1">
                  {audiobookId && <Badge tone="violet">🎧 {t("libgrid.plusAudiobook")}</Badge>}
                  {/* One clear status, plus the chapter count. */}
                  {(() => {
                    const s = STATUS_BADGE[w.library_status] ?? STATUS_BADGE.ongoing;
                    const status = STATUS_BADGE[w.library_status] ? w.library_status : "ongoing";
                    return (
                      <span title={w.health_detail ?? t(`library.statusHelp.${status}`)}>
                        <Badge tone={s.tone}>{s.icon} {t(`library.status.${status}`)}</Badge>
                      </span>
                    );
                  })()}
                  {/* File problem found by the background integrity scan — surfaced so a broken
                      title is visible in the grid, not just discovered on open. */}
                  {(w.health === "missing" || w.health === "corrupt") && (
                    <span title={w.health_detail ?? undefined}>
                      <Badge tone="red">⚠ {t(`work.health.${w.health}`)}</Badge>
                    </span>
                  )}
                  {(() => {
                    // Never display fewer than we've gathered (a serial can pass its old ceiling).
                    const total = Math.max(
                      w.total_chapters_expected ?? w.total_chapters_known ?? 0,
                      w.chapters_fetched,
                    );
                    return <Badge>{t("libgrid.chapterCount", { fetched: w.chapters_fetched, total: total ? `/${total}` : "" })}</Badge>;
                  })()}
                  {(w.start_chapter ?? 1) > 1 && (
                    <span title={t("libgrid.hookedFromTitle", { chapter: w.start_chapter })}>
                      <Badge tone="violet">{t("libgrid.fromChapter", { chapter: w.start_chapter })}</Badge>
                    </span>
                  )}
                </div>
                {(() => {
                  // Progress bar only while actively gathering — a caught-up or incomplete title
                  // isn't "in progress", so a bar there just reads as broken.
                  if (w.library_status !== "gathering") return null;
                  const total = Math.max(
                    w.total_chapters_expected ?? w.total_chapters_known ?? 0,
                    w.chapters_fetched,
                  );
                  if (!total || w.chapters_fetched >= total) return null;
                  const pct = Math.min(100, Math.round((w.chapters_fetched / total) * 100));
                  return (
                    <div className="pt-1">
                      <div className="h-1 w-full overflow-hidden rounded-full bg-surface-2">
                        <div className="h-full rounded-full bg-accent" style={{ width: `${pct}%` }} />
                      </div>
                      <div className="mt-0.5 text-[10px] text-muted">
                        {t("libgrid.gatheringProgress", { fetched: w.chapters_fetched, total })}
                      </div>
                    </div>
                  );
                })()}
                {/* One obvious primary (Read), plus the two controls that don't fit a plain menu —
                    the 🎧 Listen download <a> and the Shelves popover (ShelfMenu) — kept beside it as
                    compact always-visible controls. Everything else (Send / maintenance / Remove)
                    moves into the ⋯ overflow, killing the up-to-8-button hover cluster. Conditions,
                    disabled and isPending behavior are unchanged. */}
                <div className="flex flex-wrap items-center gap-1.5 pt-2">
                  <Button size="sm" variant="primary" onClick={() => navigate(`/read/${w.id}`)}>
                    {t("libgrid.read")}
                  </Button>
                  {audiobookId && (
                    <button
                      // playWork must run inside the tap (iOS requires play() in a user gesture).
                      onClick={() => useAudio.getState().playWork(audiobookId)}
                      title={t("libgrid.playAudiobook")}
                      className="inline-flex items-center gap-1 rounded-lg border border-border px-2.5 py-1 text-xs font-medium hover:bg-surface-2"
                    >
                      🎧 {t("libgrid.listen")}
                    </button>
                  )}
                  <ShelfMenu work={w} shelves={shelves} />
                  <OverflowMenu
                    label={t("libgrid.moreActions", { title: w.title })}
                    items={[
                      audiobookId && {
                        label: t("libgrid.action.downloadAudiobook"),
                        onClick: () => {
                          const a = document.createElement("a");
                          a.href = api.audioUrl(audiobookId); a.download = "";
                          a.click();
                        },
                      },
                      {
                        label: t("libgrid.action.send"),
                        onClick: () => setSendWork(w),
                      },
                      {
                        label: t("libgrid.action.fixMetadata"),
                        onClick: () => setFixWork(w),
                      },
                      w.library_status === "incomplete" && {
                        label: repair.isPending && repair.variables === w.id ? t("libgrid.action.fixing") : t("libgrid.action.fix"),
                        disabled: repair.isPending && repair.variables === w.id,
                        onClick: () => repair.mutate(w.id),
                      },
                      w.library_status === "paused" && {
                        label: resumeOne.isPending && resumeOne.variables === w.id ? t("libgrid.action.resuming") : t("libgrid.action.resume"),
                        disabled: resumeOne.isPending && resumeOne.variables === w.id,
                        onClick: () => resumeOne.mutate(w.id),
                      },
                      w.hooked && w.library_status !== "paused" && w.status === "ongoing" && {
                        label: checkOne.isPending && checkOne.variables === w.id ? t("libgrid.action.checking") : t("libgrid.action.updates"),
                        disabled: checkOne.isPending && checkOne.variables === w.id,
                        onClick: () => checkOne.mutate(w.id),
                      },
                      w.hooked && w.library_status !== "paused" && w.status === "ongoing" && {
                        label: pauseOne.isPending && pauseOne.variables === w.id ? t("libgrid.action.pausing") : t("libgrid.action.pause"),
                        disabled: pauseOne.isPending && pauseOne.variables === w.id,
                        onClick: () => pauseOne.mutate(w.id),
                      },
                      {
                        label: t("libgrid.action.remove"),
                        danger: true,
                        onClick: async () => {
                          if (await confirm({ title: t("libgrid.removeConfirmTitle"), message: t("libgrid.removeConfirmMessage", { title: w.title }), danger: true, confirmText: t("libgrid.action.remove") }))
                            del.mutate(w.id);
                        },
                      },
                    ]}
                  />
                </div>
              </div>
            </Card>
          );
        })}
      </div>

      {detailId != null && (
        <WorkDetailModal workId={detailId} onClose={() => setDetailId(null)} />
      )}
      {sendWork && (
        <SendDialog workId={sendWork.id} title={sendWork.title} onClose={() => setSendWork(null)} />
      )}
      {fixWork && (
        <FixMetadataDialog work={fixWork} onClose={() => setFixWork(null)} />
      )}
    </>
  );
}

// --- Series grouping in the library ---------------------------------------------------------
type GridItem = { kind: "work"; work: Work } | { kind: "series"; name: string; books: Work[] };

// Collapse library works that belong to the same series into ONE grid entry (books ordered by
// series position); standalone works pass through unchanged.
function buildGridItems(works: Work[] | undefined): GridItem[] {
  const out: GridItem[] = [];
  const seen = new Set<string>();
  for (const w of works ?? []) {
    if (w.series) {
      if (seen.has(w.series)) continue;
      const books = (works ?? [])
        .filter((x) => x.series === w.series)
        .sort((a, b) => (a.series_position ?? 9999) - (b.series_position ?? 9999));
      // Only collapse into a Series card when 2+ owned volumes share the series. A single owned
      // volume renders as a normal work card (read in one tap, no series-modal detour). (F31)
      if (books.length >= 2) {
        seen.add(w.series);
        out.push({ kind: "series", name: w.series, books });
      } else {
        out.push({ kind: "work", work: w });
      }
    } else {
      out.push({ kind: "work", work: w });
    }
  }
  return out;
}

function SeriesLibraryCard({ name, books }: { name: string; books: Work[] }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const cover = books.find((b) => b.cover_url)?.cover_url ?? null;
  const first = books[0];
  return (
    <>
      <Card className="group relative overflow-hidden hover-lift">
        <button
          className="block w-full text-left"
          onClick={() => setOpen(true)}
          title={t("libgrid.series.openTitle", { name })}
        >
          <div className="aspect-[2/3] w-full overflow-hidden">
            <Cover title={name} author={first?.author ?? null} coverUrl={cover} />
          </div>
        </button>
        <div className="space-y-1 p-3">
          <button
            className="w-full text-left font-medium leading-tight line-clamp-2 hover:underline"
            onClick={() => setOpen(true)}
          >
            {name}
          </button>
          <div className="text-xs text-muted line-clamp-1">{first?.author ?? t("libgrid.series.badge")}</div>
          <div className="flex flex-wrap items-center gap-1.5 pt-1">
            <Badge tone="violet">{t("libgrid.series.badge")}</Badge>
            <Badge>{t("libgrid.series.inLibrary", { count: books.length })}</Badge>
          </div>
          <div className="pt-2">
            <Button size="sm" variant="primary" onClick={() => setOpen(true)}>
              {t("libgrid.series.open")}
            </Button>
          </div>
        </div>
      </Card>
      {open && <SeriesLibraryModal name={name} books={books} onClose={() => setOpen(false)} />}
    </>
  );
}

function SeriesLibraryModal({
  name,
  books,
  onClose,
}: {
  name: string;
  books: Work[];
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const toast = useApp((s) => s.toast);
  const seedId = books[0]?.id;
  const full = useQuery({
    queryKey: qk.workSeries(seedId),
    queryFn: () => api.workSeries(seedId),
    enabled: !!seedId,
  });
  const focusRef = useDialogFocus(onClose);   // Escape + focus trap/restore (shared dialog behavior)
  const confirm = useConfirm();

  // Remove the WHOLE series from the library: delete every owned volume at once (volumes not in the
  // library are untouched). Fixes the gap where a series could only be removed one volume at a time.
  const removeSeries = useMutation({
    // Sequential, not Promise.all: concurrent DELETEs storm SQLite (each does a write txn + cache clear)
    // and a mid-flight failure would leave the series half-removed. One at a time is the safe default.
    mutationFn: async () => {
      for (const b of books) await api.deleteWork(b.id);
    },
    onSuccess: () => {
      toast(t("libgrid.series.removed", { name, count: books.length }), "success");
      onClose();
    },
    onError: (e) => toast((e as Error).message, "error"),
    // Always refresh the library, even on partial failure, so the UI reflects what was actually deleted.
    onSettled: () => {
      qc.invalidateQueries({ queryKey: qk.works() });
      qc.invalidateQueries({ queryKey: qk.continue() });
    },
  });

  const vols: SeriesBook[] = full.data?.books ?? [];
  const missing = vols.filter((v) => !v.in_library && v.ref && v.catalog_id);
  const seedCatalog = vols.find((v) => v.catalog_id)?.catalog_id ?? null;

  const fetchMissing = useMutation({
    mutationFn: () =>
      api.acquireSeries(seedCatalog!, { refs: missing.map((m) => m.ref!) }),
    onSuccess: (r) => {
      toast(t("libgrid.series.fetchingMissing", { count: r.results.length }), "success");
      qc.invalidateQueries({ queryKey: qk.downloads() });
      qc.invalidateQueries({ queryKey: qk.works() });
    },
    onError: (e) => toast((e as Error).message, "error"),
  });

  return (
    <div
      className="fixed inset-0 z-50 flex justify-center overflow-y-auto bg-black/50 p-0 sm:p-6"
      onClick={onClose}
    >
      <div
        ref={focusRef}
        role="dialog"
        aria-modal="true"
        aria-label={t("libgrid.series.dialogLabel", { name })}
        tabIndex={-1}
        className="relative h-full w-full max-w-xl overflow-y-auto bg-surface sm:h-auto sm:rounded-2xl sm:shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="sticky top-0 z-10 flex items-center justify-between gap-2 border-b border-border bg-surface px-4 py-3">
          <div className="truncate font-semibold">
            {name}
            {vols.length ? (
              <span className="text-muted">
                {" "}
                · {t("libgrid.series.ownedCount", { owned: vols.length - missing.length, total: vols.length })}
              </span>
            ) : (
              <span className="text-muted"> · {t("libgrid.series.inLibrary", { count: books.length })}</span>
            )}
          </div>
          <div className="flex shrink-0 items-center gap-1">
            <Button
              size="sm"
              variant="danger"
              disabled={removeSeries.isPending}
              onClick={async () => {
                if (await confirm({
                  title: t("libgrid.series.removeConfirmTitle"),
                  message: t("libgrid.series.removeConfirmMessage", { name, count: books.length }),
                  danger: true,
                  confirmText: t("libgrid.series.removeConfirm"),
                })) removeSeries.mutate();
              }}
            >
              {removeSeries.isPending ? t("libgrid.series.removing") : t("libgrid.series.removeConfirm")}
            </Button>
            <Button size="sm" variant="ghost" aria-label={t("libgrid.series.close")} onClick={onClose}>
              ✕
            </Button>
          </div>
        </div>
        <div className="px-4 py-3">
          {full.isLoading && <Spinner label={t("libgrid.series.finding")} />}
          {!full.isLoading && missing.length > 0 && (
            <div className="mb-3 flex items-center justify-between gap-2 rounded-lg border border-amber-500/30 bg-amber-500/5 p-2.5 text-sm">
              <span>
                ⚠ {t("libgrid.series.missingWarning", { count: missing.length })}
              </span>
              {seedCatalog && (
                <Button
                  size="sm"
                  variant="primary"
                  disabled={fetchMissing.isPending}
                  onClick={() => fetchMissing.mutate()}
                >
                  {fetchMissing.isPending ? t("libgrid.series.fetching") : t("libgrid.series.fetchMissing", { count: missing.length })}
                </Button>
              )}
            </div>
          )}
          <div className="space-y-1">
            {(vols.length ? vols : books.map((b) => ({
              title: b.title, author: b.author, year: null, position: b.series_position,
              cover_url: b.cover_url, ref: null, catalog_id: null,
              hooked_work_id: b.id, in_library: true,
            }) as SeriesBook)).map((v, i) => {
              // A volume is readable when the server says it's in the user's library AND knows which
              // local work it maps to. (in_library is computed server-side from the user's own
              // membership, so the hooked work is always theirs — no need to cross-check the grid.)
              const owned = !!(v.in_library && v.hooked_work_id);
              return (
                <div
                  key={v.ref ?? `${v.title}:${i}`}
                  className="flex items-center gap-2 rounded px-1 py-1 text-sm hover:bg-bg/50"
                >
                  {v.cover_url ? (
                    <img
                      src={coverSrc(v.cover_url) ?? ""}
                      alt=""
                      loading="lazy"
                      className="h-10 w-7 shrink-0 rounded border border-border object-cover"
                      onError={(e) => (e.currentTarget.style.display = "none")}
                    />
                  ) : null}
                  <span className="min-w-0 flex-1 truncate">
                    {v.position != null ? <span className="text-muted">#{v.position} </span> : ""}
                    {v.title}
                    {v.year ? <span className="text-muted"> ({v.year})</span> : null}
                  </span>
                  {owned ? (
                    <Button
                      size="sm"
                      variant="primary"
                      onClick={() => navigate(`/read/${v.hooked_work_id}`)}
                    >
                      {t("libgrid.read")}
                    </Button>
                  ) : v.in_library ? (
                    <Badge tone="green">{t("libgrid.series.volInLibrary")}</Badge>
                  ) : (
                    <Badge tone="amber">{t("libgrid.series.volMissing")}</Badge>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
}
