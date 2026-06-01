#!/usr/bin/env bash
#
# Shelf — fire-and-forget installer.
#
#   ./install.sh            # install / update + run as a systemd service
#   SHELF_PORT=9000 ./install.sh
#   SHELF_USER=alice ./install.sh
#
# It is idempotent: re-run any time to update deps, rebuild the UI, and restart.
# Works run as root or via sudo; it resolves its own location so the working
# directory does not matter, runs the app as a systemd service, and picks a free
# port so it never collides with something already listening.
#
set -euo pipefail

APP_NAME="Shelf"
SERVICE="shelf.service"
DESIRED_PORT="${SHELF_PORT:-8000}"

# --- locate the repo (this script's directory) -----------------------------
SOURCE="${BASH_SOURCE[0]:-$0}"
while [ -L "$SOURCE" ]; do SOURCE="$(readlink "$SOURCE")"; done
SCRIPT_DIR="$(cd "$(dirname "$SOURCE")" >/dev/null 2>&1 && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"
FRONTEND_DIR="$SCRIPT_DIR/frontend"
VENV="$BACKEND_DIR/.venv"

c_info='\033[1;36m'; c_ok='\033[1;32m'; c_warn='\033[1;33m'; c_err='\033[1;31m'; c_off='\033[0m'
log()  { printf "${c_info}==>${c_off} %s\n" "$*"; }
ok()   { printf "${c_ok}✓${c_off} %s\n" "$*"; }
warn() { printf "${c_warn}warn:${c_off} %s\n" "$*" >&2; }
die()  { printf "${c_err}error:${c_off} %s\n" "$*" >&2; exit 1; }

[ -d "$BACKEND_DIR" ] || die "backend/ not found next to install.sh (looked in $SCRIPT_DIR)"

# --- privilege: run as root, or escalate the privileged bits via sudo -------
if [ "$(id -u)" -eq 0 ]; then
  SUDO=""
elif command -v sudo >/dev/null 2>&1; then
  SUDO="sudo"
else
  die "this installer needs root (for system packages + systemd); install sudo or run as root"
fi

# --- who owns + runs the service (so the SQLite DB stays writable) ----------
owner_of() { stat -c '%U' "$1" 2>/dev/null || stat -f '%Su' "$1" 2>/dev/null || true; }
RUN_USER="${SHELF_USER:-}"
[ -n "$RUN_USER" ] || RUN_USER="$(owner_of "$SCRIPT_DIR")"
[ -n "$RUN_USER" ] && [ "$RUN_USER" != "root" ] || RUN_USER="${SUDO_USER:-root}"
id "$RUN_USER" >/dev/null 2>&1 || RUN_USER="root"
RUN_HOME="$(getent passwd "$RUN_USER" 2>/dev/null | cut -d: -f6)"; [ -n "$RUN_HOME" ] || RUN_HOME="/root"
log "Installing $APP_NAME from $SCRIPT_DIR (service user: $RUN_USER)"

# --- 0) safety: never lose data — snapshot any existing database first ------
if [ -f "$BACKEND_DIR/shelf.db" ]; then
  _stamp="$(date +%Y%m%d-%H%M%S 2>/dev/null || echo backup)"
  _bk="$BACKEND_DIR/shelf.db.bak-$_stamp"
  for _ext in "" "-wal" "-shm"; do
    [ -f "$BACKEND_DIR/shelf.db$_ext" ] && cp -p "$BACKEND_DIR/shelf.db$_ext" "${_bk}$_ext" 2>/dev/null || true
  done
  [ -f "$_bk" ] && ok "Backed up existing database → $(basename "$_bk")"
fi

# Run a command as the service user (keeps build artifacts correctly owned).
as_user() {
  if [ "$(id -un)" = "$RUN_USER" ]; then "$@"; else $SUDO -u "$RUN_USER" -H "$@"; fi
}

# The app provisions its OWN Python + Node toolchain when the system's are missing
# or too old, so installation succeeds regardless of what's on the host.
have() { command -v "$1" >/dev/null 2>&1; }
TOOLCHAIN="$SCRIPT_DIR/.toolchain"

fetch_stdout() { if have curl; then curl -fsSL "$1"; elif have wget; then wget -qO- "$1"; else return 1; fi; }
fetch_to()     { if have curl; then curl -fsSL "$1" -o "$2"; elif have wget; then wget -qO "$2" "$1"; else return 1; fi; }

# --- 1) only a downloader is required from the system (best-effort extras) --
detect_pm() { for c in apt-get dnf yum pacman zypper apk; do command -v "$c" >/dev/null 2>&1 && { echo "$c"; return; }; done; }
PM="$(detect_pm)"
pm_install() {  # best-effort, never fatal
  [ "$#" -gt 0 ] || return 0
  case "$PM" in
    apt-get) $SUDO apt-get update -y -qq || true; $SUDO apt-get install -y -qq "$@" ;;
    dnf|yum) $SUDO "$PM" install -y "$@" ;;
    pacman)  $SUDO pacman -Sy --noconfirm "$@" ;;
    zypper)  $SUDO zypper --non-interactive install "$@" ;;
    apk)     $SUDO apk add --no-cache "$@" ;;
    *)       return 1 ;;
  esac
}
if ! have curl && ! have wget; then
  log "Installing a downloader (curl)…"
  pm_install curl ca-certificates || die "need 'curl' or 'wget' to bootstrap; please install one and re-run"
fi
# CBR comics (optional) — best effort.
if ! { have unar || have lsar || have unrar; }; then
  case "$PM" in pacman) pm_install unarchiver ;; ?*) pm_install unar ;; esac >/dev/null 2>&1 || true
  { have unar || have lsar || have unrar; } || warn "no unar/unrar — CBR comics won't import (CBZ still works)"
fi

mkdir -p "$TOOLCHAIN"

# --- 2) Python: a working system one (>=3.11), else install via the package
#        manager, else provision a fully private CPython via uv ---------------
py_ver()    { "$1" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null; }
py_ge_311() { "$1" -c 'import sys;sys.exit(0 if sys.version_info[:2]>=(3,11) else 1)' 2>/dev/null; }
py_can_venv() {  # actually CREATE a venv — catches "python3 present but python3-venv/ensurepip missing"
  local d; d="$(mktemp -d 2>/dev/null)" || return 1
  if "$1" -m venv "$d/v" >/dev/null 2>&1 && [ -x "$d/v/bin/python" ]; then rm -rf "$d"; return 0; fi
  rm -rf "$d"; return 1
}
pick_python() {
  local cand
  for cand in python3.13 python3.12 python3.11 python3 python; do
    if have "$cand" && py_ge_311 "$cand" && py_can_venv "$cand"; then
      PYTHON="$(command -v "$cand")"; return 0
    fi
  done
  return 1
}

UV=""
find_uv() { local u; for u in "$TOOLCHAIN/bin/uv" "$HOME/.local/bin/uv" "$RUN_HOME/.local/bin/uv"; do
  [ -x "$u" ] && { UV="$u"; return 0; }; done; have uv && { UV="$(command -v uv)"; return 0; }; return 1; }
export UV_INSTALL_DIR="$TOOLCHAIN/bin" XDG_BIN_HOME="$TOOLCHAIN/bin" \
       UV_PYTHON_INSTALL_DIR="$TOOLCHAIN/python" UV_CACHE_DIR="$TOOLCHAIN/uv-cache" \
       UV_NO_MODIFY_PATH=1 INSTALLER_NO_MODIFY_PATH=1

PYTHON=""
if pick_python; then
  ok "using system Python $(py_ver "$PYTHON")  ($PYTHON)"
else
  # Install Python + venv + pip from the system package manager, then re-check.
  if [ -n "$PM" ]; then
    log "Python 3.11+ with venv/pip is missing — installing it via $PM…"
    case "$PM" in
      apt-get) pm_install python3 python3-venv python3-pip ;;
      dnf|yum) pm_install python3 python3-pip ;;
      pacman)  pm_install python python-pip ;;
      zypper)  pm_install python3 python3-venv python3-pip ;;
      apk)     pm_install python3 py3-pip ;;
      *)       false ;;
    esac || warn "package manager could not install Python — will try a private build"
    pick_python && ok "using system Python $(py_ver "$PYTHON")  ($PYTHON)" || true
  fi
fi
if [ -z "$PYTHON" ]; then
  # Last resort: a fully self-contained CPython via uv (needs no system Python).
  log "Provisioning a private Python via uv…"
  find_uv || { fetch_stdout https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 || true; find_uv; } \
    || die "could not obtain Python 3.11+ (tried system packages and uv); install python3 + python3-venv + python3-pip and re-run"
  "$UV" python install 3.12 >/dev/null 2>&1 || warn "uv python install reported issues (continuing)"
fi

# --- 3) virtualenv + backend dependencies ----------------------------------
log "Setting up the virtualenv + backend dependencies"
if [ -n "$PYTHON" ]; then
  [ -x "$VENV/bin/python" ] || "$PYTHON" -m venv "$VENV" || die "failed to create venv"
  "$VENV/bin/python" -m pip install --quiet --upgrade pip wheel || true
  "$VENV/bin/python" -m pip install --quiet -e "$BACKEND_DIR" || die "backend dependency install failed (pip)"
  "$VENV/bin/python" -m pip install --quiet rarfile >/dev/null 2>&1 || true
else
  find_uv || die "uv missing after provisioning"
  [ -x "$VENV/bin/python" ] || "$UV" venv --python 3.12 "$VENV" || "$UV" venv "$VENV" || die "failed to create venv (uv)"
  "$UV" pip install --python "$VENV/bin/python" -e "$BACKEND_DIR" || die "backend dependency install failed (uv)"
  "$UV" pip install --python "$VENV/bin/python" rarfile >/dev/null 2>&1 || true
fi
[ -x "$VENV/bin/python" ] || die "virtualenv python missing after install"
ok "backend dependencies installed"

# Install a package into the venv via whichever toolchain we're using.
venv_pip() {
  if [ -n "$PYTHON" ]; then "$VENV/bin/python" -m pip install --quiet "$@"
  else "$UV" pip install --python "$VENV/bin/python" "$@"; fi
}

# --- 3b) JS rendering (Playwright + Chromium) — installed by default --------
# Needed for sources with render_js enabled. Self-contained: the browser lives in
# the repo toolchain. Opt out with SHELF_NO_RENDER=1.
PW_PATH="$RUN_HOME/.cache/ms-playwright"
if [ "${SHELF_NO_RENDER:-0}" = "1" ]; then
  log "Skipping JS-render setup (SHELF_NO_RENDER=1)"
else
  PW_PATH="$TOOLCHAIN/ms-playwright"
  log "Setting up JS rendering (Playwright + Chromium)…"
  mkdir -p "$PW_PATH"
  export PLAYWRIGHT_BROWSERS_PATH="$PW_PATH"
  if venv_pip playwright; then
    # Chromium's OS libraries (best effort; supported on Debian/Ubuntu/Fedora/…).
    $SUDO env PLAYWRIGHT_BROWSERS_PATH="$PW_PATH" "$VENV/bin/python" -m playwright install-deps chromium >/dev/null 2>&1 \
      || warn "couldn't auto-install Chromium's OS libraries — JS render may need them (run: $VENV/bin/python -m playwright install-deps chromium)"
    if "$VENV/bin/python" -m playwright install chromium >/dev/null 2>&1; then
      chmod -R a+rX "$PW_PATH" 2>/dev/null || true
      ok "JS rendering ready (Chromium installed)"
    else
      warn "Chromium download failed — JS-render sources stay off until 'playwright install chromium' succeeds"
    fi
  else
    warn "Playwright install failed — JS-render sources will be unavailable"
  fi
fi

# --- 4) Node: prefer a good system one (>=18), else download a private LTS --
node_ok() { local v; v="$(node --version 2>/dev/null)" || return 1; v="${v#v}"; [ -n "$v" ] && [ "${v%%.*}" -ge 18 ]; }
NODE_BIN=""
if node_ok && have npm; then
  ok "using system Node $(node --version)"
else
  NODE_VER="v20.18.1"
  case "$(uname -m)" in
    x86_64|amd64)  NARCH=x64 ;;
    aarch64|arm64) NARCH=arm64 ;;
    armv7l)        NARCH=armv7l ;;
    *)             NARCH="" ;;
  esac
  NODE_DIR="$TOOLCHAIN/node-$NODE_VER-linux-$NARCH"
  if [ -x "$NODE_DIR/bin/node" ]; then
    NODE_BIN="$NODE_DIR/bin"
  elif [ -n "$NARCH" ]; then
    log "Provisioning a private Node.js $NODE_VER ($NARCH)…"
    if fetch_to "https://nodejs.org/dist/$NODE_VER/node-$NODE_VER-linux-$NARCH.tar.gz" "$TOOLCHAIN/node.tgz" \
        && tar -xzf "$TOOLCHAIN/node.tgz" -C "$TOOLCHAIN"; then
      NODE_BIN="$NODE_DIR/bin"; rm -f "$TOOLCHAIN/node.tgz"
    fi
  fi
  if [ -n "$NODE_BIN" ] && [ -x "$NODE_BIN/node" ]; then
    export PATH="$NODE_BIN:$PATH"
    ok "using private Node $("$NODE_BIN/node" --version)"
  fi
fi

# --- 5) frontend build --------------------------------------------------------
# IMPORTANT: never silently serve a stale dist. An old (e.g. pre-auth) UI against a
# newer API looks like a broken/empty app — so if we can't rebuild, fail loudly
# rather than reuse whatever dist happens to be lying around.
export npm_config_cache="$TOOLCHAIN/npm-cache" npm_config_update_notifier=false
hash -r 2>/dev/null || true
if have npm; then
  log "Building the web UI"
  ( cd "$FRONTEND_DIR" && npm install --no-audit --no-fund --silent && npm run build --silent ) \
    || die "frontend build failed — see the npm output above"
  [ -f "$FRONTEND_DIR/dist/index.html" ] || die "frontend build produced no dist/index.html"
  ok "web UI built"
elif [ "${SHELF_USE_PREBUILT:-0}" = "1" ] && [ -f "$FRONTEND_DIR/dist/index.html" ]; then
  warn "Node.js unavailable — serving the EXISTING prebuilt frontend/dist (SHELF_USE_PREBUILT=1)."
  warn "Ensure that build matches this code, or the UI will be incompatible with the API."
else
  die "Node.js 18+ is required to build the web UI, and none is available (no usable system Node, \
and the automatic download from nodejs.org did not succeed — check your network/proxy). \
Install Node.js 18+ and re-run. (To intentionally ship a prebuilt frontend/dist, set SHELF_USE_PREBUILT=1.)"
fi

# --- 5a) make provisioned toolchain + venv readable by the service user -----
if [ "$RUN_USER" != "$(id -un)" ]; then
  chmod -R a+rX "$VENV" "$TOOLCHAIN" "$FRONTEND_DIR/dist" 2>/dev/null || true
fi

# --- 4) stop any running instance, then pick a free port -------------------
HAVE_SYSTEMD=0
command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ] && HAVE_SYSTEMD=1

# If a previous install lived in a DIFFERENT directory (e.g. you re-cloned the repo),
# carry its database over so your library + accounts aren't "lost" to a new empty DB.
if [ "$HAVE_SYSTEMD" -eq 1 ]; then
  OLD_WD="$($SUDO systemctl show -p WorkingDirectory --value "$SERVICE" 2>/dev/null || true)"
  if [ -n "$OLD_WD" ] && [ "$OLD_WD" != "$BACKEND_DIR" ] \
     && [ -f "$OLD_WD/shelf.db" ] && [ ! -f "$BACKEND_DIR/shelf.db" ]; then
    warn "A previous install at $OLD_WD has a database; this location ($BACKEND_DIR) is empty."
    for _ext in "" "-wal" "-shm"; do
      [ -f "$OLD_WD/shelf.db$_ext" ] && cp -p "$OLD_WD/shelf.db$_ext" "$BACKEND_DIR/shelf.db$_ext" 2>/dev/null || true
    done
    [ -f "$BACKEND_DIR/shelf.db" ] && ok "Carried your existing library over from $OLD_WD"
  fi
  $SUDO systemctl stop "$SERVICE" 2>/dev/null || true  # free our own port before probing
fi

PORT="$("$VENV/bin/python" - "$DESIRED_PORT" <<'PY'
import socket, sys
start = int(sys.argv[1])
for p in range(start, start + 500):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("0.0.0.0", p)); print(p); break
    except OSError:
        continue
    finally:
        s.close()
else:
    print(start)
PY
)"
[ "$PORT" = "$DESIRED_PORT" ] && log "Using port $PORT" || warn "port $DESIRED_PORT busy — using free port $PORT"

# --- 5) install + start the systemd service --------------------------------
if [ "$HAVE_SYSTEMD" -eq 0 ]; then
  warn "systemd not detected — skipping service install."
  echo "Run it manually:  SHELF_PORT=$PORT $VENV/bin/python -m app   (from $BACKEND_DIR)"
  exit 0
fi

# --- 5a) hardening profile (SHELF_TUNNEL=1 for internet exposure via a proxy) ---
BIND_HOST="0.0.0.0"
HARDEN_LINES=""
if [ "${SHELF_TUNNEL:-0}" = "1" ]; then
  # Bind to loopback only: the public interface is NOT exposed; only the local
  # reverse proxy (e.g. cloudflared) can reach the app. Trust its forwarded headers,
  # require Secure cookies + HSTS (the proxy terminates TLS).
  BIND_HOST="127.0.0.1"
  HARDEN_LINES="Environment=SHELF_TRUST_PROXY=true
Environment=SHELF_COOKIE_SECURE=true
Environment=SHELF_HSTS=true
Environment=SHELF_COOKIE_SAMESITE=lax"
  log "Tunnel/hardening profile ON — binding 127.0.0.1, Secure cookies, trust-proxy."
fi
[ -n "${SHELF_SETUP_TOKEN:-}" ] && HARDEN_LINES="${HARDEN_LINES}
Environment=SHELF_SETUP_TOKEN=${SHELF_SETUP_TOKEN}"
[ -n "${SHELF_ALLOWED_HOSTS:-}" ] && HARDEN_LINES="${HARDEN_LINES}
Environment=SHELF_ALLOWED_HOSTS=${SHELF_ALLOWED_HOSTS}"

UNIT="/etc/systemd/system/$SERVICE"
log "Writing systemd unit $UNIT"
$SUDO tee "$UNIT" >/dev/null <<EOF
[Unit]
Description=$APP_NAME — self-hosted reader (API + UI)
Documentation=file:$SCRIPT_DIR/README.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$BACKEND_DIR
Environment=SHELF_HOST=$BIND_HOST
Environment=SHELF_PORT=$PORT
Environment=PLAYWRIGHT_BROWSERS_PATH=$PW_PATH
$HARDEN_LINES
ExecStart=$VENV/bin/python -m app
Restart=always
RestartSec=3
TimeoutStopSec=20
NoNewPrivileges=true
ProtectSystem=full
ReadWritePaths=$SCRIPT_DIR $RUN_HOME/.cache

[Install]
WantedBy=multi-user.target
EOF

$SUDO systemctl daemon-reload
$SUDO systemctl enable "$SERVICE" >/dev/null 2>&1 || true
$SUDO systemctl restart "$SERVICE"

# --- 5b) install the `shelfcli` terminal-reader launcher -------------------
# A tiny wrapper that points the venv entry point at the real database with an
# absolute URL, so `shelfcli` works from any directory and shares reading
# progress with the web app.
install_launcher() {
  local target content user_local
  content="#!/usr/bin/env bash
# Auto-generated by Shelf install.sh — terminal reader launcher.
export SHELF_DATABASE_URL=\"sqlite:///$BACKEND_DIR/shelf.db\"
exec \"$VENV/bin/shelfcli\" \"\$@\"
"
  if [ -d /usr/local/bin ] && ( [ -w /usr/local/bin ] || [ -n "$SUDO" ] ); then
    target=/usr/local/bin/shelfcli
    printf '%s' "$content" | $SUDO tee "$target" >/dev/null
    $SUDO chmod +x "$target"
  else
    target="$RUN_HOME/.local/bin/shelfcli"
    as_user mkdir -p "$RUN_HOME/.local/bin"
    printf '%s' "$content" | as_user tee "$target" >/dev/null
    as_user chmod +x "$target"
    warn "installed shelfcli to $target — ensure ~/.local/bin is on your PATH"
  fi
  # `pip install -e backend` also drops a console_scripts shim into
  # ~/.local/bin/shelfcli that does NOT set SHELF_DATABASE_URL — and since
  # ~/.local/bin is typically ahead of /usr/local/bin on PATH, it would silently
  # shadow our wrapper and open an empty default DB. Replace it with the same
  # wrapper so whichever copy PATH resolves to behaves identically.
  user_local="$RUN_HOME/.local/bin/shelfcli"
  if [ "$target" != "$user_local" ] && [ -e "$user_local" ]; then
    as_user mkdir -p "$RUN_HOME/.local/bin"
    printf '%s' "$content" | as_user tee "$user_local" >/dev/null
    as_user chmod +x "$user_local"
  fi
  ok "terminal reader installed: run 'shelfcli'"
}
if [ -x "$VENV/bin/shelfcli" ]; then
  install_launcher
else
  warn "shelfcli entry point missing (backend install incomplete?) — skipping launcher"
fi

# --- 6) health check -------------------------------------------------------
log "Waiting for $APP_NAME to come up on port $PORT…"
up=0
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then up=1; break; fi
  sleep 1
done

if [ "$up" -eq 1 ]; then
  IP="$(hostname -I 2>/dev/null | awk '{print $1}')"; [ -n "$IP" ] || IP="localhost"
  ok "$APP_NAME is running"
  echo
  if [ "$BIND_HOST" = "127.0.0.1" ]; then
    echo "    Bound:    127.0.0.1:$PORT  (loopback only — reach it via your tunnel/proxy)"
  else
    echo "    Local:    http://localhost:$PORT"
    [ "$IP" != "localhost" ] && echo "    Network:  http://$IP:$PORT"
  fi
  echo "    Terminal: shelfcli            (browse + read in the terminal)"
  echo "    Service:  systemctl status $SERVICE   ·   journalctl -u $SERVICE -f"
  echo
  if [ "${SHELF_TUNNEL:-0}" = "1" ]; then
    echo "  Exposure checklist (see deploy/cloudflare-tunnel.md):"
    echo "    1) Create the first admin BEFORE the tunnel is public — or set SHELF_SETUP_TOKEN."
    echo "    2) cloudflared tunnel → http://localhost:$PORT  (origin stays on loopback)."
    echo "    3) Strongly recommended: put Cloudflare Access (Zero Trust) in front."
    echo "    4) Block direct origin access:  ufw default deny incoming; ufw allow ssh; ufw enable"
    echo
  fi
else
  warn "service did not answer health checks yet — recent logs:"
  $SUDO journalctl -u "$SERVICE" -n 20 --no-pager || true
  die "startup failed; see logs above"
fi
