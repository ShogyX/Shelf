// Shared constants + helpers for the e2e suite.
//
// Everything here targets the DEDICATED e2e stack brought up by playwright.config.ts (throwaway DB
// under backend/.e2e/, API on E2E_API_PORT, web on E2E_WEB_PORT) — never the dev/live instance.
import { expect, type Page } from "@playwright/test";

export const API_PORT = Number(process.env.E2E_API_PORT ?? 8099);
export const API_URL = `http://127.0.0.1:${API_PORT}`;

/** The first admin, created by the setup project (auth.setup.ts) on the throwaway DB. */
export const ADMIN = { username: "e2e-admin", password: "e2e-password" };

/** Bookshelf the setup project uses to give that admin library membership of the seeded work
 *  (placing a work on a shelf is the API path that also ensures membership). */
export const SHELF_NAME = "E2E";

/** Title of the work created by `python -m app.seed` (backend/app/seed.py) — "… (Seed)" suffixed. */
export const SEED_TITLE = "A Quiet Ascension";

/** Where the setup project parks the authenticated session cookie (gitignored). */
export const AUTH_FILE = "e2e/.auth/admin.json";

/** Chapter titles the seeder generates: "Chapter {n}: The Threshold". */
export const seedChapter = (n: number) => `Chapter ${n}: The Threshold`;

/** The library home's rail whose heading is `name` (e.g. "New in your library"). */
export function rail(page: Page, name: string) {
  return page.locator("section").filter({ has: page.getByRole("heading", { name, exact: true }) });
}

/** The detail sheet's primary action — "Read", or "Continue" once there's a saved position. */
export const READ_ACTION = /^(Read|Continue)$/;

/** Library home → open the seeded title's detail sheet → Read. Leaves the page in the reader. */
export async function openSeededWork(page: Page): Promise<void> {
  await page.goto("/");
  await rail(page, "New in your library")
    .getByRole("button", { name: new RegExp(SEED_TITLE) })
    .click();
  const sheet = page.getByRole("dialog");
  await expect(sheet.getByRole("heading", { name: new RegExp(SEED_TITLE) })).toBeVisible();
  await sheet.getByRole("button", { name: READ_ACTION }).click();
  // The reader resolves ?/read/:workId → /read/:workId/:chapterId itself (continue-or-first chapter).
  await expect(page).toHaveURL(/\/read\/\d+\/\d+/);
}

/** The reader's scrolling content column. */
export const readerScroller = (page: Page) => page.locator(".scrollbar-thin").first();
