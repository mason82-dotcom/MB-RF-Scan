#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_DIR="${HOME}/.config/systemd/user"
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/keyrfmonitor.service" <<EOF
[Unit]
Description=KeyRFMonitor - passive local RF dashboard (SDR starts only on user request)
After=default.target

[Service]
Type=simple
WorkingDirectory=$ROOT
ExecStart=/usr/bin/python3 -m keyrfmonitor serve --host 127.0.0.1 --port 8765
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
UMask=0077

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now keyrfmonitor.service
systemctl --user --no-pager status keyrfmonitor.service
printf '\nWeb-UI: http://127.0.0.1:8765 (nur Pi bzw. SSH-Tunnel)\n'
printf 'Der Dienst startet KEINE SDR-Messung automatisch und aendert ADS-B nicht.\n'
