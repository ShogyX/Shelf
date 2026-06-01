import { defineConfig, devices } from "@playwright/test";

// Spins up a fresh backend (seeded, in-memory-ish temp DB) + the Vite dev server,
// then runs the read-and-resume flow against them.
export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  fullyParallel: false,
  reporter: [["list"]],
  use: {
    baseURL: "http://localhost:5173",
    trace: "on-first-retry",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      command:
        "cd ../backend && . .venv/bin/activate && SHELF_DATABASE_URL=sqlite:///./e2e.db SHELF_SCHEDULER_ENABLED=false python -m app.seed && SHELF_DATABASE_URL=sqlite:///./e2e.db SHELF_SCHEDULER_ENABLED=false uvicorn app.main:app --port 8000",
      url: "http://localhost:8000/api/health",
      reuseExistingServer: !process.env.CI,
      timeout: 60_000,
    },
    {
      command: "npm run dev",
      url: "http://localhost:5173",
      reuseExistingServer: !process.env.CI,
      timeout: 60_000,
    },
  ],
});
