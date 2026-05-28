#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
APP_ID="de.cais.DrivePulse"

# Respect XDG_DATA_HOME — phosh/FuriOS may point it somewhere other than
# ~/.local/share, in which case hardcoding the latter writes the launcher to
# a directory the menu never reads.
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
ICON_DEST="$DATA_HOME/icons/hicolor/128x128/apps/$APP_ID.png"
DESKTOP_DEST="$DATA_HOME/applications/$APP_ID.desktop"

# Emit the canonical .desktop content to the path given as $1.
write_desktop() {
    cat > "$1" << EOF
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
}

echo "Installing DrivePulse…"

mkdir -p "$(dirname "$ICON_DEST")"
cp "$PROJECT_ROOT/icons/icon.png" "$ICON_DEST"
echo "  Icon         → $ICON_DEST"

ICONS_SRC="$PROJECT_ROOT/icons/hicolor/symbolic/actions"
ICONS_DEST="$DATA_HOME/icons/hicolor/symbolic/actions"
shopt -s nullglob
svg_files=("$ICONS_SRC"/*.svg)
shopt -u nullglob
if [ ${#svg_files[@]} -gt 0 ]; then
    mkdir -p "$ICONS_DEST"
    cp "${svg_files[@]}" "$ICONS_DEST/"
    echo "  SVG-Icons    → $ICONS_DEST"
fi

# Neuen Start anlegen.
mkdir -p "$(dirname "$DESKTOP_DEST")"
write_desktop "$DESKTOP_DEST"
echo "  Desktop-Datei → $DESKTOP_DEST"

# Legacy nachziehen: ältere Installer schrieben Exec=python3 .../drivepulse.py,
# ein Einstiegspunkt der nicht mehr existiert — der Launcher scheitert dann
# still (Terminal=false). Solche Alt-Dateien können in jedem Applications-Ordner
# aus $XDG_DATA_HOME / $XDG_DATA_DIRS liegen; alle beschreibbaren auf den neuen
# Start nachziehen, der Rest (z. B. /usr/share) wird gemeldet.
app_dirs=("$DATA_HOME/applications" "$HOME/.local/share/applications")
IFS=':' read -ra _xdg <<< "${XDG_DATA_DIRS:-/usr/local/share:/usr/share}"
for d in "${_xdg[@]}"; do
    [ -n "$d" ] && app_dirs+=("$d/applications")
done

declare -A _seen=()
for dir in "${app_dirs[@]}"; do
    [ -d "$dir" ] || continue
    real="$(cd "$dir" && pwd)"
    [ -n "${_seen[$real]:-}" ] && continue
    _seen[$real]=1
    shopt -s nullglob
    for f in "$dir"/*.desktop; do
        # Die gerade geschriebene kanonische Datei überspringen.
        [ "$f" -ef "$DESKTOP_DEST" ] && continue
        grep -q "drivepulse\.py" "$f" || continue
        if [ -w "$f" ]; then
            write_desktop "$f"
            echo "  Legacy nachgezogen → $f"
        else
            echo "  Legacy gefunden, aber nicht beschreibbar (sudo nötig): $f"
        fi
    done
    shopt -u nullglob
done

if command -v gtk-update-icon-cache &>/dev/null; then
    gtk-update-icon-cache -f -t "$DATA_HOME/icons/hicolor" 2>/dev/null && \
        echo "  Icon-Cache aktualisiert"
fi
if command -v update-desktop-database &>/dev/null; then
    update-desktop-database "$(dirname "$DESKTOP_DEST")" 2>/dev/null && \
        echo "  Desktop-Datenbank aktualisiert"
fi

echo ""
echo "Fertig. DrivePulse ist jetzt im Anwendungsmenü verfügbar."
