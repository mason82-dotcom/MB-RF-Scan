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

# systemd Type=simple meldet 'started' bereits vor dem HTTP-Listen.
# Abwarten statt beim ersten ECONNREFUSED sofort einen Rollback auszufuehren.
wait_for_dashboard() {
  local attempt
  for ((attempt=1; attempt<=20; attempt++)); do
    if systemctl --user is-active --quiet keyrfmonitor.service &&
       curl --silent --fail --max-time 2 http://127.0.0.1:8890/api/status >/dev/null 2>&1; then
      return 0
    fi
    if ((attempt < 20)); then sleep 1; fi
  done
  return 1
}

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
    if ! systemctl --user restart keyrfmonitor.service || ! wait_for_dashboard; then
      echo "Webdienst nach bis zu 20 s nicht erreichbar. Dienstprotokoll:" >&2
      systemctl --user --no-pager status keyrfmonitor.service >&2 || true
      journalctl --user -u keyrfmonitor.service -n 30 --no-pager >&2 || true
      echo "Rollback auf $old" >&2
      git reset --hard "$old"
      if systemctl --user restart keyrfmonitor.service && wait_for_dashboard; then
        echo "Vorheriger Webdienst wieder erreichbar." >&2
      else
        echo "WARNUNG: Webdienst auch nach Rollback nicht erreichbar! Bitte Journal pruefen." >&2
      fi
      exit 1
    fi
  fi
fi
systemctl is-active --quiet dump1090.service || { echo "WARNUNG: ADS-B ist nicht aktiv!" >&2; exit 1; }
echo "MB-RF-Scan aktualisiert: $old -> $new; ADS-B aktiv."
