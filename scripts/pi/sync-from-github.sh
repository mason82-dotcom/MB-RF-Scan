#!/usr/bin/env bash
# Sicherer, manueller Fast-Forward-Deploy vom GitHub-main auf den Pi.
# Berührt keinen SDR, stoppt niemals dump1090.service.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
# Verhindert parallele Deployments (manuelle Aufrufe und Timer).
command -v flock >/dev/null || { echo "flock fehlt; Abbruch." >&2; exit 2; }
mkdir -p "$HOME/.local/share/keyrfmonitor"
exec 9>"$HOME/.local/share/keyrfmonitor/mb-rf-scan-sync.lock"
flock -n 9 || { echo "Ein anderer MB-RF-Scan-Abgleich laeuft bereits."; exit 0; }
[ -d .git ] || { echo "Kein Git-Checkout." >&2; exit 2; }
case "$(git remote get-url origin)" in
  https://github.com/mason82-dotcom/MB-RF-Scan.git|git@github.com:mason82-dotcom/MB-RF-Scan.git|ssh://git@github.com/mason82-dotcom/MB-RF-Scan.git) ;;
  *) echo "Unerwartetes GitHub-Remote; Abbruch." >&2; exit 2 ;;
esac
[ "$(git branch --show-current)" = main ] || { echo "Nur main wird synchronisiert." >&2; exit 2; }
[ -z "$(git status --porcelain)" ] || { echo "Lokale Aenderungen vorhanden: git status pruefen; Abbruch." >&2; exit 2; }
if ! systemctl is-active --quiet dump1090.service; then
  echo "ADS-B derzeit nicht aktiv; moeglicherweise laeuft ein Scan. Kein Update." >&2
  exit 2
fi

old="$(git rev-parse HEAD)"
git fetch origin main
new="$(git rev-parse origin/main)"
if [ "$old" = "$new" ]; then
  echo "Bereits aktuell: $old"
  exit 0
fi
git merge-base --is-ancestor "$old" "$new" || { echo "Kein Fast-Forward moeglich; Abbruch." >&2; exit 2; }
git merge --ff-only origin/main
if ! /usr/bin/python3 -m unittest discover -s tests -q; then
  echo "Tests fehlgeschlagen; stelle vorherigen Commit wieder her." >&2
  git reset --hard "$old"
  exit 1
fi

if systemctl --user is-active --quiet keyrfmonitor.service; then
  service_dir="$(systemctl --user show keyrfmonitor.service --property=WorkingDirectory --value)"
  if [ "$service_dir" != "$ROOT" ]; then
    echo "Hinweis: Webdienst verwendet $service_dir und wird nicht neu gestartet."
    echo "Zuerst docs/PI_SYNC.md zum Umzug des Webdienstes lesen."
  else
    if ! systemctl --user restart keyrfmonitor.service || ! curl -fsS --max-time 5 http://127.0.0.1:8890/ >/dev/null; then
      echo "Webtest fehlgeschlagen; Rollback auf $old" >&2
      git reset --hard "$old"
      systemctl --user restart keyrfmonitor.service || true
      exit 1
    fi
  fi
fi
systemctl is-active --quiet dump1090.service || { echo "WARNUNG: ADS-B ist nicht aktiv!" >&2; exit 1; }
echo "MB-RF-Scan aktualisiert: $old -> $new; ADS-B aktiv."
