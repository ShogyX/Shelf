import { useMemo, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, FeaturedConfig } from "../../api/client";
import { qk } from "../../api/queryKeys";
import { Badge, Button, Card, InfoHint, Spinner, inputCls } from "../ui";
import { laneKey } from "./layout";

const METHODS: { value: FeaturedConfig["method"]; label: string; hint: string }[] = [
  { value: "popular", label: "Most popular", hint: "Drawn from the top of the popularity ranking." },
  { value: "newest", label: "Newest", hint: "Most recently added titles first." },
  { value: "random", label: "Random", hint: "A different pick each visit." },
];

const ROTATE_PRESETS: { h: number; label: string }[] = [
  { h: 0, label: "Every visit" },
  { h: 24, label: "Daily" },
  { h: 168, label: "Weekly" },
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
  const qc = useQueryClient();
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

  if (cfgQ.isLoading) return <Card className="mb-4 p-4"><Spinner label="Loading featured rules…" /></Card>;

  const toggle = (key: "media" | "categories", v: string) => {
    const cur = new Set(value[key]);
    cur.has(v) ? cur.delete(v) : cur.add(v);
    setDraft({ ...value, [key]: [...cur] });
  };

  return (
    <Card className="mb-4 p-4">
      <h2 className="flex items-center gap-1.5 font-semibold">
        Featured this week
        <Badge tone="violet">admin</Badge>
        <InfoHint text={<>The rules for the big billboard title at the top of the Discover page: how it's
          picked, which genres + media types it's drawn from, and how often it changes. Applied on top of
          each user's permission-filtered catalog, so it can only ever narrow what they're already allowed
          to see.</>} />
      </h2>

      <div className="mt-3 space-y-4">
        <div>
          <div className="mb-1.5 text-xs font-semibold text-muted">Selection</div>
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
            Media types <span className="font-normal text-muted/70">— none selected = all</span>
          </div>
          {mediaOpts.length === 0 ? (
            <p className="text-xs text-muted">No media discovered yet.</p>
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
            Categories <span className="font-normal text-muted/70">— none selected = all</span>
          </div>
          {catOpts.length === 0 ? (
            <p className="text-xs text-muted">No genres discovered yet.</p>
          ) : (
            <div className="flex max-h-40 flex-wrap gap-1.5 overflow-y-auto rounded-lg border border-border p-2">
              {catOpts.map((c) => (
                <Chip key={c} on={value.categories.includes(c)} onClick={() => toggle("categories", c)}>{c}</Chip>
              ))}
            </div>
          )}
        </div>

        <div>
          <div className="mb-1.5 text-xs font-semibold text-muted">Rotation</div>
          <div className="flex flex-wrap items-center gap-1.5">
            {ROTATE_PRESETS.map((p) => (
              <Chip key={p.h} on={value.rotateHours === p.h} onClick={() => setDraft({ ...value, rotateHours: p.h })}>
                {p.label}
              </Chip>
            ))}
            <span className="ml-1 inline-flex items-center gap-1.5 text-xs text-muted">
              every
              <input
                type="number"
                min={0}
                max={720}
                value={value.rotateHours}
                onChange={(e) => setDraft({ ...value, rotateHours: Math.max(0, Math.min(720, Number(e.target.value) || 0)) })}
                className={`${inputCls} w-20!`}
              />
              hours
            </span>
          </div>
          <p className="mt-1.5 text-[11px] leading-snug text-muted">
            0 = a lively carousel that changes each visit. Any other value pins one title for everyone for
            that many hours (168 = a steady “featured this week”).
          </p>
        </div>
      </div>

      <div className="mt-4 flex items-center gap-2">
        <Button variant="primary" size="sm" disabled={!draft || save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? "Saving…" : "Save for everyone"}
        </Button>
        {draft && <Button size="sm" onClick={() => setDraft(null)}>Discard</Button>}
        {save.isError && <span className="text-sm text-red-500">{(save.error as Error).message}</span>}
        {save.isSuccess && !draft && <Badge tone="green">saved</Badge>}
      </div>
    </Card>
  );
}
