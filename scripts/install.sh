#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
APP_ID="de.cais.DrivePulse"
ICON_DEST="$HOME/.local/share/icons/hicolor/128x128/apps/$APP_ID.png"
DESKTOP_DEST="$HOME/.local/share/applications/$APP_ID.desktop"

echo "Installing DrivePulse…"

mkdir -p "$(dirname "$ICON_DEST")"
cp "$PROJECT_ROOT/icons/icon.png" "$ICON_DEST"
echo "  Icon         → $ICON_DEST"

ICONS_SRC="$PROJECT_ROOT/icons/hicolor/symbolic/actions"
ICONS_DEST="$HOME/.local/share/icons/hicolor/symbolic/actions"
shopt -s nullglob
svg_files=("$ICONS_SRC"/*.svg)
shopt -u nullglob
if [ ${#svg_files[@]} -gt 0 ]; then
    mkdir -p "$ICONS_DEST"
    cp "${svg_files[@]}" "$ICONS_DEST/"
    echo "  SVG-Icons    → $ICONS_DEST"
fi

mkdir -p "$(dirname "$DESKTOP_DEST")"

# Legacy-Schutz: ältere Installer schrieben Exec=python3 .../drivepulse.py.
# Dieser Einstiegspunkt existiert nicht mehr, sodass ein Alt-Launcher still
# scheitert (Terminal=false). Vorhandene Alt-Datei vor dem Neuschreiben
# wegräumen, damit kein toter Eintrag im Menü überlebt.
if [ -f "$DESKTOP_DEST" ] && grep -q "drivepulse\.py" "$DESKTOP_DEST"; then
    rm -f "$DESKTOP_DEST"
    echo "  Alt-Launcher (drivepulse.py) entfernt"
fi

cat > "$DESKTOP_DEST" << EOF
[Desktop Entry]
Version=1.1
Type=Application
Name=DrivePulse
Comment=OBD-II Dashboard
Comment[de]=OBD-II Armaturenbrett
Icon=$APP_ID
Path=$PROJECT_ROOT
Exec=env PYTHONPATH=$PROJECT_ROOT python3 -m drivepulse_app.app
Terminal=false
Categories=Utility;
Keywords=OBD;Auto;Fahrzeug;Dashboard;GPS;
StartupWMClass=drivepulse
EOF
echo "  Desktop-Datei → $DESKTOP_DEST"

if command -v gtk-update-icon-cache &>/dev/null; then
    gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null && \
        echo "  Icon-Cache aktualisiert"
fi
if command -v update-desktop-database &>/dev/null; then
    update-desktop-database "$(dirname "$DESKTOP_DEST")" 2>/dev/null && \
        echo "  Desktop-Datenbank aktualisiert"
fi

echo ""
echo "Fertig. DrivePulse ist jetzt im Anwendungsmenü verfügbar."
