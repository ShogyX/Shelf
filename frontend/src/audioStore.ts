// Global audiobook player state. The actual <audio> element lives in <AudioPlayer> (mounted once at
// the app root and never unmounted), so playback survives route changes / the reader / a hidden tab.
// The element is registered here via attachEl(); store actions drive it. Native <audio> (not Web
// Audio) is deliberate: an AudioContext suspends in the background on iOS and breaks lock-screen
// control. Position is debounced+beacon-saved so a backgrounding phone still persists it.
import { create } from "zustand";
import { api, AudioManifest } from "./api/client";

let el: HTMLAudioElement | null = null;       // the one persistent <audio>; set by the component
let saveTimer: ReturnType<typeof setTimeout> | undefined;
let lastUiTick = 0;
// Track indices (of the CURRENT book) whose native codec the browser couldn't decode — we reload
// these via a forced AAC transcode (?transcode=1). Cleared when a new book opens. Safari can't play
// opus/vorbis/flac, which ffprobe reports as "native", so the only reliable signal is a decode error.
const forceTranscode = new Set<number>();

export function attachEl(node: HTMLAudioElement | null) {
  el = node;
}
export function getEl(): HTMLAudioElement | null {
  return el;
}

export interface AudioState {
  workId: number | null;
  manifest: AudioManifest | null;
  currentTrack: number;
  positionGlobal: number;   // seconds from the start of the whole book
  duration: number;         // total book duration (s)
  playing: boolean;
  rate: number;
  autoplayNext: boolean;    // synced from reader prefs by <AudioPlayer>; gates track auto-advance
  expanded: boolean;        // mini-bar vs full view
  loading: boolean;         // manifest / track is loading or transcoding (no audio ready yet)
  buffering: boolean;       // playback stalled waiting for data (incl. the first on-demand transcode)
  error: boolean;           // this track wouldn't play even after a forced transcode
  sleepAt: number | null;          // wall-clock ms deadline: pause at this time (timed sleep timer)
  sleepChapterTarget: number | null; // global-pos target: pause here (end-of-chapter sleep timer)
  // internal: a seek to apply once the freshly-loaded track reports its metadata
  _pendingSeek: number | null;
  _autoplay: boolean;

  playWork: (workId: number, resume?: { track: number; posS: number }) => Promise<void>;
  togglePlay: () => void;
  seekGlobal: (s: number) => void;
  skip: (delta: number) => void;
  nextChapter: () => void;
  prevChapter: () => void;
  setRate: (r: number) => void;
  setExpanded: (v: boolean) => void;
  setSleep: (opt: number | "chapter" | null) => void;  // minutes, "chapter", or null to cancel
  close: () => void;
  // wired to <audio> events by the component:
  _onLoadedMeta: () => void;
  _onTimeUpdate: () => void;
  _onPlayPause: () => void;
  _onEnded: () => void;
  _onError: () => void;
  _onWaiting: () => void;
  _onCanPlay: () => void;
}

// Prefix sums of track durations → map between a global position and a (track, in-track offset).
function cum(m: AudioManifest | null): number[] {
  const out: number[] = [];
  let run = 0;
  for (const t of m?.tracks ?? []) { out.push(run); run += t.duration_s || 0; }
  return out;
}
function trackStart(m: AudioManifest | null, track: number): number {
  return cum(m)[track] ?? 0;
}
function globalToTrack(m: AudioManifest | null, g: number): { track: number; offset: number } {
  const tracks = m?.tracks ?? [];
  const starts = cum(m);
  for (let i = tracks.length - 1; i >= 0; i--) {
    if (g >= starts[i]) return { track: i, offset: Math.max(0, g - starts[i]) };
  }
  return { track: 0, offset: Math.max(0, g) };
}

export const useAudio = create<AudioState>((set, get) => {
  // Persist the current position. Throttled during playback; flushed (via sendBeacon, which survives a
  // backgrounding tab) on pause/ended/visibility-hidden — see the component.
  const save = (immediate = false) => {
    const { workId, currentTrack } = get();
    if (!el || workId == null) return;
    const pos = el.currentTime || 0;
    const send = () => api.saveAudioProgress(workId, currentTrack, pos).catch(() => {});
    clearTimeout(saveTimer);
    if (immediate) {
      // Beacon: a normal fetch can be killed when the tab freezes (screen lock / app switch).
      const body = JSON.stringify({ track: currentTrack, pos_s: pos });
      if (navigator.sendBeacon?.(`/api/works/${workId}/audio/progress`,
            new Blob([body], { type: "application/json" }))) return;
      send();
    } else {
      saveTimer = setTimeout(send, 5000);
    }
  };

  const loadTrack = (track: number, offset: number, autoplay: boolean) => {
    const { workId } = get();
    if (!el || workId == null) return;
    set({ currentTrack: track, _pendingSeek: offset, _autoplay: autoplay, buffering: true, error: false });
    el.src = api.audioStreamUrl(workId, track, forceTranscode.has(track));
    el.load();
  };

  return {
    workId: null, manifest: null, currentTrack: 0, positionGlobal: 0, duration: 0,
    playing: false, rate: 1, autoplayNext: true, expanded: false, loading: false,
    buffering: false, error: false, sleepAt: null, sleepChapterTarget: null,
    _pendingSeek: null, _autoplay: false,

    playWork: async (workId, resume) => {
      // The first play() MUST run synchronously inside the tap — iOS only unlocks audio in a user
      // gesture. So load+play immediately: a known resume spot if given, else track 0 from the top.
      // Once the element has played once, later programmatic play()s (the seek to saved progress
      // below) are permitted, so the saved position is applied after the async fetch without a 2nd tap.
      forceTranscode.clear();
      set({ loading: true, workId, expanded: false, error: false, sleepAt: null, sleepChapterTarget: null });
      loadTrack(resume?.track ?? 0, resume?.posS ?? 0, true);
      try {
        const manifest = await api.audioManifest(workId);
        set({ manifest, duration: manifest.total_duration_s });
        if (!resume) {
          try {
            const p = await api.getAudioProgress(workId);
            if (p.track !== 0 || p.pos_s > 0) loadTrack(p.track, p.pos_s, true);
          } catch { /* no saved progress — track 0 from the top is already playing */ }
        }
      } catch {
        set({ loading: false });
        return;
      }
      set({ loading: false });
    },

    togglePlay: () => {
      if (!el) return;
      if (el.paused) el.play().catch(() => {}); else el.pause();
    },

    seekGlobal: (s) => {
      const { manifest, currentTrack } = get();
      const clamped = Math.max(0, Math.min(s, get().duration || s));
      const { track, offset } = globalToTrack(manifest, clamped);
      if (track !== currentTrack) {
        loadTrack(track, offset, !(el?.paused ?? true));
      } else if (el) {
        el.currentTime = offset;
      }
      set({ positionGlobal: clamped });
    },

    skip: (delta) => get().seekGlobal((el?.currentTime != null
      ? trackStart(get().manifest, get().currentTrack) + el.currentTime
      : get().positionGlobal) + delta),

    nextChapter: () => {
      const { manifest, positionGlobal } = get();
      const chs = manifest?.chapters ?? [];
      const nxt = chs.find((c) => c.global_start_s > positionGlobal + 0.5);
      if (nxt) get().seekGlobal(nxt.global_start_s);
    },
    prevChapter: () => {
      const { manifest, positionGlobal } = get();
      const chs = manifest?.chapters ?? [];
      // Within the first ~3s of a chapter → previous; else restart the current one.
      const prior = [...chs].reverse().find((c) => c.global_start_s < positionGlobal - 3);
      get().seekGlobal(prior ? prior.global_start_s : 0);
    },

    setRate: (r) => { if (el) el.playbackRate = r; set({ rate: r }); },
    setExpanded: (v) => set({ expanded: v }),

    setSleep: (opt) => {
      if (opt == null) { set({ sleepAt: null, sleepChapterTarget: null }); return; }
      if (opt === "chapter") {
        // Pause at the start of the chapter AFTER the one we're in (or the book's end if it's the last).
        const { manifest, positionGlobal, duration } = get();
        const next = (manifest?.chapters ?? []).find((c) => c.global_start_s > positionGlobal + 0.5);
        set({ sleepChapterTarget: next ? next.global_start_s : (duration || null), sleepAt: null });
      } else {
        set({ sleepAt: Date.now() + opt * 60_000, sleepChapterTarget: null });
      }
    },

    close: () => {
      save(true);
      if (el) { el.pause(); el.removeAttribute("src"); el.load(); }
      set({ workId: null, manifest: null, playing: false, positionGlobal: 0, expanded: false,
            buffering: false, error: false, sleepAt: null, sleepChapterTarget: null });
    },

    _onLoadedMeta: () => {
      const { _pendingSeek, _autoplay, rate } = get();
      if (!el) return;
      if (_pendingSeek != null) { try { el.currentTime = _pendingSeek; } catch { /* not seekable yet */ } }
      el.playbackRate = rate;
      if (_autoplay) el.play().catch(() => {});
      set({ _pendingSeek: null, _autoplay: false, buffering: false, loading: false });
    },

    _onTimeUpdate: () => {
      if (!el) return;
      const g = trackStart(get().manifest, get().currentTrack) + (el.currentTime || 0);
      // Sleep timer: pause when the wall-clock deadline or the end-of-chapter target is reached.
      const { sleepAt, sleepChapterTarget } = get();
      if ((sleepAt != null && Date.now() >= sleepAt) ||
          (sleepChapterTarget != null && g >= sleepChapterTarget)) {
        el.pause(); save(true);
        set({ sleepAt: null, sleepChapterTarget: null });
      }
      const now = Date.now();
      if (now - lastUiTick > 500) { lastUiTick = now; set({ positionGlobal: g }); }  // throttle re-renders
      save(false);
    },

    _onPlayPause: () => { if (el) set({ playing: !el.paused }); },

    // Native decode failed (commonly Safari on opus/vorbis/flac that ffprobe called "native"). Retry
    // this track ONCE via a forced AAC transcode at the same spot; if that fails too, surface the error.
    _onError: () => {
      const { workId, currentTrack, manifest, positionGlobal, playing, _autoplay } = get();
      if (!el || workId == null) return;
      if (!forceTranscode.has(currentTrack)) {
        forceTranscode.add(currentTrack);
        const offset = get()._pendingSeek ??
          Math.max(0, positionGlobal - trackStart(manifest, currentTrack));
        loadTrack(currentTrack, offset, playing || _autoplay);
      } else {
        set({ buffering: false, loading: false, error: true, playing: false });
      }
    },

    _onWaiting: () => set({ buffering: true }),
    _onCanPlay: () => set({ buffering: false, error: false }),

    _onEnded: () => {
      const { manifest, currentTrack, autoplayNext } = get();
      const n = manifest?.tracks.length ?? 0;
      if (autoplayNext && currentTrack + 1 < n) {
        loadTrack(currentTrack + 1, 0, true);   // gapless-ish advance to the next track/chapter
      } else {
        save(true);
        set({ playing: false });
      }
    },
  };
});

// Flush the current position immediately (used on pagehide / visibility-hidden from the component).
export function flushAudioProgress() {
  const s = useAudio.getState();
  if (s.workId == null || !el) return;
  const body = JSON.stringify({ track: s.currentTrack, pos_s: el.currentTime || 0 });
  navigator.sendBeacon?.(`/api/works/${s.workId}/audio/progress`,
    new Blob([body], { type: "application/json" }));
}
