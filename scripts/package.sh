#!/usr/bin/env bash
#
# Package Shelf into a distributable tarball (source + installer, no heavy/
# generated artifacts). The result is a self-contained archive whose only
# post-extract step is `./install.sh`.
#
#   ./scripts/package.sh [output-dir]
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" >/dev/null 2>&1 && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
OUT_DIR="${1:-$ROOT/dist-pkg}"
VERSION="$(grep -m1 '^version' "$ROOT/backend/pyproject.toml" | sed -E 's/.*"([^"]+)".*/\1/' || echo 0.0.0)"
NAME="shelf-${VERSION}"
TARBALL="$OUT_DIR/${NAME}.tar.gz"

mkdir -p "$OUT_DIR"
echo "==> Packaging $NAME"

# Exclude virtualenvs, node_modules, builds, DBs, caches, and local media.
tar --exclude-vcs \
    --exclude='./backend/.venv' \
    --exclude='./backend/shelf.db*' \
    --exclude='./backend/covers' \
    --exclude='./backend/media' \
    --exclude='./frontend/node_modules' \
    --exclude='./frontend/dist' \
    --exclude='./.toolchain' \
    --exclude='./dist-pkg' \
    --exclude='*/__pycache__' \
    --exclude='*.pyc' \
    --transform "s,^\.,${NAME}," \
    -czf "$TARBALL" -C "$ROOT" .

echo "==> Wrote $TARBALL ($(du -h "$TARBALL" | cut -f1))"
echo "    Install with:  tar xzf $(basename "$TARBALL") && cd $NAME && ./install.sh"
