#!/bin/sh
# Enable legacy / Bluetooth 2.0 pairing for ELM327 BT clones (HC-05/HC-06 modules).
#
# BlueZ 5.66+ (Ubuntu 24.04, Debian 13, current Mobian/FuriOS) ships with
# ClassicBondedOnly=true by default — that rejects every pair attempt against
# adapters that don't speak modern SSP. Symptom: the OS PIN prompt accepts the
# code but the bond is dropped before completion.
#
# Run as root:    pkexec sh scripts/fix-bluetooth-classic.sh
# or:             sudo  sh scripts/fix-bluetooth-classic.sh
#
# Idempotent — re-running is safe. A timestamped backup is left next to the
# original config the first time the file is changed.

set -eu

CONF=/etc/bluetooth/main.conf
STAMP=$(date +%Y%m%d-%H%M%S)

if [ "$(id -u)" -ne 0 ]; then
    echo "Bitte mit pkexec oder sudo starten." >&2
    exit 1
fi

if [ ! -f "$CONF" ]; then
    echo "Anlegen: $CONF (lag noch nicht vor)"
    mkdir -p "$(dirname "$CONF")"
    printf '[General]\n' > "$CONF"
fi

cp -a "$CONF" "${CONF}.bak-${STAMP}"
echo "Backup -> ${CONF}.bak-${STAMP}"

ensure_kv() {
    key=$1; val=$2
    if grep -qE "^\s*${key}\s*=" "$CONF"; then
        # Wert ersetzen (auch wenn auskommentiert).
        sed -i -E "s|^\s*#?\s*${key}\s*=.*|${key} = ${val}|" "$CONF"
    elif grep -qE "^\s*\[General\]\s*$" "$CONF"; then
        # Direkt unter [General] einfügen.
        sed -i -E "/^\s*\[General\]\s*$/a ${key} = ${val}" "$CONF"
    else
        # [General] gibt's nicht — am Anfang neu anlegen.
        { printf '[General]\n%s = %s\n' "$key" "$val"; cat "$CONF"; } > "${CONF}.new"
        mv "${CONF}.new" "$CONF"
    fi
    echo "  set ${key} = ${val}"
}

ensure_kv ClassicBondedOnly false
ensure_kv JustWorksRepairing always
ensure_kv TemporaryTimeout 0

# bluetoothd neu starten, damit die Settings greifen.
if command -v systemctl >/dev/null 2>&1; then
    systemctl restart bluetooth.service && echo "bluetooth.service neu gestartet."
elif command -v rc-service >/dev/null 2>&1; then
    rc-service bluetooth restart && echo "bluetooth (OpenRC) neu gestartet."
else
    pkill -HUP bluetoothd 2>/dev/null \
        && echo "bluetoothd: SIGHUP gesendet." \
        || echo "Konnte bluetoothd nicht neu starten — bitte manuell."
fi

echo
echo "Fertig. ELM/HC-05-Pairing sollte jetzt durchgehen."
echo "Probiere in der App erneut 'Verbinden' — oder DrivePulse neu starten."
