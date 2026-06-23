import { Suspense, lazy, useEffect, useState, type ReactElement } from "react";
import { NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "./api/client";
import { useApp } from "./store";
import { useAuth, useCurrentUser, useHasPermission, useIsAdmin } from "./auth";
import { THEME_MAP } from "./themes";
import ThemePicker from "./components/ThemePicker";
import { NotificationBell } from "./components/NotificationBell";
import { AuthSpinner, Forgot, Login, Register, Reset, Setup } from "./components/AuthGate";
import { Skeleton } from "./components/ui";
// Route destinations are code-split so admin-only pages (Settings/Users/Jobs/Stock)
// don't ship in the main bundle for users who can't reach them.
const Library = lazy(() => import("./pages/Library"));
const Reader = lazy(() => import("./pages/Reader"));
const Jobs = lazy(() => import("./pages/Jobs"));
const Settings = lazy(() => import("./pages/Settings"));
const AddPage = lazy(() => import("./pages/AddWork"));
const IndexPage = lazy(() => import("./pages/Index"));
const BrowseCatalog = lazy(() => import("./pages/BrowseCatalog"));
const Users = lazy(() => import("./pages/Users"));
const Stock = lazy(() => import("./pages/Stock"));
const Watchlist = lazy(() => import("./pages/Watchlist"));
const ListImports = lazy(() => import("./pages/ListImports"));
import Toaster from "./components/Toaster";
import AudioPlayer from "./components/AudioPlayer";
import { useAudio } from "./audioStore";
import { ConfirmProvider } from "./components/confirm";
import { ShelfPromptProvider } from "./components/ShelfPrompt";

function ThemeButton() {
  const { theme } = useApp();
  const [open, setOpen] = useState(false);
  const name = theme === "system" ? "System" : THEME_MAP[theme]?.name ?? "Theme";
  return (
    <div className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        title="Color mode"
        className="rounded-lg border border-border px-2.5 py-1.5 text-sm hover:bg-surface-2"
      >
        <span className="sm:mr-1">🎨</span>
        <span className="hidden sm:inline">{name}</span>
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          {/* Viewport-anchored on mobile (full-width below the bar) so the wide picker can't run off
              a phone edge; trigger-anchored on desktop. */}
          <div className="fixed inset-x-2 top-[calc(env(safe-area-inset-top)_+_3.25rem)] z-50 rounded-xl border border-border bg-surface p-3 shadow-2xl sm:absolute sm:inset-x-auto sm:right-0 sm:top-full sm:mt-2 sm:w-72">
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
  async function logout() {
    await api.logout().catch(() => {});
    qc.clear();
    await refresh();
  }
  return (
    <div className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        title="Account"
        className="flex items-center gap-1.5 rounded-lg border border-border px-2.5 py-1.5 text-sm hover:bg-surface-2"
      >
        <span className="flex h-5 w-5 items-center justify-center rounded-full bg-accent text-[11px] font-semibold text-accent-fg">
          {(user?.display_name || user?.username || "?")[0]?.toUpperCase()}
        </span>
        <span className="hidden max-w-[8rem] truncate sm:inline">{user?.username}</span>
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div className="absolute right-0 z-50 mt-2 w-44 max-w-[calc(100vw-1rem)] rounded-xl border border-border bg-surface p-1.5 shadow-2xl">
            <div className="px-2 py-1.5 text-xs text-muted">
              Signed in as <span className="font-medium text-text">{user?.username}</span>
              {user?.role === "admin" && " · admin"}
            </div>
            <button
              onClick={logout}
              className="w-full rounded-lg px-2 py-1.5 text-left text-sm text-text hover:bg-surface-2"
            >
              Sign out
            </button>
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

function Nav() {
  const isAdmin = useIsAdmin();
  const canIndex = useHasPermission("index.view");
  const canAdd = useHasPermission("add.use");
  const canJobs = useHasPermission("jobs.view");
  const canSources = useHasPermission("sources.view");
  const canOpenAdd = canAdd || canSources;
  const link = (to: string, label: string) => (
    <NavLink
      to={to}
      end={to === "/"}
      className={({ isActive }) =>
        `shrink-0 whitespace-nowrap rounded-lg px-3 py-1.5 text-sm font-medium transition ${
          isActive ? "bg-accent text-accent-fg" : "text-muted hover:bg-surface-2 hover:text-text"
        }`
      }
    >
      {label}
    </NavLink>
  );
  return (
    <header
      className="sticky top-0 z-30 border-b border-border/50 bg-surface sm:bg-surface/70 sm:backdrop-blur-xl"
      style={{ paddingTop: "env(safe-area-inset-top)" }}
    >
      <div className="mx-auto flex max-w-5xl items-center gap-2 px-3 py-2 sm:px-4 sm:py-3">
        <NavLink to="/" className="flex flex-1 shrink-0 items-center gap-1.5 font-semibold text-text sm:flex-none">
          <span className="text-lg">📚</span>
          <span className="hidden sm:inline">Shelf</span>
        </NavLink>
        {/* Inline links wrap to one row at ≥sm; on phones they'd stack into ~5 rows and eat the
            first screen, so they're hidden there in favour of the fixed bottom tab bar below. */}
        <nav className="hidden flex-1 flex-wrap items-center gap-1 sm:flex">
          {link("/", "Library")}
          {canOpenAdd && link("/add", "Add")}
          {link("/watchlist", "Watchlist")}
          {link("/imports", "Imports")}
          {canIndex && link("/index", "Catalog")}
          {/* Jobs is an operator surface — shown to admins and to users granted the read
              permission (managing it stays admin-only). Sources now live behind the Add tabs. */}
          {canJobs && link("/jobs", "Jobs")}
          {isAdmin && link("/stock", "Stock")}
          {link("/settings", "Settings")}
          {isAdmin && link("/users", "Users")}
        </nav>
        <div className="flex shrink-0 items-center gap-1.5">
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

  // Remaining permitted destinations that don't fit the 5 primary tabs.
  const moreLinks: [string, string, string][] = [
    ...(canOpenAdd ? [["/add", "➕", "Add"] as [string, string, string]] : []),
    ["/imports", "📥", "Imports"],
    ...(canJobs ? [["/jobs", "⚙️", "Jobs"] as [string, string, string]] : []),
    ...(isAdmin ? [["/stock", "📦", "Stock"] as [string, string, string]] : []),
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
        {canIndex && tab("/index", "🔍", "Catalog")}
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
  // Operator-only pages: non-admins are redirected to their library.
  const adminOnly = (el: ReactElement) => (isAdmin ? el : <Navigate to="/" replace />);
  // Permission-gated pages: redirected to the library if the capability is missing.
  const need = (ok: boolean, el: ReactElement) => (ok ? el : <Navigate to="/" replace />);

  return (
    <ConfirmProvider>
    <ShelfPromptProvider>
    <div className="min-h-full">
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
        <Route path="/watchlist" element={<Watchlist />} />
        <Route path="/imports" element={<ListImports />} />
        {/* Old pages merged into Watchlist — keep redirects so bookmarks/links don't 404. */}
        <Route path="/missing" element={<Navigate to="/watchlist" replace />} />
        <Route path="/following" element={<Navigate to="/watchlist" replace />} />
        <Route path="/add" element={need(canOpenAdd, <AddPage />)} />
        <Route path="/index" element={need(canIndex, <IndexPage />)} />
        <Route path="/browse/:dimension/:value" element={need(canIndex, <BrowseCatalog />)} />
        {/* Sources merged into the Add page (behind a tab). Keep the route so old links don't 404. */}
        <Route path="/sources" element={<Navigate to="/add" replace />} />
        <Route path="/jobs" element={need(canJobs, <Jobs />)} />
        <Route path="/settings" element={<Settings />} />
        <Route path="/users" element={adminOnly(<Users />)} />
        <Route path="/stock" element={adminOnly(<Stock />)} />
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
