// The one persistent <audio> for the whole app. Mounted once at the app root (see App.tsx) and never
// unmounted, so playback survives every route change incl. the reader and a hidden/locked tab. The
// element is registered with the store via attachEl(); store actions drive it. Renders nothing but the
// (silent) <audio> until a book is playing, then a mini-bar (tap to expand to the full view).
import { useEffect, useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
import { useAudio, attachEl, flushAudioProgress, type AudioState } from "../audioStore";
import { useApp, AUDIO_SPEEDS } from "../store";

function fmt(s: number): string {
  if (!isFinite(s) || s < 0) s = 0;
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = Math.floor(s % 60);
  const mm = h ? String(m).padStart(2, "0") : String(m);
  return (h ? `${h}:` : "") + `${mm}:${String(sec).padStart(2, "0")}`;
}

export default function AudioPlayer() {
  const s = useAudio();
  const prefs = useApp((st) => st.prefs);
  const ref = useRef<HTMLAudioElement | null>(null);

  // Register the single element with the store once.
  useEffect(() => { attachEl(ref.current); return () => attachEl(null); }, []);

  // Apply the saved default speed when a book opens (and live, if changed in Settings while listening).
  useEffect(() => { useAudio.getState().setRate(prefs.audioSpeed || 1); }, [s.workId, prefs.audioSpeed]);
  // Keep the store's auto-advance gate in sync with the pref (read by _onEnded in the store).
  useEffect(() => { useAudio.setState({ autoplayNext: prefs.audioAutoplayNext }); }, [prefs.audioAutoplayNext]);

  // Persist position when the tab is backgrounded / the phone locks — a normal fetch can be killed
  // there, so flushAudioProgress() uses navigator.sendBeacon.
  useEffect(() => {
    const onHide = () => { if (document.visibilityState === "hidden") flushAudioProgress(); };
    window.addEventListener("pagehide", flushAudioProgress);
    document.addEventListener("visibilitychange", onHide);
    return () => {
      window.removeEventListener("pagehide", flushAudioProgress);
      document.removeEventListener("visibilitychange", onHide);
    };
  }, []);

  // Media Session — lock-screen / OS controls + background continuation. Metadata + handlers change
  // rarely (per book/track), so they live in their own effect; the position is pushed separately below.
  useEffect(() => {
    if (!("mediaSession" in navigator) || !s.manifest) return;
    const ms = navigator.mediaSession;
    ms.metadata = new MediaMetadata({
      title: s.manifest.title,
      artist: s.manifest.author ?? "",
      artwork: s.manifest.cover_url ? [{ src: s.manifest.cover_url, sizes: "512x512" }] : [],
    });
    const A = useAudio.getState;
    const set = (k: MediaSessionAction, h: MediaSessionActionHandler | null) => {
      try { ms.setActionHandler(k, h); } catch { /* action unsupported on this browser */ }
    };
    set("play", () => A().togglePlay());
    set("pause", () => A().togglePlay());
    set("seekbackward", () => A().skip(-prefs.audioSkipBack));
    set("seekforward", () => A().skip(prefs.audioSkipForward));
    set("seekto", (e) => { if (e.seekTime != null) A().seekGlobal(e.seekTime); });
    set("previoustrack", () => A().prevChapter());
    set("nexttrack", () => A().nextChapter());
  }, [s.manifest, prefs.audioSkipBack, prefs.audioSkipForward]);

  // Push playback state + the GLOBAL position/duration to the OS (throttled by the store's 500ms
  // positionGlobal tick, so this runs at most ~twice a second).
  useEffect(() => {
    if (!("mediaSession" in navigator) || !s.manifest) return;
    navigator.mediaSession.playbackState = s.playing ? "playing" : "paused";
    if (s.duration > 0) {
      try {
        navigator.mediaSession.setPositionState({
          duration: s.duration,
          position: Math.min(s.positionGlobal, s.duration),
          playbackRate: s.rate,
        });
      } catch { /* some browsers reject odd values */ }
    }
  }, [s.manifest, s.playing, s.positionGlobal, s.duration, s.rate]);

  return (
    <>
      <audio
        ref={ref}
        preload="metadata"
        onLoadedMetadata={s._onLoadedMeta}
        onTimeUpdate={s._onTimeUpdate}
        onPlay={s._onPlayPause}
        onPause={s._onPlayPause}
        onEnded={s._onEnded}
        onError={s._onError}
        onWaiting={s._onWaiting}
        onCanPlay={s._onCanPlay}
      />
      {s.workId != null && (s.expanded ? <FullView s={s} /> : <MiniBar s={s} />)}
    </>
  );
}

const iconBtn =
  "flex h-9 w-9 shrink-0 items-center justify-center rounded-full hover:bg-surface-2";

// Inline transport icons — match the app's de-emoji nav style (App.tsx Ico): currentColor + round caps
// so accent/muted styling falls out for free. Play/pause/chapter-skip are filled; back/fwd are the
// circular "rotate" arrows (a seconds label is centered inside them by the caller).
function Ico({ d, size = 22, fill }: { d: ReactNode; size?: number; fill?: boolean }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill={fill ? "currentColor" : "none"}
      stroke={fill ? "none" : "currentColor"} strokeWidth="1.9" strokeLinecap="round"
      strokeLinejoin="round" aria-hidden="true">{d}</svg>
  );
}
const AIcon = {
  play: <Ico fill size={24} d={<path d="M7 4.5 19.5 12 7 19.5z" />} />,
  pause: <Ico fill size={24} d={<path d="M7 4.5h3.2v15H7zM13.8 4.5H17v15h-3.2z" />} />,
  prevCh: <Ico fill d={<><path d="M18.5 5.2 9.5 12l9 6.8z" /><rect x="5" y="5" width="2.3" height="14" rx="0.7" /></>} />,
  nextCh: <Ico fill d={<><path d="M5.5 5.2 14.5 12l-9 6.8z" /><rect x="16.7" y="5" width="2.3" height="14" rx="0.7" /></>} />,
  back: <Ico size={24} d={<><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" /><path d="M3 3v5h5" /></>} />,
  fwd: <Ico size={24} d={<><path d="M21 12a9 9 0 1 1-9-9 9.75 9.75 0 0 1 6.74 2.74L21 8" /><path d="M21 3v5h-5" /></>} />,
  down: <Ico d={<path d="m6 9 6 6 6-6" />} />,
  up: <Ico d={<path d="m6 15 6-6 6 6" />} />,
  close: <Ico size={20} d={<path d="M18 6 6 18M6 6l12 12" />} />,
  moon: <Ico size={15} d={<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />} />,
  list: <Ico size={15} d={<><path d="M8 6h13M8 12h13M8 18h13" /><path d="M3 6h.01M3 12h.01M3 18h.01" /></>} />,
};

function Spinner({ size = 20 }: { size?: number }) {
  const { t } = useTranslation();
  return <span className="inline-block animate-spin rounded-full border-2 border-current border-t-transparent"
    style={{ width: size, height: size }} aria-label={t("audio.loading")} />;
}

const buildSleepOptions = (t: TFunction): { label: string; value: number | "chapter" }[] => [
  { label: t("audio.sleep15"), value: 15 },
  { label: t("audio.sleep30"), value: 30 },
  { label: t("audio.sleep45"), value: 45 },
  { label: t("audio.sleep60"), value: 60 },
  { label: t("audio.sleepEndOfChapter"), value: "chapter" },
];
// Active sleep-timer label for the control button: remaining mm:ss for a timed sleep, "Chapter" for
// end-of-chapter, null when off. Re-renders on the store's 500ms position tick, so the countdown ticks.
function sleepLabel(s: AudioState, t: TFunction): string | null {
  if (s.sleepChapterTarget != null) return t("audio.sleepChapter");
  if (s.sleepAt != null) return fmt(Math.max(0, (s.sleepAt - Date.now()) / 1000));
  return null;
}

// Animated now-playing equalizer (pauses to a flat rest state when not playing / reduced-motion).
function Equalizer({ playing }: { playing: boolean }) {
  return (
    <span className="flex h-4 w-4 items-end justify-center gap-[2px]" aria-hidden>
      {[0, 1, 2].map((i) => (
        <span
          key={i}
          className={`w-[3px] rounded-sm bg-[var(--accent-bright,var(--accent))] ${playing ? "sp-eq" : ""}`}
          style={{ height: "100%", transform: playing ? undefined : "scaleY(0.4)",
                   transformOrigin: "bottom", animationDelay: `${i * 0.18}s` }}
        />
      ))}
    </span>
  );
}

// Gradient scrubber: a hairline track + accent-gradient fill, with a transparent native range on top
// for drag/keyboard. Shared by the mini-bar and the full view.
function Scrubber({ value, max, onSeek, thick }: {
  value: number; max: number; onSeek: (v: number) => void; thick?: boolean;
}) {
  const { t } = useTranslation();
  const pct = max > 0 ? Math.min(100, (Math.min(value, max) / max) * 100) : 0;
  const h = thick ? "h-1.5" : "h-1";
  return (
    <div className="group relative flex h-4 items-center">
      <div className={`absolute inset-x-0 ${h} rounded-full bg-[color-mix(in_srgb,var(--text)_14%,transparent)]`} />
      <div className={`absolute left-0 ${h} rounded-full bg-gradient-to-r from-accent to-[var(--accent-bright,var(--accent))]`}
        style={{ width: `${pct}%` }} />
      <span className="absolute h-3 w-3 -translate-x-1/2 rounded-full bg-[var(--accent-bright,var(--accent))] opacity-0 shadow transition-opacity group-hover:opacity-100 group-focus-within:opacity-100"
        style={{ left: `${pct}%` }} aria-hidden />
      <input
        type="range" min={0} max={max} step={1} value={Math.min(value, max)}
        onChange={(e) => onSeek(Number(e.target.value))}
        className="absolute inset-x-0 w-full cursor-pointer opacity-0"
        aria-label={t("audio.seek")}
      />
    </div>
  );
}

function MiniBar({ s }: { s: AudioState }) {
  const { t } = useTranslation();
  const prefs = useApp((st) => st.prefs);
  const max = s.duration || 0;
  return (
    <div
      className="fixed inset-x-0 z-30 border-t border-[var(--hair-strong)] bg-[var(--nav-bg)] px-3 pb-1 pt-2 shadow-[0_-8px_30px_-12px_rgba(0,0,0,0.45)] backdrop-blur-xl sm:bottom-0"
      style={{ bottom: "calc(3.5rem + env(safe-area-inset-bottom))" }}
    >
      <div className="mx-auto flex max-w-5xl items-center gap-2 sm:gap-3">
        <button onClick={() => s.setExpanded(true)} className="flex min-w-0 flex-1 items-center gap-2.5 text-left">
          {s.manifest?.cover_url && (
            <img src={s.manifest.cover_url} alt="" className="h-11 w-11 shrink-0 rounded-md object-cover shadow-[var(--pop-shadow)]" />
          )}
          <span className="min-w-0">
            <span className="block truncate text-sm font-medium leading-tight">
              {s.manifest?.title ?? t("audio.audiobook")}
            </span>
            <span className="flex items-center gap-1.5 text-xs text-[var(--text-soft,var(--muted))]">
              <Equalizer playing={s.playing} />
              <span className="truncate tabular-nums">{fmt(s.positionGlobal)} / {fmt(s.duration)}</span>
            </span>
          </span>
        </button>
        <button onClick={() => s.skip(-prefs.audioSkipBack)} title={t("audio.back", { seconds: prefs.audioSkipBack })} className={`${iconBtn} relative text-muted`}>
          {AIcon.back}<span className="absolute inset-0 flex items-center justify-center text-[9px] font-bold">{prefs.audioSkipBack}</span>
        </button>
        <button onClick={() => s.togglePlay()} title={s.playing ? t("audio.pause") : t("audio.play")}
          className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-accent to-[var(--accent-bright,var(--accent))] text-accent-fg shadow-[var(--pop-shadow)]">
          {s.buffering ? <Spinner size={16} /> : (s.playing ? AIcon.pause : AIcon.play)}
        </button>
        <button onClick={() => s.skip(prefs.audioSkipForward)} title={t("audio.forward", { seconds: prefs.audioSkipForward })} className={`${iconBtn} relative text-muted`}>
          {AIcon.fwd}<span className="absolute inset-0 flex items-center justify-center text-[9px] font-bold">{prefs.audioSkipForward}</span>
        </button>
        <button onClick={() => s.setExpanded(true)} title={t("audio.openPlayerHint")}
          aria-label={t("audio.openFullPlayer")} className={`${iconBtn} text-muted`}>{AIcon.up}</button>
        <button onClick={() => s.close()} title={t("audio.closePlayer")} className={`${iconBtn} text-muted`}>{AIcon.close}</button>
      </div>
      <div className="mx-auto mt-0.5 max-w-5xl">
        <Scrubber value={s.positionGlobal} max={max} onSeek={(v) => s.seekGlobal(v)} />
      </div>
    </div>
  );
}

// A small popover floating above the speed/sleep control bar; click-away (the transparent backdrop)
// closes it. Anchored to the bar's relative wrapper.
function Popover({ children, onClose }: { children: ReactNode; onClose: () => void }) {
  return (
    <>
      <div className="fixed inset-0 z-0" onClick={onClose} aria-hidden />
      <div className="sp-pop absolute inset-x-0 bottom-full z-10 mb-2 rounded-2xl border border-[var(--hair-strong,var(--border))] bg-surface p-2 shadow-[var(--pop-shadow)]">
        {children}
      </div>
    </>
  );
}

// Slide-up chapter list (full roster, scrollable) over the now-playing screen.
function ChaptersSheet({ s, cur, onClose }: { s: AudioState; cur: number; onClose: () => void }) {
  const { t } = useTranslation();
  const chs = s.manifest?.chapters ?? [];
  return (
    <div className="absolute inset-0 z-20 flex flex-col justify-end" role="dialog" aria-label={t("audio.chaptersDialog")}>
      <div className="absolute inset-0 bg-black/40" onClick={onClose} aria-hidden />
      <div className="sp-pop relative flex max-h-[78%] flex-col rounded-t-3xl border-t border-[var(--hair-strong,var(--border))] bg-surface shadow-[var(--pop-shadow)]">
        <div className="flex items-center justify-between px-5 py-3">
          <span className="text-sm font-semibold">{t("audio.chapters")}</span>
          <button onClick={onClose} className={`${iconBtn} text-muted`} title={t("common.close")}>{AIcon.close}</button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-[max(1rem,env(safe-area-inset-bottom))]">
          {chs.length === 0 && <div className="px-3 py-6 text-center text-sm text-muted">{t("audio.noChapterMarkers")}</div>}
          {chs.map((c, i) => (
            <button key={i} onClick={() => { s.seekGlobal(c.global_start_s); onClose(); }}
              className={`flex w-full items-center justify-between gap-3 rounded-xl px-3 py-2.5 text-left text-sm ${
                i === cur ? "bg-accent/15 text-accent" : "hover:bg-surface-2"}`}>
              <span className="flex min-w-0 items-center gap-3">
                <span className={`flex h-5 w-5 shrink-0 items-center justify-center text-xs tabular-nums ${i === cur ? "text-accent" : "text-muted"}`}>
                  {i === cur ? <Equalizer playing={s.playing} /> : i + 1}
                </span>
                <span className="min-w-0 flex-1 truncate">{c.title || t("audio.chapterN", { number: i + 1 })}</span>
              </span>
              <span className="shrink-0 text-xs tabular-nums text-muted">{fmt(c.global_start_s)}</span>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

function FullView({ s }: { s: AudioState }) {
  const { t } = useTranslation();
  const prefs = useApp((st) => st.prefs);
  const setPrefs = useApp((st) => st.setPrefs);
  const [panel, setPanel] = useState<null | "chapters" | "speed" | "sleep">(null);
  const SLEEP_OPTIONS = buildSleepOptions(t);
  const chs = s.manifest?.chapters ?? [];
  const cover = s.manifest?.cover_url;
  const max = s.duration || 0;
  let cur = -1;
  for (let i = 0; i < chs.length; i++) if (chs[i].global_start_s <= s.positionGlobal + 0.5) cur = i;
  const curTitle = cur >= 0 ? (chs[cur].title || t("audio.chapterN", { number: cur + 1 })) : null;
  const sleep = sleepLabel(s, t);
  const ctrlBtn = "flex flex-1 items-center justify-center gap-1.5 rounded-xl border border-[var(--hair,var(--border))] py-2.5 transition disabled:opacity-40";

  return (
    <div className="fixed inset-0 z-50 flex flex-col overflow-hidden bg-surface" role="dialog" aria-label={t("audio.playerDialog")}>
      {/* Ambient backdrop: the cover, blurred + dimmed, fading into the surface — adapts to each book. */}
      {cover && (
        <div className="pointer-events-none absolute inset-0 -z-10" aria-hidden>
          <img src={cover} alt="" className="h-full w-full scale-125 object-cover opacity-30 blur-3xl saturate-150" />
          <div className="absolute inset-0 bg-gradient-to-b from-surface/30 via-surface/70 to-surface" />
        </div>
      )}

      {/* Header */}
      <div className="flex items-center gap-2 px-4 py-3" style={{ paddingTop: "max(0.75rem, env(safe-area-inset-top))" }}>
        <button onClick={() => s.setExpanded(false)} className={`${iconBtn} text-muted`} title={t("audio.minimize")}>{AIcon.down}</button>
        <div className="min-w-0 flex-1 text-center text-[11px] font-semibold uppercase tracking-[0.14em] text-[var(--text-soft,var(--muted))]">
          {t("audio.nowPlaying")}
        </div>
        <button onClick={() => s.close()} className={`${iconBtn} text-muted`} title={t("audio.closePlayer")}>{AIcon.close}</button>
      </div>

      {/* Cover + meta */}
      <div className="flex min-h-0 flex-1 flex-col items-center justify-center px-6">
        <div className="relative aspect-square w-full max-w-[14rem] sm:max-w-[16rem]">
          {cover ? (
            <img src={cover} alt="" className="h-full w-full rounded-2xl object-cover shadow-[0_24px_60px_-15px_rgba(0,0,0,0.65)] ring-1 ring-black/10" />
          ) : (
            <div className="flex h-full w-full items-center justify-center rounded-2xl bg-surface-2 text-5xl">🎧</div>
          )}
          {(s.buffering || s.error) && (
            <div className="absolute inset-0 flex items-center justify-center rounded-2xl bg-black/45 backdrop-blur-sm">
              {s.error
                ? <span className="px-4 text-center text-sm font-medium text-white">{t("audio.playbackError")}</span>
                : <span className="text-white"><Spinner size={34} /></span>}
            </div>
          )}
        </div>
        <div className="mt-7 w-full max-w-md text-center">
          <div className="truncate text-xl font-semibold leading-tight">{s.manifest?.title}</div>
          {s.manifest?.author && <div className="mt-1 truncate text-sm text-[var(--text-soft,var(--muted))]">{s.manifest.author}</div>}
          {curTitle && (
            <button onClick={() => setPanel("chapters")} className="mx-auto mt-2.5 flex max-w-full items-center gap-1.5 text-xs text-accent hover:underline">
              <span className="truncate">{curTitle}</span>
              {chs.length > 1 && <span className="shrink-0 text-[var(--text-soft,var(--muted))]">{t("audio.chapterOfTotal", { current: cur + 1, total: chs.length })}</span>}
            </button>
          )}
        </div>
      </div>

      {/* Controls */}
      <div className="px-6 pb-[max(1.25rem,env(safe-area-inset-bottom))] pt-2">
        <div className="mx-auto w-full max-w-md">
          <Scrubber value={s.positionGlobal} max={max} onSeek={(v) => s.seekGlobal(v)} thick />
          <div className="mb-5 mt-2 flex justify-between text-xs tabular-nums text-[var(--text-soft,var(--muted))]">
            <span>{fmt(s.positionGlobal)}</span>
            <span>-{fmt(Math.max(0, s.duration - s.positionGlobal))}</span>
          </div>

          {/* Transport */}
          <div className="flex items-center justify-center gap-2 sm:gap-4">
            <button onClick={() => s.prevChapter()} className={`${iconBtn} text-muted disabled:opacity-30`} title={t("audio.prevChapter")} disabled={chs.length === 0}>{AIcon.prevCh}</button>
            <button onClick={() => s.skip(-prefs.audioSkipBack)} className={`${iconBtn} relative h-11 w-11 text-text`} title={t("audio.back", { seconds: prefs.audioSkipBack })}>
              {AIcon.back}<span className="absolute inset-0 flex items-center justify-center text-[10px] font-bold">{prefs.audioSkipBack}</span>
            </button>
            <button onClick={() => s.togglePlay()} title={s.playing ? t("audio.pause") : t("audio.play")}
              className="flex h-16 w-16 items-center justify-center rounded-full bg-gradient-to-br from-accent to-[var(--accent-bright,var(--accent))] text-accent-fg shadow-[var(--pop-shadow)] transition active:scale-95">
              {s.buffering ? <Spinner size={24} /> : (s.playing ? AIcon.pause : AIcon.play)}
            </button>
            <button onClick={() => s.skip(prefs.audioSkipForward)} className={`${iconBtn} relative h-11 w-11 text-text`} title={t("audio.forward", { seconds: prefs.audioSkipForward })}>
              {AIcon.fwd}<span className="absolute inset-0 flex items-center justify-center text-[10px] font-bold">{prefs.audioSkipForward}</span>
            </button>
            <button onClick={() => s.nextChapter()} className={`${iconBtn} text-muted disabled:opacity-30`} title={t("audio.nextChapter")} disabled={chs.length === 0}>{AIcon.nextCh}</button>
          </div>

          {/* Speed / Sleep / Chapters */}
          <div className="relative mt-6 flex items-stretch gap-2 text-xs font-medium">
            <button onClick={() => setPanel(panel === "speed" ? null : "speed")}
              className={`${ctrlBtn} ${panel === "speed" ? "bg-surface-2 text-accent" : "text-[var(--text-soft,var(--muted))] hover:bg-surface-2"}`}>
              <span className="tabular-nums">{s.rate}×</span>
            </button>
            <button onClick={() => setPanel(panel === "sleep" ? null : "sleep")}
              className={`${ctrlBtn} ${sleep || panel === "sleep" ? "bg-surface-2 text-accent" : "text-[var(--text-soft,var(--muted))] hover:bg-surface-2"}`}>
              {AIcon.moon}<span className="tabular-nums">{sleep ?? t("audio.sleep")}</span>
            </button>
            <button onClick={() => setPanel(panel === "chapters" ? null : "chapters")} disabled={chs.length === 0}
              className={`${ctrlBtn} ${panel === "chapters" ? "bg-surface-2 text-accent" : "text-[var(--text-soft,var(--muted))] hover:bg-surface-2"}`}>
              {AIcon.list}<span>{t("audio.chapters")}</span>
            </button>

            {panel === "speed" && (
              <Popover onClose={() => setPanel(null)}>
                <div className="grid grid-cols-3 gap-1.5">
                  {AUDIO_SPEEDS.map((r) => (
                    <button key={r} onClick={() => { s.setRate(r); setPrefs({ audioSpeed: r }); }}
                      className={`rounded-lg py-2 text-sm font-medium tabular-nums ${
                        s.rate === r ? "bg-accent text-accent-fg" : "bg-surface-2 hover:opacity-80"}`}>
                      {r}×
                    </button>
                  ))}
                </div>
              </Popover>
            )}
            {panel === "sleep" && (
              <Popover onClose={() => setPanel(null)}>
                <div className="flex flex-col">
                  {SLEEP_OPTIONS.map((o) => (
                    <button key={o.label} onClick={() => { s.setSleep(o.value); setPanel(null); }}
                      className="rounded-lg px-3 py-2 text-left text-sm hover:bg-surface-2">{o.label}</button>
                  ))}
                  {sleep && (
                    <button onClick={() => { s.setSleep(null); setPanel(null); }}
                      className="mt-1 rounded-lg px-3 py-2 text-left text-sm text-red-400 hover:bg-surface-2">{t("audio.turnOffTimer")}</button>
                  )}
                </div>
              </Popover>
            )}
          </div>
        </div>
      </div>

      {panel === "chapters" && <ChaptersSheet s={s} cur={cur} onClose={() => setPanel(null)} />}
    </div>
  );
}
