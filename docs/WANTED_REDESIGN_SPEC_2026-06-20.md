# Wanted ⨉ Following merge — redesign + feature spec (ui-ux-pro-max)

Design produced via the `ui-ux-pro-max` skill (ui-designer agent), grounded in a
read-only survey. Fits Shelf's existing Tailwind + `ui.tsx` kit — no new visual
style, just better IA + density + two new features (Planned/Released tag, mass rescan).

## The merged page (replaces Missing.tsx + Following.tsx)
- **One page** unifying "titles I want" with "authors/series I follow". Primary axis =
  the author/series, not the individual title.
- **IA:** collapsible accordion, Author → Series → title rows; standalone titles under
  their author; an "Ungrouped/Other" bucket last. A FOLLOW is an attribute of a group
  header (the follow toggle lives there). A followed author/series with zero wanted
  items still shows as a group header ("Following — new releases auto-fetch. Nothing
  outstanding.").
- **Pattern:** Data-Dense Dashboard / collapsible grouped list (dense rows in ONE outer
  Card with `divide-y`, not a stack of big cards). `max-w-3xl`, mobile-first.
- **Group header:** chevron · name · kind badge · counts (`8 owned · 3 wanted · 1 planned`,
  non-zero only) · **Follow toggle** · admin **Rescan** · `⋯` overflow (Request all,
  Recheck force). Author tier `bg-surface-2`; series tier indented `pl-4`, hosts the
  SeriesModal chip.
- **Title row (one dense line):** title · `#pos` · **Released/Planned tag** · status badge
  (open/searching=violet/unavailable=amber+reason/resolved=green) · **3 source dots `T U A`
  + the existing ℹ popover** · attempts·next-recheck · admin recheck icon. Mobile: actions
  always-visible icons, meta behind ℹ.
- **Controls bar:** Sort (new default "Needs attention"; + Author/Series/Title/Newest) ·
  admin filters (status/reason/origin) · "Followed only" · "Hide planned" · Expand/Collapse
  all. Collapses into a "Sort & filter" Disclosure on mobile. Admin stats in a summary strip.
- **States:** skeleton load (not spinner); 4 empty cases; admin-vs-user gating exactly as
  today (filters/stats/recheck/rescan admin-only; sort + follow for everyone).

## Mass rescan (R: easy trigger, batched, sequential — never parallel)
- Affordances: **Rescan all** (summary strip, admin), per-group **Rescan** (header),
  per-row **Recheck now** (existing). Large rescans `useConfirm` ("Queue 42 titles? They
  run in batches, a few at a time.").
- **Progress strip** (persistent in the summary Card while the queue is non-empty):
  `⟳ Rescanning · 6 of 42 done · 2 in progress · 34 queued [Pause][Cancel]` + a bg-accent
  progress fill. Rows show `Queued`→`Rescanning…`→resolved; group headers show `Queued(3)`.
  One run in flight globally (triggering again ADDS to the queue). Completion → one summary
  toast. Drives off a polling query of the rescan-queue status.

## Planned vs Released (R: tag + pipeline skips non-released)
- **Planned** = an announced future title (e.g. next volume of a followed series) the
  pipeline will NOT search until it releases. Reads as *waiting*, distinct from
  *unavailable* (released, searched, not found).
- Tag: `Badge violet` `🕘 Planned · 2026` (clock + expected year/date) + muted "waiting for
  release"; no source dots, no recheck, no attempts; group counts split `3 wanted · 1 planned`.
- Released = default (no Planned tag; the status badge carries state). Color discipline:
  violet="system handles it, wait"; amber="needs attention"; glyph+text, never color alone.

## ui-ux-pro-max guidelines applied
Data-Dense Dashboard style; Empty States, Bulk Actions, Loading States (skeleton +
progress strip, not toast), Loading Buttons, Submit Feedback, Color-Only a11y,
Number-Tabular, Truncation, Overflow/Sticky/Mobile-table, Progressive Disclosure.
</content>
