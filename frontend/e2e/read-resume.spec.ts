// The core reading journey against the seeded work: find it in the library, open its detail sheet,
// read a chapter, move between chapters, and come back to exactly where you stopped.
import { expect, test } from "@playwright/test";
import { READ_ACTION, SEED_TITLE, openSeededWork, rail, readerScroller, seedChapter } from "./support";

test("library home surfaces the seeded title and its detail sheet", async ({ page }) => {
  await page.goto("/");

  // The work is in the library, so it shows on the "new" rail and on its bookshelf's rail.
  const card = rail(page, "New in your library").getByRole("button", { name: new RegExp(SEED_TITLE) });
  await expect(card).toBeVisible();
  await expect(rail(page, "E2E").getByRole("button", { name: new RegExp(SEED_TITLE) })).toBeVisible();

  await card.click();
  const sheet = page.getByRole("dialog");
  await expect(sheet.getByRole("heading", { name: new RegExp(SEED_TITLE) })).toBeVisible();
  await expect(sheet.getByText("Demo Author").first()).toBeVisible();
  await expect(sheet.getByRole("button", { name: READ_ACTION })).toBeVisible();

  // The Chapters tab lists what the seeder gathered.
  await sheet.getByRole("button", { name: "Chapters" }).click();
  await expect(sheet.getByRole("button", { name: new RegExp(seedChapter(1)) })).toBeVisible();
  await expect(sheet.getByRole("button", { name: new RegExp(seedChapter(8)) })).toBeVisible();

  // "Browse all" hands off to the dense manage-everything grid, which lists the same title.
  await sheet.getByRole("button", { name: "Close" }).click();
  await rail(page, "New in your library").getByRole("link", { name: "Browse all" }).click();
  await expect(page).toHaveURL(/\/library\/browse/);
  await expect(page.getByRole("heading", { name: "Browse library" })).toBeVisible();
  await expect(page.getByRole("button", { name: new RegExp(SEED_TITLE) }).first()).toBeVisible();
});

test("read a chapter, move to the next one, and resume the saved position", async ({ page }) => {
  await openSeededWork(page);

  // Chapter content renders (sanitized prose), and the top bar names work + chapter.
  const prose = page.locator(".reader-prose");
  await expect(prose).toBeVisible();
  await expect(prose).toContainText("Lin Yue");
  await expect(page.getByText(seedChapter(1))).toBeVisible();
  const firstChapterUrl = page.url();

  // Next chapter → new chapter id in the URL and the next chapter's content.
  await page.getByRole("button", { name: "Next →", exact: true }).click();
  await expect(page).not.toHaveURL(firstChapterUrl);
  await expect(page).toHaveURL(/\/read\/\d+\/\d+/);
  await expect(page.getByText(seedChapter(2))).toBeVisible();
  await expect(prose).toContainText("Chapter 2");
  const secondChapterUrl = page.url();

  // Scroll into the chapter and wait for the debounced save to actually land (no fixed sleep).
  const saved = page.waitForResponse(
    (r) =>
      /\/api\/works\/\d+\/progress$/.test(r.url()) &&
      r.request().method() === "POST" &&
      r.ok() &&
      (r.request().postDataJSON()?.scroll_fraction ?? 0) > 0,
  );
  await readerScroller(page).evaluate((el) => el.scrollTo(0, el.scrollHeight / 2));
  await saved;

  // Back to the library: the read title is now the "Continue reading" hero.
  await page.goto("/");
  await expect(page.getByText("Continue reading").first()).toBeVisible();
  await expect(rail(page, "Jump back in").getByRole("link", { name: new RegExp(SEED_TITLE) })).toBeVisible();

  // Resuming lands on the same chapter, scrolled back to where we stopped.
  await page.getByRole("button", { name: "Continue reading" }).click();
  await expect(page).toHaveURL(secondChapterUrl);
  await expect(page.locator(".reader-prose")).toBeVisible();
  await expect(page.getByText(seedChapter(2))).toBeVisible();
  await expect
    .poll(() => readerScroller(page).evaluate((el) => el.scrollTop))
    .toBeGreaterThan(0);
});

test("the Aa panel drives contents, chapter nav and focus mode", async ({ page }) => {
  await openSeededWork(page);
  const openAa = () => page.getByRole("button", { name: "Reading settings" }).click();

  // Contents: the drawer lists every gathered chapter and jumps to the picked one.
  await openAa();
  await page.getByRole("button", { name: "Contents", exact: true }).click();
  const toc = page.getByRole("dialog", { name: "Table of contents" });
  await expect(toc).toBeVisible();
  await toc.getByRole("button", { name: new RegExp(seedChapter(5)) }).click();
  await expect(toc).toBeHidden();
  await expect(page.getByText(seedChapter(5))).toBeVisible();
  await expect(page.locator(".reader-prose")).toContainText("Chapter 5");

  // Prev/Next in the same panel step chapters (the floating reader control they replaced is gone).
  await openAa();
  // "← Prev" is the panel's control; the one at the end of the chapter reads "← Previous".
  await page.getByRole("button", { name: "← Prev", exact: true }).click();
  await expect(page.getByText(seedChapter(4))).toBeVisible();

  // Focus mode hides all chrome and stays exitable. (The panel stays open across a chapter change,
  // so it's still on screen here. Its scrim covers the top bar, so a click on "Aa" lands on the
  // scrim and dismisses the panel — the same as clicking anywhere else outside it.)
  await page.getByRole("button", { name: "Focus mode" }).click();
  await expect(page.getByRole("button", { name: "Reading settings" })).toBeHidden();
  await expect(page.locator(".reader-prose")).toBeVisible();
  await page.getByRole("button", { name: "Exit focus mode" }).click();
  await expect(page.getByRole("button", { name: "Reading settings" })).toBeVisible();
});

test("the Aa panel behaves like the app's other dialogs", async ({ page }) => {
  await openSeededWork(page);
  const openAa = () => page.getByRole("button", { name: "Reading settings" }).click();

  // It's a real dialog to assistive tech and the keyboard, not a bare div: the scrim already made
  // everything behind it unreachable, so this only ever lacked the semantics.
  await openAa();
  const panel = page.getByRole("dialog", { name: "Reading settings" });
  await expect(panel).toBeVisible();

  // Escape closes it, like every other dialog. The reader's key handler bails out while the panel
  // is open, so Escape previously did nothing here and "Done" was the only way out.
  await page.keyboard.press("Escape");
  await expect(panel).toBeHidden();

  // Focus returns to the button that opened it, so keyboard users don't lose their place.
  await expect(page.getByRole("button", { name: "Reading settings" })).toBeFocused();

  // Stepping chapters from inside the panel still leaves it open (deliberate — you can walk through
  // chapters without reopening it), and it stays dismissable afterwards. Land on a known chapter
  // first: this suite runs workers:1 on one shared account, so where the reader resumes depends on
  // whichever spec ran before, and the last chapter has no "Next →".
  await openAa();
  await page.getByRole("button", { name: "Contents", exact: true }).click();
  const toc = page.getByRole("dialog", { name: "Table of contents" });
  await toc.getByRole("button", { name: new RegExp(seedChapter(3)) }).click();
  await expect(toc).toBeHidden();

  await openAa();
  // Scoped to the dialog: the end-of-chapter control carries the same label.
  await panel.getByRole("button", { name: "Next →", exact: true }).click();
  await expect(page.getByText(seedChapter(4))).toBeVisible();
  await expect(panel).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(panel).toBeHidden();
});
