#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_DIR="${HOME}/.config/systemd/user"
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/keyrfmonitor.service" <<EOF
[Unit]
Description=KeyRFMonitor - passive local RF dashboard (SDR starts only on user request)
Wants=network-online.target
After=default.target network-online.target

[Service]
Type=simple
WorkingDirectory=$ROOT
ExecStart=/usr/bin/python3 -m keyrfmonitor serve --host 127.0.0.1 --port 8890
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
UMask=0077

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable keyrfmonitor.service
# restart statt 'enable --now': ein bereits laufender Dienst uebernimmt sonst
# WorkingDirectory und ExecStart erst beim naechsten Neustart.
systemctl --user restart keyrfmonitor.service
systemctl --user --no-pager status keyrfmonitor.service
printf '\nWeb-UI: http://127.0.0.1:8890 (nur Pi bzw. SSH-Tunnel)\n'
printf 'Der Dienst startet KEINE SDR-Messung automatisch und aendert ADS-B nicht.\n'
