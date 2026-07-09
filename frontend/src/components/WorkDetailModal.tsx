// The library work detail "sheet" (Wave 5, conformed to the design handoff in the design-review pass):
// a wide two-column cinematic modal — cover + rating/year/genre + format chips on the left, title +
// author·series + action row (Read now / Add to shelf / Follow author / ⋯) + underline tabs (Overview /
// Chapters / Sources / Details) on the right. Opened by clicking a work's poster anywhere in the library.
// Reuses the existing per-work primitives (ShelfMenu, SendDialog, FixMetadataDialog, RelatedTitles).
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, Edition, WorkDetail } from "../api/client";
import { qk } from "../api/queryKeys";
import { useApp } from "../store";
import { useAudio } from "../audioStore";
import { useLanguageName } from "./LanguageBadge";
import Cover, { coverSrc } from "./Cover";
import { cleanText } from "../lib/text";
import RelatedTitles from "./RelatedTitles";
import SendDialog from "./SendDialog";
import { ReportIssueDialog } from "./IssuesPanel";
import { ShelfMenu, FixMetadataDialog } from "../pages/Library";
import {
  Badge, Button, Chip, EmptyState, Modal, OverflowMenu, Select, Spinner, StatusChip,
} from "./ui";

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  const mb = n / (1024 * 1024);
  if (mb < 1) return `${(n / 1024).toFixed(0)} KB`;
  if (mb < 1024) return `${mb.toFixed(1)} MB`;
  return `${(mb / 1024).toFixed(2)} GB`;
}
function fmtDate(s: string): string {
  const d = new Date(s);
  return isNaN(d.getTime()) ? s : d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}
// Exportable/available formats, derived from the work's medium (honest about what we can deliver).
function formatChips(work: WorkDetail, t: (key: string) => string): string[] {
  const out = [work.media_kind === "comic" ? "CBZ" : "EPUB"];
  if (work.audiobook_work_id) out.push(t("work.format.audio"));
  return out;
}

// Normalized comparison key for an edition's language (null → "" bucket). Used to collapse editions
// to one entry per distinct language for the reading/listening selectors.
const langKey = (lang: string | null): string => (lang ?? "").trim().toLowerCase();

// One edition per distinct language, in first-seen order. A selector is only worth showing when this
// yields MORE THAN ONE entry.
function distinctByLanguage(editions: Edition[]): Edition[] {
  const seen = new Set<string>();
  const out: Edition[] = [];
  for (const e of editions) {
    const k = langKey(e.language);
    if (seen.has(k)) continue;
    seen.add(k);
    out.push(e);
  }
  return out;
}

type Tab = "overview" | "chapters" | "sources" | "details";
// Labels are resolved at render via t(`work.tab.${value}`) — a module-level const can't call the hook.
const TABS: Tab[] = ["overview", "chapters", "sources", "details"];
// library_status values that have a friendly translated label (library.status.*); an unknown status
// falls back to the raw string.
const STATUS_KEYS = new Set(["paused", "gathering", "ongoing", "complete", "incomplete"]);
// work.health values with a translated label (work.health.*); anything else falls back to the raw string.
const HEALTH_KEYS = new Set(["unknown", "ok", "incomplete", "no_chapters", "unreachable", "missing", "corrupt"]);
// Health states that mean the title's FILE/content is bad (danger chip, not just neutral).
const HEALTH_BAD = new Set(["incomplete", "missing", "corrupt"]);

export default function WorkDetailModal({ workId, onClose }: { workId: number; onClose: () => void }) {
  const { t, i18n } = useTranslation();
  const languageName = useLanguageName();
  const qc = useQueryClient();
  const toast = useApp((s) => s.toast);
  const navigate = useNavigate();
  const [tab, setTab] = useState<Tab>("overview");
  const [showSend, setShowSend] = useState(false);
  const [showFix, setShowFix] = useState(false);
  const [showReport, setShowReport] = useState(false);
  // Chosen edition work_id per format (null = fall back to the computed default below). These are the
  // CONTENT language (which edition of the title), independent of the UI-locale switcher in Settings.
  const [readWorkId, setReadWorkId] = useState<number | null>(null);
  const [listenWorkId, setListenWorkId] = useState<number | null>(null);

  const { data: work, isLoading } = useQuery({ queryKey: qk.work(workId), queryFn: () => api.getWork(workId) });
  const { data: shelves = [] } = useQuery({ queryKey: qk.bookshelves(), queryFn: api.listBookshelves });

  const enrich = useMutation({
    mutationFn: () => api.enrichWork(workId),
    onSuccess: () => { qc.invalidateQueries({ queryKey: qk.work(workId) }); toast(t("work.toast.metadataRefreshed"), "success"); },
    onError: (e) => toast((e as Error).message, "error"),
  });
  const check = useMutation({
    mutationFn: () => api.checkWorkUpdates(workId),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: qk.work(workId) });
      qc.invalidateQueries({ queryKey: qk.works() });
      if (!r.checked) toast(t("work.toast.noNewChaptersSource"));
      else if (r.error) toast(t("work.toast.checkFailed", { error: r.error }), "error");
      else if (r.new_chapters > 0) toast(t("work.toast.foundNew", { count: r.new_chapters }), "success");
      else toast(r.metadata_changed ? t("work.toast.metadataNoNew") : t("work.toast.upToDate"));
    },
    onError: (e) => toast((e as Error).message, "error"),
  });
  const repair = useMutation({
    mutationFn: () => api.repairWork(workId),
    onSuccess: (rep) => {
      qc.invalidateQueries({ queryKey: qk.work(workId) });
      qc.invalidateQueries({ queryKey: qk.works() });
      toast(t("work.toast.diagnosis", { health: rep.health, detail: rep.detail ?? "", actions: rep.actions.length ? rep.actions.join("; ") : t("work.toast.noFixable") }));
    },
    onError: (e) => toast((e as Error).message, "error"),
  });
  const pause = useMutation({
    mutationFn: () => api.pauseWork(workId),
    onSuccess: () => { qc.invalidateQueries({ queryKey: qk.work(workId) }); qc.invalidateQueries({ queryKey: qk.works() }); toast(t("work.toast.paused")); },
    onError: (e) => toast((e as Error).message, "error"),
  });
  const resume = useMutation({
    mutationFn: () => api.resumeWork(workId),
    onSuccess: () => { qc.invalidateQueries({ queryKey: qk.work(workId) }); qc.invalidateQueries({ queryKey: qk.works() }); toast(t("work.toast.resumed"), "success"); },
    onError: (e) => toast((e as Error).message, "error"),
  });
  const remove = useMutation({
    mutationFn: () => api.deleteWork(workId),
    onSuccess: () => {
      for (const key of [qk.works(), qk.continue(), qk.continueListening(), qk.bookshelves()])
        qc.invalidateQueries({ queryKey: key });
      onClose();
    },
    onError: (e) => toast((e as Error).message, "error"),
  });

  // Follow author/series is intentionally NOT offered here: a work in this modal is already in the
  // user's library (it auto-gathers updates), so a per-item "track" control would be redundant.
  // Following an author/series to catch their *other* releases lives on the Discover/Browse cards.

  if (isLoading || !work) {
    return (
      <Modal title="" onClose={onClose} variant="fullscreen-sheet" width="max-w-4xl" hideHeader>
        <div className="py-16"><Spinner label={t("common.loading")} /></div>
      </Modal>
    );
  }

  const reading = work.library_status === "gathering";

  // --- Per-title content-language selectors (reading = ebook/comic, listening = audiobook) ---
  // Collapse each format's editions to one entry per distinct language; only offer a selector when
  // there's more than one language to choose from.
  const readEditions = distinctByLanguage(work.reading_editions ?? []);
  const listenEditions = distinctByLanguage(work.listening_editions ?? []);
  const uiLang = langKey(i18n.language); // e.g. "en-us" → "en-us"; compared by first segment below too
  // Default reading edition: the work being viewed → the UI-locale language → the first edition.
  const defaultReadId =
    readEditions.find((e) => e.work_id === work.id)?.work_id ??
    readEditions.find((e) => langKey(e.language) === uiLang || langKey(e.language) === uiLang.split("-")[0])?.work_id ??
    readEditions[0]?.work_id ??
    work.id;
  // Default listening edition: the language-matched audiobook (audiobook_work_id) → the first edition.
  const defaultListenId =
    listenEditions.find((e) => e.work_id === work.audiobook_work_id)?.work_id ??
    listenEditions[0]?.work_id ??
    work.audiobook_work_id;
  const selectedReadId = readWorkId ?? defaultReadId;
  const selectedListenId = listenWorkId ?? defaultListenId;
  const showReadingLangs = readEditions.length > 1;
  const showListeningLangs = listenEditions.length > 1;

  // Read opens the chosen ebook edition. Preserve the resume-into-last-chapter behavior only when the
  // chosen edition is THIS work (another edition is a different Work with its own progress/chapters).
  const onRead = () =>
    navigate(
      selectedReadId === workId && work.last_chapter_id
        ? `/read/${workId}/${work.last_chapter_id}`
        : `/read/${selectedReadId}`,
    );
  const subtitle = [work.author, work.series && `${work.series}${work.series_position != null ? ` · #${work.series_position}` : ""}`]
    .filter(Boolean).join(" · ");

  return (
    <Modal title="" onClose={onClose} variant="fullscreen-sheet" width="max-w-4xl" hideHeader>
      <div className="flex flex-col gap-6 pt-2 sm:flex-row sm:gap-7">
        {/* ---- LEFT: cover + meta + formats ---- */}
        <div className="mx-auto w-44 shrink-0 sm:mx-0 sm:w-[260px]">
          <div className="aspect-[2/3] w-full overflow-hidden rounded-[14px] border border-[var(--hair,var(--border))] shadow-[var(--pop-shadow)]">
            {coverSrc(work.cover_url) ? (
              <img src={coverSrc(work.cover_url)!} alt="" className="h-full w-full object-cover" />
            ) : (
              <Cover title={work.title} author={work.author} />
            )}
          </div>
          <div className="mt-3 flex flex-wrap items-center gap-x-2 gap-y-1 text-sm text-[var(--text-soft,var(--muted))]">
            {work.rating != null && <span className="font-semibold text-text">★ {work.rating.toFixed(1)}</span>}
            {work.year != null && <span>{work.year}</span>}
            {work.genres?.[0] && <span>· {work.genres[0]}</span>}
          </div>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {formatChips(work, t).map((f) => <Badge key={f}>{f}</Badge>)}
          </div>
        </div>

        {/* ---- RIGHT: title + actions + tabs + content ---- */}
        <div className="min-w-0 flex-1">
          <h2 className="font-display text-[26px] font-semibold leading-[1.1] text-text sm:text-[32px]">{work.title}</h2>
          {subtitle && <div className="mt-1.5 text-sm font-semibold text-[var(--text-soft,var(--muted))]">{subtitle}</div>}

          {/* Content-language selectors — only when a format offers more than one language. Picks which
              edition the Read / Listen buttons below open. Separate from the UI-locale switcher. */}
          {(showReadingLangs || showListeningLangs) && (
            <div className="mt-4 flex flex-wrap gap-3">
              {showReadingLangs && (
                <div className="w-40">
                  <Select
                    label={t("work.readingLanguage")}
                    value={String(selectedReadId)}
                    onChange={(v) => setReadWorkId(Number(v))}
                    options={readEditions.map((e) => ({ value: String(e.work_id), label: languageName(langKey(e.language) || "en") }))}
                  />
                </div>
              )}
              {showListeningLangs && (
                <div className="w-40">
                  <Select
                    label={t("work.listeningLanguage")}
                    value={String(selectedListenId)}
                    onChange={(v) => setListenWorkId(Number(v))}
                    options={listenEditions.map((e) => ({ value: String(e.work_id), label: languageName(langKey(e.language) || "en") }))}
                  />
                </div>
              )}
            </div>
          )}

          {/* Action row */}
          <div className="mt-4 flex flex-wrap items-center gap-2">
            <Button variant="primary" onClick={onRead}>
              ▶ {reading ? t("work.action.read") : work.scroll_fraction > 0 || work.last_chapter_id ? t("work.action.continue") : t("work.action.read")}
            </Button>
            {work.audiobook_work_id && selectedListenId != null && (
              <Button variant="outline" onClick={() => useAudio.getState().playWork(selectedListenId)}>🎧 {t("work.action.listen")}</Button>
            )}
            <ShelfMenu work={work} shelves={shelves} />
            <OverflowMenu
              label={t("work.moreActions", { title: work.title })}
              items={[
                { label: t("work.action.sendExport"), onClick: () => setShowSend(true) },
                { label: enrich.isPending ? t("work.action.refreshing") : t("work.action.refreshMetadata"), disabled: enrich.isPending, onClick: () => enrich.mutate() },
                { label: t("work.action.editMetadata"), onClick: () => setShowFix(true) },
                { label: t("work.action.reportIssue"), onClick: () => setShowReport(true) },
                { label: check.isPending ? t("work.action.checking") : t("work.action.checkUpdates"), disabled: check.isPending, onClick: () => check.mutate() },
                (work.health === "incomplete" || work.library_status === "incomplete") && {
                  label: repair.isPending ? t("work.action.repairing") : t("work.action.repair"), disabled: repair.isPending, onClick: () => repair.mutate(),
                },
                work.library_status === "paused"
                  ? { label: resume.isPending ? t("work.action.resuming") : t("work.action.resume"), disabled: resume.isPending, onClick: () => resume.mutate() }
                  : work.hooked && work.status === "ongoing" && { label: pause.isPending ? t("work.action.pausing") : t("work.action.pause"), disabled: pause.isPending, onClick: () => pause.mutate() },
                { label: t("work.action.removeFromLibrary"), danger: true, onClick: () => remove.mutate() },
              ]}
            />
          </div>

          {/* Underline tabs */}
          <div className="mt-5 flex gap-5 border-b border-[var(--hair,var(--border))]">
            {TABS.map((tabValue) => (
              <button key={tabValue} onClick={() => setTab(tabValue)}
                className={`-mb-px border-b-2 pb-2 text-sm font-semibold transition ${
                  tab === tabValue
                    ? "border-accent text-text"
                    : "border-transparent text-[var(--text-soft,var(--muted))] hover:text-text"
                }`}>
                {t(`work.tab.${tabValue}`)}
              </button>
            ))}
          </div>

          <div className="pt-4">
            {tab === "overview" && <OverviewTab work={work} workId={workId} />}
            {tab === "chapters" && <ChaptersTab work={work} workId={workId} onPick={(cid) => navigate(`/read/${workId}/${cid}`)} />}
            {tab === "sources" && <SourcesTab work={work} workId={workId} onRepair={() => repair.mutate()} onCheck={() => check.mutate()} repairBusy={repair.isPending} checkBusy={check.isPending} />}
            {tab === "details" && <DetailsTab work={work} onRefresh={() => enrich.mutate()} onEdit={() => setShowFix(true)} refreshBusy={enrich.isPending} />}
          </div>
        </div>
      </div>

      {showSend && <SendDialog workId={workId} title={work.title} onClose={() => setShowSend(false)} />}
      {showFix && <FixMetadataDialog work={work} onClose={() => setShowFix(false)} />}
      {showReport && <ReportIssueDialog workId={workId} title={work.title} onClose={() => setShowReport(false)} />}
    </Modal>
  );
}

// Small Overview info tile (Status / Chapters), mirroring the handoff's two-card pattern.
function InfoTile({ label, value, tone }: { label: string; value: React.ReactNode; tone?: string }) {
  return (
    <div className="flex-1 rounded-xl border border-[var(--hair,var(--border))] bg-surface-2/40 px-3.5 py-2.5">
      <div className="text-[11px] font-semibold uppercase tracking-wide text-muted">{label}</div>
      <div className={`mt-0.5 text-sm font-semibold ${tone ?? "text-text"}`}>{value}</div>
    </div>
  );
}

function OverviewTab({ work, workId }: { work: WorkDetail; workId: number }) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  const total = Math.max(work.chapters_total, work.chapters_fetched);
  const desc = cleanText(work.description);
  return (
    <div className="space-y-4">
      {desc ? (
        <div>
          <p className={`whitespace-pre-line text-sm leading-relaxed text-[var(--text-soft,var(--muted))] ${expanded ? "" : "line-clamp-6"}`}>
            {desc}
          </p>
          {desc.length > 280 && (
            <button onClick={() => setExpanded((v) => !v)} className="mt-1 text-xs font-semibold text-accent hover:underline">
              {expanded ? t("work.overview.showLess") : t("work.overview.showMore")}
            </button>
          )}
        </div>
      ) : (
        <p className="text-sm text-muted">{t("work.overview.noDescription")}</p>
      )}
      <div className="flex flex-col gap-2.5 sm:flex-row">
        <InfoTile label={t("work.field.status")}
          value={STATUS_KEYS.has(work.library_status) ? t(`library.status.${work.library_status}`) : work.library_status}
          tone={work.library_status === "complete" ? "text-[var(--success,#16a34a)]" : undefined} />
        <InfoTile label={t("work.field.chapters")} value={total > 0 ? t("work.overview.chaptersGathered", { fetched: work.chapters_fetched, total }) : "—"} />
      </div>
      <RelatedTitles workId={workId} />
    </div>
  );
}

function ChaptersTab({ work, workId, onPick }: { work: WorkDetail; workId: number; onPick: (chapterId: number) => void }) {
  const { t } = useTranslation();
  const { data: chapters = [], isLoading } = useQuery({ queryKey: qk.chaptersAll(workId), queryFn: () => api.listAllChapters(workId) });
  if (isLoading) return <div className="py-6"><Spinner label={t("work.chapters.loading")} /></div>;
  if (chapters.length === 0) return <EmptyState title={t("work.chapters.emptyTitle")} hint={t("work.chapters.emptyHint")} />;
  return (
    <div>
      <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">
        {t("work.overview.chaptersGathered", { fetched: work.chapters_fetched, total: Math.max(work.chapters_total, work.chapters_fetched) })}
      </div>
      <div className="max-h-[46vh] divide-y divide-[var(--hair,var(--border))] overflow-y-auto rounded-xl border border-[var(--hair,var(--border))]">
        {chapters.map((c) => (
          <button
            key={c.id}
            onClick={() => c.has_content && onPick(c.id)}
            disabled={!c.has_content}
            className="flex w-full items-center gap-3 px-3 py-2 text-left text-sm transition hover:bg-surface-2 disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:bg-transparent"
          >
            <span className="w-10 shrink-0 text-xs tabular-nums text-muted">{c.number}</span>
            <span className="min-w-0 flex-1 truncate text-text">{c.title || t("work.chapters.chapterN", { number: c.number })}</span>
            <span className="shrink-0 text-xs text-muted">{c.has_content ? "→" : "…"}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

function SourcesTab({
  work, workId, onRepair, onCheck, repairBusy, checkBusy,
}: {
  work: WorkDetail; workId: number;
  onRepair: () => void; onCheck: () => void; repairBusy: boolean; checkBusy: boolean;
}) {
  const { t } = useTranslation();
  const { data: prov } = useQuery({ queryKey: ["work-provenance", workId], queryFn: () => api.getWorkProvenance(workId) });
  const healthy = work.health === "ok";
  return (
    <div className="space-y-4">
      {prov && (prov.source_name || prov.source_ref || prov.filename || prov.catalog_title) && (
        <div className="rounded-xl border border-[var(--hair,var(--border))] bg-surface-2/40 p-3 text-xs">
          <div className="mb-1.5 font-semibold uppercase tracking-wide text-muted">{t("work.provenance.heading")}</div>
          <div className="space-y-1">
            {(prov.source_name || prov.source_ref) && (
              <div className="flex gap-2">
                <span className="w-20 shrink-0 text-muted">{t("work.provenance.source")}</span>
                <span className="min-w-0 flex-1 break-words text-text">
                  {prov.source_name || "—"}{prov.source_ref ? ` · ${prov.source_ref}` : ""}
                  {prov.source_url && <a href={prov.source_url} target="_blank" rel="noreferrer" className="ml-1 text-accent underline">{t("common.open")}</a>}
                </span>
              </div>
            )}
            {prov.filename && (
              <div className="flex gap-2"><span className="w-20 shrink-0 text-muted">{t("work.provenance.file")}</span><span className="min-w-0 flex-1 break-words text-text">{prov.filename}</span></div>
            )}
            {prov.catalog_title && (
              <div className="flex gap-2">
                <span className="w-20 shrink-0 text-muted">{t("work.provenance.catalog")}</span>
                <span className="min-w-0 flex-1 break-words text-text">{prov.catalog_title}{prov.catalog_author ? ` · ${prov.catalog_author}` : ""}{prov.catalog_domain ? ` · ${prov.catalog_domain}` : ""}</span>
              </div>
            )}
          </div>
        </div>
      )}

      <div className="rounded-xl border border-[var(--hair,var(--border))] p-3">
        <div className="mb-2 flex items-center gap-2">
          <span className="text-xs font-semibold uppercase tracking-wide text-muted">{t("work.sources.health")}</span>
          <StatusChip tone={healthy ? "success" : HEALTH_BAD.has(work.health) ? "danger" : "neutral"}>{HEALTH_KEYS.has(work.health) ? t(`work.health.${work.health}`) : work.health}</StatusChip>
        </div>
        {work.health_detail && <p className="mb-2 text-sm text-[var(--text-soft,var(--muted))]">{work.health_detail}</p>}
        <div className="flex flex-wrap gap-2">
          <Button size="sm" variant="outline" disabled={checkBusy} onClick={onCheck}>{checkBusy ? t("work.action.checking") : t("work.action.checkUpdates")}</Button>
          {/* Repair re-crawls chapter gaps — meaningless for a missing/corrupt FILE (fix is re-acquire/replace). */}
          {!healthy && !["missing", "corrupt"].includes(work.health) && <Button size="sm" variant="outline" disabled={repairBusy} onClick={onRepair}>{repairBusy ? t("work.action.repairing") : t("work.action.repair")}</Button>}
        </div>
      </div>

      <RelatedTitles workId={workId} />
    </div>
  );
}

function DetailsTab({ work, onRefresh, onEdit, refreshBusy }: { work: WorkDetail; onRefresh: () => void; onEdit: () => void; refreshBusy: boolean }) {
  const { t } = useTranslation();
  const isbns = Array.isArray((work.identifiers as Record<string, unknown> | null)?.isbn)
    ? ((work.identifiers as Record<string, unknown>).isbn as unknown[]).map(String)
    : [];
  const crawl = work.crawl_interval_s
    ? t("work.details.crawlEvery", { hours: Math.round(work.crawl_interval_s / 3600) }) + (work.crawl_window_start != null && work.crawl_window_end != null ? ` · ${work.crawl_window_start}:00–${work.crawl_window_end}:00` : "")
    : null;

  const rows: { label: string; value: React.ReactNode }[] = [];
  if (work.rating != null)
    rows.push({ label: t("work.field.rating"), value: `${work.rating.toFixed(1)} ★${work.rating_count != null ? ` · ${t("work.details.ratingCount", { count: work.rating_count.toLocaleString() })}` : ""}` });
  if (work.year != null) rows.push({ label: t("work.field.year"), value: work.year });
  if (work.genres && work.genres.length > 0)
    rows.push({ label: t("work.field.genres"), value: <span className="flex flex-wrap gap-1.5">{work.genres.map((g) => <Chip key={g}>{g}</Chip>)}</span> });
  if (work.publisher) rows.push({ label: t("work.field.publisher"), value: work.publisher });
  if (work.narrator) rows.push({ label: t("work.field.narrator"), value: work.narrator });
  if (work.language) rows.push({ label: t("work.field.language"), value: work.language });
  if (work.page_count != null) rows.push({ label: t("work.field.pages"), value: work.page_count });
  rows.push({ label: t("work.field.status"), value: STATUS_KEYS.has(work.library_status) ? t(`library.status.${work.library_status}`) : work.library_status });
  if (isbns.length > 0) rows.push({ label: isbns.length > 1 ? t("work.field.isbns") : t("work.field.isbn"), value: isbns.join(", ") });
  if (work.created_at) rows.push({ label: t("work.field.added"), value: fmtDate(work.created_at) });
  if (work.local_size != null) rows.push({ label: t("work.field.size"), value: fmtBytes(work.local_size) });
  if (crawl) rows.push({ label: t("work.field.updateSchedule"), value: crawl });

  return (
    <div className="space-y-4">
      <dl className="grid grid-cols-1 gap-x-6 gap-y-2.5 sm:grid-cols-2">
        {rows.map((r) => (
          <div key={r.label} className="flex flex-col gap-0.5 border-b border-[var(--hair,var(--border))] pb-2 last:border-0 sm:flex-row sm:items-baseline sm:gap-3">
            <dt className="shrink-0 text-xs font-semibold uppercase tracking-wide text-muted sm:w-28">{r.label}</dt>
            <dd className="min-w-0 text-sm text-text">{r.value}</dd>
          </div>
        ))}
      </dl>
      <div className="flex flex-wrap gap-2">
        <Button size="sm" variant="outline" disabled={refreshBusy} onClick={onRefresh}>{refreshBusy ? t("work.action.refreshing") : t("work.action.refreshMetadata")}</Button>
        <Button size="sm" variant="outline" onClick={onEdit}>{t("work.action.editMetadata")}</Button>
      </div>
    </div>
  );
}
