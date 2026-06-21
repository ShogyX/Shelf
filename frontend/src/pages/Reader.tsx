import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { qk } from "../api/queryKeys";
import { useApp, FONT_STACKS } from "../store";
import { tokensFor, colorWithLightness, setThemeColor, hexToHsl } from "../themes";
import { Button } from "../components/ui";
import ReaderControls from "../components/ReaderControls";
import ReaderFab from "../components/ReaderFab";
import TocDrawer from "../components/TocDrawer";
import ComicReader, { ComicNav } from "../components/ComicReader";
import { DISGUISE_SKINS, DisguiseHeader, WorkMode, disguiseBody } from "../components/ReaderDisguise";

export default function Reader() {
  const { workId, chapterId } = useParams();
  const wid = Number(workId);
  const navigate = useNavigate();
  const { prefs, theme } = useApp();

  const [showControls, setShowControls] = useState(false);
  const [showToc, setShowToc] = useState(false);
  const [chromeHidden, setChromeHidden] = useState(false);
  const [immersive, setImmersive] = useState(false); // full-screen, text only
  const [progress, setProgress] = useState(0); // 0..1 within chapter — for the bar on (re)render
  // The progress bar is driven by a ref during scroll (direct style write, no React re-render per
  // frame — that per-frame render + width transition was a real battery/heat cost on mobile). The
  // `progress` state is only resynced (throttled) so a re-render shows the right width. (PERF)
  const barRef = useRef<HTMLDivElement>(null);
  const progThrottle = useRef(0);
  const setBarWidth = (frac: number) => {
    if (barRef.current) barRef.current.style.width = `${Math.min(1, Math.max(0, frac)) * 100}%`;
    const now = Date.now();
    if (now - progThrottle.current > 150) { progThrottle.current = now; setProgress(frac); }
  };
  const [page, setPage] = useState(0);
  const [pageCount, setPageCount] = useState(1);

  const scrollRef = useRef<HTMLDivElement>(null);
  const colRef = useRef<HTMLDivElement>(null);
  const restoredFor = useRef<number | null>(null);
  const comicNav = useRef<ComicNav | null>(null);

  const work = useQuery({ queryKey: qk.work(wid), queryFn: () => api.getWork(wid) });
  const prog = useQuery({
    queryKey: qk.progress(wid),
    queryFn: () => api.getProgress(wid),
    staleTime: 10_000,  // avoid refetch storms re-triggering the restore effect
  });

  // Comics/manga are stacked full-width image pages: never paginate them (CSS columns slice
  // the tall images), and let them use the full reader width rather than the prose measure.
  const isComic = work.data?.media_kind === "comic";
  const paginated = prefs.mode === "paginated" && !isComic;

  const resolvedChapterId = useMemo(() => {
    if (chapterId) return Number(chapterId);
    if (prog.data?.continue_chapter_id) return prog.data.continue_chapter_id;
    return undefined;
  }, [chapterId, prog.data]);

  useEffect(() => {
    if (!chapterId && resolvedChapterId) {
      navigate(`/read/${wid}/${resolvedChapterId}`, { replace: true });
    }
  }, [chapterId, resolvedChapterId, wid, navigate]);

  const chapter = useQuery({
    queryKey: qk.chapter(resolvedChapterId),
    queryFn: () => api.getChapter(resolvedChapterId!),
    enabled: !!resolvedChapterId,
  });

  // ---- progress persistence (debounced) ----
  const qc = useQueryClient();

  // ---- text cleanup (de-censor + reflow a badly-scraped chapter / whole title) ----
  const [cleanNote, setCleanNote] = useState<string | null>(null);
  const cleanChapterM = useMutation({
    mutationFn: () => api.cleanChapter(resolvedChapterId!),
    onSuccess: (data) => {
      qc.setQueryData(qk.chapter(resolvedChapterId), data);  // refresh the page in place
      setCleanNote("Cleaned this chapter ✓");
    },
    onError: (e: any) => setCleanNote(e?.message || "Couldn't clean this chapter."),
  });
  const cleanWorkM = useMutation({
    mutationFn: () => api.cleanWork(wid),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: qk.chapter(resolvedChapterId) });
      setCleanNote(`Cleaned ${r.cleaned} of ${r.total} chapters ✓`);
    },
    onError: (e: any) => setCleanNote(e?.message || "Couldn't clean this title."),
  });
  // On leaving the reader, refresh the "Continue reading" shelf + this work's progress — saveProgress
  // doesn't invalidate them, so the Library would otherwise show a stale position/percentage.
  useEffect(() => () => {
    qc.invalidateQueries({ queryKey: qk.continue() });
    qc.invalidateQueries({ queryKey: qk.progress(wid) });
  }, [qc, wid]);
  const saveTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const save = useCallback(
    // `paragraph` may be a thunk so the caller can DEFER an expensive layout read (firstVisibleBlock
    // does querySelectorAll + getBoundingClientRect) until the debounce fires — not on every scroll
    // event. (PERF)
    (fraction: number, paragraph: number | (() => number) = 0) => {
      if (!resolvedChapterId) return;
      clearTimeout(saveTimer.current);
      saveTimer.current = setTimeout(() => {
        const para = typeof paragraph === "function" ? paragraph() : paragraph;
        api
          .saveProgress(wid, resolvedChapterId, Math.min(1, Math.max(0, fraction)), Math.max(0, para))
          .catch(() => {});
      }, 500);
    },
    [wid, resolvedChapterId]
  );

  // Block-level elements we track for precise paragraph positioning.
  const blocks = (): HTMLElement[] =>
    colRef.current
      ? Array.from(colRef.current.querySelectorAll<HTMLElement>("p, h1, h2, h3, blockquote, li"))
      : [];

  // First block at/after the viewport top (scroll mode).
  const firstVisibleBlock = (): number => {
    const el = scrollRef.current;
    if (!el) return 0;
    const top = el.getBoundingClientRect().top;
    const bs = blocks();
    for (let i = 0; i < bs.length; i++) {
      if (bs[i].getBoundingClientRect().bottom > top + 4) return i;
    }
    return Math.max(0, bs.length - 1);
  };

  // Which paginated page a given block sits on (measured at translate 0).
  const blockPage = (idx: number): number => {
    const col = colRef.current;
    const bs = blocks();
    if (!col || !bs[idx]) return 0;
    const prev = col.style.transform;
    col.style.transform = "translateX(0px)";
    const x = bs[idx].getBoundingClientRect().left - col.getBoundingClientRect().left;
    col.style.transform = prev;
    return Math.max(0, Math.round(x / pageStep()));
  };

  // First block whose page == current page (paginated mode).
  const firstBlockOnPage = (p: number): number => {
    const bs = blocks();
    for (let i = 0; i < bs.length; i++) if (blockPage(i) >= p) return i;
    return 0;
  };

  // ---- paginated geometry ----
  // Each "page" is one CSS column the exact width of the prose box; we translateX
  // by whole columns. column-width must be a pixel length (not %), set here.
  const pageStep = () => colRef.current?.clientWidth || 1;
  const setupPagination = useCallback(() => {
    const el = colRef.current;
    if (!paginated || !el) return 1;
    const w = el.clientWidth;
    el.style.columnWidth = `${w}px`;
    // reflow, then count columns
    const total = Math.max(1, Math.round(el.scrollWidth / w));
    return total;
  }, [paginated]);
  const recomputePages = useCallback(() => {
    if (!paginated) return;
    setPageCount(setupPagination());
  }, [paginated, setupPagination]);

  const goToPage = useCallback(
    (p: number, totalOverride?: number) => {
      // Accept a freshly-computed total: the restore path calls this right after setPageCount(total),
      // and React state updates are async, so reading pageCount here would clamp against the PREVIOUS
      // chapter's page count and mis-restore the saved position (UX1).
      const total = totalOverride ?? pageCount;
      const clamped = Math.max(0, Math.min(total - 1, p));
      setPage(clamped);
      if (colRef.current) colRef.current.style.transform = `translateX(-${clamped * pageStep()}px)`;
      const frac = total > 1 ? clamped / (total - 1) : 0;
      setProgress(frac);
      save(frac, firstBlockOnPage(clamped));
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [pageCount, save]
  );

  // ---- scroll handler ----
  const onScroll = () => {
    if (paginated) return;
    const el = scrollRef.current;
    if (!el) return;
    const frac = el.scrollTop / Math.max(1, el.scrollHeight - el.clientHeight);
    setBarWidth(frac);            // direct DOM write + throttled state — no per-frame render (PERF)
    save(frac, firstVisibleBlock); // pass the thunk: layout read deferred into the 500ms debounce
  };

  // ---- restore position when content loads / mode changes ----
  useEffect(() => {
    // Wait until progress has actually loaded — otherwise an early run (prog.data still
    // undefined) would fall into the else-branch and save(0), clobbering the saved spot.
    if (!chapter.data || !prog.isSuccess) return;
    const el = scrollRef.current;
    if (!el) return;
    const savedHere = prog.data?.last_chapter_id === chapter.data.chapter_id;
    const wantRestore = savedHere && restoredFor.current !== chapter.data.chapter_id;
    const frac = wantRestore ? prog.data!.scroll_fraction : 0;
    const para = wantRestore ? prog.data!.paragraph_index ?? 0 : 0;

    requestAnimationFrame(() => {
      if (paginated) {
        requestAnimationFrame(() => {
          const total = setupPagination();
          setPageCount(total);
          const target = para > 0 ? blockPage(para) : Math.round(frac * (total - 1));
          goToPage(target, total);   // pass the fresh total, not the stale pageCount state
        });
      } else {
        const bs = blocks();
        if (para > 0 && bs[para]) {
          // Land precisely on the saved paragraph.
          const delta = bs[para].getBoundingClientRect().top - el.getBoundingClientRect().top;
          el.scrollTop += delta;
        } else {
          el.scrollTop = frac * (el.scrollHeight - el.clientHeight);
        }
        setProgress(el.scrollTop / Math.max(1, el.scrollHeight - el.clientHeight));
      }
    });
    if (wantRestore) {
      restoredFor.current = chapter.data.chapter_id;
    } else if (!savedHere && restoredFor.current !== chapter.data.chapter_id) {
      // Genuinely a different chapter than the saved one → start at the top (once).
      restoredFor.current = chapter.data.chapter_id;
      save(0);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chapter.data, prog.data, prog.isSuccess, paginated]);

  useEffect(() => {
    const onResize = () => recomputePages();
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [recomputePages]);

  // ---- navigation ----
  const goPrevChapter = useCallback(() => {
    if (chapter.data?.prev_chapter_id) navigate(`/read/${wid}/${chapter.data.prev_chapter_id}`);
  }, [chapter.data, wid, navigate]);
  const goNextChapter = useCallback(() => {
    if (chapter.data?.next_chapter_id) navigate(`/read/${wid}/${chapter.data.next_chapter_id}`);
  }, [chapter.data, wid, navigate]);

  const forward = useCallback(() => {
    if (isComic) { comicNav.current?.next(); return; }
    if (paginated) {
      if (page < pageCount - 1) goToPage(page + 1);
      else goNextChapter();
    } else goNextChapter();
  }, [isComic, paginated, page, pageCount, goToPage, goNextChapter]);
  const backward = useCallback(() => {
    if (isComic) { comicNav.current?.prev(); return; }
    if (paginated) {
      if (page > 0) goToPage(page - 1);
      else goPrevChapter();
    } else goPrevChapter();
  }, [isComic, paginated, page, goToPage, goPrevChapter]);

  // ---- immersive / focus mode (full-screen, text only) ----
  const enterImmersive = useCallback(() => {
    setShowControls(false);
    setImmersive(true);
    const el = document.documentElement as any;
    (el.requestFullscreen?.() ?? el.webkitRequestFullscreen?.())?.catch?.(() => {});
  }, []);
  const exitImmersive = useCallback(() => {
    setImmersive(false);
    if (document.fullscreenElement) document.exitFullscreen?.().catch(() => {});
  }, []);

  // Keep state in sync if the user leaves fullscreen via the browser (Esc / gesture).
  useEffect(() => {
    const onFs = () => {
      if (!document.fullscreenElement) setImmersive(false);
    };
    document.addEventListener("fullscreenchange", onFs);
    return () => document.removeEventListener("fullscreenchange", onFs);
  }, []);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (showControls || showToc) return;
      if (e.key === "ArrowRight") forward();
      else if (e.key === "ArrowLeft") backward();
      else if (e.key === " " && paginated) { e.preventDefault(); forward(); }
      else if (e.key === "j") scrollRef.current?.scrollBy({ top: 140, behavior: "smooth" });
      else if (e.key === "k") scrollRef.current?.scrollBy({ top: -140, behavior: "smooth" });
      else if ((e.key === "+" || e.key === "=") && isComic) comicNav.current?.zoomIn();
      else if (e.key === "-" && isComic) comicNav.current?.zoomOut();
      else if (e.key === "0" && isComic) comicNav.current?.resetZoom();
      else if (e.key === "t") setShowToc(true);
      else if (e.key === "h") setChromeHidden((s) => !s);
      else if (e.key === "f") immersive ? exitImmersive() : enterImmersive();
      else if (e.key === "Escape" && immersive) exitImmersive();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [forward, backward, paginated, isComic, showControls, showToc, immersive, enterImmersive, exitImmersive]);

  const hideChrome = chromeHidden || immersive;

  // Desktop? (panel anchors to the FAB on desktop; bottom-sheet on mobile)
  const [isDesktop, setIsDesktop] = useState(
    typeof window !== "undefined" ? window.innerWidth >= 640 : true
  );
  useEffect(() => {
    const mq = window.matchMedia("(min-width: 640px)");
    const on = () => setIsDesktop(mq.matches);
    mq.addEventListener?.("change", on);
    return () => mq.removeEventListener?.("change", on);
  }, []);

  // Settings panel position: anchored next to the floating control on desktop,
  // full-width bottom sheet on mobile.
  const panelStyle: React.CSSProperties = (() => {
    if (!isDesktop)
      return {
        left: 8,
        right: 8,
        bottom: "max(8px, env(safe-area-inset-bottom))",
        maxHeight: "min(82vh, 82dvh)", // dvh tracks the mobile URL bar so the sheet isn't clipped
      };
    // Anchor the panel at the top-right, just under the "Aa" settings button now in the top bar.
    return {
      width: "21rem",
      top: "calc(env(safe-area-inset-top) + 3.25rem)",
      right: "0.5rem",
    } as React.CSSProperties;
  })();

  // Text/background colors: a lightness slider tunes the theme color's L while
  // keeping its hue+saturation; null means follow the theme as-is.
  const tk = tokensFor(theme);
  // Work mode ("disguise"): an office skin overrides theme colors + font so the
  // reader looks like documentation / a business article / an email.
  const workMode = (prefs.workMode ?? "off") as WorkMode;
  const disguised = workMode !== "off";
  const skin = disguised ? DISGUISE_SKINS[workMode as Exclude<WorkMode, "off">] : null;
  // Restructure the prose itself (not just the chrome) so it reads like docs/article/email.
  const bodyHtml = useMemo(
    () => (disguised && chapter.data
      ? disguiseBody(chapter.data.html, workMode as Exclude<WorkMode, "off">)
      : chapter.data?.html ?? ""),
    [chapter.data?.html, disguised, workMode]
  );
  const textColor =
    skin ? skin.text
    : prefs.textLightness != null ? colorWithLightness(tk.text, prefs.textLightness)
    : prefs.textColor || undefined;
  const bgColor =
    skin ? skin.bg
    : prefs.bgLightness != null ? colorWithLightness(tk.bg, prefs.bgLightness)
    : prefs.bgColor || tk.bg;  // fall back to the theme bg so the reader surface always
                               // follows the selected color mode (incl. behind comic pages)

  // Is the reader's actual reading surface dark? Drives the floating controls' colours so they
  // contrast with the page even when the reader brightness diverges from the app theme.
  const readerDark =
    skin ? false
    : prefs.bgLightness != null ? prefs.bgLightness < 50
    : prefs.bgColor ? hexToHsl(prefs.bgColor).l < 50
    : hexToHsl(tk.bg).l < 50;

  // Colour directly beneath the status bar: the chrome bar when it's showing, the reading
  // surface when it's hidden. Used for both the safe-area fill strip (standalone) and the
  // theme-color meta (regular Safari tab) so the top always matches the reader.
  const topColor = hideChrome ? bgColor : skin?.panel ?? tk.surface;
  useEffect(() => {
    setThemeColor(topColor);
    return () => setThemeColor(tk.surface);
  }, [topColor, tk.surface]);

  // Position to restore the comic viewer to (only when the saved spot is this chapter).
  const comicRestore = useMemo(() => {
    if (!isComic || !chapter.data || !prog.isSuccess) return null;
    if (prog.data?.last_chapter_id === chapter.data.chapter_id) {
      return { fraction: prog.data.scroll_fraction, index: prog.data.paragraph_index ?? 0 };
    }
    return null;
  }, [isComic, chapter.data, prog.data, prog.isSuccess]);

  // Horizontal placement of the text column (0=left … 50=center … 100=right):
  // distribute the free space (viewport − measure) between the two margins.
  const hpos = Math.max(0, Math.min(100, prefs.textPosition ?? 50)) / 100;
  const freeW = `(100% - min(${prefs.measure}rem, 100%))`;
  const proseStyle: React.CSSProperties = {
    ["--reader-font-size" as any]: `${prefs.fontSize}px`,
    ["--reader-line-height" as any]: prefs.lineHeight,
    ["--reader-letter-spacing" as any]: `${prefs.letterSpacing}px`,
    ["--reader-font-family" as any]: skin?.fontStack ?? FONT_STACKS[prefs.fontFamily] ?? FONT_STACKS.serif,
    ["--reader-measure" as any]: `${prefs.measure}rem`,
    ["--reader-para-spacing" as any]: `${prefs.paragraphSpacing}em`,
    ["--reader-align" as any]: prefs.justify ? "justify" : "left",
    ["--reader-text-color" as any]: textColor,
    ...(!paginated
      ? {
          marginLeft: `calc(${freeW} * ${hpos})`,
          marginRight: `calc(${freeW} * ${1 - hpos})`,
        }
      : {}),
  };
  const readingMinutes = chapter.data ? Math.max(1, Math.round(chapter.data.word_count / 220)) : 0;

  // Work failed to load → a clear error with a way back, instead of a blank title bar + empty surface.
  if (work.isError) {
    return (
      <div className="fixed inset-0 flex flex-col items-center justify-center gap-4 p-6 text-center"
           style={{ background: bgColor, color: textColor }}>
        <p className="text-lg font-medium">Couldn’t load this title</p>
        <p className="max-w-sm text-sm opacity-70">
          {(work.error as Error)?.message || "Something went wrong fetching this work."}
        </p>
        <div className="flex gap-2">
          <Button variant="primary" onClick={() => work.refetch()}>Retry</Button>
          <Button variant="ghost" onClick={() => navigate("/")}>Back to library</Button>
        </div>
      </div>
    );
  }

  // Audiobooks have no in-app reader — point to the download instead of a blank reader.
  if (work.data?.media_kind === "audio") {
    return (
      <div className="fixed inset-0 flex flex-col items-center justify-center gap-4 p-6 text-center"
           style={{ background: bgColor, color: textColor }}>
        <p className="text-lg font-medium">{work.data.title}</p>
        <p className="max-w-sm text-sm opacity-70">This is an audiobook — download it to listen.</p>
        <div className="flex gap-2">
          <a href={api.audioUrl(wid)} download
             className="rounded-lg bg-accent px-4 py-2 text-sm font-medium text-accent-fg">⤓ Download audiobook</a>
          <Button variant="ghost" onClick={() => navigate("/")}>Back to library</Button>
        </div>
      </div>
    );
  }

  return (
    <div className="fixed inset-0 flex flex-col" style={{ background: bgColor }}>
      {/* Solid fill for the iOS status-bar region (standalone draws full-bleed under it).
          Matches the chrome bar when it's showing, the reading surface when it's hidden, so the
          colour scheme always reaches the very top. */}
      <div
        aria-hidden
        className="pointer-events-none fixed inset-x-0 top-0 z-30"
        style={{ height: "env(safe-area-inset-top)", background: topColor }}
      />

      {/* progress bar (hidden in immersive/focus mode) */}
      {!immersive && (
        <div className="pointer-events-none absolute left-0 top-0 z-30 h-[3px] w-full bg-transparent">
          <div ref={barRef} className="h-full bg-accent" style={{ width: `${progress * 100}%` }} />
        </div>
      )}

      {/* top bar — removed from layout when chrome is hidden, so text goes full-screen */}
      {!hideChrome && (
        <div
          className="flex items-center gap-1 border-b border-border bg-surface px-2 py-2 sm:gap-2 sm:px-3"
          style={
            skin
              ? { background: skin.panel, borderColor: skin.border, color: skin.text,
                  paddingTop: "max(0.5rem, env(safe-area-inset-top))" }
              : { paddingTop: "max(0.5rem, env(safe-area-inset-top))" }
          }
        >
          <Button variant="ghost" size="sm" onClick={() => navigate(`/`)}>←</Button>
          {disguised ? (
            // Camouflaged: a neutral, work-looking title bar (no book/chapter names).
            <div className="mx-1 min-w-0 flex-1 truncate text-sm font-medium" style={{ color: skin!.muted }}>
              {workMode === "docs" ? "Documentation" : workMode === "article" ? "Reader" : "Inbox"}
            </div>
          ) : (
            <>
              <div className="mx-1 min-w-0 flex-1 truncate text-sm text-muted">
                <span className="text-text">{work.data?.title}</span>
                {chapter.data ? ` · ${chapter.data.title}` : ""}
              </div>
              {chapter.data && (
                <span className="hidden text-xs text-muted sm:inline">{readingMinutes} min · {chapter.data.word_count} w</span>
              )}
            </>
          )}
          {/* Reading settings ("Aa") — top-right of the bar, beside the word-count/minute estimate. */}
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setShowControls((s) => !s)}
            title="Reading settings"
            aria-label="Reading settings"
            className="shrink-0 font-semibold"
          >
            Aa
          </Button>
        </div>
      )}

      {/* content */}
      {isComic && chapter.data ? (
        <ComicReader
          ref={comicNav}
          html={chapter.data.html}
          bgColor={bgColor}
          restore={comicRestore}
          onProgress={(frac, index) => { setBarWidth(frac); save(frac, index); }}
          onPrevChapter={goPrevChapter}
          onNextChapter={goNextChapter}
          hasPrev={!!chapter.data.prev_chapter_id}
          hasNext={!!chapter.data.next_chapter_id}
          chromeHidden={hideChrome}
          onToggleChrome={() => setChromeHidden((s) => !s)}
        />
      ) : (
      <div
        ref={scrollRef}
        onScroll={onScroll}
        className={`scrollbar-thin relative flex-1 overscroll-contain ${paginated ? "overflow-hidden" : "overflow-y-auto"}`}
      >
        {/* tap zones (paginated) */}
        {paginated && chapter.data && (
          <>
            <button aria-label="Previous page" onClick={backward}
              className="absolute left-0 top-0 z-20 h-full w-[22%] cursor-w-resize" />
            <button aria-label="Next page" onClick={forward}
              className="absolute right-0 top-0 z-20 h-full w-[22%] cursor-e-resize" />
            <button aria-label="Toggle bars" onClick={() => setChromeHidden((s) => !s)}
              className="absolute left-[22%] top-0 z-10 h-full w-[56%]" />
          </>
        )}

        <div className={paginated ? "h-full px-6 py-8" : "px-5 py-10 sm:py-14"}>
          {chapter.isLoading && <p className="mx-auto max-w-[38rem] text-muted">Loading chapter…</p>}
          {chapter.isError && (
            <div className="mx-auto max-w-[38rem] text-center text-muted">
              <p>This chapter hasn’t been fetched yet — the slow crawler may still be working.</p>
              <Link to="/jobs" className="text-accent underline">Check crawl jobs</Link>
            </div>
          )}
          {chapter.data && (
            <div
              className={paginated ? "mx-auto h-full overflow-hidden" : "mx-auto"}
              style={{
                maxWidth: paginated ? `${prefs.measure}rem` : disguised ? "44rem" : undefined,
                ...(disguised && !paginated && workMode === "email"
                  ? { background: skin!.panel, border: `1px solid ${skin!.border}`,
                      borderRadius: "12px", padding: "1.5rem 1.5rem 2rem",
                      boxShadow: "0 1px 3px rgba(0,0,0,0.06)" }
                  : {}),
              }}
            >
              {/* Camouflage chrome (scroll mode only — paginated keeps the column geometry). */}
              {disguised && !paginated && (
                <DisguiseHeader
                  mode={workMode as Exclude<WorkMode, "off">}
                  workTitle={work.data?.title ?? "Project"}
                  chapterTitle={chapter.data.title}
                  minutes={readingMinutes}
                />
              )}
              <article
                ref={colRef}
                className={`reader-prose${disguised ? ` wm-${workMode}` : ""}`}
                style={{
                  ...proseStyle,
                  ...(disguised ? { marginLeft: 0, marginRight: 0 } : {}),
                  // Comics: full-width centered image column, ignoring the prose measure +
                  // text-position margins so manga/webtoon pages aren't squashed to ~38rem.
                  ...(isComic
                    ? { maxWidth: "60rem", marginLeft: "auto", marginRight: "auto" }
                    : {}),
                  ...(paginated
                    ? { height: "100%", columnGap: "0", columnFill: "auto",
                        transition: "transform 0.25s ease", maxWidth: "none" }
                    : {}),
                }}
                dangerouslySetInnerHTML={{ __html: bodyHtml }}
              />
            </div>
          )}

          {chapter.data && !paginated && (
            <div className="mx-auto mt-12 flex max-w-[38rem] items-center justify-between border-t border-border pt-6">
              <Button onClick={goPrevChapter} disabled={!chapter.data.prev_chapter_id}>← Previous</Button>
              <span className="text-xs text-muted">end of chapter</span>
              <Button variant="primary" onClick={goNextChapter} disabled={!chapter.data.next_chapter_id}>Next →</Button>
            </div>
          )}
        </div>
      </div>
      )}

      {/* page indicator (paginated) */}
      {paginated && chapter.data && !hideChrome && (
        <div className="border-t border-border bg-surface px-3 py-1.5 text-center text-xs text-muted">
          Page {page + 1} / {pageCount}
        </div>
      )}

      {/* movable floating controls */}
      {!hideChrome && (
        <ReaderFab
          onToc={() => setShowToc(true)}
          onFocus={enterImmersive}
          onPrev={backward}
          onNext={forward}
          dark={readerDark}
        />
      )}

      {/* immersive: unobtrusive, always-available exit */}
      {immersive && (
        <button
          onClick={exitImmersive}
          title="Exit focus mode (Esc)"
          aria-label="Exit focus mode"
          className="fixed right-3 top-3 z-40 flex h-10 w-10 items-center justify-center rounded-full bg-surface text-text opacity-40 shadow transition hover:opacity-100"
          style={{ top: "max(0.75rem, env(safe-area-inset-top))" }}
        >
          ✕
        </button>
      )}

      {showControls && (
        <ReaderControls
          onClose={() => setShowControls(false)}
          onFocus={enterImmersive}
          panelStyle={panelStyle}
          isComic={isComic}
          onCleanChapter={resolvedChapterId ? () => cleanChapterM.mutate() : undefined}
          onCleanWork={() => cleanWorkM.mutate()}
          cleaning={cleanChapterM.isPending || cleanWorkM.isPending}
          cleanNote={cleanNote}
        />
      )}
      {showToc && (
        <TocDrawer
          workId={wid}
          currentChapterId={resolvedChapterId}
          onClose={() => setShowToc(false)}
          onPick={(id) => { setShowToc(false); navigate(`/read/${wid}/${id}`); }}
        />
      )}
    </div>
  );
}

