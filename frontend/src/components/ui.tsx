import React, { useEffect, useLayoutEffect, useRef, useState } from "react";

const FOCUSABLE =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

// A stack of the currently-open dialogs (innermost last). Only the TOP dialog reacts to Escape/Tab,
// so a dialog opened over another (e.g. the Acquire prompt over the catalog detail) doesn't fight or
// close the one beneath it (FE-H1/H2).
const _dialogStack: symbol[] = [];
let _bodyOverflowPrev = "";

/** Focus management shared by every dialog: move focus in on open, trap Tab inside, restore focus to
 *  the opener on close, close on Escape, and lock body scroll while open. Stacked dialogs cooperate
 *  via a shared stack (only the topmost handles keys). The ONE dialog primitive — don't hand-roll. */
export function useDialogFocus(onClose: () => void) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const meId = Symbol("dialog");
    _dialogStack.push(meId);
    if (_dialogStack.length === 1) {  // FE-M5: lock page scroll (save the prior value to restore exactly)
      _bodyOverflowPrev = document.body.style.overflow;
      document.body.style.overflow = "hidden";
    }
    const isTop = () => _dialogStack[_dialogStack.length - 1] === meId;

    const opener = document.activeElement as HTMLElement | null;
    const el = ref.current;
    // Move focus to the first focusable control (or the dialog itself).
    const first = el?.querySelector<HTMLElement>(FOCUSABLE);
    (first ?? el)?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (!isTop()) return; // only the topmost dialog responds (stacked dialogs don't double-handle)
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
        return;
      }
      if (e.key !== "Tab" || !el) return;
      const items = Array.from(el.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
        (n) => n.offsetParent !== null || n === document.activeElement,
      );
      if (items.length === 0) {
        e.preventDefault();
        el.focus();
        return;
      }
      const firstEl = items[0];
      const lastEl = items[items.length - 1];
      const active = document.activeElement as HTMLElement | null;
      if (e.shiftKey && (active === firstEl || !el.contains(active))) {
        e.preventDefault();
        lastEl.focus();
      } else if (!e.shiftKey && (active === lastEl || !el.contains(active))) {
        e.preventDefault();
        firstEl.focus();
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => {
      window.removeEventListener("keydown", onKey, true);
      const i = _dialogStack.indexOf(meId);
      if (i >= 0) _dialogStack.splice(i, 1);
      if (_dialogStack.length === 0) document.body.style.overflow = _bodyOverflowPrev; // restore on last close
      opener?.focus?.(); // hand focus back to whatever opened the dialog
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return ref;
}

/** A modal dialog with a dimmed backdrop. Closes on backdrop click or Escape; traps + restores focus
 *  and is labelled by its title for screen readers (the ONE dialog primitive — don't hand-roll chrome).
 *  - "center": centered card.  - "sheet": full-height right-side panel for big content.
 *  - "fullscreen-sheet": full-screen on mobile, centered capped card on ≥sm, with a pinned header +
 *    scrollable body + pinned footer (for the catalog/series/stock detail dialogs). Pass `width` as a
 *    `max-w-*` cap for this variant (it falls back to max-w-xl if a plain w-* is given). */
export function Modal({
  title,
  onClose,
  children,
  footer,
  width = "w-[26rem]",
  variant = "center",
}: {
  title: React.ReactNode;
  onClose: () => void;
  children: React.ReactNode;
  footer?: React.ReactNode;
  width?: string;
  variant?: "center" | "sheet" | "fullscreen-sheet";
}) {
  const ref = useDialogFocus(onClose);
  const titleId = React.useId();

  if (variant === "fullscreen-sheet") {
    // This variant is full-width on mobile and a capped card on ≥sm, so `width` is a max-w-* cap.
    // Fall back to a sane cap if a caller passed (or defaulted to) a plain w-* (which `w-full` would
    // otherwise override, leaving the card uncapped). Belt to the docstring contract.
    const cap = width.includes("max-w") ? width : "max-w-xl";
    return (
      <div className="fixed inset-0 z-50 flex justify-center overflow-y-auto bg-black/50 p-0 sm:p-6"
        onClick={onClose}>
        <div ref={ref} role="dialog" aria-modal="true" aria-labelledby={titleId} tabIndex={-1}
          className={`relative flex h-full w-full ${cap} flex-col bg-surface sm:h-auto sm:max-h-[88vh] sm:rounded-2xl sm:shadow-2xl`}
          onClick={(e) => e.stopPropagation()}>
          <div className="flex items-start justify-between gap-2 border-b border-border px-5 py-3.5">
            <h3 id={titleId} className="min-w-0 truncate text-base font-semibold">{title}</h3>
            <button onClick={onClose} aria-label="Close" className="shrink-0 text-muted hover:text-text">✕</button>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">{children}</div>
          {footer && <div className="border-t border-border px-5 py-3">{footer}</div>}
        </div>
      </div>
    );
  }

  const shape =
    variant === "sheet"
      ? `fixed right-0 top-0 z-50 h-full ${width} max-w-[calc(100vw-1.5rem)] overflow-y-auto border-l border-border bg-surface p-5 shadow-2xl`
      : `fixed left-1/2 top-1/2 z-50 ${width} max-w-[calc(100vw-1.5rem)] -translate-x-1/2 -translate-y-1/2 rounded-2xl border border-border bg-surface p-5 shadow-2xl`;
  return (
    <>
      <div className="fixed inset-0 z-40 bg-black/40" onClick={onClose} />
      <div ref={ref} role="dialog" aria-modal="true" aria-labelledby={titleId} tabIndex={-1} className={shape}>
        <div className="mb-3 flex items-center justify-between gap-3">
          <h3 id={titleId} className="font-semibold">{title}</h3>
          <button onClick={onClose} aria-label="Close" className="text-muted hover:text-text">✕</button>
        </div>
        {children}
        {footer && <div className="mt-4 flex justify-end gap-2">{footer}</div>}
      </div>
    </>
  );
}

export function Card({ className = "", children }: { className?: string; children: React.ReactNode }) {
  return (
    <div
      className={`rounded-xl border border-border bg-surface shadow-[0_1px_2px_rgba(16,18,27,0.04),0_8px_24px_-14px_rgba(16,18,27,0.14)] dark:shadow-[0_4px_24px_-8px_rgba(0,0,0,0.55)] ${className}`}
    >
      {children}
    </div>
  );
}

type BtnProps = React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "ghost" | "outline" | "danger";
  size?: "sm" | "md" | "icon";
};
export function Button({ variant = "outline", size = "md", className = "", ...rest }: BtnProps) {
  const base =
    "inline-flex items-center justify-center gap-2 rounded-lg font-medium transition duration-150 [transition-timing-function:var(--ease)] active:scale-[0.97] disabled:opacity-50 disabled:cursor-not-allowed disabled:active:scale-100";
  // "icon": a square ≥36px tap target for single-glyph buttons (UI-L2; was sub-44px size="sm").
  const sizes = { sm: "px-2.5 py-1 text-sm", md: "px-3.5 py-2 text-sm", icon: "h-9 w-9 p-0 text-sm" }[size];
  const variants = {
    // Subtle vertical gradient + accent glow so the primary action reads "premium," not a flat fill.
    primary:
      "bg-gradient-to-b from-[color-mix(in_srgb,var(--accent)_92%,white)] to-[var(--accent)] text-accent-fg shadow-[0_2px_10px_-2px_color-mix(in_srgb,var(--accent)_55%,transparent)] hover:brightness-110",
    ghost: "text-text hover:bg-surface-2",
    outline: "border border-border text-text hover:bg-surface-2",
    danger: "border border-red-400/40 text-red-500 hover:bg-red-500/10",
  }[variant];
  return <button className={`${base} ${sizes} ${variants} ${className}`} {...rest} />;
}

export function Toggle({
  checked,
  onChange,
  label,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label?: string;
}) {
  return (
    <button
      type="button"
      onClick={() => onChange(!checked)}
      className="inline-flex items-center gap-2"
      aria-pressed={checked}
    >
      <span
        className={`relative h-5 w-9 rounded-full transition ${
          checked ? "bg-accent" : "bg-surface-2 border border-border"
        }`}
      >
        <span
          className={`absolute top-0.5 h-4 w-4 rounded-full bg-white shadow transition-all ${
            checked ? "left-[18px]" : "left-0.5"
          }`}
        />
      </span>
      {label && <span className="text-sm text-text">{label}</span>}
    </button>
  );
}

export function Slider({
  value,
  min,
  max,
  step = 1,
  onChange,
  label,
  suffix,
}: {
  value: number;
  min: number;
  max: number;
  step?: number;
  onChange: (v: number) => void;
  label: string;
  suffix?: string;
}) {
  return (
    <label className="block">
      <div className="mb-1 flex justify-between text-xs text-muted">
        <span>{label}</span>
        <span>
          {value}
          {suffix}
        </span>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(parseFloat(e.target.value))}
        className="w-full accent-[var(--accent)]"
      />
    </label>
  );
}

export function Select({
  value,
  onChange,
  options,
  label,
}: {
  value: string;
  onChange: (v: string) => void;
  options: { value: string; label: string }[];
  label?: string;
}) {
  return (
    <label className="block">
      {label && <div className="mb-1 text-xs text-muted">{label}</div>}
      {/* appearance-none + our own chevron so the control matches Button/Input chrome instead of
          rendering raw OS dropdown chrome (which read as a "default form" tell). */}
      <div className="relative">
        <select
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className="w-full appearance-none rounded-lg border border-border bg-surface px-2.5 py-2 pr-8 text-sm text-text transition focus:border-accent focus:outline-none"
        >
          {options.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
        <span className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 text-xs text-muted">▾</span>
      </div>
    </label>
  );
}

export function Badge({
  children,
  tone = "default",
}: {
  children: React.ReactNode;
  tone?: "default" | "green" | "amber" | "violet" | "red";
}) {
  const tones = {
    default: "bg-surface-2 text-muted",
    green: "bg-green-500/15 text-green-600 dark:text-green-400",
    amber: "bg-amber-500/15 text-amber-600 dark:text-amber-400",
    violet: "bg-violet-500/15 text-violet-600 dark:text-violet-300",
    red: "bg-red-500/15 text-red-600 dark:text-red-400",
  }[tone];
  return (
    <span className={`inline-flex shrink-0 items-center whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-medium ${tones}`}>
      {children}
    </span>
  );
}

export function Tabs({
  tabs,
  active,
  onChange,
  className = "",
}: {
  tabs: { id: string; label: string }[];
  active: string;
  onChange: (id: string) => void;
  className?: string;
}) {
  return (
    <div
      role="tablist"
      // Wrap onto multiple rows when the full tab set doesn't fit the container width. The previous
      // single-row scroll hid its scrollbar, so on a narrow viewport the rightmost tabs (e.g. the
      // Settings "Storage"/paths tab, 9th of 11) scrolled off-screen with no way to reach them.
      // Wrapping keeps every tab visible and clickable at any width.
      className={`flex flex-wrap gap-1 border-b border-border ${className}`}
    >
      {tabs.map((t) => {
        const on = t.id === active;
        return (
          <button
            key={t.id}
            role="tab"
            type="button"
            aria-selected={on}
            onClick={() => onChange(t.id)}
            className={`-mb-px shrink-0 whitespace-nowrap border-b-2 px-3.5 py-2 text-sm font-medium transition ${
              on
                ? "border-accent text-text"
                : "border-transparent text-muted hover:text-text"
            }`}
          >
            {t.label}
          </button>
        );
      })}
    </div>
  );
}

/** Keep a trigger-anchored popover of `widthPx` fully on-screen on any viewport (the fix for popovers
 *  running off a phone's edge — side-anchoring alone can't help when the popover is wider than the
 *  space on either side of a mid-screen trigger). Returns a ref for the TRIGGER and a `style`: a FIXED
 *  viewport position (`{position,left,top,maxWidth}`) computed from the trigger rect and CLAMPED into
 *  [8px, vw-8px]; `undefined` until measured. `prefer` picks the natural horizontal side before
 *  clamping. Apply `style={style}` to the popover and keep a fallback `absolute left-0/right-0`
 *  className for the pre-measure frame (the fixed style overrides it once set). */
export function useEdgeFlip<T extends HTMLElement>(
  open: boolean, widthPx: number, prefer: "left" | "right" = "left",
) {
  const ref = useRef<T>(null);
  // A FIXED viewport position computed from the trigger rect — unambiguous (no ancestor-relative
  // offset math) and clamped so the popover never crosses an edge on any phone. `undefined` until
  // measured (the element's fallback className anchors it meanwhile).
  const [style, setStyle] = useState<React.CSSProperties | undefined>(undefined);
  useLayoutEffect(() => {
    if (!open) { setStyle(undefined); return; }
    const r = ref.current?.getBoundingClientRect();
    if (!r) return;
    // clientWidth (the layout viewport), NOT window.innerWidth — innerWidth inflates while the
    // popover's pre-positioned fallback render briefly overflows the page horizontally.
    const M = 8, vw = document.documentElement.clientWidth;
    const w = Math.min(widthPx, vw - 2 * M);                       // cap to viewport on tiny screens
    const natural = prefer === "left" ? r.left : r.right - w;      // viewport-left at the preferred side
    const left = Math.min(Math.max(natural, M), Math.max(M, vw - w - M));
    setStyle({ position: "fixed", left: Math.round(left), top: Math.round(r.bottom + 6), maxWidth: w });
  }, [open, widthPx, prefer]);
  return { ref, style };
}

/** A compact '?' help affordance: hover or click/focus to reveal help text in a popover, so dense
 *  setting descriptions can move out of the always-on layout. Keyboard- + screen-reader-accessible. */
export function InfoHint({ text, className = "", align = "left" }:
  { text: React.ReactNode; className?: string; align?: "left" | "right" }) {
  const [open, setOpen] = useState(false);
  const { ref, style } = useEdgeFlip<HTMLButtonElement>(open, 256, align); // 256 = w-64
  return (
    <span className={`relative inline-flex align-middle ${className}`}>
      <button
        ref={ref}
        type="button"
        aria-label="More information"
        aria-expanded={open}
        className="inline-flex h-[15px] w-[15px] items-center justify-center rounded-full border border-border text-[10px] font-semibold leading-none text-muted transition hover:border-text hover:text-text"
        onClick={() => setOpen((v) => !v)}
        onMouseEnter={() => setOpen(true)}
        onMouseLeave={() => setOpen(false)}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
      >?</button>
      {open && (
        <span
          role="tooltip"
          style={style}
          className={`absolute top-5 z-50 w-64 max-w-[calc(100vw-1rem)] rounded-lg border border-border bg-surface p-2 text-left text-xs font-normal leading-snug text-muted shadow-lg ${
            align === "right" ? "right-0" : "left-0"
          }`}
        >
          {text}
        </span>
      )}
    </span>
  );
}

/** A '⋯' overflow menu: one icon button that opens a popover of secondary actions, so a card can
 *  carry a single primary action plus a tidy "More" menu instead of a wall of competing buttons.
 *  Mirrors the App.tsx UserButton popover (fixed scrim + absolute menu); closes on click, Escape, or
 *  outside-click. Items are role="menuitem" buttons; falsy items are filtered so callers can do
 *  `items={[cond && {…}].filter(Boolean)}`. */
export function OverflowMenu({
  items,
  label,
  align = "right",
}: {
  // Items may be falsy (incl. "" / 0 from `cond && {…}` where cond is a string|number) — filtered out.
  items: Array<
    | { label: React.ReactNode; onClick: () => void; danger?: boolean; disabled?: boolean }
    | false
    | null
    | undefined
    | ""
    | 0
  >;
  label?: string;
  align?: "left" | "right";
}) {
  const [open, setOpen] = useState(false);
  const [dropUp, setDropUp] = useState(false); // flip above the trigger when it sits low in the viewport
  const [alignRight, setAlignRight] = useState(align === "right"); // flip side when a viewport edge would clip it
  const rootRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const real = items.filter(Boolean) as Array<{
    label: React.ReactNode;
    onClick: () => void;
    danger?: boolean;
    disabled?: boolean;
  }>;
  // Return focus to the ⋯ trigger (the root's direct-child button) when the menu closes.
  const focusTrigger = () => rootRef.current?.querySelector<HTMLButtonElement>(":scope > button")?.focus();
  const close = (restore = true) => {
    setOpen(false);
    if (restore) focusTrigger();
  };
  // Open → move focus into the menu (first enabled item) so it's keyboard-operable, per the
  // role="menu" contract. Escape closes + restores focus to the trigger.
  useEffect(() => {
    if (!open) return;
    menuRef.current?.querySelector<HTMLButtonElement>("button:not([disabled])")?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        close();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);
  if (real.length === 0) return null;
  // Roving focus: Arrow/Home/End move between enabled items; Tab closes (focus leaves naturally).
  const onMenuKey = (e: React.KeyboardEvent<HTMLDivElement>) => {
    const btns = Array.from(menuRef.current?.querySelectorAll<HTMLButtonElement>("button:not([disabled])") ?? []);
    if (btns.length === 0) return;
    const i = btns.indexOf(document.activeElement as HTMLButtonElement);
    if (e.key === "ArrowDown") { e.preventDefault(); btns[(i + 1) % btns.length].focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); btns[(i - 1 + btns.length) % btns.length].focus(); }
    else if (e.key === "Home") { e.preventDefault(); btns[0].focus(); }
    else if (e.key === "End") { e.preventDefault(); btns[btns.length - 1].focus(); }
    else if (e.key === "Tab") { close(false); }
  };
  return (
    <div ref={rootRef} className="relative">
      <Button
        size="icon"
        variant="ghost"
        aria-label={label ?? "More actions"}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => {
          if (!open) {
            const r = rootRef.current?.querySelector(":scope > button")?.getBoundingClientRect();
            if (r) {
              setDropUp(r.bottom > window.innerHeight * 0.6); // low trigger → open upward
              // Horizontal edge-flip: the menu is w-56 (224px). Open from whichever side keeps it
              // on-screen (a right-aligned menu on a left-edge trigger ran off the left on mobile).
              const W = 224;
              let right = align === "right";
              if (right && r.right - W < 8) right = false;
              else if (!right && r.left + W > window.innerWidth - 8) right = true;
              setAlignRight(right);
            }
          }
          setOpen((v) => !v);
        }}
      >
        ⋯
      </Button>
      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => close(false)} />
          <div
            ref={menuRef}
            role="menu"
            onKeyDown={onMenuKey}
            className={`absolute ${alignRight ? "right-0" : "left-0"} ${
              dropUp ? "bottom-full mb-2" : "top-full mt-2"
            } z-50 w-56 rounded-xl border border-border bg-surface p-1.5 shadow-2xl`}
          >
            {real.map((it, i) => (
              <button
                key={i}
                type="button"
                role="menuitem"
                disabled={it.disabled}
                className={`w-full rounded-lg px-2 py-1.5 text-left text-sm hover:bg-surface-2 disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:bg-transparent ${
                  it.danger ? "text-red-500" : "text-text"
                }`}
                onClick={() => {
                  close(false);
                  it.onClick();
                }}
              >
                {it.label}
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

export function Spinner({ label }: { label?: string }) {
  return (
    <div className="flex items-center gap-3 text-muted">
      <span className="h-4 w-4 animate-spin rounded-full border-2 border-border border-t-accent" />
      {label && <span className="text-sm">{label}</span>}
    </div>
  );
}

/** A pulsing placeholder block. Compose these into layout-shaped skeletons so loads show the page's
 *  silhouette instead of a lonely spinner (perceived-speed win). */
export function Skeleton({ className = "" }: { className?: string }) {
  return <div aria-hidden className={`animate-pulse rounded-md bg-surface-2 ${className}`} />;
}

/** Poster-shaped skeleton grid matching the library/catalog cover wall, so a grid load reserves its
 *  real silhouette. Mirrors the live grid's responsive columns. */
export function PosterGridSkeleton({ count = 10 }: { count?: number }) {
  return (
    <div
      role="status"
      aria-busy
      aria-label="Loading…"
      className="grid grid-cols-2 gap-4 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6"
    >
      {Array.from({ length: count }).map((_, i) => (
        <div key={i} className="space-y-2">
          <Skeleton className="aspect-[2/3] w-full rounded-xl" />
          <Skeleton className="h-3.5 w-4/5" />
          <Skeleton className="h-3 w-1/2" />
        </div>
      ))}
    </div>
  );
}

export function EmptyState({
  title,
  hint,
  action,
  icon,
}: {
  title: string;
  hint?: string;
  action?: React.ReactNode;
  icon?: React.ReactNode; // decorative glyph in the accent disc (defaults to a sparkle)
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 rounded-2xl border border-border/60 bg-surface/40 py-14 text-center">
      <div className="flex h-12 w-12 items-center justify-center rounded-full bg-accent/10 text-xl text-accent">
        {icon ?? "✦"}
      </div>
      <p className="font-semibold text-text">{title}</p>
      {hint && <p className="max-w-sm text-sm text-muted">{hint}</p>}
      {action}
    </div>
  );
}

/** The standard page header: an optional uppercase accent eyebrow, a large bold title, optional
 *  description, and optional right-aligned actions. Gives every top-level page presence and a
 *  consistent rhythm instead of a flat inline <h1>. */
export function PageHeader({ eyebrow, title, desc, actions }: {
  eyebrow?: React.ReactNode;
  title: React.ReactNode;
  desc?: React.ReactNode;
  actions?: React.ReactNode;
}) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-3">
      <div className="min-w-0">
        {eyebrow && (
          <div className="mb-1 text-xs font-semibold uppercase tracking-widest text-accent">{eyebrow}</div>
        )}
        <h1 className="text-2xl font-bold tracking-tight text-text sm:text-3xl">{title}</h1>
        {desc && <p className="mt-1.5 max-w-2xl text-sm text-muted">{desc}</p>}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </div>
  );
}

/** Shared input styling so hand-rolled <input>/<select> match Select/Button chrome. Settings,
 *  Users and the catalog modals all duplicated this string — import it instead. */
export const inputCls =
  "w-full rounded-lg border border-border bg-bg px-3 py-2 text-sm text-text outline-none transition focus:border-accent";

/** A consistent card header: title (+ optional help popover, status badge) and a muted description.
 *  Replaces the two divergent hand-rolled patterns (the `<h2 font-semibold>` one and the
 *  `<div font-medium> + <p text-muted>` one) so every settings/admin card reads the same. */
export function CardHeader({ title, hint, badge, desc }: {
  title: React.ReactNode;
  hint?: React.ReactNode;       // InfoHint text (renders the ? popover)
  badge?: React.ReactNode;      // e.g. a "configured"/"connected" status Badge
  desc?: React.ReactNode;       // one-line description under the title
}) {
  return (
    <div className={desc ? "mb-3" : "mb-2"}>
      <div className="flex items-center gap-2">
        <h2 className="flex items-center gap-1.5 font-semibold text-text">
          {title}
          {hint != null && <InfoHint text={hint} />}
        </h2>
        {badge}
      </div>
      {desc && <p className="mt-1 text-sm text-muted">{desc}</p>}
    </div>
  );
}

/** A label/description on the left, a control on the right; stacks on mobile. The default row
 *  shape for settings so spacing stops being re-invented per card. */
export function SettingRow({ label, hint, htmlFor, children }: {
  label: React.ReactNode;
  hint?: React.ReactNode;
  htmlFor?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-2 border-b border-border/60 py-3 last:border-0 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
      <label htmlFor={htmlFor} className="min-w-0 sm:max-w-[62%]">
        <div className="text-sm font-medium text-text">{label}</div>
        {hint && <div className="mt-0.5 text-xs leading-snug text-muted">{hint}</div>}
      </label>
      <div className="shrink-0">{children}</div>
    </div>
  );
}

/** A collapsible section: a header bar that reveals its children on click. Use to keep advanced or
 *  rarely-touched settings compressed until the user wants them (less always-on bloat). */
export function Disclosure({ title, subtitle, defaultOpen = false, children }: {
  title: React.ReactNode;
  subtitle?: React.ReactNode;
  defaultOpen?: boolean;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="mb-4">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center justify-between gap-2 rounded-xl border border-border bg-surface px-4 py-3 text-left transition hover:bg-surface-2"
      >
        <div className="min-w-0">
          <div className="text-sm font-medium text-text">{title}</div>
          {subtitle && <div className="truncate text-xs text-muted">{subtitle}</div>}
        </div>
        <span className="shrink-0 text-xs text-muted">{open ? "Hide ▲" : "Show ▼"}</span>
      </button>
      {open && <div className="mt-3">{children}</div>}
    </div>
  );
}

/** A small uppercase divider for grouping controls inside a card. */
export function SectionHeader({ children, hint }: { children: React.ReactNode; hint?: React.ReactNode }) {
  return (
    <div className="mb-2 mt-4 flex items-center gap-1.5 border-t border-border/60 pt-3 text-xs font-semibold uppercase tracking-wide text-muted first:mt-0 first:border-0 first:pt-0">
      {children}
      {hint}
    </div>
  );
}
