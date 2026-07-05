import { useMemo, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, FeaturedConfig } from "../../api/client";
import { qk } from "../../api/queryKeys";
import { Badge, Button, Card, InfoHint, Spinner, inputCls } from "../ui";
import { laneKey } from "./layout";

const buildMethods = (t: TFunction): { value: FeaturedConfig["method"]; label: string; hint: string }[] => [
  { value: "popular", label: t("featured.methodPopular"), hint: t("featured.methodPopularHint") },
  { value: "newest", label: t("featured.methodNewest"), hint: t("featured.methodNewestHint") },
  { value: "random", label: t("featured.methodRandom"), hint: t("featured.methodRandomHint") },
];

const buildRotatePresets = (t: TFunction): { h: number; label: string }[] => [
  { h: 0, label: t("featured.rotateEveryVisit") },
  { h: 24, label: t("featured.rotateDaily") },
  { h: 168, label: t("featured.rotateWeekly") },
];

const DEFAULTS: FeaturedConfig = { method: "popular", categories: [], media: [], rotateHours: 0 };

function Chip({ on, onClick, children }: { on: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`rounded-full border px-2.5 py-1 text-xs transition ${
        on ? "border-accent bg-accent text-accent-fg" : "border-border bg-surface text-muted hover:bg-surface-2"
      }`}
    >
      {on ? "✓ " : ""}
      {children}
    </button>
  );
}

/** Admin-only: the rules behind the Discover page's "Featured this week" billboard — how the title is
 *  picked, which genres + media it's drawn from, and how often it rotates. Applied client-side on top
 *  of each user's permission-filtered catalog, so it can only ever narrow what they already see. */
export default function FeaturedSettings() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const METHODS = buildMethods(t);
  const ROTATE_PRESETS = buildRotatePresets(t);
  const cfgQ = useQuery({ queryKey: qk.featuredConfig(), queryFn: api.getFeaturedConfig });
  const rowsQ = useQuery({ queryKey: qk.catalogRows(), queryFn: () => api.catalogRows() });
  // The app's global index layout — its hidden categories/lanes are excluded from the options below,
  // so the billboard can only ever feature content that's actually shown on the Discover page.
  const layoutQ = useQuery({ queryKey: qk.indexLayout(), queryFn: () => api.getIndexLayout() });
  const [draft, setDraft] = useState<FeaturedConfig | null>(null);
  const value = draft ?? cfgQ.data ?? DEFAULTS;

  const save = useMutation({
    mutationFn: () => api.putFeaturedConfig(value),
    onSuccess: (d) => { qc.setQueryData(qk.featuredConfig(), d); setDraft(null); },
  });

  // Real options from the admin's own catalog: distinct media labels + genre/theme lane labels,
  // minus anything hidden in the app's index layout (a hidden category drops its whole subtree).
  const { mediaOpts, catOpts } = useMemo(() => {
    const hiddenCats = new Set(layoutQ.data?.hiddenCategories ?? []);
    const hiddenLanes = new Set(layoutQ.data?.hiddenLanes ?? []);
    const media = new Set<string>(), cats = new Set<string>();
    for (const row of rowsQ.data ?? []) {
      if (hiddenCats.has(row.media_category)) continue;
      if (row.kind !== "popular" && row.label && !hiddenLanes.has(laneKey(row))) cats.add(row.label);
      for (const it of row.items ?? []) if (it.media_label) media.add(it.media_label);
    }
    return { mediaOpts: [...media].sort(), catOpts: [...cats].sort() };
  }, [rowsQ.data, layoutQ.data]);

  if (cfgQ.isLoading) return <Card className="mb-4 p-4"><Spinner label={t("featured.loading")} /></Card>;

  const toggle = (key: "media" | "categories", v: string) => {
    const cur = new Set(value[key]);
    cur.has(v) ? cur.delete(v) : cur.add(v);
    setDraft({ ...value, [key]: [...cur] });
  };

  return (
    <Card className="mb-4 p-4">
      <h2 className="flex items-center gap-1.5 font-semibold">
        {t("featured.title")}
        <Badge tone="violet">{t("featured.admin")}</Badge>
        <InfoHint text={t("featured.infoHint")} />
      </h2>

      <div className="mt-3 space-y-4">
        <div>
          <div className="mb-1.5 text-xs font-semibold text-muted">{t("featured.selection")}</div>
          <div className="flex flex-wrap gap-1.5">
            {METHODS.map((m) => (
              <button
                key={m.value}
                type="button"
                title={m.hint}
                onClick={() => setDraft({ ...value, method: m.value })}
                className={`rounded-full border px-3 py-1 text-xs transition ${
                  value.method === m.value
                    ? "border-accent bg-accent text-accent-fg"
                    : "border-border bg-surface text-muted hover:bg-surface-2"
                }`}
              >
                {m.label}
              </button>
            ))}
          </div>
        </div>

        <div>
          <div className="mb-1.5 text-xs font-semibold text-muted">
            {t("featured.mediaTypes")} <span className="font-normal text-muted/70">{t("featured.noneSelectedAll")}</span>
          </div>
          {mediaOpts.length === 0 ? (
            <p className="text-xs text-muted">{t("featured.noMedia")}</p>
          ) : (
            <div className="flex flex-wrap gap-1.5">
              {mediaOpts.map((m) => (
                <Chip key={m} on={value.media.includes(m)} onClick={() => toggle("media", m)}>{m}</Chip>
              ))}
            </div>
          )}
        </div>

        <div>
          <div className="mb-1.5 text-xs font-semibold text-muted">
            {t("featured.categories")} <span className="font-normal text-muted/70">{t("featured.noneSelectedAll")}</span>
          </div>
          {catOpts.length === 0 ? (
            <p className="text-xs text-muted">{t("featured.noGenres")}</p>
          ) : (
            <div className="flex max-h-40 flex-wrap gap-1.5 overflow-y-auto rounded-lg border border-border p-2">
              {catOpts.map((c) => (
                <Chip key={c} on={value.categories.includes(c)} onClick={() => toggle("categories", c)}>{c}</Chip>
              ))}
            </div>
          )}
        </div>

        <div>
          <div className="mb-1.5 text-xs font-semibold text-muted">{t("featured.rotation")}</div>
          <div className="flex flex-wrap items-center gap-1.5">
            {ROTATE_PRESETS.map((p) => (
              <Chip key={p.h} on={value.rotateHours === p.h} onClick={() => setDraft({ ...value, rotateHours: p.h })}>
                {p.label}
              </Chip>
            ))}
            <span className="ml-1 inline-flex items-center gap-1.5 text-xs text-muted">
              {t("featured.every")}
              <input
                type="number"
                min={0}
                max={720}
                value={value.rotateHours}
                onChange={(e) => setDraft({ ...value, rotateHours: Math.max(0, Math.min(720, Number(e.target.value) || 0)) })}
                className={`${inputCls} w-20!`}
              />
              {t("featured.hours")}
            </span>
          </div>
          <p className="mt-1.5 text-[11px] leading-snug text-muted">
            {t("featured.rotationHint")}
          </p>
        </div>
      </div>

      <div className="mt-4 flex items-center gap-2">
        <Button variant="primary" size="sm" disabled={!draft || save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? t("featured.saving") : t("featured.saveForEveryone")}
        </Button>
        {draft && <Button size="sm" onClick={() => setDraft(null)}>{t("featured.discard")}</Button>}
        {save.isError && <span className="text-sm text-red-500">{(save.error as Error).message}</span>}
        {save.isSuccess && !draft && <Badge tone="green">{t("featured.saved")}</Badge>}
      </div>
    </Card>
  );
}
