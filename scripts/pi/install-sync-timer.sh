#!/usr/bin/env bash
# Opt-in: installiert einen stündlichen GitHub->Pi-Abgleich, ohne SDR-Zugriff.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
[ -d "$ROOT/.git" ] || { echo "MB-RF-Scan Git-Checkout fehlt: $ROOT" >&2; exit 2; }
[ "$(git -C "$ROOT" branch --show-current)" = main ] || {
  echo "Bitte zuerst den Branch main auschecken." >&2; exit 2;
}
command -v flock >/dev/null || { echo "flock fehlt (Paket util-linux)." >&2; exit 2; }

UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"

cat > "$UNIT_DIR/mb-rf-scan-sync.service" <<EOF
[Unit]
Description=MB-RF-Scan: GitHub main sicher auf Raspberry Pi aktualisieren
After=default.target

[Service]
Type=oneshot
WorkingDirectory=$ROOT
ExecStart=/usr/bin/bash $ROOT/scripts/pi/sync-from-github.sh
TimeoutStartSec=180
NoNewPrivileges=true
UMask=0077
EOF

cat > "$UNIT_DIR/mb-rf-scan-sync.timer" <<'EOF'
[Unit]
Description=MB-RF-Scan: GitHub-Abgleich stuendlich

[Timer]
OnCalendar=hourly
Persistent=true
RandomizedDelaySec=5min
Unit=mb-rf-scan-sync.service

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now mb-rf-scan-sync.timer
echo
echo "MB-RF-Scan: stündlicher, nur von GitHub nach Pi gerichteter Abgleich eingerichtet."
echo "Kein automatischer Push, keine SDR-Messung, kein ADS-B-Stopp."
systemctl --user list-timers --all mb-rf-scan-sync.timer --no-pager
