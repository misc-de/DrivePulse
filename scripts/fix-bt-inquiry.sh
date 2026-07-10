#!/bin/sh
# Persistently enable Bluetooth page + inquiry scan on hci0.
#
# On FuriOS / MediaTek binder-BT the adapter comes up with only PSCAN (page
# scan) — it accepts incoming connections but does not actively look for
# BR/EDR devices. Every ``bluetoothctl scan on`` / ``hcitool inq`` and the
# DrivePulse Settings auto-scan then returns zero devices. System updates
# reset this every time; a systemd unit keeps it applied.
#
# hci0 here is a *virtual* device published by ``bluebinder.service`` and it
# comes up asynchronously: a plain oneshot that runs at ``bluetooth.target``
# loses the race and dies with "Can't set scan mode on hci0: Network is down".
# We therefore install a unit that WAITS for the adapter, forces it ``up`` and
# retries — plus a udev rule that re-triggers the unit every time the adapter
# (re)appears, so bluebinder resets don't silently drop ISCAN again.
#
# Run once as root:  pkexec sh scripts/fix-bt-inquiry.sh
# Idempotent.

set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "Bitte mit pkexec oder sudo starten." >&2
    exit 1
fi

echo "=== enabling PISCAN now ==="
# Adapter may currently be down (bluebinder still settling) — force it up first,
# tolerate failure so ``set -e`` doesn't abort before we install persistence.
hciconfig hci0 up 2>/dev/null || true
hciconfig hci0 piscan 2>/dev/null || echo "  (hci0 not ready yet — the unit will retry on boot)"
hciconfig hci0 2>/dev/null | grep -E "UP|SCAN" || true

UNIT=/etc/systemd/system/bluetooth-piscan.service
echo "=== installing $UNIT ==="
# Retry loop: wait up to ~60 s for the virtual adapter, force it up, then set
# piscan. Ordered *after* bluebinder (the hci0 provider) and re-run whenever
# bluetooth restarts (PartOf).
cat > "$UNIT" <<'EOF'
[Unit]
Description=Enable Bluetooth page+inquiry scan on hci0
After=bluetooth.service bluebinder.service
Wants=bluebinder.service
PartOf=bluetooth.service

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'n=0; while [ $n -lt 30 ]; do /usr/bin/hciconfig hci0 up 2>/dev/null; if /usr/bin/hciconfig hci0 piscan 2>/dev/null; then exit 0; fi; n=$((n+1)); sleep 2; done; echo "hci0 never came up" >&2; exit 1'
RemainAfterExit=yes

[Install]
WantedBy=bluetooth.target
EOF

RULE=/etc/udev/rules.d/91-drivepulse-bt-piscan.rules
echo "=== installing $RULE ==="
# The virtual hci0 emits an ``add`` event whenever bluebinder (re)creates it.
# Re-run the (retry-capable) unit each time so a bluebinder reset can't leave
# the adapter stuck on PSCAN-only again.
cat > "$RULE" <<'EOF'
# DrivePulse: (re)enable inquiry scan whenever the virtual BT adapter appears.
ACTION=="add", SUBSYSTEM=="bluetooth", KERNEL=="hci0", RUN+="/usr/bin/systemctl --no-block restart bluetooth-piscan.service"
EOF
udevadm control --reload-rules 2>/dev/null || true

systemctl daemon-reload
systemctl enable bluetooth-piscan.service
systemctl restart bluetooth-piscan.service || true

echo ""
echo "=== final state ==="
hciconfig hci0 2>/dev/null | grep -E "UP|SCAN" || true
systemctl is-active bluetooth-piscan.service || true
systemctl is-enabled bluetooth-piscan.service || true

echo ""
echo "Fertig. Ab jetzt startet der Inquiry-Scan bei jedem Boot automatisch."
