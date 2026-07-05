import { Suspense, lazy, useEffect, useState, type ReactElement } from "react";
import { useTranslation } from "react-i18next";
import { Link, NavLink, Navigate, Route, Routes, useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "./api/client";
import { qk } from "./api/queryKeys";
import { useApp } from "./store";
import { useAuth, useCurrentUser, useHasPermission, useIsAdmin } from "./auth";
import { THEME_MAP } from "./themes";
import ThemePicker from "./components/ThemePicker";
import { NotificationBell } from "./components/NotificationBell";
import { AuthSpinner, Forgot, Login, Register, Reset, Setup } from "./components/AuthGate";
import { Skeleton, useEscapeClose } from "./components/ui";
// Route destinations are code-split so admin-only pages (Settings/Users/Jobs)
// don't ship in the main bundle for users who can't reach them.
const Library = lazy(() => import("./pages/Library"));
const BrowseLibrary = lazy(() => import("./pages/BrowseLibrary"));
const Reader = lazy(() => import("./pages/Reader"));
const SourcesHub = lazy(() => import("./pages/SourcesHub"));
const Settings = lazy(() => import("./pages/Settings"));
const AddPage = lazy(() => import("./pages/AddWork"));
// Add-flow modals reused by the nav "+" menu (and the /add tabs). Static import so the "+" popup
// opens instantly — these are global add entry points, not a heavy code-split page.
import { AddByUrlModal, UploadFilesModal } from "./pages/AddWork";
const IndexPage = lazy(() => import("./pages/Index"));
const BrowseCatalog = lazy(() => import("./pages/BrowseCatalog"));
const BrowseAudiobooks = lazy(() => import("./pages/BrowseAudiobooks"));
const Wanted = lazy(() => import("./pages/Wanted"));
import { AddListModal } from "./pages/ListImports";
import Toaster from "./components/Toaster";
import { applyAccentBackdrop, initAmbientMotion } from "./lib/coverBackdrop";
import AudioPlayer from "./components/AudioPlayer";
import { useAudio } from "./audioStore";
import { ConfirmProvider } from "./components/confirm";
import { ShelfPromptProvider } from "./components/ShelfPrompt";

// Inline stroke icons for the chrome (mobile tab bar, "More" sheet, popovers) — monochrome,
// currentColor-driven, matching the desktop nav/button SVG style (stroke-width ~2, round caps) so
// active=accent / inactive=muted falls out for free. No icon dependency by design.
function Ico({ d, size = 22 }: { d: React.ReactNode; size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {d}
    </svg>
  );
}
const NavIcon = {
  library: <Ico d={<><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" /><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" /></>} />,
  discover: <Ico d={<><circle cx="12" cy="12" r="10" /><path d="m15.5 8.5-3 5.5-5.5 1.5 3-5.5 5.5-1.5z" /></>} />,
  // Bookmark/flag — "titles you want" (distinct from the Discover compass + Library book).
  wanted: <Ico d={<path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z" />} />,
  settings: <Ico d={<><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" /></>} />,
  more: <Ico d={<><circle cx="5" cy="12" r="1" fill="currentColor" /><circle cx="12" cy="12" r="1" fill="currentColor" /><circle cx="19" cy="12" r="1" fill="currentColor" /></>} />,
  add: <Ico size={18} d={<path d="M12 5v14M5 12h14" />} />,
  sources: <Ico size={18} d={<path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18v3h3l6.3-6.3a4 4 0 0 0 5.4-5.4l-2.6 2.6-2-2 2.6-2.6z" />} />,
  users: <Ico size={18} d={<><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" /><circle cx="9" cy="7" r="4" /><path d="M22 21v-2a4 4 0 0 0-3-3.87" /><path d="M16 3.13a4 4 0 0 1 0 7.75" /></>} />,
} as const;

// Inline icons for the desktop popovers (Add "+" menu, Account menu) — drop-in for the old emoji.
const PopIcon = {
  search: <Ico size={17} d={<><circle cx="11" cy="11" r="7" /><path d="m21 21-4.3-4.3" /></>} />,
  link: <Ico size={17} d={<><path d="M10 13a5 5 0 0 0 7.07 0l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" /><path d="M14 11a5 5 0 0 0-7.07 0l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" /></>} />,
  importList: <Ico size={17} d={<><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><path d="M7 10l5 5 5-5" /><path d="M12 15V3" /></>} />,
  upload: <Ico size={17} d={<><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><path d="M17 8l-5-5-5 5" /><path d="M12 3v12" /></>} />,
  wanted: <Ico size={16} d={<path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z" />} />,
  settings: <Ico size={16} d={<><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" /></>} />,
  users: <Ico size={16} d={<><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" /><circle cx="9" cy="7" r="4" /><path d="M22 21v-2a4 4 0 0 0-3-3.87" /><path d="M16 3.13a4 4 0 0 1 0 7.75" /></>} />,
  account: <Ico size={16} d={<><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" /><circle cx="12" cy="7" r="4" /></>} />,
  signout: <Ico size={16} d={<><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" /><path d="M16 17l5-5-5-5" /><path d="M21 12H9" /></>} />,
} as const;

function ThemeButton() {
  const { t } = useTranslation();
  const { theme } = useApp();
  const [open, setOpen] = useState(false);
  useEscapeClose(open, () => setOpen(false));
  const name = theme === "system" ? t("nav.themeSystem") : THEME_MAP[theme]?.name ?? t("nav.theme");
  return (
    <div className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        title={`${t("nav.theme")} — ${name}`}
        aria-label={t("nav.theme")} aria-haspopup="menu" aria-expanded={open}
        className="flex h-[38px] w-[38px] items-center justify-center rounded-[11px] border border-[var(--hair,var(--border))] bg-surface text-text transition hover:bg-surface-2"
      >
        {/* Palette (inline SVG, inherits currentColor + theme accent — emoji didn't). */}
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <circle cx="13.5" cy="6.5" r=".5" fill="currentColor" />
          <circle cx="17.5" cy="10.5" r=".5" fill="currentColor" />
          <circle cx="8.5" cy="7.5" r=".5" fill="currentColor" />
          <circle cx="6.5" cy="12.5" r=".5" fill="currentColor" />
          <path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10c.926 0 1.648-.746 1.648-1.688 0-.437-.18-.835-.437-1.125-.29-.289-.438-.652-.438-1.125a1.64 1.64 0 0 1 1.668-1.668h1.996c3.051 0 5.555-2.503 5.555-5.555C21.965 6.012 17.461 2 12 2z" />
        </svg>
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          {/* Viewport-anchored on mobile (full-width below the bar) so the wide picker can't run off
              a phone edge; trigger-anchored on desktop. */}
          <div className="sp-pop fixed inset-x-2 top-[calc(env(safe-area-inset-top)_+_3.75rem)] z-50 rounded-[15px] border border-[var(--hair-strong,var(--border))] bg-surface p-3 shadow-[var(--pop-shadow)] sm:absolute sm:inset-x-auto sm:right-0 sm:top-full sm:mt-2 sm:w-72">
            <ThemePicker columns={3} />
          </div>
        </>
      )}
    </div>
  );
}

function UserButton() {
  const { t } = useTranslation();
  const user = useCurrentUser();
  const { refresh } = useAuth();
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  useEscapeClose(open, () => setOpen(false));
  async function logout() {
    await api.logout().catch(() => {});
    qc.clear();
    await refresh();
  }
  return (
    <div className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        title={t("nav.account")} aria-label={t("nav.account")} aria-haspopup="menu" aria-expanded={open}
        className="flex h-[38px] items-center gap-1.5 rounded-[11px] border border-[var(--hair,var(--border))] bg-surface pl-1.5 pr-2 transition hover:bg-surface-2"
      >
        <span className="flex h-[26px] w-[26px] items-center justify-center rounded-lg bg-gradient-to-br from-[var(--accent)] to-[color-mix(in_srgb,var(--accent)_50%,#000)] text-[13px] font-semibold text-accent-fg">
          {(user?.display_name || user?.username || "?")[0]?.toUpperCase()}
        </span>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" className="text-muted"><path d="m6 9 6 6 6-6" /></svg>
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div className="sp-pop absolute right-0 z-50 mt-2 w-52 max-w-[calc(100vw-1rem)] rounded-[15px] border border-[var(--hair-strong,var(--border))] bg-surface p-1.5 shadow-[var(--pop-shadow)]">
            <div className="flex items-center gap-3 px-2.5 py-2">
              <span className="flex h-[38px] w-[38px] items-center justify-center rounded-[11px] bg-gradient-to-br from-[var(--accent)] to-[color-mix(in_srgb,var(--accent)_50%,#000)] text-base font-semibold text-accent-fg">
                {(user?.display_name || user?.username || "?")[0]?.toUpperCase()}
              </span>
              <span className="min-w-0">
                <span className="block truncate text-sm font-bold text-text">{user?.display_name || user?.username}</span>
                <span className="block text-xs text-muted">{user?.role === "admin" ? t("nav.administrator") : t("nav.reader")}</span>
              </span>
            </div>
            <div className="my-1 h-px bg-[var(--hair,var(--border))]" />
            <Link to="/settings#account" onClick={() => setOpen(false)}
              className="flex w-full items-center gap-2.5 rounded-[9px] px-2.5 py-2 text-sm font-medium text-text transition hover:bg-surface-2"><span className="shrink-0 text-muted">{PopIcon.account}</span>{t("nav.account")}</Link>
            <Link to="/wanted" onClick={() => setOpen(false)}
              className="flex w-full items-center gap-2.5 rounded-[9px] px-2.5 py-2 text-sm font-medium text-text transition hover:bg-surface-2"><span className="shrink-0 text-muted">{PopIcon.wanted}</span>{t("nav.myWanted")}</Link>
            <Link to="/settings" onClick={() => setOpen(false)}
              className="flex w-full items-center gap-2.5 rounded-[9px] px-2.5 py-2 text-sm font-medium text-text transition hover:bg-surface-2"><span className="shrink-0 text-muted">{PopIcon.settings}</span>{t("nav.settings")}</Link>
            <button onClick={logout}
              className="flex w-full items-center gap-2.5 rounded-[9px] px-2.5 py-2 text-left text-sm font-medium text-text transition hover:bg-surface-2"><span className="shrink-0 text-muted">{PopIcon.signout}</span>{t("nav.signOut")}</button>
          </div>
        </>
      )}
    </div>
  );
}

// Route-level Suspense fallback: a neutral header + block skeleton while a code-split page loads,
// instead of a lonely corner spinner — reads as instant, not broken. Kept generic (not a poster
// wall) so it fits non-grid pages like Settings/Reader/Jobs as well as the Library.
function RouteFallback() {
  return (
    <main className="mx-auto max-w-6xl px-4 py-8">
      <Skeleton className="mb-2 h-3 w-24" />
      <Skeleton className="mb-6 h-8 w-48" />
      <div className="space-y-4">
        {Array.from({ length: 6 }).map((_, i) => (
          <Skeleton key={i} className="h-20 w-full rounded-xl" />
        ))}
      </div>
    </main>
  );
}

// The "+" Add popover: entry points to the add flows. Each item opens its OWN popup modal in place
// (no route/selection awareness) — except "Search & request", which jumps to Discover. The modals
// reuse the same shared add logic as the /add tabs (attestation gate, crawl policy, import) so the
// page and popup can never drift.
type AddModal = "url" | "list" | "upload";
function AddMenu() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const canAdd = useHasPermission("add.use");
  const canSources = useHasPermission("sources.view");
  const [open, setOpen] = useState(false);
  const [modal, setModal] = useState<AddModal | null>(null);
  useEscapeClose(open, () => setOpen(false));
  if (!(canAdd || canSources)) return null;
  const items: { icon: ReactElement; label: string; desc: string; action: () => void }[] = [
    { icon: PopIcon.search, label: t("nav.addSearch"), desc: t("nav.addSearchDesc"), action: () => navigate("/discover") },
    { icon: PopIcon.link, label: t("nav.addUrl"), desc: t("nav.addUrlDesc"), action: () => setModal("url") },
    { icon: PopIcon.importList, label: t("nav.addList"), desc: t("nav.addListDesc"), action: () => setModal("list") },
    { icon: PopIcon.upload, label: t("nav.addUpload"), desc: t("nav.addUploadDesc"), action: () => setModal("upload") },
  ];
  return (
    <div className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        title={t("nav.add")} aria-label={t("nav.add")} aria-haspopup="menu" aria-expanded={open}
        className="flex h-[38px] w-[38px] items-center justify-center rounded-[11px] border border-[var(--hair,var(--border))] bg-surface text-text transition hover:bg-surface-2"
      >
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.1" strokeLinecap="round"><path d="M12 5v14M5 12h14" /></svg>
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div className="sp-pop fixed inset-x-2 top-[calc(env(safe-area-inset-top)_+_3.75rem)] z-50 rounded-[15px] border border-[var(--hair-strong,var(--border))] bg-surface p-1.5 shadow-[var(--pop-shadow)] sm:absolute sm:inset-x-auto sm:right-0 sm:top-12 sm:w-64">
            <div className="px-2.5 py-2 text-[11px] font-bold uppercase tracking-wider text-muted">{t("nav.addToShelf")}</div>
            {items.map((m) => (
              <button
                key={m.label}
                onClick={() => { setOpen(false); m.action(); }}
                className="flex w-full items-center gap-3 rounded-[10px] p-2.5 text-left transition hover:bg-surface-2"
              >
                <span className="flex h-[34px] w-[34px] shrink-0 items-center justify-center rounded-[9px] bg-surface-2 text-[var(--accent-bright,var(--accent))]">{m.icon}</span>
                <span className="min-w-0">
                  <span className="block text-[13.5px] font-semibold text-text">{m.label}</span>
                  <span className="block text-xs text-muted">{m.desc}</span>
                </span>
              </button>
            ))}
          </div>
        </>
      )}
      {modal === "url" && <AddByUrlModal onClose={() => setModal(null)} />}
      {modal === "list" && <AddListModal onClose={() => setModal(null)} />}
      {modal === "upload" && <UploadFilesModal onClose={() => setModal(null)} />}
    </div>
  );
}

function useDebounced<T>(value: T, ms = 250): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

// The ONE search box (top nav). It drives the Library ("/") and Discover ("/discover") grids via the
// shared ?q= URL param. On those routes typing live-updates ?q= (debounced, replace — no history
// spam); on any other route, Enter sends you to /discover?q=<text>. Each route keeps its own ?q=, so
// switching pages restores that page's search; the input resets to the visited route's ?q= on nav.
function NavSearch() {
  const { t } = useTranslation();
  const { pathname } = useLocation();
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();
  const searchable = pathname === "/" || pathname === "/library/browse" || pathname === "/discover";
  const [q, setQ] = useState(() => searchParams.get("q") ?? "");
  // Reflect the visited route's current ?q= whenever the route changes (Library & Discover keep
  // independent searches). Keyed on pathname only — typing must not be clobbered by this.
  useEffect(() => {
    setQ(new URLSearchParams(window.location.search).get("q") ?? "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pathname]);
  const debounced = useDebounced(q.trim());
  // On searchable routes, write the debounced text to ?q= (replace). Functional updater reads the
  // live URL so it never depends on searchParams (which would re-trigger); preserves every other
  // param (?detail, mode, filters). Guard the redundant same-value write. Keyed on debounced only.
  useEffect(() => {
    if (!searchable) return;
    setSearchParams(
      (prev) => {
        const cur = prev.get("q") ?? "";
        if (cur === debounced) return prev;
        const next = new URLSearchParams(prev);
        if (debounced) next.set("q", debounced);
        else next.delete("q");
        return next;
      },
      { replace: true },
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [debounced, searchable]);
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        // On a non-search route, Enter jumps to Discover with the query (no live nav while typing).
        if (!searchable) navigate(`/discover?q=${encodeURIComponent(q.trim())}`);
      }}
      className="relative flex w-[150px] min-w-0 items-center sm:w-[200px] md:w-[240px]"
    >
      <span className="pointer-events-none absolute left-3 text-muted">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><circle cx="11" cy="11" r="7" /><path d="m21 21-4.3-4.3" /></svg>
      </span>
      <input
        type="search"
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder={t("nav.searchPlaceholder")}
        aria-label={t("nav.searchAria")}
        className="h-[38px] w-full rounded-[11px] border border-[var(--hair,var(--border))] bg-surface pl-9 pr-3 text-[13.5px] text-text transition placeholder:text-muted focus:border-[color-mix(in_srgb,var(--accent)_50%,var(--border))] focus:outline-none"
      />
    </form>
  );
}

function Nav() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const isAdmin = useIsAdmin();
  const canIndex = useHasPermission("index.view");
  const canJobs = useHasPermission("jobs.view");
  const canSources = useHasPermission("sources.view");
  const canOperate = canJobs || canSources || isAdmin;
  // Warm the destination's primary list on hover so the page renders from cache on click (cached-first).
  const prefetch = (to: string) => {
    if (to === "/") qc.prefetchQuery({ queryKey: qk.works("", null), queryFn: () => api.listWorks() });
    else if (to === "/discover") qc.prefetchQuery({ queryKey: qk.catalogRows(), queryFn: () => api.catalogRows() });
  };
  // A center nav pill: active = full-strength text + a 2px accent underline; inactive = muted.
  const pill = (to: string, label: string, end = false) => (
    <NavLink
      to={to}
      end={end}
      onMouseEnter={() => prefetch(to)}
      className={({ isActive }) =>
        `relative shrink-0 whitespace-nowrap rounded-[9px] px-3.5 py-2 text-sm font-semibold transition ${
          isActive ? "text-text" : "text-muted hover:bg-surface-2 hover:text-text"
        }`
      }
    >
      {({ isActive }) => (
        <>
          {label}
          <span className={`absolute inset-x-3.5 bottom-[3px] h-0.5 rounded bg-accent transition-opacity ${isActive ? "opacity-100" : "opacity-0"}`} />
        </>
      )}
    </NavLink>
  );
  return (
    <header
      className="sticky top-0 z-30 border-b border-[var(--hair,var(--border))] bg-[var(--nav-bg,var(--surface))] [backdrop-filter:blur(18px)_saturate(1.4)]"
      style={{ paddingTop: "env(safe-area-inset-top)" }}
    >
      <div className="mx-auto flex h-16 max-w-6xl items-center gap-3 px-4 sm:px-6">
        <button onClick={() => navigate("/")} className="flex shrink-0 items-center gap-2.5" aria-label={t("nav.home")}>
          <span className="flex h-8 w-8 items-center justify-center rounded-[9px] bg-gradient-to-br from-[var(--accent)] to-[color-mix(in_srgb,var(--accent)_55%,#000)] text-accent-fg shadow-[0_4px_14px_color-mix(in_srgb,var(--accent)_45%,transparent)]">
            <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.1" strokeLinecap="round" strokeLinejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" /><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" /></svg>
          </span>
          <span className="font-display hidden text-[21px] font-semibold tracking-tight text-text sm:inline">Shelf</span>
        </button>
        {/* Center nav: ≥sm only; phones use the fixed bottom tab bar below. min-w-0 + scroll so a
            cramped sm–md width scrolls the pills instead of pushing the right-side icons off-screen. */}
        <nav className="ml-2 hidden min-w-0 items-center gap-1 overflow-x-auto scrollbar-none sm:flex">
          {pill("/", t("nav.library"), true)}
          {canIndex && pill("/discover", t("nav.discover"))}
          {pill("/wanted", t("nav.wanted"))}
          {canOperate && pill("/sources", t("nav.sources"))}
          {pill("/settings", t("nav.settings"))}
        </nav>
        <div className="flex-1" />
        <NavSearch />
        <div className="flex shrink-0 items-center gap-1.5">
          {/* Add is redundant on phones (it's in the bottom "More" sheet), so hide it there to give
              the search field room — it was being squeezed to "Sear…" by the crowded icon row. */}
          <span className="hidden sm:block"><AddMenu /></span>
          <NotificationBell />
          <ThemeButton />
          <UserButton />
        </div>
      </div>
    </header>
  );
}

// Fixed bottom tab bar for phones (< sm). The wrapping top-nav row is fine on a wide screen but
// stacks into ~5 rows on a phone, so on mobile the inline links are hidden (see Nav) and primary
// destinations move here. Permission gating mirrors the desktop nav exactly.
function MobileTabBar() {
  const { t } = useTranslation();
  const isAdmin = useIsAdmin();
  const canIndex = useHasPermission("index.view");
  const canAdd = useHasPermission("add.use");
  const canJobs = useHasPermission("jobs.view");
  const canSources = useHasPermission("sources.view");
  const canOpenAdd = canAdd || canSources;
  const [moreOpen, setMoreOpen] = useState(false);

  // Close the More sheet on Escape.
  useEffect(() => {
    if (!moreOpen) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setMoreOpen(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [moreOpen]);

  const tabCls = ({ isActive }: { isActive: boolean }) =>
    `flex flex-1 flex-col items-center justify-center gap-0.5 py-1.5 text-[11px] font-medium transition ${
      isActive ? "text-accent" : "text-muted hover:text-text"
    }`;
  const tab = (to: string, icon: ReactElement, label: string, end = false) => (
    <NavLink to={to} end={end} className={tabCls}>
      <span className="leading-none">{icon}</span>
      <span>{label}</span>
    </NavLink>
  );

  const canOperate = canJobs || canSources || isAdmin;
  // Remaining permitted destinations that don't fit the 5 primary tabs.
  const moreLinks: [string, ReactElement, string][] = [
    ...(canOpenAdd ? [["/add", NavIcon.add, t("nav.add")] as [string, ReactElement, string]] : []),
    ...(canOperate ? [["/sources", NavIcon.sources, t("nav.sources")] as [string, ReactElement, string]] : []),
    // Users management now lives under Settings → Users (admin sub-tab); /users redirects there.
  ];

  return (
    <>
      {moreOpen && (
        <div className="fixed inset-0 z-50 sm:hidden" aria-hidden={!moreOpen}>
          <div className="absolute inset-0 bg-black/40" onClick={() => setMoreOpen(false)} />
          <div
            className="absolute inset-x-0 bottom-0 rounded-t-2xl border-t border-border bg-surface p-2 shadow-2xl"
            style={{ paddingBottom: "max(0.5rem, env(safe-area-inset-bottom))" }}
          >
            <div className="mx-auto mb-2 h-1 w-10 rounded-full bg-border" />
            <div className="grid grid-cols-2 gap-1.5 p-1">
              {moreLinks.map(([to, icon, label]) => (
                <NavLink
                  key={to}
                  to={to}
                  onClick={() => setMoreOpen(false)}
                  className={({ isActive }) =>
                    `flex items-center gap-2.5 rounded-xl px-3 py-2.5 text-sm font-medium transition ${
                      isActive ? "bg-accent text-accent-fg" : "text-text hover:bg-surface-2"
                    }`
                  }
                >
                  <span className="shrink-0">{icon}</span>
                  {label}
                </NavLink>
              ))}
            </div>
          </div>
        </div>
      )}
      <nav
        aria-label={t("nav.primary")}
        className="fixed inset-x-0 bottom-0 z-40 flex items-stretch border-t border-border/60 bg-surface sm:hidden"
        style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
      >
        {tab("/", NavIcon.library, t("nav.library"), true)}
        {canIndex && tab("/discover", NavIcon.discover, t("nav.discover"))}
        {tab("/wanted", NavIcon.wanted, t("nav.wanted"))}
        {tab("/settings", NavIcon.settings, t("nav.settings"))}
        <button
          type="button"
          onClick={() => setMoreOpen((o) => !o)}
          aria-expanded={moreOpen}
          aria-label={t("nav.more")}
          className={`flex flex-1 flex-col items-center justify-center gap-0.5 py-1.5 text-[11px] font-medium transition ${
            moreOpen ? "text-accent" : "text-muted hover:text-text"
          }`}
        >
          <span className="leading-none">{NavIcon.more}</span>
          <span>{t("nav.more")}</span>
        </button>
      </nav>
    </>
  );
}

function AuthedApp() {
  const { load } = useApp();
  const theme = useApp((s) => s.theme);
  const location = useLocation();
  const isAdmin = useIsAdmin();
  const canIndex = useHasPermission("index.view");
  const canAdd = useHasPermission("add.use");
  const canJobs = useHasPermission("jobs.view");
  const canSources = useHasPermission("sources.view");
  const canOpenAdd = canAdd || canSources;
  useEffect(() => {
    load();
  }, [load]);
  // Seed the cover-backdrop vars to the theme accent on first paint + whenever the theme changes
  // (a no-op while a cover-derived backdrop is active, so it never stomps a hero's colours).
  useEffect(() => {
    applyAccentBackdrop();
  }, [theme]);
  // Drive the ambient background motion (idle drift + scroll shift). Idempotent.
  useEffect(() => {
    initAmbientMotion();
  }, []);

  const isReader = location.pathname.startsWith("/read/");
  const playerOpen = useAudio((st) => st.workId != null);
  const canOperate = canJobs || canSources || isAdmin;
  // Permission-gated pages: redirected to the library if the capability is missing.
  const need = (ok: boolean, el: ReactElement) => (ok ? el : <Navigate to="/" replace />);

  return (
    <ConfirmProvider>
    <ShelfPromptProvider>
    <div className="relative min-h-full">
      {/* Ambient shell glow (the "cinematic" backdrop): an absolute, full-DOCUMENT layer behind all
          content (-z-10) whose cover-derived colour blooms TILE down the page and SCROLL WITH the
          content — so the colour travels with you (no fixed band) and there's no seam at any depth.
          The reader paints its own bg, so it's skipped there. See `.ambient-layer` in index.css. */}
      {!isReader && <div aria-hidden className="ambient-layer" />}
      {/* Vignette + grain over the aurora for depth (fixed framing while the colour scrolls). */}
      {!isReader && <div aria-hidden className="ambient-depth" />}
      {/* Solid themed fill for the iOS status-bar / notch region in a standalone home-screen
          app (black-translucent draws the page full-bleed under the bar). Height is 0 in a
          normal browser, so it's invisible there. The reader paints its own (see Reader). */}
      {!isReader && (
        <div
          aria-hidden
          className="fixed inset-x-0 top-0 z-40 bg-surface"
          style={{ height: "env(safe-area-inset-top)" }}
        />
      )}
      {!isReader && <Nav />}
      {/* Reserve space on mobile so the fixed bottom tab bar never covers the last content.
          The reader paints full-bleed and hides the bar, so it's left untouched there. */}
      <div className={isReader ? undefined : playerOpen ? "pb-44 sm:pb-24" : "pb-24 sm:pb-0"}>
      <Suspense fallback={<RouteFallback />}>
      <Routes>
        <Route path="/" element={<Library />} />
        <Route path="/library/browse" element={<BrowseLibrary />} />
        <Route path="/wanted" element={<Wanted />} />
        {/* List imports merged into Sources — keep a redirect so old bookmarks/links resolve. */}
        <Route path="/imports" element={<Navigate to="/sources" replace />} />
        {/* Old pages folded into Wanted — keep redirects so bookmarks/links don't 404. */}
        <Route path="/watchlist" element={<Navigate to="/wanted" replace />} />
        <Route path="/missing" element={<Navigate to="/wanted" replace />} />
        <Route path="/following" element={<Navigate to="/wanted" replace />} />
        <Route path="/add" element={need(canOpenAdd, <AddPage />)} />
        {/* Catalog → Discover (renamed). Keep /index as a redirect so old bookmarks resolve. */}
        <Route path="/discover" element={need(canIndex, <IndexPage />)} />
        <Route path="/index" element={<Navigate to="/discover" replace />} />
        <Route path="/browse/:dimension/:value" element={need(canIndex, <BrowseCatalog />)} />
        <Route path="/audiobooks" element={need(canIndex, <BrowseAudiobooks />)} />
        {/* Sources & Acquisitions — the merged operator page (jobs + downloads + index + folders +
            imports). /jobs redirects here so old links resolve. */}
        <Route path="/sources" element={need(canOperate, <SourcesHub />)} />
        <Route path="/jobs" element={<Navigate to="/sources" replace />} />
        <Route path="/settings" element={<Settings />} />
        {/* Users management moved into Settings → Users (admin sub-tab); keep a redirect for old links. */}
        <Route path="/users" element={<Navigate to="/settings#users" replace />} />
        {/* Stocking folded into Sources — keep a redirect so old bookmarks/links resolve. */}
        <Route path="/stock" element={<Navigate to="/sources" replace />} />
        <Route path="/read/:workId" element={<Reader />} />
        <Route path="/read/:workId/:chapterId" element={<Reader />} />
      </Routes>
      </Suspense>
      </div>
      {!isReader && <MobileTabBar />}
      {/* One persistent player for the whole app — mounted unconditionally so playback survives every
          route change (incl. the reader). It renders nothing until a book is playing. */}
      <AudioPlayer />
      <Toaster />
    </div>
    </ShelfPromptProvider>
    </ConfirmProvider>
  );
}

export default function App() {
  const { loaded, me, refresh } = useAuth();
  useEffect(() => {
    refresh();
  }, [refresh]);

  if (!loaded) return <AuthSpinner />;
  if (me?.needs_setup) return <Setup />;
  if (!me?.authenticated) {
    // Logged-out public routes: self-registration + password recovery. Everything else falls back
    // to the sign-in screen. (The emailed reset link is "<base>/reset?token=…".)
    return (
      <Routes>
        <Route path="/register" element={<Register />} />
        <Route path="/forgot" element={<Forgot />} />
        <Route path="/reset" element={<Reset />} />
        <Route path="*" element={<Login />} />
      </Routes>
    );
  }
  return <AuthedApp />;
}
