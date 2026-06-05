import { useLayoutEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, Chapter } from "../api/client";
import RelatedTitles from "./RelatedTitles";
import { Spinner } from "./ui";

const ROW_H = 40; // fixed row height (px) — drives the windowed list math
const OVERSCAN = 10; // rows rendered above/below the viewport to mask fast scrolling

/** Virtualized chapter list: only the rows in (or near) the viewport are mounted, so a work
 *  with tens of thousands of chapters still scrolls smoothly and the whole TOC is reachable. */
function ChapterList({
  items,
  currentChapterId,
  onPick,
}: {
  items: Chapter[];
  currentChapterId?: number;
  onPick: (chapterId: number) => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [viewport, setViewport] = useState(600);

  // Measure the scroll viewport, and re-measure on resize.
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => setViewport(el.clientHeight || 600);
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // On open, center the chapter currently being read so the reader lands in context. Layout
  // effect (not useEffect) so the scroll is set before paint — no flash of the list top.
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el || currentChapterId == null) return;
    const idx = items.findIndex((c) => c.id === currentChapterId);
    if (idx >= 0) el.scrollTop = Math.max(0, idx * ROW_H - el.clientHeight / 2);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items, currentChapterId]);

  const start = Math.max(0, Math.floor(scrollTop / ROW_H) - OVERSCAN);
  const end = Math.min(items.length, Math.ceil((scrollTop + viewport) / ROW_H) + OVERSCAN);
  const visible = items.slice(start, end);

  return (
    <div
      ref={ref}
      onScroll={(e) => setScrollTop(e.currentTarget.scrollTop)}
      className="scrollbar-thin flex-1 overflow-y-auto"
    >
      {/* Spacer sized to the full list; the visible slice is offset into place. Only
          HORIZONTAL padding here — vertical padding would shift rows off the N*ROW_H grid the
          windowing math relies on. */}
      <div style={{ height: items.length * ROW_H, position: "relative" }} className="px-2">
        <div style={{ transform: `translateY(${start * ROW_H}px)` }}>
          {visible.map((c) => {
            const active = c.id === currentChapterId;
            return (
              <button
                key={c.id}
                disabled={!c.has_content}
                onClick={() => onPick(c.id)}
                style={{ height: ROW_H }}
                className={`flex w-full items-center justify-between gap-2 rounded-lg px-3 text-left text-sm transition ${
                  active ? "bg-accent text-accent-fg" : "hover:bg-surface-2"
                } ${!c.has_content ? "opacity-40" : ""}`}
              >
                <span className="truncate">
                  <span className="mr-2 text-xs opacity-60">{c.number}</span>
                  {c.title}
                </span>
                {!c.has_content && <span className="text-xs">⏳</span>}
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}

export default function TocDrawer({
  workId,
  currentChapterId,
  onClose,
  onPick,
}: {
  workId: number;
  currentChapterId?: number;
  onClose: () => void;
  onPick: (chapterId: number) => void;
}) {
  const chapters = useQuery({
    queryKey: ["chapters-all", workId],
    queryFn: () => api.listAllChapters(workId),
  });

  const items = chapters.data ?? [];

  return (
    <>
      <div className="fixed inset-0 z-40 bg-black/30" onClick={onClose} />
      <aside className="fixed left-0 top-0 z-50 flex h-full w-80 max-w-[85vw] flex-col border-r border-border bg-surface shadow-xl">
        {/* Pad for the iOS status bar in standalone PWA mode (viewport-fit=cover +
            black-translucent draw the drawer full-bleed under the notch). */}
        <div
          className="flex items-center justify-between border-b border-border px-4 py-3"
          style={{ paddingTop: "max(0.75rem, env(safe-area-inset-top))" }}
        >
          <h3 className="font-semibold">
            Contents{items.length ? ` · ${items.length}` : ""}
          </h3>
          <button onClick={onClose} className="text-muted hover:text-text">
            ✕
          </button>
        </div>
        {/* Related titles stay at the top (out of the virtualized region); capped so a long
            related list can never crowd out the chapter list. */}
        <div className="max-h-[40%] shrink-0 overflow-y-auto">
          <RelatedTitles workId={workId} />
        </div>
        {chapters.isLoading ? (
          <div className="p-4">
            <Spinner label="Loading…" />
          </div>
        ) : (
          <ChapterList items={items} currentChapterId={currentChapterId} onPick={onPick} />
        )}
      </aside>
    </>
  );
}
