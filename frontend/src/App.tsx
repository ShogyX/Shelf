import { Suspense, lazy, useEffect, useState, type ReactElement } from "react";
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
const Users = lazy(() => import("./pages/Users"));
const Watchlist = lazy(() => import("./pages/Watchlist"));
import { AddListModal } from "./pages/ListImports";
import Toaster from "./components/Toaster";
import AudioPlayer from "./components/AudioPlayer";
import { useAudio } from "./audioStore";
import { ConfirmProvider } from "./components/confirm";
import { ShelfPromptProvider } from "./components/ShelfPrompt";

function ThemeButton() {
  const { theme } = useApp();
  const [open, setOpen] = useState(false);
  useEscapeClose(open, () => setOpen(false));
  const name = theme === "system" ? "System" : THEME_MAP[theme]?.name ?? "Theme";
  return (
    <div className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        title={`Theme — ${name}`}
        aria-label="Theme" aria-haspopup="menu" aria-expanded={open}
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
        title="Account" aria-label="Account" aria-haspopup="menu" aria-expanded={open}
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
                <span className="block text-xs text-muted">{user?.role === "admin" ? "Administrator" : "Reader"}</span>
              </span>
            </div>
            <div className="my-1 h-px bg-[var(--hair,var(--border))]" />
            <Link to="/watchlist" onClick={() => setOpen(false)}
              className="flex w-full items-center gap-2.5 rounded-[9px] px-2.5 py-2 text-sm font-medium text-text transition hover:bg-surface-2">☆ My watchlist</Link>
            <Link to="/settings" onClick={() => setOpen(false)}
              className="flex w-full items-center gap-2.5 rounded-[9px] px-2.5 py-2 text-sm font-medium text-text transition hover:bg-surface-2">⚙ Settings</Link>
            {user?.role === "admin" && (
              <Link to="/users" onClick={() => setOpen(false)}
                className="flex w-full items-center gap-2.5 rounded-[9px] px-2.5 py-2 text-sm font-medium text-text transition hover:bg-surface-2">👤 Users</Link>
            )}
            <button onClick={logout}
              className="flex w-full items-center gap-2.5 rounded-[9px] px-2.5 py-2 text-left text-sm font-medium text-text transition hover:bg-surface-2">⤓ Sign out</button>
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
  const navigate = useNavigate();
  const canAdd = useHasPermission("add.use");
  const canSources = useHasPermission("sources.view");
  const [open, setOpen] = useState(false);
  const [modal, setModal] = useState<AddModal | null>(null);
  useEscapeClose(open, () => setOpen(false));
  if (!(canAdd || canSources)) return null;
  const items: { icon: string; label: string; desc: string; action: () => void }[] = [
    { icon: "🔍", label: "Search & request", desc: "Find a title to acquire", action: () => navigate("/discover") },
    { icon: "🔗", label: "Add by URL / ISBN", desc: "Paste a link or identifier", action: () => setModal("url") },
    { icon: "📥", label: "Import a list", desc: "Goodreads, AniList, CSV…", action: () => setModal("list") },
    { icon: "⤓", label: "Upload files", desc: "EPUB, CBZ, PDF…", action: () => setModal("upload") },
  ];
  return (
    <div className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        title="Add" aria-label="Add" aria-haspopup="menu" aria-expanded={open}
        className="flex h-[38px] w-[38px] items-center justify-center rounded-[11px] border border-[var(--hair,var(--border))] bg-surface text-text transition hover:bg-surface-2"
      >
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.1" strokeLinecap="round"><path d="M12 5v14M5 12h14" /></svg>
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div className="sp-pop fixed inset-x-2 top-[calc(env(safe-area-inset-top)_+_3.75rem)] z-50 rounded-[15px] border border-[var(--hair-strong,var(--border))] bg-surface p-1.5 shadow-[var(--pop-shadow)] sm:absolute sm:inset-x-auto sm:right-0 sm:top-12 sm:w-64">
            <div className="px-2.5 py-2 text-[11px] font-bold uppercase tracking-wider text-muted">Add to Shelf</div>
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
        placeholder="Search titles, authors…"
        aria-label="Search titles, authors"
        className="h-[38px] w-full rounded-[11px] border border-[var(--hair,var(--border))] bg-surface pl-9 pr-3 text-[13.5px] text-text transition placeholder:text-muted focus:border-[color-mix(in_srgb,var(--accent)_50%,var(--border))] focus:outline-none"
      />
    </form>
  );
}

function Nav() {
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
        <button onClick={() => navigate("/")} className="flex shrink-0 items-center gap-2.5" aria-label="Home">
          <span className="flex h-8 w-8 items-center justify-center rounded-[9px] bg-gradient-to-br from-[var(--accent)] to-[color-mix(in_srgb,var(--accent)_55%,#000)] text-accent-fg shadow-[0_4px_14px_color-mix(in_srgb,var(--accent)_45%,transparent)]">
            <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.1" strokeLinecap="round" strokeLinejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" /><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" /></svg>
          </span>
          <span className="font-display hidden text-[21px] font-semibold tracking-tight text-text sm:inline">Shelf</span>
        </button>
        {/* Center nav: ≥sm only; phones use the fixed bottom tab bar below. min-w-0 + scroll so a
            cramped sm–md width scrolls the pills instead of pushing the right-side icons off-screen. */}
        <nav className="ml-2 hidden min-w-0 items-center gap-1 overflow-x-auto scrollbar-none sm:flex">
          {pill("/", "Library", true)}
          {canIndex && pill("/discover", "Discover")}
          {pill("/watchlist", "Watchlist")}
          {canOperate && pill("/sources", "Sources")}
          {pill("/settings", "Settings")}
        </nav>
        <div className="flex-1" />
        <NavSearch />
        <div className="flex shrink-0 items-center gap-1.5">
          <AddMenu />
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
  const tab = (to: string, icon: string, label: string, end = false) => (
    <NavLink to={to} end={end} className={tabCls}>
      <span className="text-lg leading-none">{icon}</span>
      <span>{label}</span>
    </NavLink>
  );

  const canOperate = canJobs || canSources || isAdmin;
  // Remaining permitted destinations that don't fit the 5 primary tabs.
  const moreLinks: [string, string, string][] = [
    ...(canOpenAdd ? [["/add", "➕", "Add"] as [string, string, string]] : []),
    ...(canOperate ? [["/sources", "🛠️", "Sources"] as [string, string, string]] : []),
    ...(isAdmin ? [["/users", "👤", "Users"] as [string, string, string]] : []),
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
                    `flex items-center gap-2 rounded-xl px-3 py-2.5 text-sm font-medium transition ${
                      isActive ? "bg-accent text-accent-fg" : "text-text hover:bg-surface-2"
                    }`
                  }
                >
                  <span className="text-base">{icon}</span>
                  {label}
                </NavLink>
              ))}
            </div>
          </div>
        </div>
      )}
      <nav
        aria-label="Primary"
        className="fixed inset-x-0 bottom-0 z-40 flex items-stretch border-t border-border/60 bg-surface sm:hidden"
        style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
      >
        {tab("/", "📚", "Library", true)}
        {canIndex && tab("/discover", "🔍", "Discover")}
        {tab("/watchlist", "👁️", "Watchlist")}
        {tab("/settings", "⚙️", "Settings")}
        <button
          type="button"
          onClick={() => setMoreOpen((o) => !o)}
          aria-expanded={moreOpen}
          aria-label="More"
          className={`flex flex-1 flex-col items-center justify-center gap-0.5 py-1.5 text-[11px] font-medium transition ${
            moreOpen ? "text-accent" : "text-muted hover:text-text"
          }`}
        >
          <span className="text-lg leading-none">⋯</span>
          <span>More</span>
        </button>
      </nav>
    </>
  );
}

function AuthedApp() {
  const { load } = useApp();
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

  const isReader = location.pathname.startsWith("/read/");
  const playerOpen = useAudio((st) => st.workId != null);
  const canOperate = canJobs || canSources || isAdmin;
  // Operator-only pages: non-admins are redirected to their library.
  const adminOnly = (el: ReactElement) => (isAdmin ? el : <Navigate to="/" replace />);
  // Permission-gated pages: redirected to the library if the capability is missing.
  const need = (ok: boolean, el: ReactElement) => (ok ? el : <Navigate to="/" replace />);

  return (
    <ConfirmProvider>
    <ShelfPromptProvider>
    <div className="relative min-h-full">
      {/* Ambient shell glow: a fixed, viewport-anchored accent-tinted radial behind all content
          (the "cinematic" backdrop). Sits below content (-z-10) on an opaque --bg base so it reads
          as one continuous field at any scroll depth (no seam past the first viewport); a slow
          drifting ::before animates the accent radials. The reader paints its own bg, so it's
          skipped there. See `.ambient-layer` in index.css. */}
      {!isReader && <div aria-hidden className="ambient-layer" />}
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
        <Route path="/watchlist" element={<Watchlist />} />
        {/* List imports merged into Sources — keep a redirect so old bookmarks/links resolve. */}
        <Route path="/imports" element={<Navigate to="/sources" replace />} />
        {/* Old pages merged into Watchlist — keep redirects so bookmarks/links don't 404. */}
        <Route path="/missing" element={<Navigate to="/watchlist" replace />} />
        <Route path="/following" element={<Navigate to="/watchlist" replace />} />
        <Route path="/add" element={need(canOpenAdd, <AddPage />)} />
        {/* Catalog → Discover (renamed). Keep /index as a redirect so old bookmarks resolve. */}
        <Route path="/discover" element={need(canIndex, <IndexPage />)} />
        <Route path="/index" element={<Navigate to="/discover" replace />} />
        <Route path="/browse/:dimension/:value" element={need(canIndex, <BrowseCatalog />)} />
        {/* Sources & Acquisitions — the merged operator page (jobs + downloads + index + folders +
            imports). /jobs redirects here so old links resolve. */}
        <Route path="/sources" element={need(canOperate, <SourcesHub />)} />
        <Route path="/jobs" element={<Navigate to="/sources" replace />} />
        <Route path="/settings" element={<Settings />} />
        <Route path="/users" element={adminOnly(<Users />)} />
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
