// Phone viewport: the top nav collapses into the fixed bottom tab bar, and the same
// library → detail sheet → reader path has to stay tappable. Run by the "mobile" project only.
import { expect, test } from "@playwright/test";
import { SEED_TITLE, openSeededWork, rail } from "./support";

test("library and reader work on a phone", async ({ page }) => {
  await page.goto("/");

  // Primary destinations live in the bottom tab bar at this width.
  const tabs = page.getByRole("navigation", { name: "Primary" });
  await expect(tabs).toBeVisible();
  await expect(tabs.getByRole("link", { name: "Library" })).toBeVisible();

  await expect(
    rail(page, "New in your library").getByRole("button", { name: new RegExp(SEED_TITLE) }),
  ).toBeVisible();

  await openSeededWork(page);
  await expect(page.locator(".reader-prose")).toContainText("Lin Yue");
  // The reader takes over the screen — the tab bar must not sit on top of the text.
  await expect(tabs).toBeHidden();
});
