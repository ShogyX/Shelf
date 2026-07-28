import { defineConfig, devices } from "@playwright/test";
import { AUTH_FILE } from "./e2e/support";

// Spins up a DEDICATED backend (seeded, throwaway DB) + Vite dev server, then runs the flows
// against them.
//
// The ports are deliberately NOT the dev/prod ones (8000/5173), and reuseExistingServer is always
// false. On a machine that also runs Shelf for real — which is the normal case for this project —
// `reuseExistingServer: !CI` pointed the whole suite at the LIVE instance on :8000, so an e2e run
// would have exercised destructive flows against production data. Own ports + never-reuse means the
// suite either brings up its own stack or fails loudly; it can't silently land on the real one.
//
// Every path the backend writes to is redirected at the same time (DB, media, covers, stock,
// audiobooks, backups) — the same forcing tests/conftest.py does — so seeding can't reach into the
// operator's real media dirs either. Override the ports with E2E_API_PORT / E2E_WEB_PORT.
const API_PORT = Number(process.env.E2E_API_PORT ?? 8099);
const WEB_PORT = Number(process.env.E2E_WEB_PORT ?? 5199);
// 127.0.0.1, never "localhost": this host resolves localhost to ::1 as well, while uvicorn and the
// Vite dev server both bind IPv4 only — so a run would intermittently die with ECONNREFUSED ::1.

// Throwaway state, all under backend/.e2e/ (gitignored) so a run leaves nothing behind.
const ENV = [
  "SHELF_DATABASE_URL=sqlite:///./.e2e/e2e.db",
  "SHELF_MEDIA_DIR=./.e2e/media",
  "SHELF_COVERS_DIR=./.e2e/covers",
  "SHELF_BACKUP_DIR=./.e2e/backups",
  "SHELF_STOCK_DIR=",
  "SHELF_AUDIOBOOK_DIR=",
  "SHELF_SCHEDULER_ENABLED=false",
].join(" ");

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  fullyParallel: false,
  // One worker: every project drives the SAME account and the same seeded work, so a parallel
  // project saving reading progress would race the resume assertions. The suite runs in ~15s.
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${WEB_PORT}`,
    trace: "on-first-retry",
  },
  projects: [
    // Creates the first admin and puts the seeded work in their library, then saves the session
    // cookie the other projects reuse. The app is behind auth and the library is per-user, so
    // nothing below can see a single title without it.
    { name: "setup", testMatch: /auth\.setup\.ts/ },
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"], storageState: AUTH_FILE },
      dependencies: ["setup"],
      testIgnore: /mobile\.spec\.ts/,
    },
    {
      name: "mobile",
      // Pixel 5 rather than an iPhone: it's the Chromium-based phone emulation, so the suite needs
      // only the one browser (`npx playwright install chromium`).
      use: { ...devices["Pixel 5"], storageState: AUTH_FILE },
      dependencies: ["setup"],
      testMatch: /mobile\.spec\.ts/,
    },
  ],
  webServer: [
    {
      // The throwaway DB is recreated per run so every spec starts from the same known state
      // (one seeded work, no users, no progress). `.e2e/` is this suite's own scratch dir.
      command:
        `cd ../backend && . .venv/bin/activate && rm -rf ./.e2e && mkdir -p .e2e && ${ENV} python -m app.seed && ` +
        `${ENV} uvicorn app.main:app --port ${API_PORT}`,
      url: `http://127.0.0.1:${API_PORT}/api/health`,
      reuseExistingServer: false,
      timeout: 120_000,
    },
    {
      command: `npm run dev -- --port ${WEB_PORT} --strictPort`,
      url: `http://127.0.0.1:${WEB_PORT}`,
      reuseExistingServer: false,
      timeout: 60_000,
      env: { VITE_API_TARGET: `http://127.0.0.1:${API_PORT}` },
    },
  ],
});
