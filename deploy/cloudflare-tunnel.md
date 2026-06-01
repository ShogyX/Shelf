# Exposing Shelf with a Cloudflare Tunnel (hardened)

This guide exposes Shelf to the internet **without opening any inbound port** on the
VM, terminates TLS at Cloudflare, and restricts access to people who hold credentials.

```
Browser ──HTTPS──▶ Cloudflare edge (TLS, WAF, rate-limit, optional Access)
                       │  outbound-only tunnel
                       ▼
                   cloudflared  ──http──▶  127.0.0.1:8000 (Shelf, loopback-only)
```

Two layers of "only those with credentials":

1. **Cloudflare Access (Zero Trust)** — blocks unauthenticated requests *at the edge*,
   before they ever reach your VM. Strongly recommended.
2. **Shelf's own login** — session-cookie auth, per-user accounts, admin roles,
   brute-force lockout. Always on (defense in depth).

---

## 1. Install Shelf in hardened tunnel mode

On the VM:

```bash
SHELF_TUNNEL=1 ./install.sh
# optional, recommended for first-run safety on an already-public box:
SHELF_TUNNEL=1 SHELF_SETUP_TOKEN="$(openssl rand -hex 16)" ./install.sh
# optional: pin the Host header to your domain (JSON list)
SHELF_TUNNEL=1 SHELF_ALLOWED_HOSTS='["shelf.example.com","127.0.0.1","localhost"]' ./install.sh
```

`SHELF_TUNNEL=1` makes the installer:

- **bind the app to `127.0.0.1` only** — the public NIC is never listening, so the app
  is unreachable except through the local tunnel;
- set `SHELF_TRUST_PROXY=true` (trust `X-Forwarded-Proto` / `CF-Connecting-IP` from the
  local proxy only), `SHELF_COOKIE_SECURE=true`, and `SHELF_HSTS=true`.

> **Create the first admin before the tunnel is public** (open it on the VM via an SSH
> tunnel: `ssh -L 8000:127.0.0.1:8000 user@vm`, then visit `http://localhost:8000`), or
> set `SHELF_SETUP_TOKEN` so the setup page can't be claimed by a stranger.

## 2. Lock down the VM firewall

Only SSH should be reachable from the internet; everything else goes through Cloudflare.

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH
sudo ufw enable
```

## 3. Install + configure cloudflared

```bash
# Debian/Ubuntu
curl -L https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt-get update && sudo apt-get install -y cloudflared

cloudflared tunnel login
cloudflared tunnel create shelf
cloudflared tunnel route dns shelf shelf.example.com
```

Create `/etc/cloudflared/config.yml` (see `deploy/cloudflared-config.example.yml`):

```yaml
tunnel: <TUNNEL-UUID>
credentials-file: /root/.cloudflared/<TUNNEL-UUID>.json
ingress:
  - hostname: shelf.example.com
    service: http://127.0.0.1:8000     # match SHELF_PORT
  - service: http_status:404
```

Run it as a service:

```bash
sudo cloudflared service install
sudo systemctl enable --now cloudflared
```

## 4. Put Cloudflare Access in front (recommended)

Cloudflare Zero Trust → **Access → Applications → Add a self-hosted app**:

- Application domain: `shelf.example.com`
- Add a **policy**: *Allow* only specific emails / an email domain / a one-time-PIN /
  an identity provider (Google, GitHub, Okta…). Everyone else is blocked at the edge.

Now a request must pass Cloudflare Access **and** log into Shelf.

## 5. Recommended Cloudflare dashboard settings

- **SSL/TLS mode: Full (strict)**.
- **Always Use HTTPS** + **Automatic HTTPS Rewrites** + minimum TLS 1.2.
- **WAF** managed rules on; add a **Rate Limiting** rule on `/api/auth/login`.
- Optionally restrict to specific countries / enable Bot Fight Mode.

---

## What Shelf enforces on its own

- All API routes require a valid session cookie; only `/api/health` and the auth
  endpoints are open. The SPA gates to a login/setup screen.
- Passwords hashed with PBKDF2-SHA256 (200k iterations); minimum length 8
  (`SHELF_MIN_PASSWORD_LENGTH`).
- **Login brute-force lockout**: after `SHELF_LOGIN_MAX_ATTEMPTS` (default 6) failures in
  `SHELF_LOGIN_WINDOW_SECONDS` (default 900s), further attempts return `429` — keyed by
  both account and client IP (real IP via `CF-Connecting-IP` behind the proxy).
- **Secure, httpOnly, SameSite** session cookies (Secure auto-on behind HTTPS).
- **Security headers**: CSP, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`,
  `Referrer-Policy`, `Permissions-Policy`, HSTS (over HTTPS), no `Server` header.
- **Interactive API docs disabled** by default (`SHELF_ENABLE_DOCS=false`).
- Static file serving is confined to the build dir (no path traversal).
- First-run **setup can be gated with `SHELF_SETUP_TOKEN`**; once an admin exists, setup
  is closed.

### Relevant env vars (set in the unit or `backend/.env`)

| Variable | Default | Purpose |
|---|---|---|
| `SHELF_HOST` | `0.0.0.0` | Bind address (tunnel mode → `127.0.0.1`) |
| `SHELF_TRUST_PROXY` | `false` | Trust `X-Forwarded-*` / `CF-Connecting-IP` from `SHELF_FORWARDED_ALLOW_IPS` |
| `SHELF_COOKIE_SECURE` | `false` | Force `Secure` cookies (auto-on behind HTTPS when trust_proxy) |
| `SHELF_COOKIE_SAMESITE` | `lax` | `lax` \| `strict` \| `none` |
| `SHELF_ALLOWED_HOSTS` | `["*"]` | Allowed `Host` headers (JSON list) |
| `SHELF_LOGIN_MAX_ATTEMPTS` | `6` | Failed logins before lockout |
| `SHELF_LOGIN_WINDOW_SECONDS` | `900` | Lockout / sliding window |
| `SHELF_MIN_PASSWORD_LENGTH` | `8` | Minimum password length |
| `SHELF_SETUP_TOKEN` | `""` | Shared secret required to create the first admin |
| `SHELF_ENABLE_DOCS` | `false` | Expose `/docs` + `/openapi.json` |
| `SHELF_HSTS` | `true` | Emit HSTS over HTTPS |

> Rotate any secret you ever pasted into a terminal or chat (GitHub tokens, setup tokens).
