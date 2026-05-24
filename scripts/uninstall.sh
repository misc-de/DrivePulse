#!/usr/bin/env bash
set -euo pipefail

APP_ID="de.cais.DrivePulse"
ICON_FILE="$HOME/.local/share/icons/hicolor/128x128/apps/$APP_ID.png"
DESKTOP_FILE="$HOME/.local/share/applications/$APP_ID.desktop"

echo "Deinstalliere DrivePulse…"

removed=0
for f in "$ICON_FILE" "$DESKTOP_FILE"; do
    if [ -f "$f" ]; then
        rm "$f"
        echo "  Entfernt: $f"
        removed=$((removed + 1))
    fi
done

if [ "$removed" -eq 0 ]; then
    echo "  Nichts gefunden – war DrivePulse installiert?"
fi

if command -v gtk-update-icon-cache &>/dev/null; then
    gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
fi
if command -v update-desktop-database &>/dev/null; then
    update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
fi

echo ""
echo "Fertig."
