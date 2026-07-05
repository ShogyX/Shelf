// The redesigned Library "home": a full-bleed billboard hero for the top Continue-reading title, then
// horizontal rails (Continue reading / Audiobooks in progress / Wanted / New in your library).
// Rendered above the existing library grid in the default (unsearched, no-shelf) state, so the
// management surface is preserved. All data comes from the existing API/query hooks.
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
import { Link, useNavigate } from "react-router-dom";
import { api, Bookshelf } from "../api/client";
import { qk } from "../api/queryKeys";
import { coverSrc } from "./Cover";
import { FeaturedHero, Dot } from "./FeaturedHero";
import { cleanText } from "../lib/text";
import { useCoverBackdrop } from "../lib/coverBackdrop";
import { CoverCard } from "./CoverCard";
import { Rail } from "./Rail";
import { useAudio } from "../audioStore";
import { useState } from "react";
import { Button, EmptyState } from "./ui";
import WorkDetailModal from "./WorkDetailModal";

function fmtMinsLeft(percent: number, totalChapters: number, t: TFunction): string {
  const left = Math.max(0, Math.round((1 - percent / 100) * totalChapters));
  return left > 0 ? t("library.home.chaptersLeft", { count: left }) : t("library.home.almostDone");
}

// Audiobook progress shown the same way as a book's "68% · 845 chapters left" — percent + time remaining.
function fmtListenProgress(it: { percent: number; total_duration_s: number; global_pos_s: number }, t: TFunction): string {
  const leftS = Math.max(0, (it.total_duration_s || 0) - (it.global_pos_s || 0));
  const h = Math.floor(leftS / 3600), m = Math.floor((leftS % 3600) / 60);
  const left = leftS <= 30
    ? t("library.home.almostDone")
    : h > 0
      ? t("library.home.hoursMinutesLeft", { h, m })
      : t("library.home.minutesLeft", { m });
  return `${Math.round(it.percent)}% · ${left}`;
}

export default function LibraryHome() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const playWork = useAudio((s) => s.playWork);
  const [detailId, setDetailId] = useState<number | null>(null); // open the work detail sheet
  const clear = useMutation({
    mutationFn: (workId: number) => api.clearProgress(workId),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.continue() }),
  });
  const clearAudio = useMutation({
    mutationFn: (workId: number) => api.clearAudioProgress(workId),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.continueListening() }),
  });
  const reading = useQuery({ queryKey: qk.continue(), queryFn: api.continueReading, refetchOnMount: "always" });
  const listening = useQuery({ queryKey: qk.continueListening(), queryFn: api.continueListening, refetchOnMount: "always" });
  const works = useQuery({ queryKey: qk.works("", null), queryFn: () => api.listWorks() });
  // "Wanted" rail: the user's most recent requests (newest first) — a peek at what they're chasing.
  const wantedParams = { scope: "me" as const, sort: "newest" as const, limit: 12, offset: 0 };
  const wanted = useQuery({
    queryKey: qk.wantedRequests(wantedParams),
    queryFn: () => api.listWantedRequests(wantedParams),
  });
  const shelves = useQuery({ queryKey: qk.bookshelves(), queryFn: api.listBookshelves });

  // Per-bookshelf rails: show the first ~6 non-empty shelves. Empty shelves render no rail at all
  // (handled inside ShelfRail), and any beyond the cap roll up into a "Manage shelves" link.
  const shelfList = shelves.data ?? [];
  const railShelves = shelfList.filter((s) => s.count > 0).slice(0, 6);
  const moreShelves = shelfList.filter((s) => s.count > 0).length > railShelves.length;

  const hero = reading.data?.[0];
  // Tint the whole-page aurora with the hero cover's colours (album-art style).
  useCoverBackdrop(coverSrc(hero?.cover_url));
  // "New in your library": the most recently-added works (listWorks returns newest-first already).
  const fresh = (works.data ?? []).slice(0, 12);
  // "Audiobooks": every library title that's an audiobook — a native audio work OR an ebook with a
  // paired 🎧 listen format. Same definition as the /library/browse audio filter so "See all" lines
  // up. The rail self-hides when empty (Rail renders nothing with no children).
  const audiobooks = (works.data ?? [])
    .filter((w) => w.media_kind === "audio" || w.audiobook_work_id != null)
    .slice(0, 12);
  // The hero's blurb comes from the already-loaded works list (no extra fetch).
  const heroBlurb = cleanText(works.data?.find((w) => w.id === hero?.work_id)?.description) || null;

  return (
    <div className="page-in">
      {/* ---- Featured title (Continue reading) ---- */}
      {hero && (
        <FeaturedHero
          eyebrow={t("library.home.continueReading")}
          title={hero.title}
          author={hero.author ?? t("library.home.unknownAuthor")}
          meta={<><Dot /><span>{Math.round(hero.percent)}% · {fmtMinsLeft(hero.percent, hero.total_chapters, t)}</span></>}
          description={heroBlurb}
          coverUrl={hero.cover_url}
          actions={
            <>
              <button
                onClick={() => navigate(`/read/${hero.work_id}/${hero.chapter_id}`)}
                className="flex items-center gap-2 rounded-xl bg-accent px-6 py-3 text-[15px] font-bold text-accent-fg shadow-[0_8px_24px_color-mix(in_srgb,var(--accent)_40%,transparent)] transition hover:-translate-y-0.5"
              >{t("library.home.continueReadingBtn")}</button>
              <button
                onClick={() => setDetailId(hero.work_id)}
                title={t("library.home.details")} aria-label={t("library.home.details")}
                className="flex h-12 w-12 shrink-0 items-center justify-center rounded-full border border-[var(--hair-strong,var(--border))] bg-[color-mix(in_srgb,var(--surface)_70%,transparent)] text-lg text-text backdrop-blur transition hover:bg-surface"
              >ⓘ</button>
            </>
          }
        />
      )}

      {/* ---- Rails ---- */}
      <div className="mx-auto max-w-6xl px-5 sm:px-6 pt-4">
        {/* The dense manage-everything surface (full grid + multi-select + per-shelf filter) lives on
            /library/browse — reached implicitly via each rail's "See all" link (no floating control). */}

        {/* Completely empty library → a single clear call to action (the rails below all skip). */}
        {!hero && works.isSuccess && (works.data ?? []).length === 0 && (
          <EmptyState
            title={t("library.home.heroEmptyTitle")}
            hint={t("library.home.heroEmptyHint")}
            action={<Link to="/discover"><Button variant="primary">{t("library.home.addFirstWork")}</Button></Link>}
          />
        )}

        <Rail title={t("library.home.continueReading")} moreLabel={t("library.home.browseAll")} moreTo="/library/browse?shelf=all">
          {(reading.data ?? []).map((it) => (
            <CoverCard key={it.work_id} title={it.title} author={it.author} coverUrl={it.cover_url}
              progress={it.percent} subtitle={it.chapter_title} to={`/read/${it.work_id}/${it.chapter_id}`}
              onClear={() => clear.mutate(it.work_id)} />
          ))}
        </Rail>

        <Rail title={t("library.home.continueListening")}>
          {(listening.data ?? []).map((it) => (
            <CoverCard key={it.work_id} title={it.title} author={it.author} coverUrl={it.cover_url}
              kind="audio" progress={it.percent} subtitle={fmtListenProgress(it, t)}
              onClick={() => playWork(it.work_id, { track: it.track, posS: it.pos_s })}
              onClear={() => clearAudio.mutate(it.work_id)} />
          ))}
        </Rail>

        <Rail title={t("library.home.wantedRail")} moreLabel={t("library.home.openWanted")} moreTo="/wanted">
          {(wanted.data?.items ?? []).map((m) => (
            <CoverCard key={m.id} title={m.title} author={m.author} coverUrl={m.cover_url}
              subtitle={m.author ?? t("library.home.wanted")} onClick={() => navigate("/wanted")} />
          ))}
        </Rail>

        <Rail title={t("library.home.newInLibrary")} moreLabel={t("library.home.browseAll")} moreTo="/library/browse?shelf=all">
          {fresh.map((w) => (
            <CoverCard key={w.id} title={w.title} author={w.author} coverUrl={w.cover_url}
              kind={w.media_kind === "comic" ? "comic" : "book"} onClick={() => setDetailId(w.id)} />
          ))}
        </Rail>

        {/* All audiobooks in the library (not just in-progress). Tapping a card starts playback of the
            audio work (the native audio work itself, or the ebook's paired 🎧 listen format). */}
        <Rail title={t("library.home.audiobooks")} moreLabel={t("library.home.browseAll")} moreTo="/library/browse?shelf=all">
          {audiobooks.map((w) => (
            <CoverCard key={w.id} title={w.title} author={w.author} coverUrl={w.cover_url}
              kind="audio"
              onClick={() => playWork(w.media_kind === "audio" ? w.id : w.audiobook_work_id!)} />
          ))}
        </Rail>

        {/* One rail per bookshelf (top ~12 of each). Empty shelves skip entirely (no blank rail). */}
        {railShelves.map((s) => (
          <ShelfRail key={s.id} shelf={s} onOpen={setDetailId} />
        ))}
        {moreShelves && (
          <div className="mt-8 px-1">
            <Link to="/settings#bookshelves" className="text-[13px] font-semibold text-[var(--accent-bright,var(--accent))] opacity-90 hover:opacity-100">
              {t("library.home.manageShelves")}
            </Link>
          </div>
        )}
      </div>
      {detailId != null && <WorkDetailModal workId={detailId} onClose={() => setDetailId(null)} />}
    </div>
  );
}

// One bookshelf's rail: its top ~12 works (one listWorks query per shelf — fine at this scale). The
// Rail renders nothing when it has no children, so a shelf whose works haven't loaded (or emptied
// out) collapses cleanly. "See all" deep-links to the shelf-filtered Browse grid.
function ShelfRail({ shelf, onOpen }: { shelf: Bookshelf; onOpen: (workId: number) => void }) {
  const { t } = useTranslation();
  const q = useQuery({
    queryKey: qk.works("", shelf.id),
    queryFn: () => api.listWorks("", { shelfId: shelf.id }),
  });
  const items = (q.data ?? []).slice(0, 12);
  return (
    <Rail title={shelf.name} moreLabel={t("library.home.browseAll")} moreTo={`/library/browse?shelf=${shelf.id}`}>
      {items.map((w) => (
        <CoverCard key={w.id} title={w.title} author={w.author} coverUrl={w.cover_url}
          kind={w.media_kind === "comic" ? "comic" : "book"} onClick={() => onOpen(w.id)} />
      ))}
    </Rail>
  );
}
