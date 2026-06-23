// The one persistent <audio> for the whole app. Mounted once at the app root (see App.tsx) and never
// unmounted, so playback survives every route change incl. the reader and a hidden/locked tab. The
// element is registered with the store via attachEl(); store actions drive it. Renders nothing but the
// (silent) <audio> until a book is playing, then a mini-bar (tap to expand to the full view).
import { useEffect, useRef } from "react";
import { useAudio, attachEl, flushAudioProgress, type AudioState } from "../audioStore";

const SPEEDS = [0.75, 1, 1.25, 1.5, 1.75, 2];

function fmt(s: number): string {
  if (!isFinite(s) || s < 0) s = 0;
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = Math.floor(s % 60);
  const mm = h ? String(m).padStart(2, "0") : String(m);
  return (h ? `${h}:` : "") + `${mm}:${String(sec).padStart(2, "0")}`;
}

export default function AudioPlayer() {
  const s = useAudio();
  const ref = useRef<HTMLAudioElement | null>(null);

  // Register the single element with the store once.
  useEffect(() => { attachEl(ref.current); return () => attachEl(null); }, []);

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
    set("seekbackward", () => A().skip(-15));
    set("seekforward", () => A().skip(30));
    set("seekto", (e) => { if (e.seekTime != null) A().seekGlobal(e.seekTime); });
    set("previoustrack", () => A().prevChapter());
    set("nexttrack", () => A().nextChapter());
  }, [s.manifest]);

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
      />
      {s.workId != null && (s.expanded ? <FullView s={s} /> : <MiniBar s={s} />)}
    </>
  );
}

const iconBtn =
  "flex h-9 w-9 shrink-0 items-center justify-center rounded-full text-lg hover:bg-surface-2";

function MiniBar({ s }: { s: AudioState }) {
  const max = s.duration || 0;
  return (
    <div
      className="fixed inset-x-0 z-30 border-t border-border bg-surface/95 px-3 pb-1 pt-2 shadow-[0_-2px_12px_rgba(0,0,0,0.18)] backdrop-blur sm:bottom-0"
      style={{ bottom: "calc(3.5rem + env(safe-area-inset-bottom))" }}
    >
      <div className="mx-auto flex max-w-5xl items-center gap-2 sm:gap-3">
        <button onClick={() => s.setExpanded(true)} className="flex min-w-0 flex-1 items-center gap-2 text-left">
          {s.manifest?.cover_url && (
            <img src={s.manifest.cover_url} alt="" className="h-10 w-10 shrink-0 rounded object-cover" />
          )}
          <span className="min-w-0">
            <span className="block truncate text-sm font-medium leading-tight">
              {s.manifest?.title ?? "Audiobook"}
            </span>
            <span className="block truncate text-xs text-muted">
              {fmt(s.positionGlobal)} / {fmt(s.duration)}
            </span>
          </span>
        </button>
        <button onClick={() => s.skip(-15)} title="Back 15s" className={iconBtn}>⏪</button>
        <button onClick={() => s.togglePlay()} title={s.playing ? "Pause" : "Play"}
          className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-accent text-lg text-accent-fg">
          {s.playing ? "⏸" : "▶"}
        </button>
        <button onClick={() => s.skip(30)} title="Forward 30s" className={iconBtn}>⏩</button>
        <button onClick={() => s.close()} title="Close player" className={`${iconBtn} text-sm text-muted`}>✕</button>
      </div>
      <input
        type="range" min={0} max={max} step={1}
        value={Math.min(s.positionGlobal, max)}
        onChange={(e) => s.seekGlobal(Number(e.target.value))}
        className="mt-1 h-1 w-full cursor-pointer accent-accent"
        aria-label="Seek"
      />
    </div>
  );
}

function FullView({ s }: { s: AudioState }) {
  const chs = s.manifest?.chapters ?? [];
  const max = s.duration || 0;
  let cur = -1;
  for (let i = 0; i < chs.length; i++) if (chs[i].global_start_s <= s.positionGlobal + 0.5) cur = i;
  return (
    <div className="fixed inset-0 z-50 flex flex-col bg-surface" role="dialog" aria-label="Audiobook player">
      <div className="flex items-center gap-2 border-b border-border px-4 py-3"
        style={{ paddingTop: "max(0.75rem, env(safe-area-inset-top))" }}>
        <button onClick={() => s.setExpanded(false)} className={iconBtn} title="Minimize">⌄</button>
        <div className="min-w-0 flex-1">
          <div className="truncate font-medium leading-tight">{s.manifest?.title}</div>
          {s.manifest?.author && <div className="truncate text-xs text-muted">{s.manifest.author}</div>}
        </div>
        <button onClick={() => s.close()} className={`${iconBtn} text-sm text-muted`} title="Close player">✕</button>
      </div>

      {/* Chapter list */}
      <div className="min-h-0 flex-1 overflow-y-auto px-2 py-2">
        {chs.length === 0 && (
          <div className="px-3 py-6 text-center text-sm text-muted">No chapter markers.</div>
        )}
        {chs.map((c, i) => (
          <button
            key={i}
            onClick={() => s.seekGlobal(c.global_start_s)}
            className={`flex w-full items-center justify-between gap-3 rounded-lg px-3 py-2 text-left text-sm ${
              i === cur ? "bg-accent/15 text-accent" : "hover:bg-surface-2"
            }`}
          >
            <span className="min-w-0 flex-1 truncate">{c.title || `Chapter ${i + 1}`}</span>
            <span className="shrink-0 text-xs text-muted">{fmt(c.global_start_s)}</span>
          </button>
        ))}
      </div>

      {/* Transport */}
      <div className="border-t border-border px-4 pt-3"
        style={{ paddingBottom: "max(1rem, env(safe-area-inset-bottom))" }}>
        {s.manifest?.cover_url && (
          <img src={s.manifest.cover_url} alt="" className="mx-auto mb-3 h-32 w-32 rounded-lg object-cover shadow" />
        )}
        <input
          type="range" min={0} max={max} step={1}
          value={Math.min(s.positionGlobal, max)}
          onChange={(e) => s.seekGlobal(Number(e.target.value))}
          className="h-1.5 w-full cursor-pointer accent-accent"
          aria-label="Seek"
        />
        <div className="mb-3 mt-1 flex justify-between text-xs text-muted">
          <span>{fmt(s.positionGlobal)}</span>
          <span>-{fmt(Math.max(0, s.duration - s.positionGlobal))}</span>
        </div>
        <div className="flex items-center justify-center gap-4">
          <button onClick={() => s.prevChapter()} className={iconBtn} title="Previous chapter">⏮</button>
          <button onClick={() => s.skip(-15)} className={iconBtn} title="Back 15s">⏪</button>
          <button onClick={() => s.togglePlay()} title={s.playing ? "Pause" : "Play"}
            className="flex h-14 w-14 items-center justify-center rounded-full bg-accent text-2xl text-accent-fg">
            {s.playing ? "⏸" : "▶"}
          </button>
          <button onClick={() => s.skip(30)} className={iconBtn} title="Forward 30s">⏩</button>
          <button onClick={() => s.nextChapter()} className={iconBtn} title="Next chapter">⏭</button>
        </div>
        <div className="mt-3 flex items-center justify-center gap-1.5">
          {SPEEDS.map((r) => (
            <button
              key={r}
              onClick={() => s.setRate(r)}
              className={`rounded-md px-2 py-1 text-xs font-medium ${
                s.rate === r ? "bg-accent text-accent-fg" : "text-muted hover:bg-surface-2"
              }`}
            >
              {r}×
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
