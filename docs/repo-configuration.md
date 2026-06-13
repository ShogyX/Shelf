# GitHub repository configuration

The repo ships its automation as files under `.github/` (CI, CodeQL, Dependabot,
templates, CODEOWNERS). A few settings can only be toggled in the GitHub **web UI**
(or via the API) — do these once after pushing.

## Security (Settings → Code security and analysis)

- [ ] **Dependabot alerts** — on (free for all repos).
- [ ] **Dependabot security updates** — on (the included `dependabot.yml` also opens
      scheduled version-bump PRs).
- [ ] **Secret scanning** + **Push protection** — on (free for public repos; blocks
      commits that contain known secret formats).
- [ ] **Code scanning** — the included `codeql.yml` runs automatically on public
      repos. Private repos need GitHub Advanced Security.
- [ ] **Private vulnerability reporting** — on (powers the link in `SECURITY.md`).

## Branch protection / rulesets (Settings → Branches, or Rules → Rulesets)

Protect `main`:

- [ ] **Require a pull request before merging** — and require **1 approval**.
- [ ] **Require review from Code Owners** (uses `.github/CODEOWNERS`).
- [ ] **Require status checks to pass** — select `Backend (pytest)` and
      `Frontend (typecheck + build)` from the CI workflow.
- [ ] **Require branches to be up to date before merging**.
- [ ] **Require conversation resolution before merging**.
- [ ] **Do not allow bypassing the above** (optional but recommended).
- [ ] Disallow force-pushes and deletions on `main`.

## General (Settings → General)

- [ ] Add a **description** and **topics** (e.g. `self-hosted`, `reader`, `fastapi`,
      `react`, `manga`, `epub`) so the repo is discoverable.
- [ ] Set **Pull Requests → Allow squash merging** (and disable merge commits if you
      prefer a linear history).
- [ ] Enable **Automatically delete head branches** after merge.

## Equivalent `gh` CLI (optional)

If you have the GitHub CLI authenticated, the branch ruleset can be created via the
API; the UI is simpler for a one-time setup. Code/secret scanning and Dependabot
are enabled under *Code security and analysis*.
