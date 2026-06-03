import { useApp, DEFAULT_PREFS, FONTS, WIDTH_PRESETS } from "../store";
import { tokensFor, hexToHsl } from "../themes";
import ThemePicker from "./ThemePicker";

function LightSlider({
  label, value, naturalL, onChange, onAuto,
}: {
  label: string; value: number | null; naturalL: number;
  onChange: (v: number) => void; onAuto: () => void;
}) {
  const v = value ?? naturalL;
  return (
    <label className="block">
      <div className="mb-1 flex items-center justify-between text-xs text-muted">
        <span>{label}</span>
        <button
          onClick={(e) => { e.preventDefault(); onAuto(); }}
          className={`rounded px-1.5 py-0.5 ${value == null ? "text-muted" : "text-accent hover:underline"}`}
        >
          {value == null ? "Auto" : "Reset"}
        </button>
      </div>
      <input
        type="range" min={0} max={100} step={1} value={v}
        onChange={(e) => onChange(parseInt(e.target.value))}
        className="w-full accent-[var(--accent)]"
      />
    </label>
  );
}

function Stepper({
  label, value, suffix, min, max, step, onChange,
}: {
  label: string; value: number; suffix?: string; min: number; max: number; step: number;
  onChange: (v: number) => void;
}) {
  const round = (n: number) => Math.round(n * 100) / 100;
  return (
    <div className="flex items-center justify-between">
      <span className="text-sm text-text">{label}</span>
      <div className="flex items-center gap-1">
        <button
          onClick={() => onChange(round(Math.max(min, value - step)))}
          className="h-7 w-7 rounded-md border border-border text-text hover:bg-surface-2"
        >−</button>
        <span className="w-14 text-center text-sm tabular-nums text-muted">
          {round(value)}{suffix}
        </span>
        <button
          onClick={() => onChange(round(Math.min(max, value + step)))}
          className="h-7 w-7 rounded-md border border-border text-text hover:bg-surface-2"
        >+</button>
      </div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="space-y-2.5">
      <div className="text-[11px] font-semibold uppercase tracking-wide text-muted">{title}</div>
      {children}
    </div>
  );
}

// Segmented two-/multi-option toggle reused by the comic controls.
function Seg<T extends string>({
  value, options, onChange,
}: {
  value: T; options: [T, string][]; onChange: (v: T) => void;
}) {
  return (
    <div className="flex overflow-hidden rounded-lg border border-border text-sm">
      {options.map(([key, label]) => (
        <button
          key={key}
          onClick={() => onChange(key)}
          className={`flex-1 px-2 py-2 transition ${
            value === key ? "bg-accent text-accent-fg" : "hover:bg-surface-2"
          }`}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

function ComicSection() {
  const { prefs, setPrefs } = useApp();
  const mode = prefs.comicMode ?? "continuous";
  const fit = prefs.comicFit ?? "width";
  return (
    <Section title="Pages">
      <div className="space-y-1">
        <div className="text-xs text-muted">Layout</div>
        <Seg
          value={mode}
          options={[["continuous", "Webtoon (scroll)"], ["single", "Manga (pages)"]]}
          onChange={(v) => setPrefs({ comicMode: v })}
        />
      </div>
      <div className="space-y-1">
        <div className="text-xs text-muted">Fit to</div>
        <Seg
          value={fit}
          options={[["width", "Width"], ["height", "Height"]]}
          onChange={(v) => setPrefs({ comicFit: v })}
        />
      </div>
      <Stepper
        label="Zoom" value={Math.round((prefs.comicZoom ?? 1) * 100)} suffix="%"
        min={50} max={400} step={10}
        onChange={(v) => setPrefs({ comicZoom: v / 100 })}
      />
      {mode === "continuous" && (
        <Stepper
          label="Page gap" value={prefs.comicGap ?? 0} suffix="px" min={0} max={40} step={2}
          onChange={(v) => setPrefs({ comicGap: v })}
        />
      )}
      <p className="text-[11px] text-muted">
        Pinch or double-tap a page to zoom; arrows / taps turn pages.
      </p>
    </Section>
  );
}

export default function ReaderControls({
  onClose,
  onFocus,
  panelStyle,
  isComic = false,
}: {
  onClose: () => void;
  onFocus?: () => void;
  panelStyle?: React.CSSProperties;
  isComic?: boolean;
}) {
  const { prefs, setPrefs, theme } = useApp();
  const tk = tokensFor(theme);
  const naturalTextL = hexToHsl(tk.text).l;
  const naturalBgL = hexToHsl(tk.bg).l;

  return (
    <>
      <div className="fixed inset-0 z-40" onClick={onClose} />
      <div
        className="fixed z-50 max-h-[82vh] overflow-y-auto scrollbar-thin rounded-2xl border border-border bg-surface p-4 shadow-2xl"
        style={{ ...panelStyle, paddingBottom: "max(1rem, env(safe-area-inset-bottom))" }}
      >
        <div className="mb-3 flex items-center justify-between">
          <h3 className="font-semibold">Reading settings</h3>
          <button
            onClick={onClose}
            className="rounded-lg px-2 py-1 text-sm text-muted hover:bg-surface-2"
          >Done</button>
        </div>

        <div className="space-y-5">
          <Section title="Color mode">
            <ThemePicker columns={3} />
          </Section>

          {isComic && <ComicSection />}

          {!isComic && (
          <>
          <Section title="Work mode">
            <p className="-mt-1 text-xs text-muted">
              Disguise the reader to look like work content.
            </p>
            <div className="grid grid-cols-4 gap-2">
              {([
                ["off", "Off"],
                ["docs", "Docs"],
                ["article", "Article"],
                ["email", "Email"],
              ] as const).map(([key, label]) => (
                <button
                  key={key}
                  onClick={() => setPrefs({ workMode: key })}
                  className={`rounded-lg border px-1 py-1.5 text-xs transition ${
                    (prefs.workMode ?? "off") === key
                      ? "border-accent bg-surface-2"
                      : "border-border hover:bg-surface-2"
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>
          </Section>

          <Section title="Font">
            <div className="grid grid-cols-3 gap-2">
              {FONTS.map((f) => (
                <button
                  key={f.key}
                  onClick={() => setPrefs({ fontFamily: f.key })}
                  style={{ fontFamily: f.stack }}
                  className={`rounded-lg border px-2 py-2 text-sm transition ${
                    prefs.fontFamily === f.key
                      ? "border-accent bg-surface-2"
                      : "border-border hover:bg-surface-2"
                  }`}
                >{f.label}</button>
              ))}
            </div>
          </Section>

          <Section title="Text">
            <Stepper label="Size" value={prefs.fontSize} suffix="px" min={14} max={32} step={1}
              onChange={(v) => setPrefs({ fontSize: v })} />
            <Stepper label="Line height" value={prefs.lineHeight} min={1.2} max={2.4} step={0.05}
              onChange={(v) => setPrefs({ lineHeight: v })} />
            <Stepper label="Letter spacing" value={prefs.letterSpacing} suffix="px" min={-0.5} max={2} step={0.1}
              onChange={(v) => setPrefs({ letterSpacing: v })} />
            <Stepper label="Paragraph gap" value={prefs.paragraphSpacing} suffix="em" min={0.4} max={2.5} step={0.1}
              onChange={(v) => setPrefs({ paragraphSpacing: v })} />
          </Section>

          <Section title="Brightness">
            <LightSlider
              label="Text lightness" value={prefs.textLightness} naturalL={naturalTextL}
              onChange={(v) => setPrefs({ textLightness: v })}
              onAuto={() => setPrefs({ textLightness: null })}
            />
            <LightSlider
              label="Background lightness" value={prefs.bgLightness} naturalL={naturalBgL}
              onChange={(v) => setPrefs({ bgLightness: v })}
              onAuto={() => setPrefs({ bgLightness: null })}
            />
          </Section>

          <Section title="Layout">
            <label className="block">
              <div className="mb-1 flex items-center justify-between text-xs text-muted">
                <span>Text position</span>
                <span>
                  {prefs.textPosition <= 40 ? "Left" : prefs.textPosition >= 60 ? "Right" : "Center"}
                </span>
              </div>
              <input
                type="range" min={0} max={100} step={1} value={prefs.textPosition}
                onChange={(e) => setPrefs({ textPosition: parseInt(e.target.value) })}
                className="w-full accent-[var(--accent)]"
              />
            </label>
            <div className="grid grid-cols-4 gap-2">
              {WIDTH_PRESETS.map((w) => (
                <button
                  key={w.key}
                  onClick={() => setPrefs({ measure: w.measure })}
                  className={`rounded-lg border px-1 py-1.5 text-xs transition ${
                    prefs.measure === w.measure
                      ? "border-accent bg-surface-2"
                      : "border-border hover:bg-surface-2"
                  }`}
                >{w.label}</button>
              ))}
            </div>
            <div className="grid grid-cols-2 gap-2 pt-1">
              <button
                onClick={() => setPrefs({ justify: !prefs.justify })}
                className={`rounded-lg border px-2 py-2 text-sm transition ${
                  prefs.justify ? "border-accent bg-surface-2" : "border-border hover:bg-surface-2"
                }`}
              >{prefs.justify ? "Justified" : "Ragged"}</button>
              <div className="flex overflow-hidden rounded-lg border border-border text-sm">
                {(["scroll", "paginated"] as const).map((m) => (
                  <button
                    key={m}
                    onClick={() => setPrefs({ mode: m })}
                    className={`flex-1 px-2 py-2 capitalize transition ${
                      prefs.mode === m ? "bg-accent text-accent-fg" : "hover:bg-surface-2"
                    }`}
                  >{m === "scroll" ? "Scroll" : "Pages"}</button>
                ))}
              </div>
            </div>
          </Section>
          </>
          )}

          {onFocus && (
            <button
              onClick={onFocus}
              className="w-full rounded-lg bg-accent py-2.5 text-sm font-medium text-accent-fg hover:opacity-90"
            >
              ⛶ Focus mode (full screen, text only)
            </button>
          )}
          <button
            onClick={() => setPrefs({ ...DEFAULT_PREFS })}
            className="w-full rounded-lg border border-border py-2 text-sm text-muted hover:bg-surface-2"
          >Reset text & layout</button>
        </div>
      </div>
    </>
  );
}
