import { useEffect, useState } from "react";
import { NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "./api/client";
import { useApp } from "./store";
import { useAuth, useCurrentUser, useHasPermission, useIsAdmin } from "./auth";
import { THEME_MAP } from "./themes";
import ThemePicker from "./components/ThemePicker";
import { AuthSpinner, Login, Setup } from "./components/AuthGate";
import Library from "./pages/Library";
import Reader from "./pages/Reader";
import Sources from "./pages/Sources";
import Jobs from "./pages/Jobs";
import Settings from "./pages/Settings";
import AddWork from "./pages/AddWork";
import IndexPage from "./pages/Index";
import BrowseCatalog from "./pages/BrowseCatalog";
import Users from "./pages/Users";
import Stock from "./pages/Stock";
import Toaster from "./components/Toaster";

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
          <div className="absolute right-0 z-50 mt-2 w-72 rounded-xl border border-border bg-surface p-3 shadow-2xl">
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
          <div className="absolute right-0 z-50 mt-2 w-44 rounded-xl border border-border bg-surface p-1.5 shadow-2xl">
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

function Nav() {
  const isAdmin = useIsAdmin();
  const canIndex = useHasPermission("index.view");
  const canAdd = useHasPermission("add.use");
  const canJobs = useHasPermission("jobs.view");
  const canSources = useHasPermission("sources.view");
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
      className="sticky top-0 z-30 border-b border-border bg-surface/85 backdrop-blur"
      style={{ paddingTop: "env(safe-area-inset-top)" }}
    >
      <div className="mx-auto flex max-w-5xl items-center gap-2 px-3 py-2 sm:px-4 sm:py-3">
        <NavLink to="/" className="flex shrink-0 items-center gap-1.5 font-semibold text-text">
          <span className="text-lg">📚</span>
          <span className="hidden sm:inline">Shelf</span>
        </NavLink>
        <nav className="scrollbar-none flex flex-1 items-center gap-1 overflow-x-auto">
          {link("/", "Library")}
          {canAdd && link("/add", "Add")}
          {canIndex && link("/index", "Index")}
          {/* Sources + Jobs are operator surfaces — shown to admins and to users granted the
              read permission (managing them stays admin-only). */}
          {canSources && link("/sources", "Sources")}
          {canJobs && link("/jobs", "Jobs")}
          {isAdmin && link("/stock", "Stock")}
          {link("/settings", "Settings")}
          {isAdmin && link("/users", "Users")}
        </nav>
        <div className="flex shrink-0 items-center gap-1.5">
          <ThemeButton />
          <UserButton />
        </div>
      </div>
    </header>
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
  useEffect(() => {
    load();
  }, [load]);

  const isReader = location.pathname.startsWith("/read/");
  // Operator-only pages: non-admins are redirected to their library.
  const adminOnly = (el: JSX.Element) => (isAdmin ? el : <Navigate to="/" replace />);
  // Permission-gated pages: redirected to the library if the capability is missing.
  const need = (ok: boolean, el: JSX.Element) => (ok ? el : <Navigate to="/" replace />);

  return (
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
      <Routes>
        <Route path="/" element={<Library />} />
        <Route path="/add" element={need(canAdd, <AddWork />)} />
        <Route path="/index" element={need(canIndex, <IndexPage />)} />
        <Route path="/browse/:dimension/:value" element={need(canIndex, <BrowseCatalog />)} />
        <Route path="/sources" element={need(canSources, <Sources />)} />
        <Route path="/jobs" element={need(canJobs, <Jobs />)} />
        <Route path="/settings" element={<Settings />} />
        <Route path="/users" element={adminOnly(<Users />)} />
        <Route path="/stock" element={adminOnly(<Stock />)} />
        <Route path="/read/:workId" element={<Reader />} />
        <Route path="/read/:workId/:chapterId" element={<Reader />} />
      </Routes>
      <Toaster />
    </div>
  );
}

export default function App() {
  const { loaded, me, refresh } = useAuth();
  useEffect(() => {
    refresh();
  }, [refresh]);

  if (!loaded) return <AuthSpinner />;
  if (me?.needs_setup) return <Setup />;
  if (!me?.authenticated) return <Login />;
  return <AuthedApp />;
}
