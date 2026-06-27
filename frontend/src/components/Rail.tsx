// A horizontal "rail": a Newsreader section heading + optional accent "see all" link, then a
// horizontally-scrolling, snap-aligned row of cover cards (scrollbar hidden). The Library/Discover
// shelves. Renders nothing when it has no children (callers pass `null`/`false` to skip empty rails).
import { useRef, type ReactNode } from "react";
import { Link } from "react-router-dom";

// Desktop-only scroll affordance: a circular chevron that nudges the rail ~one screen, revealed on
// hover (touch devices keep native swipe, so it's hidden there). Positioned just OUTSIDE the rail's
// edges (translate-x-full) so it never overlaps the cards — avoids misclicking a poster when aiming
// for the arrow. Edge clamping is handled by the browser, so no scroll-position bookkeeping is needed.
function RailArrow({ side, onClick }: { side: "left" | "right"; onClick: () => void }) {
  return (
    <button
      type="button"
      aria-label={side === "left" ? "Scroll left" : "Scroll right"}
      onClick={onClick}
      className={`absolute top-[42%] z-10 hidden h-10 w-10 -translate-y-1/2 items-center justify-center rounded-full border border-[var(--hair-strong,var(--border))] bg-[color-mix(in_srgb,var(--surface)_88%,transparent)] text-text opacity-0 shadow-[var(--card-shadow)] backdrop-blur transition hover:scale-105 hover:bg-surface group-hover/rail:opacity-100 sm:flex ${
        side === "left" ? "left-0 -translate-x-full -ml-1.5" : "right-0 translate-x-full -mr-1.5"
      }`}
    >
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
        {side === "left" ? <path d="m15 18-6-6 6-6" /> : <path d="m9 18 6-6-6-6" />}
      </svg>
    </button>
  );
}

// Reusable horizontal scroller with the hover arrows and hidden scrollbar. Shared by Rail (Library)
// and the Discover lanes so both browse identically. `gap` lets a denser lane keep tighter spacing.
export function RailScroller({ children, gap = "gap-[18px]" }: { children: ReactNode; gap?: string }) {
  const scroller = useRef<HTMLDivElement>(null);
  const nudge = (dir: number) =>
    scroller.current?.scrollBy({ left: dir * scroller.current.clientWidth * 0.8, behavior: "smooth" });
  return (
    <div className="group/rail relative">
      <div ref={scroller} className={`flex ${gap} overflow-x-auto scrollbar-none pb-2 [scroll-snap-type:x_proximity]`}>
        {children}
      </div>
      <RailArrow side="left" onClick={() => nudge(-1)} />
      <RailArrow side="right" onClick={() => nudge(1)} />
    </div>
  );
}

export function Rail({ title, moreLabel, moreTo, onMore, children }: {
  title: ReactNode;
  moreLabel?: string;
  moreTo?: string;
  onMore?: () => void;
  children: ReactNode;
}) {
  const kids = Array.isArray(children) ? children.filter(Boolean) : children;
  if (Array.isArray(kids) && kids.length === 0) return null;
  const more = moreLabel && (moreTo ? (
    <Link to={moreTo} className="text-[13px] font-semibold text-[var(--accent-bright,var(--accent))] opacity-90 hover:opacity-100">{moreLabel}</Link>
  ) : (
    <button type="button" onClick={onMore} className="text-[13px] font-semibold text-[var(--accent-bright,var(--accent))] opacity-90 hover:opacity-100">{moreLabel}</button>
  ));
  return (
    // A very slight, edge-faded hairline centered in the gap separates each rail (including the first,
    // which sits under the featured section). It's a ::before (no layout shift); `via` is the only
    // opaque stop, so the line is a faint cut in the middle that dissolves toward both ends.
    <section className="relative mt-8 first:mt-7 before:pointer-events-none before:absolute before:inset-x-0 before:-top-4 before:h-px before:bg-gradient-to-r before:from-transparent before:via-[var(--hair-strong,var(--border))] before:to-transparent before:content-['']">
      <div className="mb-3.5 flex items-baseline gap-3 px-1">
        <h2 className="font-display text-[23px] font-semibold tracking-tight text-text">{title}</h2>
        {more}
      </div>
      <RailScroller>{children}</RailScroller>
    </section>
  );
}
