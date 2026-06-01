// Curated, low-eye-strain reading themes. Each maps to the CSS variable tokens
// consumed across the app (chrome + reader). None use pure #000/#fff — every
// palette is gently toned for comfortable long-form reading.

export interface ThemeTokens {
  bg: string;
  surface: string;
  surface2: string;
  border: string;
  text: string;
  muted: string;
  accent: string;
  accentFg: string;
}

export interface Theme {
  key: string;
  name: string;
  group: "light" | "dark";
  tokens: ThemeTokens;
}

export const THEMES: Theme[] = [
  // ---- Light / warm ----
  {
    key: "light", name: "Daylight", group: "light",
    tokens: { bg: "#f7f7f5", surface: "#ffffff", surface2: "#eeeeec", border: "#e3e3de",
      text: "#23262b", muted: "#6b7280", accent: "#6d5cff", accentFg: "#ffffff" },
  },
  {
    key: "paper", name: "Paper", group: "light",
    tokens: { bg: "#f3ecdb", surface: "#fbf5e7", surface2: "#ece0c6", border: "#dccba6",
      text: "#463b27", muted: "#897a59", accent: "#b07d3b", accentFg: "#fff8ec" },
  },
  {
    key: "sepia", name: "Sepia", group: "light",
    tokens: { bg: "#efe6d3", surface: "#f6efe0", surface2: "#e7dcc3", border: "#d8c9a8",
      text: "#43381f", muted: "#7c6f52", accent: "#a8761f", accentFg: "#fdf8ee" },
  },
  {
    key: "solarized-light", name: "Solarized Light", group: "light",
    tokens: { bg: "#fdf6e3", surface: "#fbf1d3", surface2: "#eee8d5", border: "#e0d9bf",
      text: "#586e75", muted: "#93a1a1", accent: "#268bd2", accentFg: "#fdf6e3" },
  },
  {
    key: "mist", name: "Mist", group: "light",
    tokens: { bg: "#e9ecef", surface: "#f4f6f8", surface2: "#dfe3e8", border: "#cfd5dc",
      text: "#2c333c", muted: "#6c757e", accent: "#3f7d8c", accentFg: "#ffffff" },
  },
  {
    key: "sage", name: "Sage", group: "light",
    tokens: { bg: "#e7efe7", surface: "#f1f7f1", surface2: "#d7e6d8", border: "#c3d8c5",
      text: "#27352a", muted: "#5f7a64", accent: "#2f8f5f", accentFg: "#ffffff" },
  },
  // ---- Dark / muted ----
  {
    key: "dark", name: "Charcoal", group: "dark",
    tokens: { bg: "#15171c", surface: "#1d2026", surface2: "#262a32", border: "#2d323c",
      text: "#d9dde4", muted: "#97a0ad", accent: "#9b87ff", accentFg: "#15171c" },
  },
  {
    key: "midnight", name: "Midnight", group: "dark",
    tokens: { bg: "#0f1420", surface: "#161d2e", surface2: "#1f2940", border: "#283450",
      text: "#c7d2e5", muted: "#7e8ba6", accent: "#5a8dee", accentFg: "#0f1420" },
  },
  {
    key: "nord", name: "Nord", group: "dark",
    tokens: { bg: "#2e3440", surface: "#353c4a", surface2: "#3b4252", border: "#434c5e",
      text: "#d8dee9", muted: "#8a93a5", accent: "#88c0d0", accentFg: "#2e3440" },
  },
  {
    key: "gruvbox", name: "Gruvbox", group: "dark",
    tokens: { bg: "#282828", surface: "#32302f", surface2: "#3c3836", border: "#504945",
      text: "#ebdbb2", muted: "#a89984", accent: "#fabd2f", accentFg: "#282828" },
  },
  {
    key: "solarized-dark", name: "Solarized Dark", group: "dark",
    tokens: { bg: "#002b36", surface: "#073642", surface2: "#0a4451", border: "#13515e",
      text: "#93a1a1", muted: "#6a8389", accent: "#2aa198", accentFg: "#002b36" },
  },
  {
    key: "slate", name: "Slate", group: "dark",
    tokens: { bg: "#1a1d24", surface: "#22262f", surface2: "#2b303b", border: "#353b48",
      text: "#cbd2dd", muted: "#8b94a3", accent: "#7aa2f7", accentFg: "#1a1d24" },
  },
  {
    key: "forest", name: "Forest", group: "dark",
    tokens: { bg: "#14201a", surface: "#1b2a22", surface2: "#24372c", border: "#2f4738",
      text: "#cfe3d4", muted: "#80a78c", accent: "#6fcf97", accentFg: "#14201a" },
  },
  {
    key: "eink", name: "E-ink", group: "dark",
    tokens: { bg: "#1c1c1c", surface: "#242424", surface2: "#2c2c2c", border: "#3a3a3a",
      text: "#bcbcb5", muted: "#7d7d77", accent: "#a7a79b", accentFg: "#1c1c1c" },
  },
];

export const THEME_MAP: Record<string, Theme> = Object.fromEntries(
  THEMES.map((t) => [t.key, t])
);

export function resolveThemeKey(theme: string): string {
  if (theme === "system") {
    const dark = window.matchMedia?.("(prefers-color-scheme: dark)").matches;
    return dark ? "dark" : "light";
  }
  return THEME_MAP[theme] ? theme : "light";
}

export function tokensFor(theme: string): ThemeTokens {
  return (THEME_MAP[resolveThemeKey(theme)] ?? THEMES[0]).tokens;
}

export function hexToHsl(hex: string): { h: number; s: number; l: number } {
  let c = hex.replace("#", "");
  if (c.length === 3) c = c.split("").map((x) => x + x).join("");
  const r = parseInt(c.slice(0, 2), 16) / 255;
  const g = parseInt(c.slice(2, 4), 16) / 255;
  const b = parseInt(c.slice(4, 6), 16) / 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b);
  let h = 0, s = 0;
  const l = (max + min) / 2;
  const d = max - min;
  if (d !== 0) {
    s = d / (1 - Math.abs(2 * l - 1));
    switch (max) {
      case r: h = ((g - b) / d) % 6; break;
      case g: h = (b - r) / d + 2; break;
      default: h = (r - g) / d + 4;
    }
    h *= 60;
    if (h < 0) h += 360;
  }
  return { h: Math.round(h), s: Math.round(s * 100), l: Math.round(l * 100) };
}

// Keep a theme color's hue + saturation but force a chosen lightness (0..100).
export function colorWithLightness(hex: string, lightness: number): string {
  const { h, s } = hexToHsl(hex);
  return `hsl(${h} ${s}% ${Math.max(0, Math.min(100, lightness))}%)`;
}

export function applyTheme(theme: string): void {
  const t = THEME_MAP[resolveThemeKey(theme)];
  if (!t) return;
  const root = document.documentElement;
  root.dataset.theme = t.group; // drives Tailwind dark: variant
  const set = (k: string, v: string) => root.style.setProperty(k, v);
  set("--bg", t.tokens.bg);
  set("--surface", t.tokens.surface);
  set("--surface-2", t.tokens.surface2);
  set("--border", t.tokens.border);
  set("--text", t.tokens.text);
  set("--muted", t.tokens.muted);
  set("--accent", t.tokens.accent);
  set("--accent-fg", t.tokens.accentFg);
}
