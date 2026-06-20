import { useEffect, useRef, useState } from "react";
import { useApp } from "../store";

// Inline SVG icons (inherit currentColor). Glyph characters like ⠿/⛶/‹/› render as blank
// "tofu" on some mobile system fonts, so the control cluster uses real vectors instead.
const ICON = "h-5 w-5";
const svg = (children: React.ReactNode) => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8}
    strokeLinecap="round" strokeLinejoin="round" className={ICON} aria-hidden="true">
    {children}
  </svg>
);
const IconGrip = () => (
  <svg viewBox="0 0 24 24" fill="currentColor" className="h-5 w-4" aria-hidden="true">
    {[7, 12, 17].map((cy) => (
      <g key={cy}><circle cx="9" cy={cy} r="1.4" /><circle cx="15" cy={cy} r="1.4" /></g>
    ))}
  </svg>
);
const IconPrev = () => svg(<polyline points="15 6 9 12 15 18" />);
const IconNext = () => svg(<polyline points="9 6 15 12 9 18" />);
const IconToc = () =>
  svg(<><line x1="4" y1="7" x2="20" y2="7" /><line x1="4" y1="12" x2="20" y2="12" /><line x1="4" y1="17" x2="20" y2="17" /></>);
const IconFocus = () =>
  svg(<><path d="M4 9V5a1 1 0 0 1 1-1h4" /><path d="M20 9V5a1 1 0 0 0-1-1h-4" /><path d="M4 15v4a1 1 0 0 0 1 1h4" /><path d="M20 15v4a1 1 0 0 1-1 1h-4" /></>);
const IconClose = () => svg(<><line x1="6" y1="6" x2="18" y2="18" /><line x1="18" y1="6" x2="6" y2="18" /></>);

// Free-floating control cluster: drag it anywhere on screen (position is remembered
// as a viewport fraction so it survives resizes). Hide it with ✕; a small reveal tab
// brings it back. The settings panel "falls out" next to it (see panelStyle in Reader).
const DEF_X = 0.93;
const DEF_Y = 0.86;

export default function ReaderFab({
  onToc,
  onFocus,
  onPrev,
  onNext,
  dark = false,
}: {
  onToc: () => void;
  onFocus: () => void;
  onPrev: () => void;
  onNext: () => void;
  // Whether the *reader's* surface (not the app theme) is dark — drives the cluster colours so
  // the pill + icons always contrast with the page being read, even when the reader's brightness
  // diverges from the main app theme.
  dark?: boolean;
}) {
  const { prefs, setPrefs } = useApp();
  const [pos, setPos] = useState({ x: prefs.fabX ?? DEF_X, y: prefs.fabY ?? DEF_Y });
  const dragging = useRef(false);
  const moved = useRef(false);
  const live = useRef(pos);
  const clusterRef = useRef<HTMLDivElement>(null);

  // Clamp a centre position (viewport fractions) so the whole cluster stays on-screen — its
  // width depends on its contents (and orientation), so use its measured box, not a guess.
  const clampPos = (x: number, y: number) => {
    const { innerWidth: w, innerHeight: h } = window;
    const el = clusterRef.current;
    const halfW = el ? el.offsetWidth / 2 + 4 : 110;
    const halfH = el ? el.offsetHeight / 2 + 4 : 28;
    const minX = Math.min(0.5, halfW / w), minY = Math.min(0.5, halfH / h);
    return {
      x: Math.max(minX, Math.min(1 - minX, x)),
      y: Math.max(minY, Math.min(1 - minY, y)),
    };
  };

  useEffect(() => {
    if (dragging.current) return;
    // Clamp on load/resize so a previously-saved position (or a smaller screen) can't leave
    // the cluster hanging off the edge.
    const apply = () => setPos(clampPos(prefs.fabX ?? DEF_X, prefs.fabY ?? DEF_Y));
    apply();
    window.addEventListener("resize", apply);
    return () => window.removeEventListener("resize", apply);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [prefs.fabX, prefs.fabY]);

  useEffect(() => {
    const move = (e: PointerEvent) => {
      if (!dragging.current) return;
      moved.current = true;
      const { innerWidth: w, innerHeight: h } = window;
      const clamped = clampPos(e.clientX / w, e.clientY / h);
      live.current = clamped;
      setPos(clamped);
    };
    const up = () => {
      if (!dragging.current) return;
      dragging.current = false;
      setPrefs({
        fabX: Math.round(live.current.x * 1000) / 1000,
        fabY: Math.round(live.current.y * 1000) / 1000,
      });
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    return () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
  }, [setPrefs]);

  // Colours sampled to the reader surface so the cluster always contrasts with the page (not
  // the app theme, which can differ from the reader's brightness).
  const pal = dark
    ? { bg: "rgba(32,35,43,0.94)", border: "rgba(255,255,255,0.16)", fg: "#eceef3", muted: "rgba(236,238,243,0.62)" }
    : { bg: "rgba(255,255,255,0.95)", border: "rgba(0,0,0,0.14)", fg: "#1c2027", muted: "rgba(28,32,39,0.55)" };
  const hover = dark ? "hover:bg-white/10" : "hover:bg-black/5";

  // Hidden → a subtle, always-available reveal tab in the corner.
  if (prefs.fabHidden) {
    return (
      <button
        onClick={() => setPrefs({ fabHidden: false })}
        title="Show reading controls"
        aria-label="Show reading controls"
        className="fixed right-3 z-40 flex h-9 w-9 items-center justify-center rounded-full border text-sm font-semibold opacity-50 shadow-lg backdrop-blur transition hover:opacity-100"
        style={{ bottom: "max(0.75rem, env(safe-area-inset-bottom))", background: pal.bg, borderColor: pal.border, color: pal.fg }}
      >
        Aa
      </button>
    );
  }

  const style: React.CSSProperties = {
    position: "fixed",
    left: `${pos.x * 100}%`,
    top: `${pos.y * 100}%`,
    transform: "translate(-50%, -50%)",
  };
  const startDrag = (e: React.PointerEvent) => {
    dragging.current = true;
    moved.current = false;
    live.current = pos;
    // Capture the pointer so the drag keeps tracking even if the finger slides off the small grip
    // (touch otherwise drops the pointermove stream once it leaves the element) — M2.
    try {
      (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    } catch {
      /* setPointerCapture unsupported / pointer already released — drag still works via window */
    }
    e.preventDefault();
  };

  const btn = `flex h-11 w-11 items-center justify-center rounded-full ${hover}`;

  return (
    <div
      ref={clusterRef}
      className="z-40 flex flex-row items-center gap-0.5 rounded-full border p-1 shadow-xl backdrop-blur touch-none select-none"
      style={{ ...style, background: pal.bg, borderColor: pal.border, color: pal.fg }}
    >
      <button
        onPointerDown={startDrag}
        title="Drag to move"
        aria-label="Move controls"
        className="flex h-11 w-6 cursor-grab items-center justify-center active:cursor-grabbing"
        style={{ color: pal.muted }}
      >
        <IconGrip />
      </button>
      <button onClick={onPrev} title="Previous page (←)" aria-label="Previous page" className={btn}>
        <IconPrev />
      </button>
      <button onClick={onNext} title="Next page (→)" aria-label="Next page" className={btn}>
        <IconNext />
      </button>
      <button onClick={onToc} title="Contents (t)" aria-label="Contents" className={btn}><IconToc /></button>
      <button onClick={onFocus} title="Focus mode (f)" aria-label="Focus mode" className={btn}><IconFocus /></button>
      <button
        onClick={() => setPrefs({ fabHidden: true })}
        title="Hide controls"
        aria-label="Hide controls"
        className={btn}
        style={{ color: pal.muted }}
      >
        <IconClose />
      </button>
    </div>
  );
}
