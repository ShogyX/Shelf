// Signed-out entry: the app is behind auth now, so the first thing a visitor sees is the sign-in
// card. Runs without the saved session (the other specs reuse it via storageState).
import { expect, test } from "@playwright/test";
import { ADMIN, SEED_TITLE } from "./support";

test.use({ storageState: { cookies: [], origins: [] } });

test("signing in leads to the user's library", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Sign in to Shelf" })).toBeVisible();

  await page.getByLabel("Username").fill(ADMIN.username);
  await page.getByLabel("Password", { exact: true }).fill(ADMIN.password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();

  await expect(page.getByRole("heading", { name: "New in your library" })).toBeVisible();
  await expect(page.getByRole("button", { name: new RegExp(SEED_TITLE) }).first()).toBeVisible();
});

test("a wrong password keeps you out and says so", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("Username").fill(ADMIN.username);
  await page.getByLabel("Password", { exact: true }).fill("definitely-not-the-password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();

  await expect(page.getByText("Invalid username or password")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Sign in to Shelf" })).toBeVisible();
});
