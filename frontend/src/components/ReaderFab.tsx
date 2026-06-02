import { useEffect, useRef, useState } from "react";
import { useApp } from "../store";

// Free-floating control cluster: drag it anywhere on screen (position is remembered
// as a viewport fraction so it survives resizes). Hide it with ✕; a small reveal tab
// brings it back. The settings panel "falls out" next to it (see panelStyle in Reader).
const DEF_X = 0.93;
const DEF_Y = 0.86;

export default function ReaderFab({
  onToc,
  onSettings,
  onFocus,
  onPrev,
  onNext,
}: {
  onToc: () => void;
  onSettings: () => void;
  onFocus: () => void;
  onPrev: () => void;
  onNext: () => void;
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

  // Hidden → a subtle, always-available reveal tab in the corner.
  if (prefs.fabHidden) {
    return (
      <button
        onClick={() => setPrefs({ fabHidden: false })}
        title="Show reading controls"
        aria-label="Show reading controls"
        className="fixed right-3 z-40 flex h-9 w-9 items-center justify-center rounded-full border border-border bg-surface/80 text-sm font-semibold text-muted opacity-40 shadow-lg backdrop-blur transition hover:opacity-100"
        style={{ bottom: "max(0.75rem, env(safe-area-inset-bottom))" }}
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
    e.preventDefault();
  };

  const btn =
    "flex h-11 w-11 items-center justify-center rounded-full text-base text-text hover:bg-surface-2";

  return (
    <div
      ref={clusterRef}
      className="z-40 flex flex-row items-center gap-0.5 rounded-full border border-border bg-surface/95 p-1 shadow-xl backdrop-blur touch-none select-none"
      style={style}
    >
      <button
        onPointerDown={startDrag}
        title="Drag to move"
        aria-label="Move controls"
        className="flex h-11 w-6 cursor-grab items-center justify-center text-muted active:cursor-grabbing"
      >
        ⠿
      </button>
      <button onClick={onPrev} title="Previous page (←)" aria-label="Previous page" className={btn}>
        ‹
      </button>
      <button onClick={onNext} title="Next page (→)" aria-label="Next page" className={btn}>
        ›
      </button>
      <button onClick={onToc} title="Contents (t)" aria-label="Contents" className={btn}>☰</button>
      <button onClick={onFocus} title="Focus mode (f)" aria-label="Focus mode" className={btn}>⛶</button>
      <button
        onClick={onSettings}
        title="Reading settings"
        aria-label="Settings"
        className={`${btn} font-semibold`}
      >
        Aa
      </button>
      <button
        onClick={() => setPrefs({ fabHidden: true })}
        title="Hide controls"
        aria-label="Hide controls"
        className={`${btn} text-muted`}
      >
        ✕
      </button>
    </div>
  );
}
