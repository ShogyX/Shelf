# Shelf — Frontend & UX Review + Improvement Plan

**Date:** 2026-06-21
**Goal:** Move Shelf from "competent self-hosted tool" to an **intuitive, seamless, premium** experience.
Two stated pain points, in priority order:

1. **Nested menus / drilling through items** — clicking into a thing, then into a sub-thing, then an
   action, gets deep, lost, and irreversible. *This is the #1 problem.*
2. **General styling & feel** — next to the sleek, snappy, premium feel of Netflix, Shelf falls flat.

**Method:** baseline research on intuitive-UI + premium-feel + nested-navigation patterns; teardown of
8 analogous apps (Netflix, Plex, Jellyfin, Audiobookshelf, Komga/Kavita, Calibre-Web, Readarr/Sonarr,
Spotify/Apple Books/Libby); first-hand **code review** of the interaction model; and a **visual browser
review** against an isolated seeded instance (screenshots in `/tmp/shelf-shots/`). The three streams
converge on the same handful of root causes.

> Framing the user asked for: **improve / add / merge / replace** toward a more intuitive, seamless,
> premium UX. Each finding below is tagged accordingly.

---

## 0. The gap, in one paragraph

The token architecture (`themes.ts` → CSS vars → Tailwind) is genuinely good, so most of the *visual*
gap is value swaps, not a rewrite. Premium apps feel premium for four reasons Shelf currently lacks:
**(1) cover art is the hero** (Shelf shows 7.5rem thumbnails beside text inside bordered cards — a
"competent tool" giveaway, not a poster wall); **(2) depth + motion** (Shelf has `shadow-sm` on
everything and no hover-lift/skeletons/eased transitions); **(3) a deep, layered dark theme** (Shelf's
dark surface steps are nearly identical, so it reads "admin panel," not "cinematic"); **(4) restraint +
confident type** (flat page titles, low-contrast muted text). Separately, the *interaction* gap is
**structural**: every drill-in (catalog detail, series, author, library series, reader TOC) is React
component state, not a URL — so the browser **Back button does nothing, refresh loses context, nothing
is shareable**, and because each card owns its own modals you can stack **4 dialogs deep**
(card → detail → series → confirm → shelf-prompt). On top of that, cards expose **6+ competing actions**
at the same visual weight, so the primary action never wins. Fix those two roots — URL-as-state and
one-primary-action — and the "nested menu" complaint largely dissolves.

---

## 1. Baseline takeaways (what the good apps do)

Condensed from the research dossier + teardowns. Full principle list (Nielsen heuristics, Hick/Fitts,
progressive disclosure) in the dossier; the adoptable specifics:

| Lever | What premium apps do | Where Shelf applies it |
|---|---|---|
| **URL = state** | Every drill-in level is a URL (Plex/Spotify/Linear). Free Back/Forward, deep-links, refresh-survival, scroll-restore. | Catalog detail, series, author, reader TOC |
| **No modal-on-modal** | One modal level max; deeper = routes or a swap-content wizard with internal back/next. | Catalog/Library drill-ins |
| **One primary + "…" overflow** | Cap visible actions to 3–5 (Hick's law); demote the rest. | Catalog card, Library card, Watchlist row |
| **Master-detail / side-sheet** | Inspect one item while the grid stays visible (Netflix expand, Material side sheet). | Catalog detail, series/author pickers |
| **Cover-art-forward** | Large edge-to-edge posters, neutral chrome, art supplies the color (Plex/Spotify/Apple Books). | Library + Catalog grids |
| **Hover-peek** | Card scales 1.03–1.05 + elevation + quick-actions in ~120ms, neighbors stable (Netflix). | Library/Catalog tiles |
| **Skeletons not spinners** | ~20% faster *perceived*; spinners only for tiny inline buttons. | Route loads, grids, detail |
| **Optimistic UI** | Flip state instantly, reconcile on server, quiet rollback (Linear/Spotify). | Follow, shelf toggle, mark-read |
| **Prefetch on intent** | Prefetch detail+cover on hover/near-scroll (`prefetchQuery`). | Grid → detail |
| **Motion house-style** | 150–250ms, ease-out enter / ease-in exit, springs for drag, *restrained*. | Global |
| **⌘K command palette** | Flat shortcut over the hierarchy (Linear/Vercel/Raycast). | Global accelerator |
| **Persistent player/reader bar + "Continue" shelf** | Resume front-and-center (Audiobookshelf/Spotify). | Home row + reader |
| **Series top-level, hub detail** | Don't bury series under author (Sonarr lesson); detail = art backdrop + 1 CTA + horizontal "more from series/author" rows (Plex hub). | Series drill-in |
| **Interactive release grid** | Show source · format · size · health, best-match pre-selected, one-click grab (Sonarr). | Acquire flow |

---

## 2. Findings

Severity: **P0** structural root / highest-impact · **P1** high · **P2** polish. Tag = improve/add/merge/replace.
Code references are `file:line` from the review.

### A. Nested navigation & modal stacking — *the #1 pain* (mostly REPLACE/ADD)

- **A1 · P0 · replace** — **No URL-as-state anywhere.** All drill-ins are component state (`Index.tsx:244`
  renders `CatalogDetail` from `detail`; reader TOC/settings are local; library series is local). Back is
  unreliable, refresh loses context, nothing is shareable, no scroll-restore. *Root cause of the friction.*
  → Put the open item in the URL: start cheap with `?detail=:id`/`?series=:name` via `useSearchParams`
  (no new routes), graduate to nested routes `/catalog/:groupId[/series|/author]`.
- **A2 · P0 · replace** — **4-deep modal stacking.** `CatalogCard` owns `SeriesModal`/`AuthorModal` as
  local state (`CatalogCard.tsx:422-435`); each opens a `confirm()` then a `ShelfPrompt`/`AcquirePrompt`
  on top → worst path **card → detail → series → confirm → shelf-prompt**. `useDialogFocus` stacks
  correctly (one Esc = one layer) but there's no "close all" and no Back. → Lift series/author to
  routes/side-sheet so max depth is 2 (detail → one transient prompt). *Never* modal-on-modal-on-modal.
- **A3 · P1 · replace** — **`CatalogDetail` is a centered `fullscreen-sheet`** that covers the grid
  (`CatalogCard.tsx:1042`), so inspecting one title hides the list you're scanning. → Right-**side sheet**
  (the `sheet` variant already exists, `ui.tsx:118`) keeps the grid visible — master-detail browse.
- **A4 · P1 · add** — **No breadcrumbs / in-drill back-affordance.** Once 2 levels deep the only way up
  is the ✕. → Breadcrumb trail on routed detail (Library / Author / Series / Title) + always-visible back.
- **A5 · P2 · improve** — **Reader drawers are local state** (`Reader.tsx:21-23`), so hardware/phone Back
  exits to Library instead of closing the TOC. → Map open-drawer to a URL/history entry.

### B. Competing affordances / choice overload — *the #1 pain, visible layer* (REPLACE/MERGE)

- **B1 · P0 · replace** — **Catalog card shows 6+ actions at equal weight** (`CatalogCard.tsx:333-406`):
  Acquire + View Series + Find anyway + View N sources + cover-button + title-button + badge-button +
  author popover (with 2 more inside). Three are identical ghost buttons; the primary never wins. → **One
  `[Acquire ▾]` primary** (▾ = format/shelf inline) **+ one `⋯` overflow** carrying View Series / Find
  anyway / Follow / Request all / View sources. "Find anyway" is an expert escape hatch — never top-level.
- **B2 · P0 · merge** — **Three elements route to the same place.** Cover, title, and "View N sources →"
  all call `onOpenDetail` (`CatalogCard.tsx:248/266/387`); the badge navigates to the reader; author text
  toggles a popover. Clicking different things does 3 different jobs and 3 do the same. → Whole card body =
  one click target → detail; explicit buttons only for *divergent* actions.
- **B3 · P1 · replace** — **Library work card hover-reveals up to 8 buttons** (Read, 🎧, 📤 Send, 🗂
  Shelves, 🩺 Fix, ⟳ Updates, ⏸ Pause, Remove) and they're `sm:opacity-0` until hover (`Library.tsx:733`)
  — invisible affordances on desktop. → Always-visible **Read** (on the cover) + one `⋯` overflow; drop
  the hover-reveal.
- **B4 · P2 · improve** — Watchlist rows duplicate follow-toggle + rescan at group *and* sub-group level
  plus a series chip opening yet another modal (`Watchlist.tsx:480/593/573`). Tidy into one row control set.

### C. Cover-art-forward & premium feel — *the #1 visual lever* (REPLACE/ADD)

- **C1 · P0 · replace** — **Covers are thumbnails beside text, not a poster wall.** → Convert Library +
  Catalog to a true poster grid: `aspect-[2/3]` tiles, `rounded-lg overflow-hidden shadow-md`, **no
  surrounding card border**, `grid grid-cols-3 sm:grid-cols-4 lg:grid-cols-6 gap-4`, min tile ~140–180px;
  title/author/badges *below* the poster (or on a hover gradient scrim). The cover **is** the card.
- **C2 · P1 · add** — **Hover-peek + lift.** `transition-[transform,box-shadow] duration-200 ease-out
  hover:-translate-y-1 hover:shadow-xl`, neighbors stable, quick-actions revealed on the scrim. Single
  biggest "Netflix" tell after the grid itself.
- **C3 · P1 · improve** — **Richer fallback covers.** Current solid-color placeholder is flat. → per-title
  gradient from a title hash + large serif title + small author + faint grain, so a sparse library still
  looks curated.
- **C4 · P1 · add** — **"Continue reading" as a distinct hero row** (wider cards, progress bar baked onto
  the art, Netflix "Continue Watching" style) above the poster grid — not the same small card.
- **C5 · P2 · add** — Ambient cover-derived gradient header on the (routed) item/series detail (Spotify/Plex).

### D. Depth, motion & perceived speed (IMPROVE/ADD)

- **D1 · P0 · replace** — **`shadow-sm` + borders on everything = no depth.** → Layered shadow on `Card`
  (`ui.tsx:139`): light `shadow-[0_1px_2px_rgba(16,18,27,.04),0_8px_24px_-12px_rgba(16,18,27,.12)]`; dark
  drop the border, use surface-step + `shadow-[0_8px_30px_rgba(0,0,0,.5)]`. Border-**or**-shadow, rarely both.
- **D2 · P1 · add** — **Skeletons, not the bare spinner.** Route-level Suspense fallback is a top-left
  `Spinner` (`App.tsx:185`) that flashes on every code-split nav; grids/detail show 1-line spinners. →
  `<Skeleton>` (`animate-pulse bg-surface-2 rounded`), poster-shaped placeholders; spinners only inside
  tiny buttons. Watchlist already has a good skeleton (`Watchlist.tsx:810`) — generalize it.
- **D3 · P1 · add** — **Motion house-style.** Define `--ease: cubic-bezier(.2,.8,.2,1)`; `duration-150–200`
  on interactive transitions; `active:scale-[.97]` on `Button` (`ui.tsx:151`); page-enter fade+slide-up
  8px (`duration-300`); ~20–30ms grid stagger.
- **D4 · P1 · add** — **Optimistic toggles** for Follow and Shelf-membership (TanStack `onMutate` cache
  write; infra already used for `pendingId`). Also stop `busyAny` from disabling the *whole* card during
  one action (`CatalogCard.tsx:230`) — disable per-action only.
- **D5 · P2 · add** — **Prefetch detail + cover on hover/near-scroll** (`prefetchQuery`) so opening is instant.
- **D6 · P2 · improve** — **De-duplicate acquire feedback.** Card fires toast *and* inline notice
  (`CatalogCard.tsx:138-168`); CatalogDetail does inline-only (`:949-981`). Pick one channel: toast for
  fire-and-forget/queued, inline for in-context result. Make both surfaces agree.

### E. Theme, typography, color (IMPROVE)

- **E1 · P0 · improve** — **Deepen dark theme + widen elevation steps.** Dark `bg/surface/surface-2`
  (`#14161a/#1c1f26/#232730`) are too close to read as layers. → e.g. `--bg:#0d0f13 --surface:#181b22
  --surface-2:#23272f --border:#333a45`. Same idea (subtler) for light `--surface-2`/`--border`.
  (`themes.ts`, `index.css`)
- **E2 · P1 · improve** — **Bigger, heavier page titles + uppercase accent eyebrow** via a shared
  `<PageHeader>` (`text-3xl sm:text-4xl font-bold tracking-tight` + `text-xs uppercase tracking-widest
  text-accent` kicker). Titles currently read like form labels.
- **E3 · P1 · improve** — **Primary button has no presence.** `bg-accent` + `hover:opacity-90` looks
  washed. → gradient `from-[#8b6bff] to-[#6d4cff]` + `shadow-[0_2px_8px_rgba(124,92,255,.35)]
  hover:brightness-110`. (`ui.tsx:151`)
- **E4 · P2 · improve** — Darken light-theme `--muted` (`#6b7280` → `~#565d68`); long descriptive
  paragraphs `text-text/80` not full muted.
- **E5 · P2 · improve** — Stronger glass nav: `backdrop-blur-xl bg-surface/70`, drop the hard
  `border-b border-border` (→ `border-border/50` or a 1px shadow) so content scrolls *through* it. (`App.tsx:120`)

### F. Forms, controls, first-impression (IMPROVE/REPLACE)

- **F1 · P1 · replace** — **Native `<select>` shows raw OS chrome** (`ui.tsx:249`, Watchlist/Users filters).
  → `appearance-none` + custom chevron + `inputCls`. Visible "default form" tell.
- **F2 · P1 · improve** — **Login screen is a plain white card in a grey void** with a desaturated button —
  the literal first impression says "internal tool." → full-bleed gradient/cover-collage bg + frosted
  `backdrop-blur` card + larger brand mark + the glowing primary button.
- **F3 · P2 · replace** — **Emoji icons** (📚 🎨 🔔 🍱) render inconsistently per-OS and read casual. →
  one icon set (Lucide/Heroicons); a real logo mark for the brand.
- **F4 · P2 · improve** — Empty states are dashed-border boxes (`ui.tsx:379`) → centered art in an
  accent-tinted circle + confident title + CTA; drop the dashed rectangle.

### G. Mobile (REPLACE/IMPROVE)

- **G1 · P0 · replace** — **The desktop nav `flex-wrap`s into a 5-row stack on mobile** (`App.tsx:128`),
  eating half the first screen — the worst mobile offense. → bottom tab bar (Library / Catalog / Watchlist
  / Settings) + "More" sheet, or hamburger → slide-in drawer.
- **G2 · P1 · improve** — Mobile poster grid `grid-cols-2 sm:grid-cols-3` (vs today's single-column list).
- **G3 · P2 · improve** — Reader bottom controls clear `env(safe-area-inset-bottom)`; tap targets ≥44px;
  on mobile prefer drag-dismiss bottom sheets over centered modals for item actions.

### H. Consistency / merge (MERGE)

- **H1 · P1 · merge** — **Three different "series" surfaces:** `SeriesModal` (catalog,
  `CatalogCard.tsx:441`), `AuthorModal` (`:621`, near-identical), and `SeriesLibraryModal`
  (`Library.tsx:899`, *hand-rolled*, doesn't use the `Modal` primitive). Same mental model, three behaviors.
  → one `<SeriesView>` on the shared `Modal`/side-sheet, used by catalog, watchlist, library.
- **H2 · P2 · merge** — **Three shelf UIs:** unified `ShelfPrompt`/`AcquirePrompt`, plus `ShelfMenu` inline
  checkbox popover (`Library.tsx:29`) and `ShelfDialog` creation dialog (`:145`). Merge `ShelfMenu` into the prompt.
- **H3 · P2 · improve** — Pick **one** cover/title click convention app-wide (today: catalog→detail,
  library→reader, library-series→series modal). Document it.

### I. Settings findability (ADD/IMPROVE)

- **I1 · P1 · add** — **Config lives at 3 depths** (tab → card → Disclosure → SystemConfigCard group) with
  **no search.** Logging is in Backup→Disclosure; List-import polling in Acquisition→Disclosure; Image
  cache in Storage→Disclosure; "Crawl speed" is a `Section` inside `IndexingCard` (invisible to scanning).
  → add a **settings search/filter box**; flatten single-item Disclosures. (`Settings.tsx:1196-1232`)

---

## 3. Implementation plan (phased)

Sequenced for **impact-to-effort** and to de-risk the one structural change (URL-as-state) that unlocks
the rest. Each wave is independently shippable with a verify step. Quick wins first so the app *feels*
better immediately while the structural work lands.

### Wave 1 — Premium-feel quick wins (visual, low effort, no refactor)
Mostly token/class swaps in `ui.tsx`, `themes.ts`, `index.css`, `App.tsx`.
- D1 layered card shadows · D3 motion house-style (`--ease`, durations, `active:scale`, page-enter) ·
  C2 hover-lift on cards · E1 deepen dark theme + elevation steps · E2 `<PageHeader>` (titles+eyebrow) ·
  E3 gradient/glow primary button · E5 stronger glass nav · F1 styled `<select>` · F4 empty-state art.
- **Verify:** screenshot Library/Catalog/Reader/Settings light+dark before/after; confirm hover-lift,
  press, and page-enter animate; AA contrast on muted text; no layout shift.

### Wave 2 — Cover-art-forward grids (the #1 visual lever)
- C1 poster-grid Library + Catalog (new `<PosterTile>` + `<PosterGrid>` component; replace the
  card-beside-text layout) · C3 richer gradient fallback covers · C4 "Continue reading" hero row ·
  D2 poster-shaped skeletons · G2 mobile 2-col grid.
- **Verify:** Library/Catalog render as a poster wall at 320/768/1280px; covers + fallbacks crisp;
  skeleton matches final layout; "Continue reading" visually distinct.

### Wave 3 — De-clutter cards: one primary + overflow (the #1 interaction quick win)
- B1 catalog card → `[Acquire ▾]` + `⋯` · B2 whole-card-body → detail, kill duplicate click targets ·
  B3 Library card → always-visible Read + `⋯`, drop hover-reveal · B4 tidy Watchlist row controls ·
  A4 (start) consistent back/close affordance.
- **Verify:** each card has exactly one obvious primary action; overflow holds the rest; click anywhere
  on the body opens detail; no action hidden behind hover on desktop.

### Wave 4 — URL-as-state + kill modal stacking (the structural backbone)
- A1 `?detail=`/`?series=` via `useSearchParams`, then nested routes `/catalog/:groupId[/series|/author]` ·
  A2 lift Series/Author out of `CatalogCard` local state (max depth 2) · A3 `CatalogDetail` → side-sheet ·
  A5 reader drawer ↔ history · A4 breadcrumbs on routed detail.
- **Verify:** open a detail → **browser Back closes it**; **refresh keeps it**; the URL is shareable;
  the grid stays visible behind the side-sheet; max one transient prompt ever stacks.

### Wave 5 — Perceived speed + consistency
- D4 optimistic Follow/Shelf toggles + per-action disabling · D5 prefetch-on-hover · D6 unify acquire
  feedback · H1 one `<SeriesView>` · H2 merge `ShelfMenu` into the prompt · H3 one click convention.
- **Verify:** toggles flip instantly (rollback on forced error); opening a prefetched detail is instant;
  one series component across catalog/library/watchlist; feedback channel consistent.

### Wave 6 — Mobile + global accelerators + settings
- G1 mobile bottom tab bar / drawer · G3 reader safe-area + tap targets · I1 settings search + flatten
  Disclosures · F2 login glow-up · F3 icon set + logo · (stretch) ⌘K command palette · C5 ambient detail header.
- **Verify:** mobile first-screen shows content (no 5-row nav); settings search finds buried items;
  ⌘K jumps to any work/series/setting; Lighthouse a11y unchanged or better.

---

## 4. Highest-leverage shortlist

If only a handful ship, do these — they answer both stated complaints directly:

1. **Poster-wall grids + hover-lift + skeletons** (C1, C2, D2) — closes most of the "Netflix feel" gap.
2. **One primary action + `⋯` overflow on every card** (B1–B3) — kills the visible "too many menus" pain.
3. **URL-as-state for drill-ins + ban modal-on-modal** (A1–A3) — makes Back/refresh/share work; collapses
   4-deep stacks to 2. *The structural fix the nested-menu complaint is really about.*
4. **Depth + motion + deep dark theme** (D1, D3, E1) — the cheap, global "premium" multiplier.

---

## Appendix — review artifacts

- Screenshots: `/tmp/shelf-shots/` (all pages, 8 settings tabs, 4 add tabs, reader, theme picker, mobile).
- Isolated review instance was seeded data on a spare port/DB — **prod `shelf.db` was never touched**.
- Three source reviews (UX research dossier, visual design critique, interaction code review) underlie
  every finding; file:line references are from the live code as of this date.
</content>
</invoke>
