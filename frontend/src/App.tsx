import { useEffect, useState } from "react";
import { NavLink, Route, Routes, useLocation } from "react-router-dom";
import { useApp } from "./store";
import { THEME_MAP } from "./themes";
import ThemePicker from "./components/ThemePicker";
import Library from "./pages/Library";
import Reader from "./pages/Reader";
import Sources from "./pages/Sources";
import Jobs from "./pages/Jobs";
import Settings from "./pages/Settings";
import AddWork from "./pages/AddWork";
import IndexPage from "./pages/Index";

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

function Nav() {
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
          {link("/add", "Add")}
          {link("/index", "Index")}
          {link("/sources", "Sources")}
          {link("/jobs", "Jobs")}
          {link("/settings", "Settings")}
        </nav>
        <div className="shrink-0">
          <ThemeButton />
        </div>
      </div>
    </header>
  );
}

export default function App() {
  const { load } = useApp();
  const location = useLocation();
  useEffect(() => {
    load();
  }, [load]);

  const isReader = location.pathname.startsWith("/read/");

  return (
    <div className="min-h-full">
      {!isReader && <Nav />}
      <Routes>
        <Route path="/" element={<Library />} />
        <Route path="/add" element={<AddWork />} />
        <Route path="/index" element={<IndexPage />} />
        <Route path="/sources" element={<Sources />} />
        <Route path="/jobs" element={<Jobs />} />
        <Route path="/settings" element={<Settings />} />
        <Route path="/read/:workId" element={<Reader />} />
        <Route path="/read/:workId/:chapterId" element={<Reader />} />
      </Routes>
    </div>
  );
}
