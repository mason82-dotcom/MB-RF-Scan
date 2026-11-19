#!/usr/bin/env bash
# Einmaliger, überprüfbarer Import des auf dem Pi laufenden KeyRFMonitor.
# Weder lokale Nutzdaten noch der bestehende Service werden verändert.
set -euo pipefail

SOURCE="$HOME/Projekte/KeyRFMonitor"
TARGET="$HOME/Projekte/MB-RF-Scan"
if [ "$#" -ge 1 ]; then SOURCE="$1"; fi
if [ "$#" -ge 2 ]; then TARGET="$2"; fi

for command in git rsync; do
  command -v "$command" >/dev/null || { echo "Fehlt: $command" >&2; exit 2; }
done
[ -f "$SOURCE/keyrfmonitor/__main__.py" ] || { echo "Quellprojekt nicht gefunden: $SOURCE" >&2; exit 2; }
[ -d "$TARGET/.git" ] || { echo "Ziel nicht geklont: $TARGET" >&2; exit 2; }
[ "$(git -C "$TARGET" branch --show-current)" = "main" ] || { echo "Ziel muss auf main stehen." >&2; exit 2; }
case "$(git -C "$TARGET" remote get-url origin)" in
  https://github.com/mason82-dotcom/MB-RF-Scan.git|git@github.com:mason82-dotcom/MB-RF-Scan.git|ssh://git@github.com/mason82-dotcom/MB-RF-Scan.git) ;;
  *) echo "Falsches GitHub-Remote; Abbruch." >&2; exit 2 ;;
esac
[ -z "$(git -C "$TARGET" status --porcelain)" ] || { echo "Ziel ist nicht sauber; Abbruch." >&2; exit 2; }

EXCLUDES=(
  --exclude='.git/' --exclude='.env' --exclude='.env.*'
  --exclude='.venv/' --exclude='__pycache__/' --exclude='*.pyc'
  --exclude='*.sqlite*' --exclude='*.db' --exclude='*.db-*'
  --exclude='*.cu8' --exclude='*.iq' --exclude='*.bin'
  --exclude='*.log' --exclude='*.pem' --exclude='*.key'
  --exclude='*.crt' --exclude='*.p12' --exclude='*.pfx'
  --exclude='captures/' --exclude='recordings/' --exclude='.pytest_cache/'
  --exclude='iq-traces/'
  # Positivliste: nur Quelltext und Dokumentation, alles andere bleibt lokal
  # (z. B. JSON-/CSV-Exporte, .npy, .wav), da das Repository oeffentlich ist.
  --include='*/'
  --include='*.py' --include='*.html' --include='*.js' --include='*.css'
  --include='*.md' --include='*.sh' --include='*.yml' --include='*.yaml'
  --exclude='*'
)
for dir in keyrfmonitor tests docs scripts; do
  if [ -d "$SOURCE/$dir" ]; then
    mkdir -p "$TARGET/$dir"
    rsync -a --prune-empty-dirs "${EXCLUDES[@]}" "$SOURCE/$dir/" "$TARGET/$dir/"
  fi
done
for file in README.md CHANGELOG.md requirements.txt .gitignore; do
  if [ -f "$SOURCE/$file" ]; then cp "$SOURCE/$file" "$TARGET/$file"; fi
done
# Der Erstimport wird sicherer, wenn lokale Artefakte unabhängig
# vom bisherigen .gitignore generell ignoriert werden. Nur fehlende
# Zeilen ergänzen, damit wiederholte Läufe nichts doppelt anhängen.
touch "$TARGET/.gitignore"
for pattern in .env '.env.*' '*.pem' '*.key' '*.crt' '*.p12' '*.pfx' \
               '*.db' '*.db-*' '*.sqlite*' '*.cu8' '*.iq' '*.bin' '*.log' \
               captures/ recordings/ iq-traces/; do
  grep -qxF -- "$pattern" "$TARGET/.gitignore" || printf '%s\n' "$pattern" >> "$TARGET/.gitignore"
done

cd "$TARGET"
/usr/bin/python3 -m unittest discover -s tests -q
git add -A -- README.md CHANGELOG.md requirements.txt .gitignore keyrfmonitor/ tests/ docs/ scripts/
echo
echo "=== Vorbereitete Git-Dateien ==="
git diff --cached --stat
echo
echo "WICHTIG: Das GitHub-Repository ist oeffentlich."
echo "Pruefe vor dem Push: git diff --cached"
read -r -p "Zum Commit und Push IMPORT eingeben: " consent
[ "$consent" = "IMPORT" ] || { echo "Nichts gepusht."; exit 1; }
git commit -m "Import KeyRFMonitor v0.3 from Raspberry Pi"
git push origin main
echo "Erstimport abgeschlossen. Bestehende KeyRFMonitor-Installation ist unveraendert."
