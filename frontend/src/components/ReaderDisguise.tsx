// "Work mode" camouflage: makes the reading view look like serious product
// documentation, a business article, or an email thread. Pure presentation —
// the chapter text is unchanged; only the surrounding chrome + skin differ.
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";

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

// --- body restructuring -----------------------------------------------------
// Reformat the actual prose so it READS like docs / an article / an email thread,
// not just re-skinned chrome. Real text blocks (p/h1-3/blockquote/li — the ones the
// reader tracks for progress) are decorated *in place* so paragraph indices stay
// aligned; all inserted framing is non-tracked <div> so resume position is preserved.
const TRACKED = ["p", "h1", "h2", "h3", "blockquote", "li"];
// Localized decoration labels for the docs/article disguises. Built from `t` per call so the
// camouflage matches the reader's language.
const docLabels = (t: TFunction) => [
  t("disguise.docLabel.note"), t("disguise.docLabel.tip"), t("disguise.docLabel.example"),
  t("disguise.docLabel.caution"), t("disguise.docLabel.seeAlso"), t("disguise.docLabel.important"),
];
const articleSubheads = (t: TFunction) => [
  t("disguise.articleSubhead.keyTakeaways"), t("disguise.articleSubhead.whatItMeans"),
  t("disguise.articleSubhead.byTheNumbers"), t("disguise.articleSubhead.bottomLine"),
  t("disguise.articleSubhead.background"), t("disguise.articleSubhead.lookingAhead"),
];
// Sample reply-thread names — decorative identifiers, kept locale-neutral (not translated).
const EMAIL_NAMES = ["A. Patel", "J. Romero", "Sam Lee", "M. Okafor", "Dana K.", "R. Singh"];

function esc(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function firstSentence(t: string): string {
  const m = (t || "").trim().match(/[^.!?]{4,}[.!?]/);
  return (m ? m[0] : (t || "")).trim().slice(0, 140);
}

export function disguiseBody(html: string, mode: Exclude<WorkMode, "off">, t: TFunction): string {
  if (typeof window === "undefined" || !html) return html;
  let root: HTMLElement | null;
  try {
    root = new DOMParser().parseFromString(`<div>${html}</div>`, "text/html")
      .body.firstElementChild as HTMLElement;
  } catch {
    return html;
  }
  if (!root) return html;
  const blocks = Array.from(root.children) as HTMLElement[];
  const isTracked = (tag: string) => TRACKED.includes(tag);
  const out: string[] = [];
  const DOC_LABELS = docLabels(t);
  const ARTICLE_SUBHEADS = articleSubheads(t);

  if (mode === "email") {
    out.push(`<div class="wm-deco wm-email-greet">${esc(t("disguise.emailGreeting"))}</div>`);
    let depth = 1, sep = 0;
    blocks.forEach((el, i) => {
      const tag = el.tagName.toLowerCase();
      if (!isTracked(tag)) { out.push(el.outerHTML); return; }
      if (i > 0 && i % 6 === 0) {
        sep++;
        depth = depth >= 3 ? 1 : depth + 1;
        const nm = EMAIL_NAMES[sep % EMAIL_NAMES.length];
        const date = `Mon, Jun ${1 + (sep % 27)}, 2026 at 9:${String((13 + sep) % 60).padStart(2, "0")} AM`;
        out.push(`<div class="wm-deco wm-email-sep">${esc(t("disguise.emailReplyHeader", { date, name: nm }))}</div>`);
      }
      out.push(`<${tag} class="wm-q${depth}">${el.innerHTML}</${tag}>`);
    });
    out.push(`<div class="wm-deco wm-email-sig">—<br/>${esc(t("disguise.emailSignature"))}</div>`);
    return out.join("");
  }

  if (mode === "docs") {
    out.push(`<div class="wm-deco wm-doc-frame"><b>${esc(t("disguise.docName"))}</b><br/>`
      + `&nbsp;&nbsp;&nbsp;&nbsp;${esc(t("disguise.docNameBody"))}<br/><br/>`
      + `<b>${esc(t("disguise.docSynopsis"))}</b><br/>&nbsp;&nbsp;&nbsp;&nbsp;<code>import { reference } from \"./core\"</code>`
      + `<br/><br/><b>${esc(t("disguise.docDescription"))}</b></div>`);
    let sec = 0, pc = 0;
    blocks.forEach((el) => {
      const tag = el.tagName.toLowerCase();
      if (/^h[1-3]$/.test(tag)) {
        sec++;
        out.push(`<${tag}>${sec}.&nbsp; ${esc(el.textContent || "")}</${tag}>`);
        return;
      }
      if (!isTracked(tag)) { out.push(el.outerHTML); return; }
      pc++;
      if (pc > 1 && pc % 5 === 0) {
        out.push(`<div class="wm-deco wm-doc-note"><b>${DOC_LABELS[(pc / 5) % DOC_LABELS.length | 0]}</b>`
          + `&nbsp;— ${esc(firstSentence(el.textContent || ""))}</div>`);
      }
      const inner = el.innerHTML.replace(/"([^"]{1,140}?)"/g, '<code>"$1"</code>');
      out.push(`<${tag}>${inner}</${tag}>`);
    });
    return out.join("");
  }

  // article
  let pc = 0;
  blocks.forEach((el) => {
    const tag = el.tagName.toLowerCase();
    if (!isTracked(tag)) { out.push(el.outerHTML); return; }
    if (tag === "p") {
      pc++;
      if (pc > 1 && pc % 5 === 0) {
        out.push(`<div class="wm-deco wm-subhead">${ARTICLE_SUBHEADS[(pc / 5) % ARTICLE_SUBHEADS.length | 0]}</div>`);
      }
      out.push(`<p${pc === 1 ? ' class="wm-lead"' : ""}>${el.innerHTML}</p>`);
      if (pc % 6 === 3) {
        const pull = firstSentence(el.textContent || "");
        if (pull.length > 20) out.push(`<div class="wm-deco wm-pull">“${esc(pull)}”</div>`);
      }
    } else {
      out.push(el.outerHTML);
    }
  });
  return out.join("");
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
  const { t } = useTranslation();
  const s = DISGUISE_SKINS[mode];
  const date = fakeDate(workTitle + chapterTitle);

  if (mode === "docs") {
    return (
      <div style={{ color: s.text }}>
        <div className="mb-4 text-xs" style={{ color: s.muted }}>
          <span style={{ color: s.accent }}>{t("disguise.docsBreadcrumb")}</span>
          <span className="px-1">/</span>
          <span style={{ color: s.accent }}>{workTitle}</span>
          <span className="px-1">/</span>
          <span>{chapterTitle}</span>
        </div>
        <div
          className="mb-2 inline-block rounded px-2 py-0.5 text-[11px] font-medium"
          style={{ background: "#ddf4ff", color: s.accent }}
        >
          {t("disguise.docsGuide")}
        </div>
        <h1 className="mb-1 text-3xl font-semibold tracking-tight">{chapterTitle}</h1>
        <div className="mb-6 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm" style={{ color: s.muted }}>
          <span>{t("disguise.docsLastUpdated", { date })}</span>
          <span>·</span>
          <span>{t("disguise.minRead", { count: minutes })}</span>
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
          {t("disguise.articleCategory")}
        </div>
        <h1 className="mb-3 text-4xl font-bold leading-tight tracking-tight">{chapterTitle}</h1>
        <div className="mb-6 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm" style={{ color: s.muted }}>
          <span>{t("disguise.articleByline")}</span>
          <span>·</span>
          <span>{date}</span>
          <span>·</span>
          <span>{t("disguise.minRead", { count: minutes })}</span>
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
              {t("disguise.emailTeamSuffix", { name: workTitle })}
            </span>
            <span style={{ color: s.muted }}>{date}</span>
          </div>
          <div style={{ color: s.muted }}>
            &lt;updates@{slug(workTitle)}.com&gt;
          </div>
          <div style={{ color: s.muted }}>{t("disguise.emailToMe")}</div>
        </div>
      </div>
      <hr style={{ borderColor: s.border }} className="mb-6" />
    </div>
  );
}
