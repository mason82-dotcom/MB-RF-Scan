#!/usr/bin/env bash
# Opt-in, rollback-safe dual IP listener on the Pi's own 192.168.178.63.
# Never starts/stops RTL-SDR or touches dump1090.service.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
UNIT="keyrfmonitor.service"
LAN="192.168.178.63"
PORT=8890
OVERRIDE_DIR="$HOME/.config/systemd/user/$UNIT.d"
OVERRIDE="$OVERRIDE_DIR/zz-lan.conf"

if ! command -v ip >/dev/null; then
  echo "Das Werkzeug ip fehlt; kein LAN-Dashboard aktiviert." >&2
  exit 2
fi
if ! ip -4 -o address show | awk '$3 == "inet" && $4 ~ /^192[.]168[.]178[.]63\/[0-9]+$/ {ok=1} END {exit !ok}'; then
  echo "Die IPv4-Adresse $LAN ist auf diesem Pi nicht konfiguriert; Abbruch." >&2
  exit 2
fi
if ! systemctl is-active --quiet dump1090.service; then
  echo "ADS-B derzeit nicht aktiv (möglicher RF-Scan); Webdienst bleibt unverändert." >&2
  exit 2
fi

ACTIVE_DIR="$(systemctl --user show "$UNIT" --property=WorkingDirectory --value)"
if [[ "$ACTIVE_DIR" != "$ROOT" ]]; then
  echo "WorkingDirectory des Webdiensts ist '$ACTIVE_DIR' statt '$ROOT'; Abbruch." >&2
  exit 2
fi
if ! systemctl --user is-active --quiet "$UNIT"; then
  echo "Der Dashboard-Dienst ist nicht aktiv; zuerst Grundinstallation prüfen." >&2
  exit 2
fi

mkdir -p "$OVERRIDE_DIR"
PREVIOUS=""
if [[ -e "$OVERRIDE" ]]; then
  PREVIOUS="$(mktemp "$OVERRIDE_DIR/.previous-lan.XXXXXX")"
  cp -p "$OVERRIDE" "$PREVIOUS"
fi

rollback() {
  echo "LAN-Einrichtung fehlgeschlagen; bisherige Dienstkonfiguration wiederherstellen." >&2
  if [[ -n "$PREVIOUS" ]]; then
    cp -p "$PREVIOUS" "$OVERRIDE"
  else
    rm -f "$OVERRIDE"
  fi
  systemctl --user daemon-reload || true
  systemctl --user restart "$UNIT" || true
}
clean() {
  if [[ -n "$PREVIOUS" ]]; then rm -f "$PREVIOUS"; fi
}
trap clean EXIT

cat >"$OVERRIDE" <<EOF
[Service]
ExecStart=
ExecStart=/usr/bin/python3 -m keyrfmonitor serve --host 127.0.0.1 --lan-host $LAN --port $PORT
EOF

if ! systemctl --user daemon-reload ||
   ! systemctl --user restart "$UNIT"; then
  rollback
  exit 1
fi

ready=0
for attempt in $(seq 1 20); do
  if systemctl --user is-active --quiet "$UNIT" &&
     curl --noproxy '*' --silent --fail --max-time 2 \
          "http://127.0.0.1:$PORT/api/status" >/dev/null &&
     curl --noproxy '*' --silent --fail --max-time 2 \
          "http://$LAN:$PORT/api/status" >/dev/null; then
    ready=1
    break
  fi
  sleep 1
done
if [[ "$ready" != 1 ]]; then
  rollback
  exit 1
fi

echo "Dashboard lokal: http://127.0.0.1:$PORT"
echo "Dashboard im Heimnetz: http://$LAN:$PORT"
echo "Nur Quellnetz 192.168.178.0/24. Keine Router-Portweiterleitung einrichten."
echo "Keine IQ-Tokenabfrage. HTTP unverschlüsselt, daher nur vertrauenswürdige Geräte."
echo "ADS-B-Dienst wurde nicht verändert."
