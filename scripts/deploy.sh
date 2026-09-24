#!/usr/bin/env bash
#
# One-command installer for the RMS Config Editor.
#
#   curl -fsSL https://raw.githubusercontent.com/Cybis320/cc-rms-config-editor/master/install.sh | bash
#       -- or, from a clone --
#   ./install.sh
#
# This is the setup step install.sh runs; the old one-liner that curls this file
# directly still works. Idempotent: clones or updates the repo, installs the
# package into the RMS virtualenv (or a local .venv), installs the desktop
# launcher and the shared hourly updater. Re-run it any time.
#
set -euo pipefail

# --- Settings (override via environment) ------------------------------------
REPO_URL="${CC_REPO_URL:-https://github.com/Cybis320/cc-rms-config-editor.git}"
# Deploys into the familiar CC_Utils/config_editor folder (repo name independent).
DEST="${CC_DEST:-$HOME/source/CC_Utils/config_editor}"
VENV="${CC_VENV:-$HOME/vRMS}"

info() { printf '\033[32m[deploy]\033[0m %s\n' "$1"; }
warn() { printf '\033[33m[deploy]\033[0m %s\n' "$1"; }

# --- 1. Get the code --------------------------------------------------------
# If we're already running from inside a clone, use it; otherwise clone/update.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/../pyproject.toml" ] \
        && grep -q '^name = "config_editor"' "$SCRIPT_DIR/../pyproject.toml"; then
    DEST="$(cd "$SCRIPT_DIR/.." && pwd)"
    info "Using existing checkout at $DEST"
elif [ -d "$DEST/.git" ]; then
    info "Updating existing checkout at $DEST"
    git -C "$DEST" pull --ff-only \
        || warn "Could not fast-forward $DEST (offline, or local changes); using it as is."
else
    info "Cloning $REPO_URL -> $DEST"
    mkdir -p "$(dirname "$DEST")"
    git clone --depth 1 "$REPO_URL" "$DEST"
fi

CC_TOOL=config_editor
if [ ! -f "$DEST/cc-utils/lib.sh" ]; then
    warn "$DEST predates this installer and could not be updated."
    warn "See: git -C $DEST status   (then re-run this command)"
    exit 1
fi
# shellcheck source=../cc-utils/lib.sh
. "$DEST/cc-utils/lib.sh"

# --- 2. Python environment --------------------------------------------------
# Pure stdlib, so any Python >= 3.9 does; the RMS virtualenv is the natural home.
CC_VENV="$VENV"
PY="$(cc_python "$DEST")"
info "Installing package into $PY"
cc_pip_install "$PY" "$DEST"

# --- 3. Desktop launcher -----------------------------------------------------
if command -v xdg-user-dir >/dev/null 2>&1 || [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
    "$DEST/scripts/install-desktop.sh"
else
    warn "No graphical session detected -- skipped the desktop icon."
    warn "Install it later from the desktop session:  $DEST/scripts/install-desktop.sh"
fi

# --- 4. Auto-update ----------------------------------------------------------
cc_install_updater "$DEST/cc-utils"
cc_mark_applied "$DEST"

echo
info "Done. Click the 'RMS Config Editor' icon, or run:"
info "    $DEST/scripts/launch.sh"
info "Editor: http://localhost:${CONFIG_EDITOR_PORT:-8421}"
info "Updates arrive hourly (cc-utils updater) and on every launcher click."
