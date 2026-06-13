<!-- Thanks for contributing to Shelf! Keep PRs small and focused. -->

## What & why

<!-- What does this change and what problem does it solve? Link any issue (#123). -->

## How it was verified

<!-- Tests added/updated, manual checks, commands run. -->

- [ ] `cd backend && pytest` is green
- [ ] `cd frontend && npm run build` typechecks + builds
- [ ] New behavior has tests where it makes sense

## Checklist

- [ ] No secrets/credentials are committed (config goes in `.env` / `SHELF_*`)
- [ ] If this touches ingestion, it respects the sourcing/compliance policy
      (no scrapers for content the operator has no right to read)
- [ ] Docs/README updated if behavior or config changed
