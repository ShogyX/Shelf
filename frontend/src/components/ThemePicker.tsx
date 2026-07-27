import { useTranslation } from "react-i18next";
import { THEMES } from "../themes";
import { useApp } from "../store";
import { Monitor } from "lucide-react";

function Swatch({ themeKey }: { themeKey: string }) {
  const { theme, setTheme } = useApp();
  const t = THEMES.find((x) => x.key === themeKey)!;
  const active = theme === themeKey;
  return (
    <button
      onClick={() => setTheme(themeKey)}
      title={t.name}
      className={`group relative flex flex-col overflow-hidden rounded-lg border text-left transition ${
        active ? "border-accent ring-2 ring-accent/40" : "border-border hover:border-accent/60"
      }`}
      style={{ background: t.tokens.bg }}
    >
      <div className="flex items-center gap-1 px-2 pt-2">
        <span className="h-3 w-3 rounded-full" style={{ background: t.tokens.accent }} />
        <span className="h-3 w-3 rounded-full" style={{ background: t.tokens.surface2 }} />
      </div>
      <div className="px-2 pb-1 pt-1.5">
        <div className="h-1.5 w-10 rounded" style={{ background: t.tokens.text, opacity: 0.85 }} />
        <div className="mt-1 h-1.5 w-7 rounded" style={{ background: t.tokens.muted }} />
      </div>
      <div
        className="px-2 pb-1.5 text-[11px] font-medium"
        style={{ color: t.tokens.text }}
      >
        {t.name}
      </div>
    </button>
  );
}

export default function ThemePicker({ columns = 3 }: { columns?: number }) {
  const { t: tr } = useTranslation();
  const { theme, setTheme } = useApp();
  const light = THEMES.filter((t) => t.group === "light");
  const dark = THEMES.filter((t) => t.group === "dark");
  return (
    <div className="space-y-3">
      <button
        onClick={() => setTheme("system")}
        className={`w-full rounded-lg border px-3 py-2 text-sm font-medium transition ${
          theme === "system" ? "border-accent ring-2 ring-accent/40" : "border-border hover:bg-surface-2"
        }`}
      >
        <Monitor className="mr-1 inline h-3.5 w-3.5 -mt-px" />{tr("themePicker.matchSystem")}
      </button>
      <div>
        <div className="mb-1 text-[11px] uppercase tracking-wide text-muted">{tr("themePicker.light")}</div>
        <div className="grid gap-2" style={{ gridTemplateColumns: `repeat(${columns}, minmax(0,1fr))` }}>
          {light.map((t) => (
            <Swatch key={t.key} themeKey={t.key} />
          ))}
        </div>
      </div>
      <div>
        <div className="mb-1 text-[11px] uppercase tracking-wide text-muted">{tr("themePicker.dark")}</div>
        <div className="grid gap-2" style={{ gridTemplateColumns: `repeat(${columns}, minmax(0,1fr))` }}>
          {dark.map((t) => (
            <Swatch key={t.key} themeKey={t.key} />
          ))}
        </div>
      </div>
    </div>
  );
}
