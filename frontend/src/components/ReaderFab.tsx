import { useEffect, useRef, useState } from "react";
import { useApp } from "../store";

type Side = "left" | "right" | "top" | "bottom";

// Floating control cluster docked to an edge of the screen. Drag it toward any
// edge to re-dock; position along that edge is remembered. The settings panel
// "falls out" adjacent to it (see panelAnchor in Reader).
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
  const [side, setSide] = useState<Side>(prefs.fabSide ?? "right");
  const [pos, setPos] = useState<number>(prefs.fabPos ?? 0.5);
  const ref = useRef<HTMLDivElement>(null);
  const dragging = useRef(false);
  const live = useRef({ side, pos });

  useEffect(() => {
    if (dragging.current) return;
    setSide(prefs.fabSide ?? "right");
    setPos(prefs.fabPos ?? 0.5);
  }, [prefs.fabSide, prefs.fabPos]);

  useEffect(() => {
    const move = (e: PointerEvent) => {
      if (!dragging.current) return;
      const { innerWidth: w, innerHeight: h } = window;
      // Nearest edge wins.
      const dl = e.clientX, dr = w - e.clientX, dt = e.clientY, db = h - e.clientY;
      const min = Math.min(dl, dr, dt, db);
      const ns: Side = min === dl ? "left" : min === dr ? "right" : min === dt ? "top" : "bottom";
      const np =
        ns === "left" || ns === "right"
          ? Math.max(0, Math.min(1, e.clientY / h))
          : Math.max(0, Math.min(1, e.clientX / w));
      live.current = { side: ns, pos: np };
      setSide(ns);
      setPos(np);
    };
    const up = () => {
      if (!dragging.current) return;
      dragging.current = false;
      setPrefs({ fabSide: live.current.side, fabPos: Math.round(live.current.pos * 1000) / 1000 });
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    return () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
  }, [setPrefs]);

  const vertical = side === "left" || side === "right";
  const style: React.CSSProperties = { position: "fixed" };
  const margin = 8;
  if (side === "left") Object.assign(style, { left: margin, top: `${pos * 100}%`, transform: "translateY(-50%)" });
  if (side === "right") Object.assign(style, { right: margin, top: `${pos * 100}%`, transform: "translateY(-50%)" });
  if (side === "top") Object.assign(style, { top: `max(${margin}px, env(safe-area-inset-top))`, left: `${pos * 100}%`, transform: "translateX(-50%)" });
  if (side === "bottom") Object.assign(style, { bottom: `max(${margin}px, env(safe-area-inset-bottom))`, left: `${pos * 100}%`, transform: "translateX(-50%)" });

  const startDrag = (e: React.PointerEvent) => {
    dragging.current = true;
    live.current = { side, pos };
    e.preventDefault();
  };

  const btn = "flex h-11 w-11 items-center justify-center rounded-full text-base text-text hover:bg-surface-2";

  return (
    <div
      ref={ref}
      className={`z-40 flex items-center gap-0.5 rounded-full border border-border bg-surface/95 p-1 shadow-xl backdrop-blur touch-none select-none ${
        vertical ? "flex-col" : "flex-row"
      }`}
      style={style}
    >
      <button
        onPointerDown={startDrag}
        title="Drag to a side to dock"
        aria-label="Move controls"
        className="flex h-11 w-6 cursor-grab items-center justify-center text-muted active:cursor-grabbing"
      >
        ⠿
      </button>
      <button
        onClick={onPrev}
        title="Previous page (←)"
        aria-label="Previous page"
        className={btn}
      >
        {vertical ? "▲" : "‹"}
      </button>
      <button
        onClick={onNext}
        title="Next page (→)"
        aria-label="Next page"
        className={btn}
      >
        {vertical ? "▼" : "›"}
      </button>
      <button onClick={onToc} title="Contents (t)" aria-label="Contents" className={btn}>☰</button>
      <button onClick={onFocus} title="Focus mode (f)" aria-label="Focus mode" className={btn}>⛶</button>
      <button onClick={onSettings} title="Reading settings" aria-label="Settings" className={`${btn} font-semibold`}>
        Aa
      </button>
    </div>
  );
}
