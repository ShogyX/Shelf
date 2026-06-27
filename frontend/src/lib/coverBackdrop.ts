// Album-art-style page backdrop: pull a few dominant colours from the active title's COVER and feed
// them to the global ambient aurora (`--cover-a/b/c`, consumed in index.css), so the whole page comes
// alive in the colours of whatever you're reading. Falls back to the theme accent when there's no
// cover (or extraction fails / the cover is greyscale). Pure client-side — covers are same-origin
// (served by the backend), so the canvas isn't tainted and `getImageData` works without CORS.
import { useEffect } from "react";

// A cover-derived backdrop is currently applied — so a theme change shouldn't stomp it back to accent.
let coverActive = false;
// Monotonic token so a slow extraction for an old cover can't overwrite a newer one.
let token = 0;

function rgbToHsl(r: number, g: number, b: number): [number, number, number] {
  r /= 255; g /= 255; b /= 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b);
  const l = (max + min) / 2;
  let h = 0, s = 0;
  if (max !== min) {
    const d = max - min;
    s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
    if (max === r) h = (g - b) / d + (g < b ? 6 : 0);
    else if (max === g) h = (b - r) / d + 2;
    else h = (r - g) / d + 4;
    h *= 60;
  }
  return [h, s, l];
}

function hslToCss(h: number, s: number, l: number): string {
  return `hsl(${Math.round(h)} ${Math.round(s * 100)}% ${Math.round(l * 100)}%)`;
}

// Turn an averaged cover colour into a glowy aurora colour: keep its hue, but floor the saturation
// and pull the lightness into a mid band so a dark/muted cover still casts visible colour.
function toGlow(r: number, g: number, b: number): string {
  const [h, s, l] = rgbToHsl(r, g, b);
  return hslToCss(h, Math.min(1, Math.max(s, 0.55)), Math.min(0.7, Math.max(l, 0.5)));
}

function loadImage(src: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.decoding = "async";
    img.onload = () => resolve(img);
    img.onerror = reject;
    img.src = src;
  });
}

// Extract up to 3 dominant, vivid hues from an image. Bins pixels by hue, weighting each by
// saturation × mid-lightness (so the cover's actual colours win over black borders / white gutters /
// muted greys), then returns the heaviest distinct hue clusters as glow colours. null if nothing
// colourful enough (e.g. a black-and-white scan) — caller then keeps the accent.
export async function extractCoverColors(src: string): Promise<string[] | null> {
  try {
    const img = await loadImage(src);
    const S = 40;
    const cv = document.createElement("canvas");
    cv.width = S; cv.height = S;
    const ctx = cv.getContext("2d", { willReadFrequently: true });
    if (!ctx) return null;
    ctx.drawImage(img, 0, 0, S, S);
    const { data } = ctx.getImageData(0, 0, S, S);
    type Bin = { r: number; g: number; b: number; w: number };
    const bins = new Map<number, Bin>();
    for (let i = 0; i < data.length; i += 4) {
      const r = data[i], g = data[i + 1], b = data[i + 2], a = data[i + 3];
      if (a < 128) continue;
      const [h, s, l] = rgbToHsl(r, g, b);
      if (l < 0.12 || l > 0.92 || s < 0.18) continue; // skip near-black/white and greys
      const key = Math.round(h / 24) % 15; // 15 coarse hue bins
      const w = s * (1 - Math.abs(l - 0.5) * 1.4); // vivid + mid-light = heaviest
      const bin = bins.get(key) ?? { r: 0, g: 0, b: 0, w: 0 };
      bin.r += r * w; bin.g += g * w; bin.b += b * w; bin.w += w;
      bins.set(key, bin);
    }
    const sorted = [...bins.values()].filter((b) => b.w > 0).sort((a, b) => b.w - a.w);
    if (sorted.length === 0) return null;
    const cols = sorted.slice(0, 3).map((b) => toGlow(b.r / b.w, b.g / b.w, b.b / b.w));
    while (cols.length < 3) cols.push(cols[cols.length - 1]); // pad to 3 when the cover is near-monochrome
    return cols;
  } catch {
    return null;
  }
}

function accentTriple(): [string, string, string] {
  const a = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#7c5cff";
  return [a, a, a];
}

function setVars([a, b, c]: string[]) {
  const s = document.documentElement.style;
  s.setProperty("--cover-a", a);
  s.setProperty("--cover-b", b);
  s.setProperty("--cover-c", c);
}

// Apply the accent triple ONLY if no cover backdrop is active (called on theme change + first paint).
export function applyAccentBackdrop(): void {
  if (coverActive) return;
  setVars(accentTriple());
}

// Drive the backdrop from a cover URL (or revert to accent when there's none).
async function applyFromCover(src: string | null | undefined): Promise<void> {
  const mine = ++token;
  if (!src) {
    coverActive = false;
    setVars(accentTriple());
    return;
  }
  const cols = await extractCoverColors(src);
  if (mine !== token) return; // a newer cover superseded this one
  if (cols) {
    coverActive = true;
    setVars(cols);
  } else {
    coverActive = false;
    setVars(accentTriple());
  }
}

// ---- Ambient motion -----------------------------------------------------------------------------
// One rAF loop drives the aurora's background-position (consumed by .ambient-layer in index.css) from
// a slow time drift PLUS the scroll offset — so the colour gently breathes at rest AND shifts around as
// you scroll, for an immersive feel. px offsets on the tiled background wrap seamlessly. Honours
// prefers-reduced-motion (loop never starts; vars default to 0 → static).
let motionInit = false;
export function initAmbientMotion(): void {
  if (motionInit || typeof window === "undefined") return;
  motionInit = true;
  if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return;
  const s = document.documentElement.style;
  const tick = (t: number) => {
    const y = window.scrollY || document.documentElement.scrollTop || 0;
    // Layer 1 drifts down slowly + light scroll parallax; layer 2 opposite + stronger, so the two
    // hue layers separate as you scroll (the colours visibly travel and recombine).
    s.setProperty("--aby1", `${(t * 0.010 + y * 0.05).toFixed(1)}px`);
    s.setProperty("--aby2", `${(-t * 0.008 - y * 0.09).toFixed(1)}px`);
    s.setProperty("--abx1", `${(Math.sin(t * 0.00007) * 3.5).toFixed(2)}%`);
    s.setProperty("--abx2", `${(Math.cos(t * 0.00006) * 4 + y * 0.012).toFixed(2)}%`);
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

// Hook for hero components: set the page backdrop from the featured cover whenever it changes, and
// revert to the theme accent when the hero leaves (so a non-hero page isn't left wearing a stale
// cover tint). The revert is race-safe (applyFromCover bumps the token) and, navigating between two
// hero pages, just retargets the in-flight 1s bloom — no hard flash.
export function useCoverBackdrop(coverSrc: string | null | undefined): void {
  useEffect(() => {
    applyFromCover(coverSrc);
    return () => {
      applyFromCover(null);
    };
  }, [coverSrc]);
}
