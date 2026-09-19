#!/usr/bin/env bash
#
# One-command installer for the RMS Config Editor.
#
#   curl -fsSL https://raw.githubusercontent.com/Cybis320/cc-rms-config-editor/master/scripts/deploy.sh | bash
#       -- or, from a clone --
#   ./scripts/deploy.sh
#
# Idempotent: clones or updates the repo, installs the package into the RMS
# virtualenv (or a local .venv), and installs the desktop launcher. Re-run it
# any time to update -- though the launcher also self-updates on every click.
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
    git -C "$DEST" pull --ff-only
else
    info "Cloning $REPO_URL -> $DEST"
    mkdir -p "$(dirname "$DEST")"
    git clone --depth 1 "$REPO_URL" "$DEST"
fi

# --- 2. Python environment --------------------------------------------------
# Pure stdlib, so any Python >= 3.9 does; the RMS virtualenv is the natural home.
if [ -x "$VENV/bin/python" ]; then
    PY="$VENV/bin/python"
    info "Using virtualenv $VENV"
else
    warn "No virtualenv at $VENV; creating one at $DEST/.venv"
    if ! python3 -m venv "$DEST/.venv"; then
        rm -rf "$DEST/.venv"
        warn "python3 -m venv failed (python3-venv not installed?). Either:"
        warn "    sudo apt install python3-venv     # then re-run this installer"
        warn "or point CC_VENV at an existing virtualenv and re-run."
        exit 1
    fi
    PY="$DEST/.venv/bin/python"
fi

info "Installing package"
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet -e "$DEST"

# --- 3. Desktop launcher -----------------------------------------------------
if command -v xdg-user-dir >/dev/null 2>&1 || [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
    "$DEST/scripts/install-desktop.sh"
else
    warn "No graphical session detected -- skipped the desktop icon."
    warn "Install it later from the desktop session:  $DEST/scripts/install-desktop.sh"
fi

echo
info "Done. Click the 'RMS Config Editor' icon, or run:"
info "    $DEST/scripts/launch.sh"
info "Editor: http://localhost:${CONFIG_EDITOR_PORT:-8421}"
info "The launcher git-pulls before each start, so it keeps itself current."
