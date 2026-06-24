// The redesigned Library "home": a full-bleed billboard hero for the top Continue-reading title, then
// horizontal rails (Continue reading / Audiobooks in progress / From your watchlist / New in your
// library). Rendered above the existing library grid in the default (unsearched, no-shelf) state, so
// the management surface is preserved. All data comes from the existing API/query hooks.
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { qk } from "../api/queryKeys";
import Cover, { coverSrc } from "./Cover";
import { cleanText } from "../lib/text";
import { CoverCard } from "./CoverCard";
import { Rail } from "./Rail";
import { useAudio } from "../audioStore";
import { useState } from "react";
import WorkDetailModal from "./WorkDetailModal";

function fmtMinsLeft(percent: number, totalChapters: number): string {
  const left = Math.max(0, Math.round((1 - percent / 100) * totalChapters));
  return left > 0 ? `${left} chapter${left === 1 ? "" : "s"} left` : "almost done";
}

export default function LibraryHome() {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const playWork = useAudio((s) => s.playWork);
  const [detailId, setDetailId] = useState<number | null>(null); // open the work detail sheet
  const clear = useMutation({
    mutationFn: (workId: number) => api.clearProgress(workId),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.continue() }),
  });
  const reading = useQuery({ queryKey: qk.continue(), queryFn: api.continueReading, refetchOnMount: "always" });
  const listening = useQuery({ queryKey: qk.continueListening(), queryFn: api.continueListening, refetchOnMount: "always" });
  const works = useQuery({ queryKey: qk.works("", null), queryFn: () => api.listWorks() });
  const missing = useQuery({ queryKey: qk.missing(), queryFn: () => api.listMissing() });

  const hero = reading.data?.[0];
  // "New in your library": the most recently-added works (listWorks returns newest-first already).
  const fresh = (works.data ?? []).slice(0, 12);
  // The hero's blurb comes from the already-loaded works list (no extra fetch).
  const heroBlurb = cleanText(works.data?.find((w) => w.id === hero?.work_id)?.description) || null;

  return (
    <div className="page-in">
      {/* ---- Billboard hero ---- */}
      {hero && (
        <section className="relative mb-2 h-[440px] overflow-hidden sm:h-[480px]">
          {/* full-bleed cover art (generative fallback is `bare` — no printed title — so it can't
              duplicate the hero title below it) */}
          <div className="absolute inset-0">
            {coverSrc(hero.cover_url) ? (
              <img src={coverSrc(hero.cover_url)!} alt="" className="h-full w-full object-cover" />
            ) : (
              <Cover title={hero.title} author={hero.author} bare />
            )}
          </div>
          {/* layered scrims for left-aligned legibility */}
          <div className="absolute inset-0" style={{
            background:
              "radial-gradient(120% 90% at 80% 10%, transparent, color-mix(in srgb, var(--bg) 35%, transparent) 55%)," +
              "linear-gradient(90deg, var(--bg) 8%, color-mix(in srgb, var(--bg) 30%, transparent) 52%, transparent 78%)," +
              "linear-gradient(0deg, var(--bg) 3%, transparent 42%)",
          }} />
          <div className="absolute inset-0 mx-auto flex max-w-6xl flex-col justify-end px-6 pb-12 sm:px-8">
            <div className="max-w-[560px]">
              <div className="mb-3 flex items-center gap-2.5">
                <span className="inline-flex items-center rounded-full border border-[color-mix(in_srgb,var(--accent)_45%,transparent)] bg-[color-mix(in_srgb,var(--accent)_22%,transparent)] px-2.5 py-1 text-[11px] font-bold uppercase tracking-wider text-[var(--accent-bright,var(--accent))]">
                  Continue
                </span>
                <span className="text-[13px] font-semibold text-[var(--text-soft,var(--muted))]">
                  {Math.round(hero.percent)}% · {fmtMinsLeft(hero.percent, hero.total_chapters)}
                </span>
              </div>
              <h1 className="font-display text-[42px] font-semibold leading-[1.04] tracking-tight text-text drop-shadow-sm sm:text-[56px]">
                {hero.title}
              </h1>
              <div className="mt-2.5 text-[15px] font-semibold text-[var(--text-soft,var(--muted))]">{hero.author ?? "Unknown author"}</div>
              {heroBlurb && (
                <p className="mt-3 line-clamp-2 max-w-[520px] text-[14px] leading-relaxed text-[var(--text-soft,var(--muted))]">
                  {heroBlurb}
                </p>
              )}
              <div className="mt-6 flex items-center gap-3">
                <button
                  onClick={() => navigate(`/read/${hero.work_id}/${hero.chapter_id}`)}
                  className="flex items-center gap-2 rounded-xl bg-text px-6 py-3 text-[15px] font-bold text-bg shadow-[0_8px_24px_rgba(0,0,0,0.25)] transition hover:-translate-y-0.5"
                >▶ Resume reading</button>
                <button
                  onClick={() => navigate("/watchlist")}
                  className="flex items-center gap-2 rounded-xl border border-[var(--hair-strong,var(--border))] bg-[color-mix(in_srgb,var(--surface)_60%,transparent)] px-5 py-3 text-[15px] font-semibold text-text backdrop-blur transition hover:bg-surface"
                >+ Watchlist</button>
                <button
                  onClick={() => setDetailId(hero.work_id)}
                  title="Details" aria-label="Title details"
                  className="flex h-12 w-12 shrink-0 items-center justify-center rounded-full border border-[var(--hair-strong,var(--border))] bg-[color-mix(in_srgb,var(--surface)_60%,transparent)] text-lg text-text backdrop-blur transition hover:bg-surface"
                >ⓘ</button>
              </div>
            </div>
          </div>
        </section>
      )}

      {/* ---- Rails ---- */}
      <div className="mx-auto max-w-6xl px-5 sm:px-6">
        <Rail title="Continue reading">
          {(reading.data ?? []).map((it) => (
            <CoverCard key={it.work_id} title={it.title} author={it.author} coverUrl={it.cover_url}
              progress={it.percent} subtitle={it.chapter_title} to={`/read/${it.work_id}/${it.chapter_id}`}
              onClear={() => clear.mutate(it.work_id)} />
          ))}
        </Rail>

        <Rail title="Audiobooks in progress">
          {(listening.data ?? []).map((it) => (
            <CoverCard key={it.work_id} title={it.title} author={it.author} coverUrl={it.cover_url}
              kind="audio" progress={it.percent}
              onClick={() => playWork(it.work_id, { track: it.track, posS: it.pos_s })} />
          ))}
        </Rail>

        <Rail title="From your watchlist" moreLabel="Open watchlist" moreTo="/watchlist">
          {(missing.data ?? []).slice(0, 12).map((m) => (
            <CoverCard key={m.id} title={m.title} author={m.author} coverUrl={m.cover_url}
              subtitle={m.author ?? "Wanted"} onClick={() => navigate("/watchlist")} />
          ))}
        </Rail>

        <Rail title="New in your library" moreLabel="See all">
          {fresh.map((w) => (
            <CoverCard key={w.id} title={w.title} author={w.author} coverUrl={w.cover_url}
              kind={w.media_kind === "comic" ? "comic" : "book"} onClick={() => setDetailId(w.id)} />
          ))}
        </Rail>
      </div>
      {detailId != null && <WorkDetailModal workId={detailId} onClose={() => setDetailId(null)} />}
    </div>
  );
}
