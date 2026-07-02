#!/bin/sh
# Persistently enable Bluetooth page + inquiry scan on hci0.
#
# On FuriOS / MediaTek binder-BT the adapter comes up with only PSCAN (page
# scan) — it accepts incoming connections but does not actively look for
# BR/EDR devices. Every ``bluetoothctl scan on`` / ``hcitool inq`` and the
# DrivePulse Settings auto-scan then returns zero devices. System updates
# reset this every time; a systemd unit keeps it applied.
#
# Run once as root:  pkexec sh scripts/fix-bt-inquiry.sh
# Idempotent.

set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "Bitte mit pkexec oder sudo starten." >&2
    exit 1
fi

echo "=== enabling PISCAN now ==="
hciconfig hci0 piscan
hciconfig hci0 | grep -E "UP|SCAN"

UNIT=/etc/systemd/system/bluetooth-piscan.service
echo "=== installing $UNIT ==="
cat > "$UNIT" <<EOF
[Unit]
Description=Enable Bluetooth page+inquiry scan on hci0
After=bluetooth.service
Requires=bluetooth.service

[Service]
Type=oneshot
ExecStart=/usr/bin/hciconfig hci0 piscan
RemainAfterExit=yes

[Install]
WantedBy=bluetooth.target
EOF

systemctl daemon-reload
systemctl enable bluetooth-piscan.service
systemctl start bluetooth-piscan.service

echo ""
echo "=== final state ==="
hciconfig hci0 | grep -E "UP|SCAN"
systemctl is-active bluetooth-piscan.service
systemctl is-enabled bluetooth-piscan.service

echo ""
echo "Fertig. Ab jetzt startet der Inquiry-Scan bei jedem Boot automatisch."
