// The premium cover card used by every rail/grid in the redesign: a 2/3 poster (real art via Cover,
// else the generative fallback), a top-left kind badge, a hover scrim + circular play/headphone
// affordance, an optional bottom progress bar, and a title + subtitle below. Reused by Library,
// Discover, and the catalog grids. Render-prop-free; pass plain fields.
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { BookOpen, Headphones, X, Zap } from "lucide-react";
import Cover from "./Cover";
import { LanguageBadge } from "./LanguageBadge";

export type CoverKind = "book" | "audio" | "comic";

const KIND_ICON: Record<CoverKind, React.ReactNode> = {
  book: <BookOpen className="h-3 w-3" />,
  audio: <Headphones className="h-3 w-3" />,
  comic: <Zap className="h-3 w-3" />,
};

export function CoverCard({
  title, author, coverUrl, kind = "book", progress, subtitle, badge, language, to, onClick, onClear, width = "168px",
}: {
  title: string;
  author?: string | null;
  coverUrl?: string | null;
  kind?: CoverKind;
  progress?: number | null;       // 0..100 → bottom progress bar
  subtitle?: React.ReactNode;     // defaults to "author · <kind>"
  badge?: React.ReactNode;        // overrides the default kind badge (top-left)
  language?: string | null;       // ISO code → a top-right badge for non-English titles (else nothing)
  to?: string;                    // makes the card a <Link>
  onClick?: () => void;
  onClear?: () => void;           // optional ✕ (e.g. remove from "Continue reading")
  width?: string;
}) {
  const { t } = useTranslation();
  const kindLabel = { book: t("coverCard.book"), audio: t("coverCard.audio"), comic: t("coverCard.comic") }[kind];
  const sub =
    subtitle ??
    `${author ?? t("coverCard.unknownAuthor")}${
      kind === "audio" ? t("coverCard.audiobookSuffix") : kind === "comic" ? t("coverCard.graphicNovelSuffix") : ""
    }`;
  const inner = (
    <>
      <div className="relative aspect-[2/3] overflow-hidden rounded-[13px] border border-[var(--hair,var(--border))] shadow-[0_6px_18px_rgba(0,0,0,0.28)]">
        <Cover title={title} author={author} coverUrl={coverUrl} small />
        {/* kind badge */}
        <span className="absolute left-2 top-2 inline-flex items-center gap-1 rounded-full bg-black/55 px-2 py-1 text-[10px] font-bold uppercase tracking-wide text-white backdrop-blur-sm">
          {badge ?? <>{KIND_ICON[kind]} {kindLabel}</>}
        </span>
        {/* language badge (non-English titles only) — top-right, unless the ✕ affordance lives there */}
        {!onClear && <span className="absolute right-2 top-2"><LanguageBadge language={language} /></span>}
        {/* hover scrim + play affordance */}
        <div className="absolute inset-0 flex items-center justify-center bg-gradient-to-t from-black/50 to-transparent opacity-0 transition-opacity duration-200 group-hover:opacity-100">
          <span className="flex h-[52px] w-[52px] items-center justify-center rounded-full bg-accent text-accent-fg shadow-[0_6px_20px_rgba(0,0,0,0.4)]">
            {kind === "audio" ? <Headphones className="h-5 w-5" /> : <BookOpen className="h-5 w-5" />}
          </span>
        </div>
        {onClear && (
          <span
            role="button"
            tabIndex={0}
            title={t("coverCard.remove")}
            onClick={(e) => { e.preventDefault(); e.stopPropagation(); onClear(); }}
            onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); e.stopPropagation(); onClear(); } }}
            className="absolute right-1.5 top-1.5 z-10 flex h-6 w-6 items-center justify-center rounded-full bg-black/55 text-white opacity-0 backdrop-blur-sm transition hover:bg-black/75 group-hover:opacity-100"
          ><X className="h-3.5 w-3.5" /></span>
        )}
        {progress != null && progress > 0 && (
          <div className="absolute inset-x-0 bottom-0 h-1 bg-black/45">
            <div className="h-full bg-accent" style={{ width: `${Math.min(100, progress)}%` }} />
          </div>
        )}
      </div>
      <div className="mt-2.5 truncate text-[13.5px] font-semibold leading-tight text-text">{title}</div>
      <div className="mt-0.5 truncate text-xs text-muted">{sub}</div>
    </>
  );
  const cls = "group block shrink-0 cursor-pointer snap-start text-left transition-transform duration-200 [transition-timing-function:var(--ease)] hover:-translate-y-1.5";
  return to ? (
    <Link to={to} className={cls} style={{ width }}>{inner}</Link>
  ) : (
    <button type="button" onClick={onClick} className={cls} style={{ width }}>{inner}</button>
  );
}
