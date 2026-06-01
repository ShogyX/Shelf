import { create } from "zustand";
import { api, ReaderPrefs } from "./api/client";
import { applyTheme } from "./themes";

export const DEFAULT_PREFS: ReaderPrefs = {
  fontFamily: "serif",
  fontSize: 19,
  lineHeight: 1.7,
  letterSpacing: 0,
  paragraphSpacing: 1.0,
  measure: 38,
  justify: false,
  mode: "scroll",
  textColor: "",
  bgColor: "",
  textLightness: null,
  bgLightness: null,
  fabX: null,
  fabY: null,
  fabSide: "right",
  fabPos: 0.5,
  fabHidden: false,
  textPosition: 50,
  workMode: "off",
};

interface AppState {
  theme: string; // "system" or a THEMES key
  prefs: ReaderPrefs;
  loaded: boolean;
  load: () => Promise<void>;
  setTheme: (t: string) => void;
  setPrefs: (p: Partial<ReaderPrefs>) => void;
}

let saveTimer: ReturnType<typeof setTimeout> | undefined;
function persist(state: AppState) {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    api.saveSettings({ theme: state.theme, reader_prefs: state.prefs }).catch(() => {});
  }, 400);
}

export { applyTheme };

// Reader font choices (system stacks — no webfont downloads). Label + CSS stack.
export const FONTS: { key: string; label: string; stack: string }[] = [
  { key: "serif", label: "Serif", stack: "Georgia, 'Times New Roman', serif" },
  { key: "oldstyle", label: "Old Style", stack: "'Iowan Old Style', Palatino, 'Book Antiqua', serif" },
  { key: "sans", label: "Sans", stack: "ui-sans-serif, system-ui, 'Segoe UI', Roboto, sans-serif" },
  { key: "humanist", label: "Humanist", stack: "Optima, Candara, 'Segoe UI', sans-serif" },
  { key: "mono", label: "Mono", stack: "ui-monospace, 'Cascadia Code', Menlo, monospace" },
  { key: "dyslexic", label: "Readable", stack: "'Comic Sans MS', 'Trebuchet MS', Verdana, sans-serif" },
];
export const FONT_STACKS: Record<string, string> = Object.fromEntries(
  FONTS.map((f) => [f.key, f.stack])
);

export const WIDTH_PRESETS = [
  { key: "narrow", label: "Narrow", measure: 30 },
  { key: "cozy", label: "Cozy", measure: 38 },
  { key: "wide", label: "Wide", measure: 46 },
  { key: "full", label: "Full", measure: 54 },
];

export const useApp = create<AppState>((set, get) => ({
  theme: "system",
  prefs: { ...DEFAULT_PREFS },
  loaded: false,
  load: async () => {
    try {
      const s = await api.getSettings();
      set({
        theme: s.theme || "system",
        prefs: { ...DEFAULT_PREFS, ...s.reader_prefs },
        loaded: true,
      });
    } catch {
      set({ loaded: true });
    }
    applyTheme(get().theme);
  },
  setTheme: (t) => {
    set({ theme: t });
    applyTheme(t);
    persist(get());
  },
  setPrefs: (p) => {
    set({ prefs: { ...get().prefs, ...p } });
    persist(get());
  },
}));

// Re-apply on OS scheme change while "system" is selected.
if (typeof window !== "undefined" && window.matchMedia) {
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", () => {
    const { theme } = useApp.getState();
    if (theme === "system") applyTheme("system");
  });
}
