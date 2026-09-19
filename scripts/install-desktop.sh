#!/bin/bash
# Install the RMS Config Editor launcher onto the Desktop and into the app menu.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
APPS_DIR="$HOME/.local/share/applications"
ENTRY="rms-config-editor.desktop"

chmod +x "$PROJECT_DIR/scripts/launch.sh"
mkdir -p "$APPS_DIR" "$DESKTOP_DIR"

cat > "$APPS_DIR/$ENTRY" <<EOF
[Desktop Entry]
Name=RMS Config Editor
Type=Application
Exec=$PROJECT_DIR/scripts/launch.sh
Icon=$PROJECT_DIR/icon.png
Comment=See and set .config options across every RMS station on this machine
Categories=System;Settings;
Terminal=false
Hidden=false
NoDisplay=false
EOF

cp "$APPS_DIR/$ENTRY" "$DESKTOP_DIR/$ENTRY"
chmod +x "$DESKTOP_DIR/$ENTRY"

# GNOME requires desktop-file launchers to be explicitly trusted, otherwise the
# icon shows as "Untrusted application launcher" and refuses to run on click.
if command -v gio >/dev/null; then
    gio set "$DESKTOP_DIR/$ENTRY" metadata::trusted true 2>/dev/null || true
fi

command -v update-desktop-database >/dev/null && update-desktop-database "$APPS_DIR" 2>/dev/null || true

echo "Installed:"
echo "  $DESKTOP_DIR/$ENTRY"
echo "  $APPS_DIR/$ENTRY"
