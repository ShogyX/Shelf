import { useState } from "react";

function hashOf(s: string): number {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return h;
}

// A tasteful, deterministic "designed" cover when no artwork exists: gradient keyed
// to the title, a darker spine, a hairline frame, the title in serif, author footer.
function Generative({ title, author, small, bare }: { title: string; author?: string | null; small?: boolean; bare?: boolean }) {
  const h = hashOf(title);
  const hue = h % 360;
  const hue2 = (hue + 35 + (h % 40)) % 360;
  const sat = 42 + (h % 16);
  const bg = `linear-gradient(150deg, hsl(${hue} ${sat}% 34%), hsl(${hue2} ${sat}% 22%))`;
  // `bare`: just the keyed gradient + highlight, NO printed title/spine/frame — for a billboard
  // background where the title is rendered separately (avoids a duplicate "ghost" title).
  if (bare) {
    return (
      <div className="relative h-full w-full overflow-hidden" style={{ background: bg }}>
        <div className="absolute inset-0"
          style={{ background: "radial-gradient(120% 80% at 70% 0%, rgba(255,255,255,.14), transparent 60%)" }} />
      </div>
    );
  }
  return (
    <div className="relative h-full w-full overflow-hidden" style={{ background: bg }}>
      {/* soft highlight */}
      <div
        className="absolute inset-0"
        style={{ background: "radial-gradient(120% 80% at 25% 0%, rgba(255,255,255,.16), transparent 60%)" }}
      />
      {/* spine */}
      <div className="absolute inset-y-0 left-0 w-[6%] bg-black/25" />
      <div className="absolute inset-y-0 left-[6%] w-px bg-white/15" />
      {/* frame */}
      <div className={`absolute ${small ? "inset-1.5" : "inset-3"} rounded-[3px] border border-white/25`} />
      <div className="relative flex h-full flex-col items-center justify-center px-[12%] text-center">
        <div
          className={`font-display font-semibold leading-tight text-white drop-shadow ${
            small ? "text-[11px] line-clamp-4" : "text-base line-clamp-5 sm:text-lg"
          }`}
        >
          {title}
        </div>
        {author && !small && (
          <>
            <div className="my-2 h-px w-8 bg-white/40" />
            <div className="text-[11px] uppercase tracking-wide text-white/75 line-clamp-2">{author}</div>
          </>
        )}
      </div>
    </div>
  );
}

export default function Cover({
  title,
  author,
  coverUrl,
  small,
  bare,
}: {
  title: string;
  author?: string | null;
  coverUrl?: string | null;
  small?: boolean;
  bare?: boolean;
}) {
  const [failed, setFailed] = useState(false);
  const src = coverSrc(coverUrl);
  if (src && !failed) {
    return (
      <img
        src={src}
        alt={title}
        loading="lazy"
        onError={() => setFailed(true)}
        className="h-full w-full object-cover"
      />
    );
  }
  return <Generative title={title} author={author} small={small} bare={bare} />;
}

/** On-disk-first cover source. A local path is served straight from disk; a remote URL is routed
 * through /api/cover, which checks the disk cache first and fetches from the web at most once (then
 * caches it) — so the browser never fetches a remote cover directly (the cause of covers flickering
 * in and out when a CDN hotlink-blocks / rate-limits / Cloudflare-challenges). */
export function coverSrc(coverUrl?: string | null): string | null {
  if (!coverUrl) return null;
  if (coverUrl.startsWith("/")) return coverUrl; // already local (served by the media/covers mount)
  return `/api/cover?u=${encodeURIComponent(coverUrl)}`;
}
