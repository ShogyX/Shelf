import { defineConfig, devices } from "@playwright/test";

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
  reporter: [["list"]],
  use: {
    baseURL: `http://localhost:${WEB_PORT}`,
    trace: "on-first-retry",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      command:
        `cd ../backend && . .venv/bin/activate && mkdir -p .e2e && ${ENV} python -m app.seed && ` +
        `${ENV} uvicorn app.main:app --port ${API_PORT}`,
      url: `http://localhost:${API_PORT}/api/health`,
      reuseExistingServer: false,
      timeout: 120_000,
    },
    {
      command: `npm run dev -- --port ${WEB_PORT} --strictPort`,
      url: `http://localhost:${WEB_PORT}`,
      reuseExistingServer: false,
      timeout: 60_000,
      env: { VITE_API_TARGET: `http://localhost:${API_PORT}` },
    },
  ],
});
