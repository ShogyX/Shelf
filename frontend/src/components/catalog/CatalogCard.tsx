// Shared catalog UI — a discovered-work card + its full detail modal. Extracted from the Index
// page so the new discovery rows and the /browse grid render titles identically (and hook the
// same way). Pure move: no behavior change.
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, CatalogGroup, CatalogSource } from "../../api/client";
import { Badge, Button, Card, Spinner } from "../ui";
import Cover from "../Cover";
import { useApp } from "../../store";
import { useIsAdmin } from "../../auth";
import { healthBadge, Tone } from "../IndexShared";

export function mediaTone(label: string): Tone {
  switch (label) {
    case "Manga":
    case "Manhua":
    case "Webtoon":
    case "Comic":
      return "violet";
    case "Book":
      return "amber";
    default:
      return "default"; // Novel
  }
}

export function CatalogCard({
  group,
  onOpenDetail,
}: {
  group: CatalogGroup;
  onOpenDetail: () => void;
}) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const toast = useApp((s) => s.toast);
  const destShelfId = useApp((s) => s.destShelfId);
  const isAdmin = useIsAdmin();
  const refetchCover = useMutation({
    mutationFn: () => api.refetchGroupCover(group.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["catalog"] });
      toast(`Fetched a new cover for “${group.title}”`, "success");
    },
    onError: (e) => toast((e as Error).message, "error"),
  });
  const [error, setError] = useState<string | null>(null);
  const [pendingId, setPendingId] = useState<number | null>(null);
  // Non-blocking hook: show a processing → done message in place; never yank the user away.
  const [doneWorkId, setDoneWorkId] = useState<number | null>(null);

  const hook = useMutation({
    mutationFn: (catalogId: number) => api.hookCatalog(catalogId, undefined, destShelfId ?? undefined),
    onMutate: (catalogId) => {
      setPendingId(catalogId);
      setError(null);
      setDoneWorkId(null);
    },
    onSuccess: (work) => {
      qc.invalidateQueries({ queryKey: ["works"] });
      qc.invalidateQueries({ queryKey: ["catalog"] });
      qc.invalidateQueries({ queryKey: ["catalog-stats"] });
      setDoneWorkId(work.id);
      toast(`Added “${group.title}” to your library`, "success");
    },
    onError: (e) => setError((e as Error).message),
    onSettled: () => setPendingId(null),
  });

  const grab = useMutation({
    mutationFn: (catalogId: number) => api.grabCatalog(catalogId),
    onMutate: (catalogId) => {
      setPendingId(catalogId);
      setError(null);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["catalog"] });
      qc.invalidateQueries({ queryKey: ["downloads"] });
      setError(null);
      setDoneWorkId(-1); // sentinel: a grab was queued (message shown below)
      toast(`Fetching “${group.title}” — added to the Jobs tab`, "success");
    },
    onError: (e) => setError((e as Error).message),
    onSettled: () => setPendingId(null),
  });

  // Priority-driven one-click acquire: resolves to the user's preferred route (hook a web source,
  // grab via a manager, or download via the usenet pipeline) — whichever can fulfill it first.
  const acquire = useMutation({
    // group.id is the GROUP key (not a CatalogWork id), so acquire via a representative source's
    // catalog_id; the backend re-clusters by title to consider every route across the group.
    mutationFn: (repId: number) => api.acquireCatalog(repId, undefined, destShelfId ?? undefined),
    onMutate: () => {
      setPendingId(group.id);
      setError(null);
      setDoneWorkId(null);
    },
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["works"] });
      qc.invalidateQueries({ queryKey: ["catalog"] });
      qc.invalidateQueries({ queryKey: ["downloads"] });
      if (r.status === "hooked" && r.work_id) {
        setDoneWorkId(r.work_id);
        toast(`Added “${group.title}” to your library`, "success");
      } else if (r.status === "none") {
        toast(`No source could fulfil “${group.title}” right now`, "error");
      } else {
        setDoneWorkId(-1); // downloading / grabbed → "queued" message
        toast(`Fetching “${group.title}” — added to the Jobs tab`, "success");
      }
    },
    onError: (e) => setError((e as Error).message),
    onSettled: () => setPendingId(null),
  });

  // Book-fuzzing: when normal matching can't find it, download every loose match and verify which
  // (if any) is the real book.
  const fuzz = useMutation({
    mutationFn: () => api.grabPipeline(group.id, { fuzz: true, shelfId: destShelfId ?? undefined }),
    onMutate: () => {
      setPendingId(group.id);
      setError(null);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["downloads"] });
      toast(`Searching every source for “${group.title}” — see the Jobs tab`, "success");
    },
    onError: (e) => toast((e as Error).message, "error"),
    onSettled: () => setPendingId(null),
  });

  const [showSeries, setShowSeries] = useState(false);
  // Metadata listings (Google Books / Open Library / Hardcover) are backend-only — they're never
  // shown as selectable sources; acquisition for those goes through Acquire (the usenet pipeline).
  const visibleSources = group.sources.filter((s) => !s.listing_only);
  // When a group carries several editions (e.g. colored vs B/W — distinct titles), label each
  // button by its own title so the user can tell them apart; otherwise media·domain suffices.
  const multiEditions = new Set(visibleSources.map((s) => s.title)).size > 1;
  const busyAny = hook.isPending || grab.isPending || acquire.isPending || fuzz.isPending;
  return (
    <Card className="flex gap-4 p-4">
      <div className="relative shrink-0">
        <button onClick={onOpenDetail} title="View details & all sources">
          <div className="h-44 overflow-hidden rounded-md border border-border" style={{ width: "7.5rem" }}>
            <Cover title={group.title} author={group.author} coverUrl={group.cover_url} small />
          </div>
        </button>
        {isAdmin && (group.media_kind === "comic" || group.media_label !== "Book" && group.media_label !== "Novel") && (
          <button
            className="absolute bottom-1 right-1 rounded bg-black/60 px-1.5 py-0.5 text-xs text-white hover:bg-black/80 disabled:opacity-50"
            title="Fetch new cover art (from AniList)"
            disabled={refetchCover.isPending}
            onClick={(e) => { e.stopPropagation(); refetchCover.mutate(); }}
          >
            {refetchCover.isPending ? "…" : "↻ cover"}
          </button>
        )}
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-start justify-between gap-2">
          <button
            onClick={onOpenDetail}
            className="text-left text-base font-semibold leading-tight text-text hover:text-accent hover:underline"
            title="View details & all sources"
          >
            {group.title}
          </button>
          {group.hooked_work_id && (
            <button
              className="shrink-0"
              onClick={() => navigate(`/read/${group.hooked_work_id}`)}
              title="Open in library"
            >
              <Badge tone="green">in library</Badge>
            </button>
          )}
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-muted">
          <Badge tone={mediaTone(group.media_label)}>{group.media_label}</Badge>
          {group.is_adult && <Badge tone="red">18+</Badge>}
          {group.author && <span className="truncate">by {group.author}</span>}
          {group.chapters != null && <span>· {group.chapters.toLocaleString()} ch</span>}
        </div>
        {group.synopsis && (
          <p className="mt-1.5 line-clamp-3 text-sm text-muted">{group.synopsis}</p>
        )}

        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          {!group.hooked_work_id && (
            <Button
              size="sm"
              variant="primary"
              disabled={busyAny}
              onClick={() => acquire.mutate(group.id)}
              title="Get this via your preferred source (crawl, manager, or usenet download)"
            >
              {acquire.isPending ? "Acquiring…" : "Acquire"}
            </Button>
          )}
          {group.series && (
            <Button
              size="sm"
              variant="ghost"
              onClick={() => setShowSeries(true)}
              title={`Part of "${group.series}" — view & fetch the series`}
            >
              View Series
            </Button>
          )}
          {!group.hooked_work_id && (
            <Button
              size="sm"
              variant="ghost"
              disabled={busyAny}
              onClick={() => fuzz.mutate()}
              title="Can't find it normally? Download every loose match and verify which is the real book."
            >
              {fuzz.isPending ? "Searching…" : "Find anyway"}
            </Button>
          )}
          {visibleSources.length > 1 && (
            <span className="text-[11px] uppercase tracking-wide text-muted">
              or {visibleSources.length} sources:
            </span>
          )}
          {visibleSources.map((s) => (
            <SourceButton
              key={s.catalog_id}
              source={s}
              multi={visibleSources.length > 1}
              byTitle={multiEditions}
              busy={pendingId === s.catalog_id}
              disabled={busyAny}
              onHook={() => hook.mutate(s.catalog_id)}
              onGrab={() => grab.mutate(s.catalog_id)}
              onOpen={(workId) => navigate(`/read/${workId}`)}
            />
          ))}
        </div>
        {busyAny && <p className="mt-1.5 text-xs text-accent">Adding to your library…</p>}
        {doneWorkId != null && doneWorkId > 0 && (
          <p className="mt-1.5 text-xs text-green-600">
            Added to your library ✓{" "}
            <button className="underline" onClick={() => navigate(`/read/${doneWorkId}`)}>
              Open
            </button>
          </p>
        )}
        {doneWorkId === -1 && (
          <p className="mt-1.5 text-xs text-green-600">
            Queued — it'll appear once downloaded into a watched folder.
          </p>
        )}
        {error && <p className="mt-1 text-xs text-red-500">Couldn't add: {error}</p>}
        {showSeries && (
          <SeriesModal
            catalogId={group.id}
            seriesName={group.series}
            onClose={() => setShowSeries(false)}
          />
        )}
      </div>
    </Card>
  );
}

function SeriesModal({
  catalogId,
  seriesName,
  onClose,
}: {
  catalogId: number;
  seriesName: string | null;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const toast = useApp((s) => s.toast);
  const destShelfId = useApp((s) => s.destShelfId);
  const q = useQuery({ queryKey: ["series", catalogId], queryFn: () => api.catalogSeries(catalogId) });
  const [sel, setSel] = useState<Set<string>>(new Set());
  useEffect(() => {
    if (q.data)
      setSel(new Set(q.data.books.filter((b) => !b.hooked_work_id && b.ref).map((b) => b.ref!)));
  }, [q.data]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const acquireAll = (all: boolean) =>
    api.acquireSeries(catalogId, {
      ...(all ? { all: true } : { refs: [...sel] }),
      ...(destShelfId != null ? { shelf_id: destShelfId } : {}),
    });
  const fetchM = useMutation({
    mutationFn: (all: boolean) => acquireAll(all),
    onSuccess: (r) => {
      const started = r.results.filter((x) =>
        ["downloading", "grabbed", "hooked"].includes(String((x as { status?: string }).status))
      ).length;
      toast(`Fetching ${started} of ${r.results.length} from the series — see the Jobs tab`, "success");
      qc.invalidateQueries({ queryKey: ["downloads"] });
      qc.invalidateQueries({ queryKey: ["catalog"] });
      onClose();
    },
    onError: (e) => toast((e as Error).message, "error"),
  });

  const d = q.data;
  const selectable = (d?.books ?? []).filter((b) => !b.hooked_work_id && b.ref);
  const toggle = (ref: string) =>
    setSel((s) => {
      const n = new Set(s);
      n.has(ref) ? n.delete(ref) : n.add(ref);
      return n;
    });

  return (
    <div
      className="fixed inset-0 z-50 flex justify-center overflow-y-auto bg-black/50 p-0 sm:p-6"
      onClick={onClose}
    >
      <div
        className="relative h-full w-full max-w-xl overflow-y-auto bg-surface sm:h-auto sm:rounded-2xl sm:shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="sticky top-0 z-10 flex items-center justify-between gap-2 border-b border-border bg-surface/95 px-4 py-3 backdrop-blur">
          <div className="truncate font-semibold">
            Series{d?.series ? `: ${d.series}` : seriesName ? `: ${seriesName}` : ""}
            {d?.books?.length ? <span className="text-muted"> · {d.books.length} books</span> : null}
          </div>
          <Button size="sm" variant="ghost" onClick={onClose}>
            ✕
          </Button>
        </div>
        <div className="px-4 py-3">
          {q.isLoading ? (
            <Spinner label="Finding all books in the series…" />
          ) : !d?.series || d.books.length === 0 ? (
            <p className="text-sm text-muted">No series information found for this title.</p>
          ) : (
            <>
              <div className="mb-2 flex items-center justify-between text-xs">
                <span className="text-muted">Select the volumes to fetch:</span>
                <div className="flex gap-3">
                  <button
                    className="text-muted underline hover:text-text"
                    onClick={() => setSel(new Set(selectable.map((b) => b.ref!)))}
                  >
                    select all
                  </button>
                  <button
                    className="text-muted underline hover:text-text"
                    onClick={() => setSel(new Set())}
                  >
                    clear
                  </button>
                </div>
              </div>
              <div className="max-h-[55vh] space-y-1 overflow-y-auto">
                {d.books.map((b) => (
                  <label
                    key={b.ref ?? b.title}
                    className="flex items-center gap-2 rounded px-1 py-0.5 text-sm hover:bg-bg/50"
                  >
                    <input
                      type="checkbox"
                      disabled={!!b.hooked_work_id || !b.ref}
                      checked={!!b.ref && sel.has(b.ref)}
                      onChange={() => b.ref && toggle(b.ref)}
                    />
                    {b.cover_url ? (
                      <img
                        src={b.cover_url}
                        alt=""
                        loading="lazy"
                        className="h-10 w-7 shrink-0 rounded border border-border object-cover"
                        onError={(e) => (e.currentTarget.style.display = "none")}
                      />
                    ) : null}
                    <span className="min-w-0 flex-1 truncate">
                      {b.position ? <span className="text-muted">#{b.position} </span> : ""}
                      {b.title}
                      {b.year ? <span className="text-muted"> ({b.year})</span> : null}
                    </span>
                    {b.hooked_work_id && <Badge tone="green">in library</Badge>}
                  </label>
                ))}
              </div>
              <div className="mt-3 flex items-center justify-end gap-2 border-t border-border pt-3">
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={fetchM.isPending || selectable.length === 0}
                  onClick={() => fetchM.mutate(true)}
                  title="Fetch every not-in-library book in the series"
                >
                  Grab whole series
                </Button>
                <Button
                  size="sm"
                  variant="primary"
                  disabled={sel.size === 0 || fetchM.isPending}
                  onClick={() => fetchM.mutate(false)}
                >
                  {fetchM.isPending ? "Fetching…" : `Fetch ${sel.size} selected`}
                </Button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function SourceButton({
  source,
  multi,
  byTitle = false,
  busy,
  disabled,
  onHook,
  onGrab,
  onOpen,
}: {
  source: CatalogSource;
  multi: boolean;
  byTitle?: boolean;
  busy: boolean;
  disabled: boolean;
  onHook: () => void;
  onGrab: () => void;
  onOpen: (workId: number) => void;
}) {
  const hb = healthBadge(source.health);
  const count = source.chapters_advertised ?? source.chapters_listed;
  if (source.hooked_work_id) {
    return (
      <Button size="sm" variant="ghost" onClick={() => onOpen(source.hooked_work_id!)}>
        Open ({source.domain})
      </Button>
    );
  }
  // Metadata listing (Google Books / Open Library / Hardcover): you can't read or fetch a book
  // FROM these — they only describe it. No hook/grab; use the card's Acquire button (pipeline).
  if (source.listing_only) {
    return (
      <span title={`Listed on ${source.domain} — use Acquire to download`}>
        <Badge>listed · {source.domain}</Badge>
      </span>
    );
  }
  // Integration source (Readarr/Kapowarr): grab it there; Shelf imports the file once it
  // downloads into a watched folder.
  if (source.kind !== "online") {
    if (source.grab_status) {
      return (
        <span title={`Requested via ${source.kind}`}>
          <Badge tone="green">requested ({source.kind})</Badge>
        </span>
      );
    }
    return (
      <Button
        size="sm"
        variant="outline"
        disabled={disabled}
        onClick={onGrab}
        title={`Add + download via ${source.kind} (${source.domain})`}
      >
        {busy ? "Grabbing…" : `Grab via ${source.kind}`}
      </Button>
    );
  }
  // Mark each source with what it is (Novel / Book / Manga / Webtoon / Comic) + its domain, so a
  // multi-source card makes clear whether you're hooking the novel or the manga. When the card
  // holds multiple EDITIONS (distinct titles), label by title instead so colored vs B/W are clear.
  const label = !multi
    ? "Add to library"
    : byTitle
      ? source.title || `${source.media_label} · ${source.domain}`
      : `${source.media_label} · ${source.domain}`;
  return (
    <Button
      size="sm"
      variant={multi ? "outline" : "primary"}
      disabled={disabled}
      onClick={onHook}
      title={
        `Hook the ${source.media_label} from ${source.domain}` +
        (count ? ` · ${count} chapters` : "") +
        (hb ? ` · ${hb.label}` : "")
      }
    >
      {busy ? (
        "Adding…"
      ) : (
        <span className="inline-block max-w-[14rem] truncate align-bottom">{label}</span>
      )}
      {multi && count ? <span className="ml-1 text-[11px] text-muted">{count}</span> : null}
    </Button>
  );
}

function srcCount(s: CatalogSource): number {
  return s.chapters_advertised ?? s.chapters_listed ?? 0;
}

/** Detailed card for one discovered work: overview + every matched source/sub-title so the
 *  user can compare and choose where to hook from. */
export function CatalogDetail({ group, onClose }: { group: CatalogGroup; onClose: () => void }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const destShelfId = useApp((s) => s.destShelfId);
  const [error, setError] = useState<string | null>(null);
  const [pendingId, setPendingId] = useState<number | null>(null);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["works"] });
    qc.invalidateQueries({ queryKey: ["catalog"] });
    qc.invalidateQueries({ queryKey: ["catalog-stats"] });
  };
  const [doneWorkId, setDoneWorkId] = useState<number | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [startCh, setStartCh] = useState(""); // hook from this chapter (blank = from the start)
  const startChapter = Math.max(1, parseInt(startCh, 10) || 1);
  const hook = useMutation({
    mutationFn: (id: number) => api.hookCatalog(id, startChapter, destShelfId ?? undefined),
    onMutate: (id) => {
      setPendingId(id);
      setError(null);
      setDoneWorkId(null);
      setNotice(null);
    },
    onSuccess: (work) => {
      invalidate();
      setDoneWorkId(work.id);
      setNotice(
        startChapter > 1 ? `Added from chapter ${startChapter} ✓` : "Added to your library ✓"
      );
    },
    onError: (e) => setError((e as Error).message),
    onSettled: () => setPendingId(null),
  });
  const grab = useMutation({
    mutationFn: (id: number) => api.grabCatalog(id),
    onMutate: (id) => {
      setPendingId(id);
      setError(null);
      setDoneWorkId(null);
      setNotice(null);
    },
    onSuccess: (r) => {
      invalidate();
      setNotice(r.message);
    },
    onError: (e) => setError((e as Error).message),
    onSettled: () => setPendingId(null),
  });
  // Sources removed in this modal session — hidden immediately so the row doesn't linger on
  // the stale `group` prop until a reopen (the catalog list refetches in the background).
  const [removedIds, setRemovedIds] = useState<Set<number>>(new Set());
  const remove = useMutation({
    mutationFn: ({ id, blockDomain }: { id: number; blockDomain: boolean }) =>
      api.removeCatalog(id, { blockDomain }),
    onMutate: () => {
      setError(null);
      setNotice(null);
    },
    onSuccess: (r, vars) => {
      invalidate();
      setNotice(
        `Removed and blocked${r.blocked?.scope === "domain" ? " (whole domain)" : ""}. ` +
          "It won't be re-added by future crawls."
      );
      const next = new Set(removedIds).add(vars.id);
      setRemovedIds(next);
      // Close the detail view once every (non-listing) source has been removed.
      if (group.sources.filter((s) => !s.listing_only).every((s) => next.has(s.catalog_id)))
        onClose();
    },
    onError: (e) => setError((e as Error).message),
  });

  // Surface the most complete / healthiest source first; hide ones removed this session and the
  // backend-only metadata listings (Google Books / Open Library / Hardcover).
  const sources = [...group.sources]
    .filter((s) => !removedIds.has(s.catalog_id) && !s.listing_only)
    .sort((a, b) => {
      const hooked = Number(!!b.hooked_work_id) - Number(!!a.hooked_work_id);
      return hooked || srcCount(b) - srcCount(a);
    });

  return (
    <div
      className="fixed inset-0 z-50 flex justify-center overflow-y-auto bg-black/50 p-0 sm:p-6"
      onClick={onClose}
    >
      <div
        className="relative h-full w-full max-w-2xl overflow-y-auto bg-surface sm:h-auto sm:rounded-2xl sm:shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="sticky top-0 z-10 flex items-center justify-between gap-2 border-b border-border bg-surface/95 px-4 py-3 backdrop-blur">
          <div className="truncate font-semibold">{group.title}</div>
          <Button size="sm" variant="ghost" onClick={onClose}>
            ✕
          </Button>
        </div>
        <div className="px-5 py-4">
          <div className="flex gap-4">
            {group.cover_url && (
              <img
                src={group.cover_url}
                alt=""
                className="h-40 w-28 shrink-0 rounded-md border border-border object-cover"
                onError={(e) => (e.currentTarget.style.display = "none")}
              />
            )}
            <div className="min-w-0">
              <div className="text-lg font-semibold leading-tight">{group.title}</div>
              {group.author && <div className="text-sm text-muted">by {group.author}</div>}
              <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted">
                <Badge tone={mediaTone(group.media_label)}>{group.media_label}</Badge>
                {group.is_adult && <Badge tone="red">18+</Badge>}
                {group.chapters != null && <span>{group.chapters.toLocaleString()} chapters</span>}
                <span>
                  · {sources.length} source{sources.length === 1 ? "" : "s"}
                </span>
              </div>
              {group.hooked_work_id && (
                <button className="mt-2" onClick={() => navigate(`/read/${group.hooked_work_id}`)}>
                  <Badge tone="green">in library — open →</Badge>
                </button>
              )}
            </div>
          </div>
          {group.synopsis && <p className="mt-3 text-sm text-text">{group.synopsis}</p>}
          {(hook.isPending || grab.isPending) && (
            <p className="mt-2 text-sm text-accent">Adding to your library…</p>
          )}
          {notice && (
            <p className="mt-2 text-sm text-green-600">
              {notice}{" "}
              {doneWorkId != null && (
                <button className="underline" onClick={() => navigate(`/read/${doneWorkId}`)}>
                  Open
                </button>
              )}
            </p>
          )}
          {error && <p className="mt-2 text-sm text-red-500">Couldn't add: {error}</p>}

          <div className="mb-2 mt-5 flex flex-wrap items-center justify-between gap-x-3 gap-y-1.5">
            <h3 className="text-sm font-semibold uppercase tracking-wide text-muted">
              Sources — choose where to read from
            </h3>
            <label
              className="flex items-center gap-1.5 text-xs text-muted"
              title="Skip chapters you've already read elsewhere — hooking begins at this chapter"
            >
              Start at chapter
              <input
                type="number"
                min={1}
                value={startCh}
                onChange={(e) => setStartCh(e.target.value)}
                placeholder="1"
                className="w-16 rounded-md border border-border bg-bg px-2 py-1 text-sm text-text"
              />
            </label>
          </div>
          {startChapter > 1 && (
            <p className="mb-2 text-[11px] text-muted">
              Will hook from chapter {startChapter} — earlier chapters are skipped.
            </p>
          )}
          <div className="space-y-2">
            {sources.map((s) => (
              <SourceDetailRow
                key={s.catalog_id}
                source={s}
                groupTitle={group.title}
                busy={pendingId === s.catalog_id}
                disabled={hook.isPending || grab.isPending}
                removing={remove.isPending && remove.variables?.id === s.catalog_id}
                onHook={() => hook.mutate(s.catalog_id)}
                onGrab={() => grab.mutate(s.catalog_id)}
                onRemove={(blockDomain) => remove.mutate({ id: s.catalog_id, blockDomain })}
                onOpen={(id) => navigate(`/read/${id}`)}
              />
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

function SourceDetailRow({
  source,
  groupTitle,
  busy,
  disabled,
  removing,
  onHook,
  onGrab,
  onRemove,
  onOpen,
}: {
  source: CatalogSource;
  groupTitle: string;
  busy: boolean;
  disabled: boolean;
  removing: boolean;
  onHook: () => void;
  onGrab: () => void;
  onRemove: (blockDomain: boolean) => void;
  onOpen: (workId: number) => void;
}) {
  const hb = healthBadge(source.health);
  const count = source.chapters_advertised ?? source.chapters_listed;
  const [confirming, setConfirming] = useState(false);
  const [blockDomain, setBlockDomain] = useState(false);
  return (
    <div className="rounded-lg border border-border p-3">
     <div className="flex gap-3">
      {source.cover_url && (
        <img
          src={source.cover_url}
          alt=""
          loading="lazy"
          className="h-20 w-14 shrink-0 rounded border border-border object-cover"
          onError={(e) => (e.currentTarget.style.display = "none")}
        />
      )}
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-1.5">
          <Badge tone={mediaTone(source.media_label)}>{source.media_label}</Badge>
          <Badge tone={source.kind === "online" ? "default" : "violet"}>
            {source.kind === "online" ? source.domain : source.kind}
          </Badge>
          {hb && <Badge tone={hb.tone}>{hb.label}</Badge>}
          {source.hooked_work_id && <Badge tone="green">in library</Badge>}
        </div>
        {/* This source's own matched title (the "sub-title") + author. */}
        <div className="mt-1 truncate text-sm font-medium text-text" title={source.title ?? undefined}>
          {source.title || groupTitle}
        </div>
        {source.author && <div className="truncate text-xs text-muted">by {source.author}</div>}
        <div className="mt-0.5 text-xs text-muted">
          {count != null ? `${count.toLocaleString()} chapters` : "chapter count unknown"}
          {source.health_detail ? ` · ${source.health_detail}` : ""}
        </div>
        <a
          href={source.work_url}
          target="_blank"
          rel="noreferrer"
          className="mt-0.5 block truncate text-[11px] text-muted underline"
        >
          {source.work_url}
        </a>
      </div>
      <div className="flex shrink-0 items-center gap-1">
        {source.hooked_work_id ? (
          <Button size="sm" variant="ghost" onClick={() => onOpen(source.hooked_work_id!)}>
            Open →
          </Button>
        ) : source.listing_only ? (
          <span title="Metadata listing — use Acquire to download">
            <Badge>listing</Badge>
          </span>
        ) : source.kind !== "online" ? (
          source.grab_status ? (
            <Badge tone="green">requested</Badge>
          ) : (
            <Button size="sm" variant="outline" disabled={disabled} onClick={onGrab}>
              {busy ? "Grabbing…" : `Grab via ${source.kind}`}
            </Button>
          )
        ) : (
          <Button size="sm" variant="primary" disabled={disabled} onClick={onHook}>
            {busy ? "Adding…" : "Hook"}
          </Button>
        )}
        {/* Remove broken/unwanted content from the index (bars it from being re-added). */}
        <Button
          size="sm"
          variant="ghost"
          title="Remove from index and block from re-adding"
          disabled={removing}
          onClick={() => setConfirming((v) => !v)}
        >
          🗑
        </Button>
      </div>
     </div>
      {confirming && (
        <div className="mt-2 rounded-lg border border-red-500/30 bg-red-500/5 p-2.5 text-sm">
          <div className="mb-2 text-text">
            Remove this source from the index and block it from being re-added by future crawls?
            {source.hooked_work_id && (
              <span className="text-muted"> Your hooked library copy is kept.</span>
            )}
          </div>
          <label className="mb-2 flex items-center gap-2 text-xs text-muted">
            <input
              type="checkbox"
              checked={blockDomain}
              onChange={(e) => setBlockDomain(e.target.checked)}
            />
            Block the whole domain ({source.domain}), not just this URL
          </label>
          <div className="flex gap-2">
            <Button
              size="sm"
              variant="danger"
              disabled={removing}
              onClick={() => onRemove(blockDomain)}
            >
              {removing ? "Removing…" : "Remove & block"}
            </Button>
            <Button size="sm" variant="ghost" disabled={removing} onClick={() => setConfirming(false)}>
              Cancel
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
