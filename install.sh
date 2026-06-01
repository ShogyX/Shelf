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

# Run a command as the service user (keeps build artifacts correctly owned).
as_user() {
  if [ "$(id -un)" = "$RUN_USER" ]; then "$@"; else $SUDO -u "$RUN_USER" -H "$@"; fi
}

# --- 1) system dependencies (install only what's actually missing) ---------
have() { command -v "$1" >/dev/null 2>&1; }
have_venv() { python3 -m venv --help >/dev/null 2>&1; }
have_pip()  { python3 -m pip --version  >/dev/null 2>&1; }
have_unar() { have unar || have lsar || have unrar; }

detect_pm() {
  for c in apt-get dnf yum pacman zypper apk; do
    command -v "$c" >/dev/null 2>&1 && { echo "$c"; return; }
  done
}
PM="$(detect_pm)"
log "Resolving system dependencies (package manager: ${PM:-none})"

# Map a generic need -> this PM's package name, then install only the missing set.
pm_install() {  # pm_install pkg...
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
pkg_name() {  # pkg_name <need>  -> distro package
  case "$1:$PM" in
    venv:apt-get) echo python3-venv ;;     venv:*) echo "" ;;
    pip:apt-get)  echo python3-pip ;;      pip:apk) echo py3-pip ;;  pip:*) echo python3-pip ;;
    node:*)       echo nodejs ;;
    npm:*)        echo npm ;;
    unar:pacman)  echo unarchiver ;;       unar:*) echo unar ;;
    python:apk)   echo python3 ;;          python:*) echo python3 ;;
    curl:*)       echo curl ;;
  esac
}

want=()
have python3            || want+=("$(pkg_name python)")
have_venv               || want+=("$(pkg_name venv)")
have_pip                || want+=("$(pkg_name pip)")
{ have node || have nodejs; } || want+=("$(pkg_name node)")
have npm                || want+=("$(pkg_name npm)")
have curl               || want+=("$(pkg_name curl)")
# Drop empties (e.g. venv is bundled on non-apt distros).
want=($(printf '%s\n' "${want[@]:-}" | awk 'NF'))

if [ "${#want[@]}" -gt 0 ]; then
  log "Installing: ${want[*]}"
  pm_install "${want[@]}" || warn "package install reported problems — continuing if tools are present"
else
  ok "core system packages already present"
fi
# CBR comics (optional) — best effort, never fatal.
if ! have_unar; then
  u="$(pkg_name unar)"; [ -n "$u" ] && pm_install "$u" >/dev/null 2>&1 || true
  have_unar || warn "no unar/unrar — CBR comics won't import (CBZ still works)"
fi

have python3 || die "python3 is required but still not available"
{ have node || have nodejs; } || warn "Node.js missing — will reuse a prebuilt frontend/dist if present"

# --- 2) python venv + backend deps -----------------------------------------
log "Setting up Python virtualenv + backend dependencies"
if [ ! -x "$VENV/bin/python" ]; then
  as_user python3 -m venv "$VENV" || die "failed to create venv (is python3-venv installed?)"
fi
as_user "$VENV/bin/python" -m pip install --quiet --upgrade pip wheel
as_user "$VENV/bin/python" -m pip install --quiet -e "$BACKEND_DIR"
as_user "$VENV/bin/python" -m pip install --quiet rarfile >/dev/null 2>&1 || true  # CBR (needs unar/unrar)
ok "backend dependencies installed"

# --- 3) frontend build (or reuse a prebuilt dist) --------------------------
if command -v npm >/dev/null 2>&1; then
  log "Building the web UI"
  ( cd "$FRONTEND_DIR" && as_user npm install --no-audit --no-fund --silent \
      && as_user npm run build --silent ) || die "frontend build failed"
  ok "web UI built"
elif [ -f "$FRONTEND_DIR/dist/index.html" ]; then
  warn "npm not found — using the existing prebuilt frontend/dist"
else
  die "npm not found and no prebuilt frontend/dist; install Node.js 18+ and re-run"
fi

# --- 4) stop any running instance, then pick a free port -------------------
HAVE_SYSTEMD=0
command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ] && HAVE_SYSTEMD=1
if [ "$HAVE_SYSTEMD" -eq 1 ]; then
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
Environment=SHELF_HOST=0.0.0.0
Environment=SHELF_PORT=$PORT
Environment=PLAYWRIGHT_BROWSERS_PATH=$RUN_HOME/.cache/ms-playwright
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
  local target content
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
  echo "    Local:    http://localhost:$PORT"
  [ "$IP" != "localhost" ] && echo "    Network:  http://$IP:$PORT"
  echo "    Terminal: shelfcli            (browse + read in the terminal)"
  echo "    Service:  systemctl status $SERVICE   ·   journalctl -u $SERVICE -f"
  echo
else
  warn "service did not answer health checks yet — recent logs:"
  $SUDO journalctl -u "$SERVICE" -n 20 --no-pager || true
  die "startup failed; see logs above"
fi
