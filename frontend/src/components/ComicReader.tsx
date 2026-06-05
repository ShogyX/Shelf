import { useEffect, useImperativeHandle, useLayoutEffect, useMemo, useRef, useState, forwardRef } from "react";
import { useApp } from "../store";

// Imperative handle so the shared reader chrome (FAB arrows, keyboard) can drive the comic
// viewer without lifting its page index into the parent.
export interface ComicNav {
  next: () => void;
  prev: () => void;
  zoomIn: () => void;
  zoomOut: () => void;
  resetZoom: () => void;
}

interface Props {
  html: string;
  bgColor: string;
  // Saved position to restore to (null → start at the top of a fresh chapter).
  restore: { fraction: number; index: number } | null;
  onProgress: (fraction: number, index: number) => void;
  onPrevChapter: () => void;
  onNextChapter: () => void;
  hasPrev: boolean;
  hasNext: boolean;
  chromeHidden: boolean;
  onToggleChrome: () => void;
}

const ZOOM_MIN = 0.5;
const ZOOM_MAX = 4;
const clampZoom = (z: number) => Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, Math.round(z * 100) / 100));

// Pull the page image URLs out of the sanitized comic markup
// (`<div class="comic"><figure class="comic-page"><img src=…>`), preserving order.
function extractImages(html: string): string[] {
  try {
    const doc = new DOMParser().parseFromString(html, "text/html");
    return Array.from(doc.querySelectorAll("img"))
      .map((img) => img.getAttribute("src") || "")
      .filter(Boolean);
  } catch {
    return [];
  }
}

const ComicReader = forwardRef<ComicNav, Props>(function ComicReader(
  { html, bgColor, restore, onProgress, onPrevChapter, onNextChapter, hasPrev, hasNext, chromeHidden, onToggleChrome },
  ref
) {
  const { prefs, setPrefs } = useApp();
  const images = useMemo(() => extractImages(html), [html]);
  const count = images.length;

  const mode = prefs.comicMode ?? "continuous";
  const fit = prefs.comicFit ?? "width";
  const zoom = clampZoom(prefs.comicZoom ?? 1);
  const gap = Math.max(0, prefs.comicGap ?? 0);

  const scrollRef = useRef<HTMLDivElement>(null);
  const imgRefs = useRef<(HTMLImageElement | null)[]>([]);
  const [idx, setIdx] = useState(restore?.index ?? 0);
  const restoredRef = useRef(false);

  // Live viewport height for "fit to height" — a CSS % height would collapse against the
  // auto-height image column, so size in pixels off the actual reading area.
  const [areaH, setAreaH] = useState(0);
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const measure = () => setAreaH(el.clientHeight);
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [mode, count]);

  // Report progress (updates the bar immediately; the parent debounces the network save).
  const report = (fraction: number, index: number) =>
    onProgress(Math.min(1, Math.max(0, fraction)), Math.max(0, index));

  // ---- continuous (webtoon) scroll → progress ----
  const onScroll = () => {
    if (mode !== "continuous") return;
    const el = scrollRef.current;
    if (!el) return;
    const frac = el.scrollTop / Math.max(1, el.scrollHeight - el.clientHeight);
    // Estimate which page is at the top of the viewport for resume + the page indicator.
    let top = 0;
    for (let i = 0; i < imgRefs.current.length; i++) {
      const im = imgRefs.current[i];
      if (im && im.offsetTop <= el.scrollTop + 8) top = i;
      else break;
    }
    setIdx(top);
    report(frac, top);
  };

  // ---- single-page navigation ----
  const goTo = (i: number) => {
    const clamped = Math.max(0, Math.min(count - 1, i));
    setIdx(clamped);
    report(count > 1 ? clamped / (count - 1) : 0, clamped);
  };
  const next = () => {
    if (mode === "single") {
      if (idx < count - 1) goTo(idx + 1);
      else if (hasNext) onNextChapter();
    } else {
      const el = scrollRef.current;
      if (el && el.scrollTop < el.scrollHeight - el.clientHeight - 4) el.scrollBy({ top: el.clientHeight * 0.9, behavior: "smooth" });
      else if (hasNext) onNextChapter();
    }
  };
  const prev = () => {
    if (mode === "single") {
      if (idx > 0) goTo(idx - 1);
      else if (hasPrev) onPrevChapter();
    } else {
      const el = scrollRef.current;
      if (el && el.scrollTop > 4) el.scrollBy({ top: -el.clientHeight * 0.9, behavior: "smooth" });
      else if (hasPrev) onPrevChapter();
    }
  };

  const setZoom = (z: number) => setPrefs({ comicZoom: clampZoom(z) });
  useImperativeHandle(ref, () => ({
    next,
    prev,
    zoomIn: () => setZoom(zoom + 0.25),
    zoomOut: () => setZoom(zoom - 0.25),
    resetZoom: () => setZoom(1),
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [idx, count, mode, zoom, hasNext, hasPrev]);

  // ---- restore position once the images up to the target have laid out ----
  const targetIdx = restore?.index ?? 0;
  const loaded = useRef<Set<number>>(new Set());
  useEffect(() => {
    // New chapter: reset restore bookkeeping and seed the single-page index.
    restoredRef.current = false;
    loaded.current = new Set();
    setIdx(Math.max(0, Math.min(restore?.index ?? 0, count - 1)));
    if (mode === "single") restoredRef.current = true; // idx is enough for single-page
    // A fresh chapter still needs its position recorded so "continue reading" points here.
    if (!restore) report(0, 0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [html, mode]);

  // Scroll the saved page to the top once it — and everything above it — has loaded, so the
  // offsetTop is stable (images above push the target down as they decode).
  const maybeRestore = () => {
    if (restoredRef.current || mode === "single") return;
    const el = scrollRef.current;
    if (!el) return;
    if (targetIdx === 0) { restoredRef.current = true; return; }
    for (let i = 0; i <= targetIdx; i++) if (!loaded.current.has(i)) return;
    const target = imgRefs.current[targetIdx];
    if (target) {
      el.scrollTop = target.offsetTop;
      restoredRef.current = true;
    }
  };

  const onImgLoad = (i: number) => {
    loaded.current.add(i);
    maybeRestore();
  };

  // ---- pinch-to-zoom (two pointers) ----
  const pointers = useRef<Map<number, { x: number; y: number }>>(new Map());
  const pinchBase = useRef<{ dist: number; zoom: number } | null>(null);
  const onPointerDown = (e: React.PointerEvent) => {
    pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
  };
  const onPointerMove = (e: React.PointerEvent) => {
    if (!pointers.current.has(e.pointerId)) return;
    pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pointers.current.size === 2) {
      const [a, b] = Array.from(pointers.current.values());
      const dist = Math.hypot(a.x - b.x, a.y - b.y);
      if (!pinchBase.current) pinchBase.current = { dist, zoom };
      else if (pinchBase.current.dist > 0) {
        setZoom(pinchBase.current.zoom * (dist / pinchBase.current.dist));
      }
    }
  };
  const endPointer = (e: React.PointerEvent) => {
    pointers.current.delete(e.pointerId);
    if (pointers.current.size < 2) pinchBase.current = null;
  };

  // double-tap toggles between fit (1×) and 2× zoom
  const lastTap = useRef(0);
  const onTapZoom = () => {
    const now = Date.now();
    if (now - lastTap.current < 280) {
      setZoom(zoom > 1.01 ? 1 : 2);
      lastTap.current = 0;
    } else {
      lastTap.current = now;
    }
  };

  if (count === 0) {
    return (
      <div className="flex flex-1 items-center justify-center px-6 text-center text-muted" style={{ background: bgColor }}>
        <p>No images in this chapter yet — the crawler may still be fetching the pages.</p>
      </div>
    );
  }

  // Per-image sizing. Width-based zoom reflows crisply (no blurry CSS transform); horizontal
  // overflow is reachable by scrolling when zoomed past the edge.
  // Manga (single page): fit the WHOLE page within the screen (contain) so one page is fully
  // visible — capped to BOTH the viewport width and height; zoom scales up from there. (Plain
  // "fit width" overflowed portrait pages off the bottom — the "too much zoom" complaint.)
  // Webtoon (continuous): fill the width (or height) and scroll through the strip.
  const areaPx = areaH || window.innerHeight;
  const imgStyle: React.CSSProperties =
    mode === "single"
      ? {
          maxWidth: `${zoom * 100}%`,
          maxHeight: `${areaPx * zoom}px`,
          width: "auto",
          height: "auto",
        }
      : fit === "width"
        ? { width: `${zoom * 100}%`, height: "auto", maxWidth: "none" }
        : { height: areaPx * zoom, width: "auto", maxWidth: "none", maxHeight: "none" };

  // ---- single page ----
  if (mode === "single") {
    const cur = Math.max(0, Math.min(idx, count - 1)); // tolerate a stale/oversized resume index
    return (
      <div
        ref={scrollRef}
        className="relative flex-1 select-none overflow-auto"
        style={{ background: bgColor, touchAction: "manipulation" }}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endPointer}
        onPointerCancel={endPointer}
      >
        {/* tap zones: sides flip pages, centre toggles chrome + handles double-tap zoom */}
        <button aria-label="Previous page" onClick={prev} className="absolute left-0 top-0 z-20 h-full w-[28%] cursor-w-resize" />
        <button aria-label="Next page" onClick={next} className="absolute right-0 top-0 z-20 h-full w-[28%] cursor-e-resize" />
        <button
          aria-label="Toggle bars"
          onClick={() => { onTapZoom(); onToggleChrome(); }}
          className="absolute left-[28%] top-0 z-10 h-full w-[44%]"
        />
        <div className="flex min-h-full min-w-full items-center justify-center">
          <img
            key={cur}
            src={images[cur]}
            alt={`Page ${cur + 1}`}
            draggable={false}
            style={imgStyle}
            className="block"
          />
        </div>
        {!chromeHidden && (
          <div className="pointer-events-none absolute bottom-2 left-1/2 z-20 -translate-x-1/2 rounded-full bg-black/55 px-3 py-1 text-xs font-medium text-white backdrop-blur">
            {cur + 1} / {count}
          </div>
        )}
      </div>
    );
  }

  // ---- continuous (webtoon strip) ----
  return (
    <div
      ref={scrollRef}
      onScroll={onScroll}
      className="relative flex-1 select-none overflow-auto"
      style={{ background: bgColor, touchAction: "manipulation" }}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={endPointer}
      onPointerCancel={endPointer}
    >
      {/* Centre tap zone toggles chrome / double-tap zoom without blocking vertical scroll. */}
      <div className="flex flex-col items-center" style={{ gap: `${gap}px`, paddingBottom: "env(safe-area-inset-bottom)" }}>
        {images.map((src, i) => (
          <img
            key={i}
            ref={(el) => (imgRefs.current[i] = el)}
            src={src}
            alt={`Page ${i + 1}`}
            // Eager-load everything up to the resume point so its offsetTop is known; lazy after.
            loading={i <= Math.max(2, targetIdx) ? "eager" : "lazy"}
            draggable={false}
            onClick={() => { onTapZoom(); onToggleChrome(); }}
            onLoad={() => onImgLoad(i)}
            style={imgStyle}
            className="block"
          />
        ))}
        <div className="flex w-full max-w-md items-center justify-between gap-3 px-5 py-8 text-xs">
          <button
            onClick={onPrevChapter}
            disabled={!hasPrev}
            className="rounded-lg border border-border px-3 py-2 text-text disabled:opacity-40"
          >
            ← Previous
          </button>
          <span className="text-muted">end of chapter</span>
          <button
            onClick={onNextChapter}
            disabled={!hasNext}
            className="rounded-lg bg-accent px-3 py-2 font-medium text-accent-fg disabled:opacity-40"
          >
            Next →
          </button>
        </div>
      </div>
      {!chromeHidden && (
        <div className="pointer-events-none fixed bottom-2 left-1/2 z-20 -translate-x-1/2 rounded-full bg-black/55 px-3 py-1 text-xs font-medium text-white backdrop-blur">
          {idx + 1} / {count}
        </div>
      )}
    </div>
  );
});

export default ComicReader;
