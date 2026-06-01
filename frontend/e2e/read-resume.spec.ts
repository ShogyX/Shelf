import { test, expect, devices } from "@playwright/test";

test("focus mode hides all chrome and is exitable", async ({ page }) => {
  await page.goto("/");
  await page.getByText("A Quiet Ascension", { exact: false }).first().click();
  await expect(page.locator(".reader-prose")).toBeVisible();

  // Enter focus mode via the floating control.
  await page.getByRole("button", { name: "Focus mode", exact: true }).click();
  // Chrome is gone: floating controls disappear; the exit affordance shows.
  await expect(page.getByRole("button", { name: "Exit focus mode" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Focus mode", exact: true })).toHaveCount(0);
  await expect(page.locator(".reader-prose")).toBeVisible();

  // Exit restores the controls.
  await page.getByRole("button", { name: "Exit focus mode" }).click();
  await expect(page.getByRole("button", { name: "Focus mode", exact: true })).toBeVisible();
});

test("mobile viewport: library and reader render", async ({ browser }) => {
  const ctx = await browser.newContext({ ...devices["iPhone 12"] });
  const page = await ctx.newPage();
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Library" })).toBeVisible();
  // Nav links remain reachable (horizontally scrollable).
  await expect(page.getByRole("link", { name: "Library", exact: true })).toBeVisible();
  await page.getByText("A Quiet Ascension", { exact: false }).first().click();
  await expect(page.locator(".reader-prose")).toBeVisible();
  // Floating controls are present and tappable on mobile.
  await expect(page.getByRole("button", { name: "Focus mode", exact: true })).toBeVisible();
  await ctx.close();
});

test("read a seeded work, navigate chapters, and resume on reload", async ({ page }) => {
  // Library lists the seeded work.
  await page.goto("/");
  const card = page.getByText("A Quiet Ascension", { exact: false }).first();
  await expect(card).toBeVisible();

  // Open the reader.
  await card.click();
  await expect(page).toHaveURL(/\/read\/\d+/);

  // Chapter content renders (sanitized prose).
  const prose = page.locator(".reader-prose");
  await expect(prose).toBeVisible();
  await expect(prose).toContainText("Lin Yue");

  // Navigate to the next chapter.
  await page.getByRole("button", { name: /Next/ }).click();
  await expect(page).toHaveURL(/\/read\/\d+\/\d+/);

  // Scroll down so a scroll fraction is saved.
  const scroller = page.locator(".scrollbar-thin").first();
  await scroller.evaluate((el) => (el.scrollTop = el.scrollHeight / 2));
  // Give the debounced progress save time to flush.
  await page.waitForTimeout(1200);

  const urlAfter = page.url();

  // Reload the library, then re-enter the work — should resume the same chapter.
  await page.goto("/");
  await page.getByText("A Quiet Ascension", { exact: false }).first().click();
  await expect(page).toHaveURL(new RegExp(urlAfter.split("/read/")[1].split("/")[0]));
  await expect(page.locator(".reader-prose")).toBeVisible();
});
