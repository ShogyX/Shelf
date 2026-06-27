import type { ReactNode } from "react";
import Cover from "./Cover";

// The featured/recommended title, presented as a complete book POSTER on the right with its details on
// the left (no full-bleed billboard). The card carries a glow in the cover's colours (--cover-*, also
// driving the page aurora) so the title and the background read as one palette. Used by both the
// Library home (Continue reading) and Discover (Featured this week).
export function FeaturedHero({
  eyebrow,
  title,
  author,
  meta,
  description,
  coverUrl,
  actions,
}: {
  eyebrow: string;
  title: string;
  author?: string | null;
  meta?: ReactNode; // the row after the author: genre · rating · type badge, etc.
  description?: string | null;
  coverUrl?: string | null;
  actions: ReactNode;
}) {
  return (
    <section className="relative mb-4 overflow-hidden">
      {/* Extra cover-coloured glow concentrated behind the poster (the global aurora handles the rest). */}
      <div aria-hidden className="featured-glow" />
      <div className="relative mx-auto grid max-w-6xl items-center gap-x-12 gap-y-7 px-5 py-9 sm:px-6 sm:py-12 lg:grid-cols-[1fr_300px] lg:py-16">
        {/* LEFT — details */}
        <div className="order-2 min-w-0 lg:order-1">
          <div className="mb-4 flex items-center gap-3 text-[11.5px] font-bold uppercase tracking-[0.2em] text-[var(--accent-bright,var(--accent))]">
            <span className="h-px w-7 bg-current opacity-70" />
            {eyebrow}
          </div>
          <h1 className="font-display text-[36px] font-semibold leading-[1.03] tracking-tight text-text sm:text-[52px]">
            {title}
          </h1>
          <div className="mt-3.5 flex flex-wrap items-center gap-x-2.5 gap-y-1.5 text-[14px] text-muted">
            {author && <span className="font-semibold text-text">{author}</span>}
            {meta}
          </div>
          {description && (
            <p className="mt-5 max-w-[540px] text-[15px] leading-relaxed text-[var(--text-soft,var(--muted))] line-clamp-3">
              {description}
            </p>
          )}
          <div className="mt-7 flex flex-wrap items-center gap-3">{actions}</div>
        </div>

        {/* RIGHT — the whole book as a poster, slightly tilted in 3D with directional shading so it
            sits in the same depth as the background (perspective wrapper → rotated card → sheen). */}
        <div className="featured-poster order-1 mx-auto w-[168px] shrink-0 sm:w-[210px] lg:order-2 lg:mx-0 lg:w-[300px]">
          <div className="featured-card relative aspect-[2/3] w-full overflow-hidden rounded-[16px]">
            <Cover title={title} author={author} coverUrl={coverUrl} />
            {/* Lit edge → shadowed edge, matching the tilt + the background's light direction. */}
            <div aria-hidden className="featured-card-sheen pointer-events-none absolute inset-0" />
          </div>
        </div>
      </div>
    </section>
  );
}

// A middot separator + a small pill badge, for building the meta row.
export function Dot() {
  return <span className="text-[var(--hair-strong,var(--border))]">·</span>;
}
export function MetaBadge({ children }: { children: ReactNode }) {
  return (
    <span className="inline-flex items-center gap-1 rounded-md bg-surface-2 px-2 py-0.5 text-[12px] font-semibold text-text">
      {children}
    </span>
  );
}
