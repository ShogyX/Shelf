// A horizontal "rail": a Newsreader section heading + optional accent "see all" link, then a
// horizontally-scrolling, snap-aligned row of cover cards (scrollbar hidden). The Library/Discover
// shelves. Renders nothing when it has no children (callers pass `null`/`false` to skip empty rails).
import { Link } from "react-router-dom";

export function Rail({ title, moreLabel, moreTo, onMore, children }: {
  title: React.ReactNode;
  moreLabel?: string;
  moreTo?: string;
  onMore?: () => void;
  children: React.ReactNode;
}) {
  const kids = Array.isArray(children) ? children.filter(Boolean) : children;
  if (Array.isArray(kids) && kids.length === 0) return null;
  const more = moreLabel && (moreTo ? (
    <Link to={moreTo} className="text-[13px] font-semibold text-[var(--accent-bright,var(--accent))] opacity-90 hover:opacity-100">{moreLabel}</Link>
  ) : (
    <button type="button" onClick={onMore} className="text-[13px] font-semibold text-[var(--accent-bright,var(--accent))] opacity-90 hover:opacity-100">{moreLabel}</button>
  ));
  return (
    <section className="mt-8 first:mt-0">
      <div className="mb-3.5 flex items-baseline gap-3 px-1">
        <h2 className="font-display text-[23px] font-semibold tracking-tight text-text">{title}</h2>
        {more}
      </div>
      <div className="flex gap-[18px] overflow-x-auto scrollbar-none pb-2 [scroll-snap-type:x_proximity]">
        {children}
      </div>
    </section>
  );
}
