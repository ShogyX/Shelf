// Shared catalog UI — a discovered-work card + its full detail modal. Extracted from the Index
// page so the new discovery rows and the /browse grid render titles identically (and hook the
// same way). Pure move: no behavior change.
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, CatalogGroup, CatalogSource, GatedResult } from "../../api/client";
import { qk } from "../../api/queryKeys";
import { Badge, Button, Card, Modal, OverflowMenu, SectionHeader, Spinner, StatusChip, type StatusTone } from "../ui";
import { LanguageBadge } from "../LanguageBadge";
import Cover, { coverSrc } from "../Cover";
import { useAudio } from "../../audioStore";
import { useApp } from "../../store";
import { useIsAdmin } from "../../auth";
import { useConfirm } from "../confirm";
import { AcquireFormat, useAcquirePrompt, useShelfPrompt } from "../ShelfPrompt";
import { healthBadge, Tone } from "../IndexShared";
import { BookOpen, Headphones } from "lucide-react";

// Source health → a StatusChip tone (the redesign's semantic palette) for the detail source rows.
function healthTone(h: string): StatusTone {
  switch (h) {
    case "ok": return "success";
    case "incomplete": return "warning";
    case "no_chapters":
    case "unreachable": return "danger";
    default: return "neutral";
  }
}

// An acquire/grab can come back "gated" when the title is known-unavailable and not yet due for a
// re-check. The non-gated acquire shape carries `status: string`, so a literal compare won't narrow
// the union — this guard does the discrimination explicitly.
function isGated(r: { status?: unknown } | null | undefined): r is GatedResult {
  return !!r && (r as { status?: unknown }).status === "gated";
}

// acquireCatalog returns a single result for ebook/audiobook, but `{ ebook, audiobook }` for
// variant="both". The non-"both" call sites only ever see the single shape at runtime; this
// collapses the union (picking the ebook half of a "both" response) so they can read .status/.work_id.
type AcquireOne = { route: string | null; status: string; work_id?: number; job_id?: number; detail?: string };
function singleResult(
  r: AcquireOne | GatedResult | { ebook: AcquireOne | GatedResult; audiobook: AcquireOne | GatedResult },
): AcquireOne | GatedResult {
  return "audiobook" in r ? r.ebook : r;
}

// A friendly day for the "re-check around <date>" gated hint (no time-of-day — the gate is a daily
// window). Takes t() for the "soon" fallback since it runs outside a component.
function gatedDate(iso: string, t: (k: string) => string): string {
  const d = new Date(iso);
  return isNaN(d.getTime())
    ? t("catalog.soon")
    : d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

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
  canStock,
}: {
  group: CatalogGroup;
  onOpenDetail: () => void;
  // Whether the operator-stock option may be offered (isAdmin && stock pipeline configured). Lifted to
  // the page so a 60-card grid shares ONE stock-summary query instead of one per card (FE-M2).
  canStock: boolean;
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const toast = useApp((s) => s.toast);
  const pickShelf = useShelfPrompt();
  const pickAcquire = useAcquirePrompt();
  const isAdmin = useIsAdmin();
  const refetchCover = useMutation({
    mutationFn: () => api.refetchGroupCover(group.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.catalog() });
      toast(t("catalog.fetchedNewCover", { title: group.title }), "success");
    },
    onError: (e) => toast((e as Error).message, "error"),
  });
  const [error, setError] = useState<string | null>(null);
  const [pendingId, setPendingId] = useState<number | null>(null);
  // Non-blocking hook: show a processing → done message in place; never yank the user away.
  const [doneWorkId, setDoneWorkId] = useState<number | null>(null);

  const hook = useMutation({
    mutationFn: ({ catalogId, shelfId }: { catalogId: number; shelfId?: number }) =>
      api.hookCatalog(catalogId, undefined, shelfId),
    onMutate: ({ catalogId }) => {
      setPendingId(catalogId);
      setError(null);
      setDoneWorkId(null);
    },
    onSuccess: (work) => {
      qc.invalidateQueries({ queryKey: qk.works() });
      qc.invalidateQueries({ queryKey: qk.catalog() });
      qc.invalidateQueries({ queryKey: qk.catalogStats() });
      setDoneWorkId(work.id);
      toast(t("catalog.addedToLibrary", { title: group.title }), "success");
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
      qc.invalidateQueries({ queryKey: qk.catalog() });
      qc.invalidateQueries({ queryKey: qk.downloads() });
      setError(null);
      setDoneWorkId(-1); // sentinel: a grab was queued (message shown below)
      toast(t("catalog.fetchingAddedToSources", { title: group.title }), "success");
    },
    onError: (e) => setError((e as Error).message),
    onSettled: () => setPendingId(null),
  });

  // Priority-driven acquire, variant-aware (ebook / audiobook / both). Resolves to the user's
  // preferred route (hook a web source, grab via a manager, or download via the usenet pipeline) —
  // whichever can fulfill it first. An in-stock ebook hooks instantly; a not-in-stock format queues.
  // "both" returns { ebook, audiobook }; ebook/audiobook return a single result. status "none" on a
  // half is NOT an error — it just means that format didn't match — so we surface it softly.
  const fetched = (x: { status: string } | GatedResult) => !isGated(x) && x.status !== "none";
  const acquire = useMutation({
    // group.id is the GROUP key (not a CatalogWork id), so acquire via a representative source's
    // catalog_id; the backend re-clusters by title to consider every route across the group.
    mutationFn: ({ repId, shelfId, variant }: { repId: number; shelfId?: number; variant: AcquireFormat }) =>
      api.acquireCatalog(repId, undefined, shelfId, variant),
    onMutate: () => {
      setPendingId(group.id);
      setError(null);
      setDoneWorkId(null);
    },
    onSuccess: (raw, vars) => {
      qc.invalidateQueries({ queryKey: qk.works() });
      qc.invalidateQueries({ queryKey: qk.catalog() });
      qc.invalidateQueries({ queryKey: qk.downloads() });
      if (vars.variant === "both" && "ebook" in raw) {
        const eb = raw.ebook, abOk = fetched(raw.audiobook);
        if (!isGated(eb) && eb.status === "hooked" && eb.work_id) setDoneWorkId(eb.work_id);
        else if (fetched(eb) || abOk) setDoneWorkId(-1);
        const ebOk = fetched(eb);
        if (ebOk && abOk) toast(t("catalog.addingEbookAudiobook", { title: group.title }), "success");
        else if (ebOk) toast(t("catalog.addingEbookNoAudio", { title: group.title }), "success");
        else if (abOk) toast(t("catalog.fetchingAudioNoEbook", { title: group.title }), "success");
        else toast(t("catalog.nothingFoundBoth", { title: group.title }), "info");
        return;
      }
      const r = singleResult(raw);
      const isAudio = vars.variant === "audiobook";
      if (isGated(r)) {
        toast(isAudio
          ? t("catalog.gatedAudio", { title: group.title, date: gatedDate(r.next_check_at, t) })
          : t("catalog.gated", { title: group.title, date: gatedDate(r.next_check_at, t) }), "info");
      } else if (r.status === "hooked" && r.work_id) {
        setDoneWorkId(r.work_id);
        toast(t("catalog.addedToLibrary", { title: group.title }), "success");
      } else if (r.status === "none") {
        toast(isAudio ? t("catalog.noAudiobookFound", { title: group.title }) : t("catalog.noSourceFulfil", { title: group.title }), isAudio ? "info" : "error");
      } else {
        setDoneWorkId(-1); // downloading / grabbed → "queued" message
        toast(t("catalog.fetchingAddedToSources", { title: group.title }), "success");
      }
    },
    onError: (e) => setError((e as Error).message),
    onSettled: () => setPendingId(null),
  });

  // Book-fuzzing: when normal matching can't find it, download every loose match and verify which
  // (if any) is the real book.
  const fuzz = useMutation({
    mutationFn: (shelfId?: number) => api.grabPipeline(group.id, { fuzz: true, shelfId }),
    onMutate: () => {
      setPendingId(group.id);
      setError(null);
    },
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: qk.downloads() });
      if (isGated(r)) {
        toast(t("catalog.gated", { title: group.title, date: gatedDate(r.next_check_at, t) }), "info");
      } else {
        toast(t("catalog.searchingEverySource", { title: group.title }), "success");
      }
    },
    onError: (e) => toast((e as Error).message, "error"),
    onSettled: () => setPendingId(null),
  });

  // Admin-only: offer "save to operator stock" at Acquire time (canStock = isAdmin && stock pipeline
  // configured, decided once at the page level), unless the title is already available. Stock = a
  // shared pre-fetch pool.
  const allowStock = canStock && !group.hooked_work_id;
  const stock = useMutation({
    mutationFn: () => api.queueStock({ name: group.title, group_ids: [group.id] }),
    onMutate: () => { setPendingId(group.id); setError(null); },
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: qk.catalog() });
      qc.invalidateQueries({ queryKey: qk.stockSummary() });  // refresh stock counts (FE-M1)
      toast(
        r.queued
          ? t("catalog.savingToStock", { title: group.title })
          : t("catalog.alreadyStocked", { title: group.title }),
        r.queued ? "success" : "info",
      );
    },
    onError: (e) => toast((e as Error).message, "error"),
    onSettled: () => setPendingId(null),
  });

  const [showSeries, setShowSeries] = useState(false);
  const [showAuthor, setShowAuthor] = useState(false);
  const follow = useMutation({
    mutationFn: (body: { kind: "author" | "series"; catalog_id?: number; series_name?: string }) =>
      api.follow(body),
    onSuccess: (s) => {
      qc.invalidateQueries({ queryKey: qk.subscriptions() });
      toast(s.kind === "author"
        ? t("catalog.followingAuthorNewTitles", { name: s.display_name })
        : t("catalog.followingSeriesNewTitles", { name: s.display_name }), "success");
    },
    onError: (e) => toast((e as Error).message, "error"),
  });
  // Metadata listings (Google Books / Open Library / Hardcover) are backend-only — they're never
  // shown as selectable sources; acquisition for those goes through Acquire (the usenet pipeline).
  const visibleSources = group.sources.filter((s) => !s.listing_only);
  // When a group carries several editions (e.g. colored vs B/W — distinct titles), label each
  // button by its own title so the user can tell them apart; otherwise media·domain suffices.
  const multiEditions = new Set(visibleSources.map((s) => s.title)).size > 1;
  const busyAny = hook.isPending || grab.isPending || acquire.isPending || fuzz.isPending || stock.isPending;
  // If this title is already a library work, preload its per-title default shelf so the acquire
  // prompt preselects it. Only fetched when there's a hooked work to read it from.
  const hookedWork = useQuery({
    queryKey: qk.work(group.hooked_work_id),
    queryFn: () => api.getWork(group.hooked_work_id!),
    enabled: group.hooked_work_id != null,
  });
  // Ask where to land the title, then run the action. A cancel (undefined) ABORTS — we never fall
  // through to the library.
  const withShelf = (run: (shelfId?: number) => void) => async () => {
    const id = await pickShelf({ defaultShelfId: hookedWork.data?.default_shelf_id ?? undefined });
    if (id === undefined) return;
    run(id ?? undefined);
  };
  // The lone non-listing source, when this card has exactly one (so the action row collapses to a
  // single representative button instead of "View N sources"). Acquire is the primary; this one
  // source's hook/grab/open/listing state is shown beside/within the overflow so the direct route
  // is preserved.
  const soleSource = visibleSources.length === 1 ? visibleSources[0] : null;
  // hover-lift is safe again now that SeriesModal/AuthorModal render as SIBLINGS of this Card (a
  // fixed Modal must not descend from a transformed element or it'd be mis-positioned).
  return (
    <>
    <Card className="flex gap-4 p-4 hover-lift">
      <div className="relative shrink-0">
        <button onClick={onOpenDetail} title={t("catalog.viewDetailsSources")}>
          <div className="aspect-[2/3] w-[7.5rem] overflow-hidden rounded-[11px] border border-[var(--hair,var(--border))] shadow-[var(--pop-shadow)]">
            <Cover title={group.title} author={group.author} coverUrl={group.cover_url} small />
          </div>
        </button>
        {isAdmin && (group.media_kind === "comic" || group.media_label !== "Book" && group.media_label !== "Novel") && (
          <button
            className="absolute bottom-1 right-1 rounded bg-black/60 px-1.5 py-0.5 text-xs text-white hover:bg-black/80 disabled:opacity-50"
            title={t("catalog.fetchNewCoverHint")}
            disabled={refetchCover.isPending}
            onClick={(e) => { e.stopPropagation(); refetchCover.mutate(); }}
          >
            {refetchCover.isPending ? "…" : t("catalog.refetchCover")}
          </button>
        )}
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-start justify-between gap-2">
          <button
            onClick={onOpenDetail}
            className="font-display text-left text-[17px] font-semibold leading-tight text-text hover:text-accent"
            title={t("catalog.viewDetailsSources")}
          >
            {group.title}
          </button>
          {group.hooked_work_id && (
            <button
              className="shrink-0"
              onClick={() => navigate(`/read/${group.hooked_work_id}`)}
              title={group.in_library ? t("catalog.openInLibrary") : t("catalog.inStockOpenToRead")}
            >
              <Badge tone={group.in_library ? "green" : "violet"}>
                {group.in_library ? t("catalog.inLibrary") : t("catalog.inStock")}
              </Badge>
            </button>
          )}
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-muted">
          <Badge tone={mediaTone(group.media_label)}>{group.media_label}</Badge>
          <LanguageBadge language={group.language} />
          {group.match_confidence && group.match_confidence !== "high" && (
            <button
              type="button"
              onClick={(e) => { e.stopPropagation(); onOpenDetail(); }}
              title={t("catalog.verifyMatchHint")}
            >
              <Badge tone={group.match_confidence === "low" ? "amber" : "default"}>
                {group.match_confidence === "low" ? t("catalog.verifyMatch") : t("catalog.verifyMatchQ")}
              </Badge>
            </button>
          )}
          <VariantBadges group={group} />
          {!(group.variants ?? []).length && group.audiobook_in_stock && group.audiobook_work_id && (
            <button
              type="button"
              onClick={(e) => { e.stopPropagation(); useAudio.getState().playWork(group.audiobook_work_id!); }}
              title={t("catalog.listenInStock")}
            >
              <Badge tone="violet">{t("catalog.audiobookBadge")}</Badge>
            </button>
          )}
          {(group.series_count ?? 1) > 1 && (
            <span title={t("catalog.seriesCardHint")}>
              <Badge tone="violet">{t("catalog.vols", { count: group.series_count })}</Badge>
            </span>
          )}
          {group.is_adult && <Badge tone="red">18+</Badge>}
          {group.author && <span className="truncate">{t("common.byAuthor", { author: group.author })}</span>}
          {group.chapters != null && <span>· {t("catalog.chaptersShort", { count: group.chapters.toLocaleString() })}</span>}
        </div>
        {group.synopsis && (
          <p className="mt-1.5 line-clamp-3 text-sm text-[var(--text-soft,var(--muted))]">{group.synopsis}</p>
        )}

        {/* ONE primary action + a single ⋯ overflow for the rest (kills the competing-button wall).
            Primary = Acquire/Add to library (unchanged); secondaries (series, find-anyway, author,
            sources) move into the overflow under the SAME conditions as before. */}
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          {!group.in_library && (
            <Button
              size="sm"
              variant="primary"
              className={group.in_stock
                ? "!bg-emerald-600 !text-white hover:!bg-emerald-500"   // in stock = instant, green like the have-it badges
                : undefined}
              disabled={busyAny}
              onClick={async () => {
                const defaultShelfId = hookedWork.data?.default_shelf_id ?? undefined;
                // Merged primary action:
                //  • IN STOCK → "Add to library": the file is already downloaded, so just pick a
                //    destination and add the stocked ebook to the user's library — no format / source /
                //    save-to-stock choices to make (an audiobook in stock is played via its 🎧 badge,
                //    never a library item).
                //  • NOT IN STOCK → "Acquire": go fetch it (format + source + optional save-to-stock).
                if (group.in_stock) {
                  const dest = await pickShelf({ defaultShelfId });
                  if (dest === undefined) return; // cancelled
                  acquire.mutate({ repId: group.id, shelfId: dest ?? undefined, variant: "ebook" });
                  return;
                }
                if (group.media_kind === "comic") {  // a comic has no audiobook → skip the format choice
                  const dest = await pickShelf({ allowStock, onStock: () => stock.mutate(), defaultShelfId });
                  if (dest === undefined) return; // cancelled, or stock chosen (onStock fired)
                  acquire.mutate({ repId: group.id, shelfId: dest ?? undefined, variant: "ebook" });
                  return;
                }
                const pick = await pickAcquire({ allowStock, onStock: () => stock.mutate(), defaultShelfId, inStock: false });
                if (pick === undefined) return; // cancelled, or stock chosen (onStock fired)
                acquire.mutate({ repId: group.id, shelfId: pick.shelfId ?? undefined, variant: pick.format });
              }}
              title={group.in_stock
                ? t("catalog.inStockAcquireHint")
                : t("catalog.acquireHint")}
            >
              {acquire.isPending
                ? (group.in_stock ? t("catalog.adding") : t("catalog.acquiring"))
                : (group.in_stock ? t("catalog.addToLibrary") : t("catalog.acquire"))}
            </Button>
          )}
          {/* A single non-listing source that's been REQUESTED via a manager shows its "requested"
              status here. A hooked source's "Open ({{domain}})" button is intentionally NOT rendered:
              the in-stock / in-library badge beside the title already opens it to read, so a second
              per-source open button (e.g. "Open (local)") was redundant. */}
          {soleSource && soleSource.grab_status && !soleSource.hooked_work_id && (
            <SourceButton
              source={soleSource}
              multi={false}
              byTitle={multiEditions}
              busy={pendingId === soleSource.catalog_id}
              disabled={busyAny}
              onHook={withShelf((shelfId) => hook.mutate({ catalogId: soleSource.catalog_id, shelfId }))}
              onGrab={() => grab.mutate(soleSource.catalog_id)}
              onOpen={(workId) => navigate(`/read/${workId}`)}
            />
          )}
          <OverflowMenu
            label={t("catalog.moreActionsFor", { title: group.title })}
            items={[
              group.series && {
                label: t("catalog.viewSeries"),
                onClick: () => setShowSeries(true),
              },
              !group.hooked_work_id && {
                label: fuzz.isPending ? t("catalog.searching") : t("catalog.findAnyway"),
                disabled: busyAny,
                onClick: withShelf((shelfId) => fuzz.mutate(shelfId)),
              },
              // The lone online source's direct hook/grab — kept reachable without a competing
              // primary button (Acquire resolves the same route by priority).
              soleSource && !soleSource.hooked_work_id && !soleSource.grab_status && (
                soleSource.kind === "online"
                  ? {
                      label: t("catalog.addFrom", { domain: soleSource.domain }),
                      disabled: busyAny,
                      onClick: withShelf((shelfId) => hook.mutate({ catalogId: soleSource.catalog_id, shelfId })),
                    }
                  : {
                      label: t("catalog.grabVia", { kind: soleSource.kind }),
                      disabled: busyAny,
                      onClick: () => grab.mutate(soleSource.catalog_id),
                    }
              ),
              // Owned titles already auto-gather updates, so a per-item "Follow author" (track) control
              // is redundant on an in-library card — following stays available on not-yet-owned titles.
              group.author && !group.in_library && {
                label: t("catalog.followName", { name: group.author }),
                disabled: follow.isPending,
                onClick: () => follow.mutate({ kind: "author", catalog_id: group.id }),
              },
              group.author && {
                label: t("catalog.requestAllBy", { name: group.author }),
                onClick: () => setShowAuthor(true),
              },
              // The editions/sources are reached via the title/cover (and the "Verify match" chip);
              // the source to acquire is auto-picked by route priority, so no per-source picker here.
            ]}
          />
        </div>
        {busyAny && <p className="mt-1.5 text-xs text-accent">{t("catalog.addingToLibrary")}</p>}
        {/* Inline message is reserved for the PERSISTENT, actionable "Added ✓ / Open" affordance.
            Transient queued/fetching results are surfaced by the toast only (the inline "Queued…"
            line that merely repeated that toast was removed — see Wave 5 feedback-unification). */}
        {doneWorkId != null && doneWorkId > 0 && (
          <p className="mt-1.5 text-xs text-green-600">
            {t("catalog.addedCheck")}{" "}
            <button className="underline" onClick={() => navigate(`/read/${doneWorkId}`)}>
              {t("catalog.open")}
            </button>
          </p>
        )}
        {error && <p className="mt-1 text-xs text-red-500">{t("catalog.couldntAdd", { error })}</p>}
      </div>
    </Card>
    {/* Rendered as SIBLINGS of the Card (not descendants): the Card has `hover-lift` (a transform on
        hover), and a fixed Modal nested under a transformed ancestor would be mis-positioned. */}
    {showSeries && (
      <SeriesModal
        catalogId={group.id}
        seriesName={group.series}
        onClose={() => setShowSeries(false)}
      />
    )}
    {showAuthor && (
      <AuthorModal
        catalogId={group.id}
        authorName={group.author}
        onClose={() => setShowAuthor(false)}
      />
    )}
    </>
  );
}

export function SeriesModal({
  catalogId,
  seriesName,
  onClose,
}: {
  catalogId: number;
  seriesName: string | null;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useApp((s) => s.toast);
  const pickShelf = useShelfPrompt();
  const confirm = useConfirm();
  const q = useQuery({ queryKey: qk.series(catalogId), queryFn: () => api.catalogSeries(catalogId) });
  // Start with nothing selected — picking volumes is a deliberate act. "Grab all" covers the
  // whole-series case without making "fetch everything" the accidental default.
  const [sel, setSel] = useState<Set<string>>(new Set());
  // "Grab all" defaults to CANON only; this opts the bulk actions into the non-canon extras
  // (novellas/side-stories). Individually selecting an extra always works regardless.
  const [includeSpecials, setIncludeSpecials] = useState(false);
  const follow = useMutation({
    mutationFn: (name: string) => api.follow({ kind: "series", catalog_id: catalogId, series_name: name }),
    onSuccess: (s) => {
      qc.invalidateQueries({ queryKey: qk.subscriptions() });
      toast(t("catalog.followingSeriesNewVolumes", { name: s.display_name }), "success");
    },
    onError: (e) => toast((e as Error).message, "error"),
  });

  const fetchM = useMutation({
    mutationFn: ({ all, specials, shelfId }: { all: boolean; specials?: boolean; shelfId?: number }) =>
      api.acquireSeries(catalogId, {
        ...(all ? { all: true, specials: !!specials } : { refs: [...sel] }),
        ...(shelfId != null ? { shelf_id: shelfId } : {}),
      }),
    onSuccess: (r) => {
      const started = r.results.filter((x) =>
        ["downloading", "grabbed", "hooked"].includes(String((x as { status?: string }).status))
      ).length;
      toast(t("catalog.fetchingFromSeries", { started, total: r.results.length }), "success");
      qc.invalidateQueries({ queryKey: qk.downloads() });
      qc.invalidateQueries({ queryKey: qk.catalog() });
      onClose();
    },
    onError: (e) => toast((e as Error).message, "error"),
  });

  const d = q.data;
  const books = d?.books ?? [];
  const selectable = books.filter((b) => !b.in_library && b.ref);   // not in MY library yet (in-stock + missing)
  const inLibrary = books.filter((b) => b.in_library).length;       // in MY library, not merely on disk
  const specialCount = selectable.filter((b) => b.special).length;
  // "Grab all" / "Select all" act on canon by default; the extras toggle widens them to the specials.
  const grabAll = includeSpecials ? selectable : selectable.filter((b) => !b.special);
  const toggle = (ref: string) =>
    setSel((s) => {
      const n = new Set(s);
      n.has(ref) ? n.delete(ref) : n.add(ref);
      return n;
    });

  // Fetch the selection (or the whole series). Confirm before queueing a big batch — a stray click
  // shouldn't kick off 20+ downloads — then pick a destination shelf.
  const fetchSeries = (all: boolean) => async () => {
    const count = all ? grabAll.length : sel.size;
    if (count === 0) return;
    if (
      count > 5 &&
      !(await confirm({
        title: all ? t("catalog.grabWholeSeriesTitle") : t("catalog.fetchSelectedVolumesTitle"),
        message: t("catalog.queueVolumesMessage", { count }),
        confirmText: t("catalog.fetchN", { count }),
      }))
    )
      return;
    const id = await pickShelf();
    if (id === undefined) return; // cancelled → abort
    fetchM.mutate({ all, specials: includeSpecials, shelfId: id ?? undefined });
  };

  const titleNode = (
    <>
      {d?.series || seriesName || t("catalog.series")}
      {books.length > 0 && (
        <span className="ml-2 text-xs font-normal text-muted">
          {t("catalog.volsCount", { count: books.length })}{inLibrary > 0 ? ` · ${t("catalog.inLibraryCount", { count: inLibrary })}` : ""}
        </span>
      )}
    </>
  );
  const footerNode = d?.series && books.length > 0 ? (
    <div className="flex w-full items-center justify-between gap-2">
      <span className="text-xs text-muted">
        {sel.size > 0 ? t("catalog.selectedOf", { count: sel.size, total: selectable.length })
          : includeSpecials ? t("catalog.volumesToFetch", { count: grabAll.length }) : t("catalog.canonVolumesToFetch", { count: grabAll.length })}
      </span>
      <div className="flex items-center gap-2">
        <Button size="sm" variant="ghost" disabled={follow.isPending || !d?.series}
          onClick={() => d?.series && follow.mutate(d.series)}
          title={t("catalog.followSeriesHint")}>
          {t("catalog.followSeries")}
        </Button>
        <Button size="sm" variant="ghost" disabled={fetchM.isPending || grabAll.length === 0}
          onClick={fetchSeries(true)}
          title={includeSpecials ? t("catalog.grabAllInclExtrasHint") : t("catalog.grabAllCanonHint")}>
          {t("catalog.grabAll")}
        </Button>
        <Button size="sm" variant="primary" disabled={sel.size === 0 || fetchM.isPending} onClick={fetchSeries(false)}>
          {fetchM.isPending ? t("catalog.fetching") : t("catalog.fetchSelected", { count: sel.size })}
        </Button>
      </div>
    </div>
  ) : undefined;

  return (
    <Modal variant="fullscreen-sheet" width="max-w-xl" title={titleNode} footer={footerNode} onClose={onClose}>
      {q.isLoading ? (
        <Spinner label={t("catalog.findingSeriesBooks")} />
      ) : !d?.series || books.length === 0 ? (
        <p className="text-sm text-muted">{t("catalog.noSeriesInfo")}</p>
      ) : (
        <>
          <div className="mb-2 flex items-center justify-between gap-2">
            <span className="text-xs font-medium uppercase tracking-wide text-muted">
              {t("catalog.chooseVolumes")}
            </span>
            <div className="flex gap-1.5">
              <Button size="sm" variant="ghost" disabled={grabAll.length === 0}
                onClick={() => setSel(new Set(grabAll.map((b) => b.ref!)))}>
                {t("catalog.selectAll")}
              </Button>
              <Button size="sm" variant="ghost" disabled={sel.size === 0} onClick={() => setSel(new Set())}>
                {t("catalog.clear")}
              </Button>
            </div>
          </div>
          {specialCount > 0 && (
            <label className="mb-2 flex cursor-pointer items-center gap-2 rounded-lg bg-surface-2 px-2.5 py-1.5 text-xs text-muted">
              <input type="checkbox" className="h-3.5 w-3.5 accent-[var(--accent)]"
                checked={includeSpecials} onChange={(e) => setIncludeSpecials(e.target.checked)} />
              <span>{t("catalog.includeExtras", { count: specialCount })}</span>
            </label>
          )}
          <div className="space-y-1.5">
            {books.map((b) => {
                  const selected = !!b.ref && sel.has(b.ref);
                  const locked = !!b.in_library || !b.ref;   // already mine, or nothing to fetch
                  return (
                    <label
                      key={b.ref ?? b.title}
                      className={`flex items-center gap-3 rounded-xl border px-2.5 py-2 text-sm transition ${
                        locked
                          ? "cursor-default border-transparent opacity-70"
                          : selected
                            ? "cursor-pointer border-accent bg-accent/10"
                            : "cursor-pointer border-[var(--hair,var(--border))] hover:bg-surface-2"
                      }`}
                    >
                      <input
                        type="checkbox"
                        className="h-4 w-4 shrink-0 accent-[var(--accent)]"
                        disabled={locked}
                        checked={selected}
                        onChange={() => b.ref && toggle(b.ref)}
                      />
                      {b.cover_url ? (
                        <img
                          src={coverSrc(b.cover_url) ?? ""}
                          alt=""
                          loading="lazy"
                          className="aspect-[2/3] h-12 shrink-0 rounded-md border border-[var(--hair,var(--border))] object-cover"
                          onError={(e) => (e.currentTarget.style.visibility = "hidden")}
                        />
                      ) : (
                        <div className="aspect-[2/3] h-12 shrink-0 rounded-md border border-[var(--hair,var(--border))] bg-surface-2" />
                      )}
                      <div className="min-w-0 flex-1">
                        <div className="truncate text-text">
                          {b.position ? <span className="text-muted">#{b.position} </span> : ""}
                          {b.title}
                          {b.year ? <span className="text-muted"> ({b.year})</span> : null}
                        </div>
                        {b.author && <div className="truncate text-xs text-muted">{t("common.byAuthor", { author: b.author })}</div>}
                      </div>
                      {b.in_library ? <Badge tone="green">{t("catalog.inLibrary")}</Badge>
                        : b.in_stock ? <Badge tone="violet">{t("catalog.inStock")}</Badge>
                        : b.special ? <Badge tone="amber">{t("catalog.extra")}</Badge> : null}
                    </label>
                  );
                })}
              </div>
            </>
          )}
    </Modal>
  );
}

export function AuthorModal({
  catalogId,
  authorName,
  onClose,
}: {
  catalogId: number;
  authorName: string | null;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const toast = useApp((s) => s.toast);
  const pickShelf = useShelfPrompt();
  const confirm = useConfirm();
  const q = useQuery({ queryKey: qk.author(catalogId), queryFn: () => api.catalogAuthor(catalogId) });
  // Start with nothing selected — picking books is a deliberate act; "Request all" covers the
  // whole-roster case without making "fetch everything by this author" the accidental default.
  const [sel, setSel] = useState<Set<string>>(new Set());

  const fetchM = useMutation({
    mutationFn: ({ all, shelfId }: { all: boolean; shelfId?: number }) =>
      api.acquireAuthor(catalogId, {
        ...(all ? { all: true } : { refs: [...sel] }),
        ...(shelfId != null ? { shelf_id: shelfId } : {}),
      }),
    onSuccess: (r) => {
      const started = r.results.filter((x) =>
        ["downloading", "grabbed", "hooked"].includes(String((x as { status?: string }).status))
      ).length;
      toast(t("catalog.fetchingByAuthor", { started, total: r.results.length }), "success");
      qc.invalidateQueries({ queryKey: qk.downloads() });
      qc.invalidateQueries({ queryKey: qk.catalog() });
      onClose();
    },
    onError: (e) => toast((e as Error).message, "error"),
  });

  const d = q.data;
  const books = d?.books ?? [];
  const selectable = books.filter((b) => !b.in_library && b.ref);   // not in MY library yet (in-stock + missing)
  const inLibrary = books.filter((b) => b.in_library).length;       // in MY library, not merely on disk
  // The FULL roster count from the backend (the acquire is server-capped at 30), so the confirm is honest.
  const fullCount = d?.count ?? selectable.length;
  const toggle = (ref: string) =>
    setSel((s) => {
      const n = new Set(s);
      n.has(ref) ? n.delete(ref) : n.add(ref);
      return n;
    });

  // Fetch the selection (or every not-owned book). Confirm before queueing a big batch; the count
  // shown is the FULL roster even though the server caps the actual queue (e.g. "Queue 30 of 142?").
  const CAP = 30;
  const fetchAuthor = (all: boolean) => async () => {
    const count = all ? selectable.length : sel.size;
    if (count === 0) return;
    const queued = Math.min(count, CAP);
    if (
      count > 5 &&
      !(await confirm({
        title: all ? t("catalog.requestAllByAuthorTitle") : t("catalog.fetchSelectedBooksTitle"),
        message:
          all && fullCount > CAP
            ? t("catalog.queueBooksCappedMessage", { queued, full: fullCount, cap: CAP })
            : t("catalog.queueBooksMessage", { count: queued }),
        confirmText: t("catalog.fetchN", { count: queued }),
      }))
    )
      return;
    const id = await pickShelf();
    if (id === undefined) return; // cancelled → abort
    fetchM.mutate({ all, shelfId: id ?? undefined });
  };

  const titleNode = (
    <>
      {d?.author || authorName || t("catalog.author")}
      {books.length > 0 && (
        <span className="ml-2 text-xs font-normal text-muted">
          {t("catalog.booksCount", { count: fullCount })}{inLibrary > 0 ? ` · ${t("catalog.inLibraryCount", { count: inLibrary })}` : ""}
        </span>
      )}
    </>
  );
  const footerNode = books.length > 0 ? (
    <div className="flex w-full items-center justify-between gap-2">
      <span className="text-xs text-muted">
        {sel.size > 0 ? t("catalog.selectedOf", { count: sel.size, total: selectable.length }) : t("catalog.availableToFetch", { count: selectable.length })}
      </span>
      <div className="flex items-center gap-2">
        <Button size="sm" variant="ghost" disabled={fetchM.isPending || selectable.length === 0}
          onClick={fetchAuthor(true)} title={t("catalog.requestAllHint")}>
          {t("catalog.requestAll")}
        </Button>
        <Button size="sm" variant="primary" disabled={sel.size === 0 || fetchM.isPending} onClick={fetchAuthor(false)}>
          {fetchM.isPending ? t("catalog.fetching") : t("catalog.fetchSelected", { count: sel.size })}
        </Button>
      </div>
    </div>
  ) : undefined;

  return (
    <Modal variant="fullscreen-sheet" width="max-w-xl" title={titleNode} footer={footerNode} onClose={onClose}>
      {q.isLoading ? (
        <Spinner label={t("catalog.findingAuthorBooks")} />
      ) : books.length === 0 ? (
        <p className="text-sm text-muted">{t("catalog.noBooksForAuthor")}</p>
      ) : (
        <>
          <div className="mb-2 flex items-center justify-between gap-2">
            <span className="text-xs font-medium uppercase tracking-wide text-muted">
              {t("catalog.chooseBooks")}
            </span>
            <div className="flex gap-1.5">
              <Button size="sm" variant="ghost" disabled={selectable.length === 0}
                onClick={() => setSel(new Set(selectable.map((b) => b.ref!)))}>
                {t("catalog.selectAll")}
              </Button>
              <Button size="sm" variant="ghost" disabled={sel.size === 0} onClick={() => setSel(new Set())}>
                {t("catalog.clear")}
              </Button>
            </div>
          </div>
          <div className="space-y-1.5">
            {books.map((b) => {
                  const selected = !!b.ref && sel.has(b.ref);
                  const locked = !!b.in_library || !b.ref;   // already mine, or nothing to fetch
                  return (
                    <label
                      key={b.ref ?? b.title}
                      className={`flex items-center gap-3 rounded-xl border px-2.5 py-2 text-sm transition ${
                        locked
                          ? "cursor-default border-transparent opacity-70"
                          : selected
                            ? "cursor-pointer border-accent bg-accent/10"
                            : "cursor-pointer border-[var(--hair,var(--border))] hover:bg-surface-2"
                      }`}
                    >
                      <input
                        type="checkbox"
                        className="h-4 w-4 shrink-0 accent-[var(--accent)]"
                        disabled={locked}
                        checked={selected}
                        onChange={() => b.ref && toggle(b.ref)}
                      />
                      {b.cover_url ? (
                        <img
                          src={coverSrc(b.cover_url) ?? ""}
                          alt=""
                          loading="lazy"
                          className="aspect-[2/3] h-12 shrink-0 rounded-md border border-[var(--hair,var(--border))] object-cover"
                          onError={(e) => (e.currentTarget.style.visibility = "hidden")}
                        />
                      ) : (
                        <div className="aspect-[2/3] h-12 shrink-0 rounded-md border border-[var(--hair,var(--border))] bg-surface-2" />
                      )}
                      <div className="min-w-0 flex-1">
                        <div className="truncate text-text">
                          {b.title}
                          {b.year ? <span className="text-muted"> ({b.year})</span> : null}
                        </div>
                        {b.author && <div className="truncate text-xs text-muted">{t("common.byAuthor", { author: b.author })}</div>}
                      </div>
                      {b.in_library ? <Badge tone="green">{t("catalog.inLibrary")}</Badge>
                        : b.in_stock ? <Badge tone="violet">{t("catalog.inStock")}</Badge> : null}
                    </label>
                  );
                })}
              </div>
            </>
          )}
    </Modal>
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
  const { t } = useTranslation();
  const hb = healthBadge(source.health);
  const count = source.chapters_advertised ?? source.chapters_listed;
  if (source.hooked_work_id) {
    return (
      <Button size="sm" variant="ghost" onClick={() => onOpen(source.hooked_work_id!)}>
        {t("catalog.openDomain", { domain: source.domain })}
      </Button>
    );
  }
  // Metadata listing (Google Books / Open Library / Hardcover): you can't read or fetch a book
  // FROM these — they only describe it. No hook/grab; use the card's Acquire button (pipeline).
  if (source.listing_only) {
    return (
      <span title={t("catalog.listedOnHint", { domain: source.domain })}>
        <Badge>{t("catalog.listedDomain", { domain: source.domain })}</Badge>
      </span>
    );
  }
  // Integration source (Readarr/Kapowarr): grab it there; Shelf imports the file once it
  // downloads into a watched folder.
  if (source.kind !== "online") {
    if (source.grab_status) {
      return (
        <span title={t("catalog.requestedViaHint", { kind: source.kind })}>
          <Badge tone="green">{t("catalog.requestedKind", { kind: source.kind })}</Badge>
        </span>
      );
    }
    return (
      <Button
        size="sm"
        variant="outline"
        disabled={disabled}
        onClick={onGrab}
        title={t("catalog.addDownloadViaHint", { kind: source.kind, domain: source.domain })}
      >
        {busy ? t("catalog.grabbing") : t("catalog.grabVia", { kind: source.kind })}
      </Button>
    );
  }
  // Mark each source with what it is (Novel / Book / Manga / Webtoon / Comic) + its domain, so a
  // multi-source card makes clear whether you're hooking the novel or the manga. When the card
  // holds multiple EDITIONS (distinct titles), label by title instead so colored vs B/W are clear.
  const label = !multi
    ? t("catalog.addToLibrary")
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
        t("catalog.hookFromHint", { media: source.media_label, domain: source.domain }) +
        (count ? ` · ${t("catalog.chaptersCount", { count })}` : "") +
        (hb ? ` · ${t(hb.label)}` : "")
      }
    >
      {busy ? (
        t("catalog.adding")
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
/** Owned-edition badges: one per (format × language) — "📖 EN", "🎧 NO" — from group.variants.
 *  Audio badges are playable (same affordance as the old single audiobook badge, which this
 *  replaces whenever variants are present). */
export function VariantBadges({ group }: { group: CatalogGroup }) {
  const { t } = useTranslation();
  const variants = group.variants ?? [];
  if (!variants.length) return null;
  const rank = (v: { kind: string; lang: string }) => `${v.kind === "audio" ? 1 : 0}:${v.lang}`;
  return (
    <>
      {[...variants].sort((a, b) => rank(a).localeCompare(rank(b))).map((v) => {
        const label = <>{v.kind === "audio" ? <Headphones className="h-3 w-3" /> : <BookOpen className="h-3 w-3" />}{" "}{v.lang === "other" ? "…" : v.lang.toUpperCase()}</>;
        const hint = t(v.kind === "audio" ? "catalog.variantListen" : "catalog.variantRead",
                       { lang: v.lang.toUpperCase() });
        return v.kind === "audio" ? (
          <button key={rank(v)} type="button" title={hint}
                  onClick={(e) => { e.stopPropagation(); useAudio.getState().playWork(v.work_id); }}>
            <Badge tone="violet">{label}</Badge>
          </button>
        ) : (
          <span key={rank(v)} title={hint}><Badge tone="green">{label}</Badge></span>
        );
      })}
    </>
  );
}


export function CatalogDetail({ group, onClose }: { group: CatalogGroup; onClose: () => void }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const pickShelf = useShelfPrompt();
  const pickAcquire = useAcquirePrompt();
  const isAdmin = useIsAdmin();
  const [error, setError] = useState<string | null>(null);
  const [pendingId, setPendingId] = useState<number | null>(null);

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: qk.works() });
    qc.invalidateQueries({ queryKey: qk.catalog() });
    qc.invalidateQueries({ queryKey: qk.catalogStats() });
  };
  const [doneWorkId, setDoneWorkId] = useState<number | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [startCh, setStartCh] = useState(""); // hook from this chapter (blank = from the start)
  const startChapter = Math.max(1, parseInt(startCh, 10) || 1);
  const hook = useMutation({
    mutationFn: ({ id, shelfId }: { id: number; shelfId?: number }) =>
      api.hookCatalog(id, startChapter, shelfId),
    onMutate: ({ id }) => {
      setPendingId(id);
      setError(null);
      setDoneWorkId(null);
      setNotice(null);
    },
    onSuccess: (work) => {
      invalidate();
      setDoneWorkId(work.id);
      setNotice(
        startChapter > 1 ? t("catalog.addedFromChapter", { chapter: startChapter }) : t("catalog.addedToLibraryCheck")
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
  // Priority-driven one-click acquire (same as the discover card): resolves to the best route —
  // hook a web source, grab via a manager, or download via the usenet pipeline. CRUCIAL for titles
  // whose ONLY sources are metadata listings (books: Google Books / Open Library / Hardcover), which
  // are filtered out of the per-source list below — without this they had NO actionable button.
  const acquire = useMutation({
    mutationFn: ({ shelfId, variant }: { shelfId?: number; variant: AcquireFormat }) =>
      api.acquireCatalog(group.id, undefined, shelfId, variant),
    onMutate: () => { setPendingId(group.id); setError(null); setDoneWorkId(null); setNotice(null); },
    onSuccess: (raw, vars) => {
      invalidate();
      qc.invalidateQueries({ queryKey: qk.downloads() });
      const fetched = (x: { status: string } | GatedResult) => !isGated(x) && x.status !== "none";
      if (vars.variant === "both" && "ebook" in raw) {
        const eb = raw.ebook, abOk = fetched(raw.audiobook);
        if (!isGated(eb) && eb.status === "hooked" && eb.work_id) setDoneWorkId(eb.work_id);
        const ebOk = fetched(eb);
        setNotice(
          ebOk && abOk ? t("catalog.noticeAddingEbookAudio")
            : ebOk ? t("catalog.noticeAddingEbook")
              : abOk ? t("catalog.noticeFetchingAudio")
                : t("catalog.noticeNothingFound"),
        );
        return;
      }
      const r = singleResult(raw);
      const isAudio = vars.variant === "audiobook";
      if (isGated(r)) {
        setNotice(isAudio
          ? t("catalog.noticeGatedAudio", { date: gatedDate(r.next_check_at, t) })
          : t("catalog.noticeGated", { date: gatedDate(r.next_check_at, t) }));
      } else if (r.status === "hooked" && r.work_id) {
        setDoneWorkId(r.work_id);
        setNotice(t("catalog.addedToLibraryCheck"));
      } else if (r.status === "none") {
        if (isAudio) setNotice(t("catalog.noticeNoAudiobook"));
        else setError(t("catalog.noticeNoSource"));
      } else {
        setNotice(t("catalog.noticeFetching"));
      }
    },
    onError: (e) => setError((e as Error).message),
    onSettled: () => setPendingId(null),
  });
  // Admin-only "save to operator stock" alternative to acquiring into one's own library — offered
  // at Acquire time when the stock pipeline is configured and the title isn't already available.
  const stockSummary = useQuery({ queryKey: qk.stockSummary(), queryFn: api.getStockSummary, enabled: isAdmin });
  const allowStock = isAdmin && !!stockSummary.data?.configured && !group.hooked_work_id;
  const stock = useMutation({
    mutationFn: () => api.queueStock({ name: group.title, group_ids: [group.id] }),
    onMutate: () => { setPendingId(group.id); setError(null); setDoneWorkId(null); setNotice(null); },
    onSuccess: (r) => {
      invalidate();
      setNotice(
        r.queued
          ? t("catalog.noticeSavingStock")
          : t("catalog.noticeAlreadyStocked"),
      );
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
        (r.blocked?.scope === "domain" ? t("catalog.removedBlockedDomain") : t("catalog.removedBlocked")) +
          " " + t("catalog.wontBeReadded")
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

  // Ask where to land the title, then run the action. A cancel (undefined) ABORTS.
  const withShelf = (run: (shelfId?: number) => void) => async () => {
    const id = await pickShelf();
    if (id === undefined) return;
    run(id ?? undefined);
  };

  return (
    <Modal variant="fullscreen-sheet" width="max-w-2xl" title={group.title} onClose={onClose}>
          <div className="flex flex-col gap-5 sm:flex-row">
            <div className="mx-auto w-36 shrink-0 sm:mx-0">
              <div className="aspect-[2/3] w-36 overflow-hidden rounded-[13px] border border-[var(--hair,var(--border))] shadow-[var(--pop-shadow)]">
                <Cover title={group.title} author={group.author} coverUrl={group.cover_url} small />
              </div>
            </div>
            <div className="min-w-0 flex-1">
              <h2 className="font-display text-[26px] font-semibold leading-[1.12] text-text sm:text-[30px]">{group.title}</h2>
              {group.author && <div className="mt-1.5 text-sm font-semibold text-[var(--text-soft,var(--muted))]">{t("common.byAuthor", { author: group.author })}</div>}
              <div className="mt-3 flex flex-wrap items-center gap-1.5">
                <Badge>{group.media_label}</Badge>
                <LanguageBadge language={group.language} />
                <VariantBadges group={group} />
                {(group.series_count ?? 1) > 1 && <Badge tone="violet">{t("catalog.vols", { count: group.series_count })}</Badge>}
                {group.is_adult && <Badge tone="red">18+</Badge>}
                {group.chapters != null && <Badge>{t("catalog.chaptersShort", { count: group.chapters.toLocaleString() })}</Badge>}
                <Badge>{t("catalog.sourcesCount", { count: sources.length })}</Badge>
              </div>

              {/* One clear primary action up top: Acquire if not yet owned, Open if it's readable. */}
              <div className="mt-3 flex flex-wrap items-center gap-2">
                {!group.in_library && (
                  <Button
                    variant="primary"
                    disabled={pendingId != null || hook.isPending || grab.isPending || stock.isPending}
                    onClick={async () => {
                      if (group.media_kind === "comic") {
                        const dest = await pickShelf({ allowStock, onStock: () => stock.mutate() });
                        if (dest === undefined) return; // cancelled, or stock chosen (onStock fired)
                        acquire.mutate({ shelfId: dest ?? undefined, variant: "ebook" });
                        return;
                      }
                      const pick = await pickAcquire({ allowStock, onStock: () => stock.mutate(), inStock: group.in_stock });
                      if (pick === undefined) return; // cancelled, or stock chosen (onStock fired)
                      acquire.mutate({ shelfId: pick.shelfId ?? undefined, variant: pick.format });
                    }}
                    title={group.in_stock
                      ? t("catalog.inStockAcquireHint")
                      : t("catalog.acquireHint")}
                  >
                    {pendingId === group.id
                      ? (group.in_stock ? t("catalog.adding") : t("catalog.acquiring"))
                      : (group.in_stock ? t("catalog.addToLibrary") : t("catalog.acquire"))}
                  </Button>
                )}
                {group.hooked_work_id && (
                  <Button
                    variant={group.in_library ? "primary" : "outline"}
                    onClick={() => navigate(`/read/${group.hooked_work_id}`)}
                  >
                    {group.in_library ? t("catalog.open") : t("catalog.openToRead")}
                  </Button>
                )}
              </div>

              {(hook.isPending || grab.isPending) && (
                <p className="mt-2 text-sm text-accent">{t("catalog.addingToLibrary")}</p>
              )}
              {notice && (
                <p className="mt-2 text-sm text-green-600">
                  {notice}{" "}
                  {doneWorkId != null && (
                    <button className="underline" onClick={() => navigate(`/read/${doneWorkId}`)}>
                      {t("catalog.open")}
                    </button>
                  )}
                </p>
              )}
              {error && <p className="mt-2 text-sm text-red-500">{t("catalog.couldntAdd", { error })}</p>}
            </div>
          </div>

          {group.synopsis && <p className="mt-4 text-sm leading-relaxed text-[var(--text-soft,var(--muted))]">{group.synopsis}</p>}

          {sources.length > 0 && (
          <>
          <div className="mb-2 mt-6 flex flex-wrap items-center justify-between gap-x-3 gap-y-1.5">
            <SectionHeader>{t("catalog.readFromSpecificSource")}</SectionHeader>
            <label
              className="flex items-center gap-1.5 text-xs text-muted"
              title={t("catalog.startAtChapterHint")}
            >
              {t("catalog.startAtChapter")}
              <input
                type="number"
                min={1}
                value={startCh}
                onChange={(e) => setStartCh(e.target.value)}
                placeholder="1"
                className="w-16 rounded-md border border-[var(--hair-strong,var(--border))] bg-bg px-2 py-1 text-sm text-text outline-none transition focus:border-accent"
              />
            </label>
          </div>
          {startChapter > 1 && (
            <p className="mb-2 text-[11px] text-muted">
              {t("catalog.willHookFromChapter", { chapter: startChapter })}
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
                onHook={withShelf((shelfId) => hook.mutate({ id: s.catalog_id, shelfId }))}
                onGrab={() => grab.mutate(s.catalog_id)}
                onRemove={(blockDomain) => remove.mutate({ id: s.catalog_id, blockDomain })}
                onOpen={(id) => navigate(`/read/${id}`)}
              />
            ))}
          </div>
          </>
          )}
    </Modal>
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
  const { t } = useTranslation();
  const hb = healthBadge(source.health);
  const count = source.chapters_advertised ?? source.chapters_listed;
  const [confirming, setConfirming] = useState(false);
  const [blockDomain, setBlockDomain] = useState(false);
  return (
    <div
      className={`rounded-xl border bg-surface p-3 ${
        source.hooked_work_id
          ? "border-l-2 border-l-accent border-[var(--hair,var(--border))]"
          : "border-[var(--hair,var(--border))]"
      }`}
    >
     <div className="flex gap-3">
      {source.cover_url ? (
        <img
          src={coverSrc(source.cover_url) ?? ""}
          alt=""
          loading="lazy"
          className="aspect-[2/3] h-20 shrink-0 rounded-md border border-[var(--hair,var(--border))] object-cover shadow-[var(--pop-shadow)]"
          onError={(e) => (e.currentTarget.style.display = "none")}
        />
      ) : (
        <div className="aspect-[2/3] h-20 shrink-0 rounded-md border border-[var(--hair,var(--border))] bg-surface-2" />
      )}
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-1.5">
          {/* Health StatusChip owns the only color here; the facts stay neutral (chip discipline). */}
          {hb && <StatusChip tone={healthTone(source.health)}>{t(hb.label)}</StatusChip>}
          <Badge>{source.media_label}</Badge>
          <Badge>{source.kind === "online" ? source.domain : source.kind}</Badge>
          {source.hooked_work_id && <Badge tone="green">{t("catalog.inLibrary")}</Badge>}
        </div>
        {/* This source's own matched title (the "sub-title") + author. */}
        <div className="mt-1.5 truncate text-sm font-semibold text-text" title={source.title ?? undefined}>
          {source.title || groupTitle}
        </div>
        {source.author && <div className="truncate text-xs text-[var(--text-soft,var(--muted))]">{t("common.byAuthor", { author: source.author })}</div>}
        <div className="mt-0.5 text-xs text-muted">
          {count != null ? t("catalog.chaptersCount", { count: count.toLocaleString() }) : t("catalog.chapterCountUnknown")}
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
      <div className="flex shrink-0 flex-col items-end gap-1.5">
        {source.hooked_work_id ? (
          <Button size="sm" variant="outline" onClick={() => onOpen(source.hooked_work_id!)}>
            {t("catalog.openArrow")}
          </Button>
        ) : source.listing_only ? (
          <span title={t("catalog.metadataListingHint")}>
            <Badge>{t("catalog.listing")}</Badge>
          </span>
        ) : source.kind !== "online" ? (
          source.grab_status ? (
            <Badge tone="green">{t("catalog.requested")}</Badge>
          ) : (
            <Button size="sm" variant="outline" disabled={disabled} onClick={onGrab}>
              {busy ? t("catalog.grabbing") : t("catalog.grabVia", { kind: source.kind })}
            </Button>
          )
        ) : (
          <Button size="sm" variant="primary" disabled={disabled} onClick={onHook}>
            {busy ? t("catalog.adding") : t("catalog.addToLibrary")}
          </Button>
        )}
        {/* Remove from the index (bars re-adding). Kept small + separated from the action above so
            it can't be fat-fingered in place of Hook/Grab. */}
        <button
          type="button"
          className="-my-1 py-1 text-[11px] text-muted underline-offset-2 transition hover:text-red-500 hover:underline disabled:opacity-50"
          title={t("catalog.removeBlockHint")}
          disabled={removing}
          onClick={() => setConfirming((v) => !v)}
        >
          {t("catalog.remove")}
        </button>
      </div>
     </div>
      {confirming && (
        <div className="mt-2 rounded-lg border border-red-500/30 bg-red-500/5 p-2.5 text-sm">
          <div className="mb-2 text-text">
            {t("catalog.removeConfirmQuestion")}
            {source.hooked_work_id && (
              <span className="text-muted"> {t("catalog.hookedCopyKept")}</span>
            )}
          </div>
          <label className="mb-2 flex items-center gap-2 text-xs text-muted">
            <input
              type="checkbox"
              checked={blockDomain}
              onChange={(e) => setBlockDomain(e.target.checked)}
            />
            {t("catalog.blockWholeDomain", { domain: source.domain })}
          </label>
          <div className="flex gap-2">
            <Button
              size="sm"
              variant="danger"
              disabled={removing}
              onClick={() => onRemove(blockDomain)}
            >
              {removing ? t("catalog.removing") : t("catalog.removeAndBlock")}
            </Button>
            <Button size="sm" variant="ghost" disabled={removing} onClick={() => setConfirming(false)}>
              {t("common.cancel")}
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
