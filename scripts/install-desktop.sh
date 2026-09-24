#!/bin/bash
# Install the RMS Config Editor launcher onto the Desktop and into the app menu.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CC_TOOL=config_editor
# shellcheck source=../cc-utils/lib.sh
. "$PROJECT_DIR/cc-utils/lib.sh"

chmod +x "$PROJECT_DIR/scripts/launch.sh"
cc_desktop_entry rms-config-editor.desktop "RMS Config Editor" \
    "$PROJECT_DIR/scripts/launch.sh" "$PROJECT_DIR/icon.png" \
    "See and set .config options across every RMS station on this machine" \
    "System;Settings;"
cc_info "Installed the RMS Config Editor launcher (Desktop + app menu)"
