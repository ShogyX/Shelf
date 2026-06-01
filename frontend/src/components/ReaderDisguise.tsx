// "Work mode" camouflage: makes the reading view look like serious product
// documentation, a business article, or an email thread. Pure presentation —
// the chapter text is unchanged; only the surrounding chrome + skin differ.

export type WorkMode = "off" | "docs" | "article" | "email";

export interface Skin {
  bg: string;        // page background
  panel: string;     // content surface
  text: string;      // body text color
  muted: string;     // secondary text
  accent: string;    // links / chips
  border: string;
  fontStack: string; // body font
}

// Fixed light "office" skins so the disguise is convincing regardless of theme.
export const DISGUISE_SKINS: Record<Exclude<WorkMode, "off">, Skin> = {
  docs: {
    bg: "#ffffff", panel: "#ffffff", text: "#1f2328", muted: "#636c76",
    accent: "#0969da", border: "#d0d7de",
    fontStack: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif",
  },
  article: {
    bg: "#ffffff", panel: "#ffffff", text: "#121212", muted: "#6b6b6b",
    accent: "#326891", border: "#e6e6e6",
    fontStack: "Georgia, 'Times New Roman', 'Source Serif Pro', serif",
  },
  email: {
    bg: "#f6f8fc", panel: "#ffffff", text: "#202124", muted: "#5f6368",
    accent: "#1a73e8", border: "#e0e3e7",
    fontStack: "Roboto, 'Segoe UI', -apple-system, Arial, sans-serif",
  },
};

function fakeDate(seed: string): string {
  // Deterministic-ish "recent" date so it doesn't flicker on re-render.
  const d = new Date();
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

function slug(s: string): string {
  return (s || "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/(^-|-$)/g, "")
    .slice(0, 48) || "overview";
}

/** The disguise chrome rendered above the chapter body (scroll mode). */
export function DisguiseHeader({
  mode,
  workTitle,
  chapterTitle,
  minutes,
}: {
  mode: Exclude<WorkMode, "off">;
  workTitle: string;
  chapterTitle: string;
  minutes: number;
}) {
  const s = DISGUISE_SKINS[mode];
  const date = fakeDate(workTitle + chapterTitle);

  if (mode === "docs") {
    return (
      <div style={{ color: s.text }}>
        <div className="mb-4 text-xs" style={{ color: s.muted }}>
          <span style={{ color: s.accent }}>Docs</span>
          <span className="px-1">/</span>
          <span style={{ color: s.accent }}>{workTitle}</span>
          <span className="px-1">/</span>
          <span>{chapterTitle}</span>
        </div>
        <div
          className="mb-2 inline-block rounded px-2 py-0.5 text-[11px] font-medium"
          style={{ background: "#ddf4ff", color: s.accent }}
        >
          GUIDE
        </div>
        <h1 className="mb-1 text-3xl font-semibold tracking-tight">{chapterTitle}</h1>
        <div className="mb-6 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm" style={{ color: s.muted }}>
          <span>Last updated {date}</span>
          <span>·</span>
          <span>{minutes} min read</span>
          <span>·</span>
          <span>v2.4</span>
          <code
            className="ml-1 rounded px-1.5 py-0.5 text-xs"
            style={{ background: "#f6f8fa", color: s.muted, border: `1px solid ${s.border}` }}
          >
            /{slug(workTitle)}/{slug(chapterTitle)}
          </code>
        </div>
        <hr style={{ borderColor: s.border }} className="mb-6" />
      </div>
    );
  }

  if (mode === "article") {
    return (
      <div style={{ color: s.text }}>
        <div className="mb-3 text-xs font-semibold uppercase tracking-[0.2em]" style={{ color: s.accent }}>
          Business · Analysis
        </div>
        <h1 className="mb-3 text-4xl font-bold leading-tight tracking-tight">{chapterTitle}</h1>
        <div className="mb-6 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm" style={{ color: s.muted }}>
          <span>By the Editorial Desk</span>
          <span>·</span>
          <span>{date}</span>
          <span>·</span>
          <span>{minutes} min read</span>
        </div>
        <hr style={{ borderColor: s.border }} className="mb-6" />
      </div>
    );
  }

  // email
  return (
    <div style={{ color: s.text }}>
      <h1 className="mb-4 text-2xl font-normal">{chapterTitle}</h1>
      <div className="mb-5 flex items-start gap-3">
        <div
          className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full text-sm font-semibold text-white"
          style={{ background: s.accent }}
        >
          {(workTitle[0] || "T").toUpperCase()}
        </div>
        <div className="min-w-0 flex-1 text-sm">
          <div className="flex flex-wrap items-baseline justify-between gap-x-2">
            <span className="font-semibold" style={{ color: s.text }}>
              {workTitle} Team
            </span>
            <span style={{ color: s.muted }}>{date}</span>
          </div>
          <div style={{ color: s.muted }}>
            &lt;updates@{slug(workTitle)}.com&gt;
          </div>
          <div style={{ color: s.muted }}>to me</div>
        </div>
      </div>
      <hr style={{ borderColor: s.border }} className="mb-6" />
    </div>
  );
}
