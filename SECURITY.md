# Security Policy

## Reporting a vulnerability

**Please do not open a public issue for security vulnerabilities.**

Report privately through GitHub's **[Security Advisories](https://github.com/ShogyX/Shelf/security/advisories/new)**
(repo → **Security** tab → **Report a vulnerability**). This keeps the report
confidential until a fix is available.

Please include:

- A description of the issue and its impact.
- Steps to reproduce (a proof-of-concept if you have one).
- Affected version / commit, and your environment (how Shelf is exposed).

You can expect an initial acknowledgement within a few days. Once a fix is
released, we're happy to credit you in the advisory unless you prefer to remain
anonymous.

## Supported versions

Shelf is developed on `main`. Security fixes land on `main`; there are no
long-term support branches. Always run the latest commit (`./install.sh` is
idempotent — re-run it to update).

## Scope & hardening notes

Shelf is **self-hosted**: its security posture depends heavily on how you deploy
it. The application ships secure-by-default building blocks, but YOU own the
deployment.

What the app enforces:

- **Authentication on every route** — session cookies are HTTP-only,
  `SameSite`, and `Secure` when `SHELF_COOKIE_SECURE`/tunnel mode is on.
- **Password hashing** (PBKDF2) and **login brute-force lockout**.
- **First-admin protection** — over a public connection, initial admin setup is
  refused (HTTP 403) unless `SHELF_SETUP_TOKEN` is set.
- **Security headers** — CSP, HSTS (https only), `X-Frame-Options`, plus a
  `Host` allow-list (`SHELF_ALLOWED_HOSTS`) and disabled API docs by default.
- **SSRF egress guard** — all user-supplied/crawled URLs are validated against
  internal/link-local/metadata address ranges, with the connection pinned to the
  validated IP (DNS-rebinding-safe) and re-checked on every redirect hop.
- **HTML sanitization** of all ingested content before it is rendered/stored.

What YOU are responsible for:

- **Never expose Shelf directly on the internet without hardening.** Run
  `SHELF_TUNNEL=1 ./install.sh` and front it with a reverse-proxy tunnel +
  access control. See **[`deploy/cloudflare-tunnel.md`](deploy/cloudflare-tunnel.md)**.
- **Create the first admin from localhost** (or an SSH tunnel) before the address
  is public, or set `SHELF_SETUP_TOKEN`.
- **Keep secrets out of the repo.** Configuration lives in `backend/.env`
  (gitignored) or `SHELF_*` environment variables — see
  [`backend/.env.example`](backend/.env.example). Never commit real credentials.
- **Respect the sourcing policy** — only enable ingestion sources for content you
  have the right to read (see the README's "Sourcing policy" section).

## Dependencies

Automated dependency-update PRs (Dependabot) and code scanning (CodeQL) run from
`.github/`. Review and merge security updates promptly.
