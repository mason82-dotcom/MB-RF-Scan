# Raspberry Pi 5 ↔ GitHub: MB-RF-Scan synchron halten

## 1. Einmaliger Import (Pi → GitHub)

Quelle: `~/Projekte/KeyRFMonitor` (funktionsfähige v0.3.0). Ziel: `~/Projekte/MB-RF-Scan` auf GitHub-`main`. Die beiden Projekte sind ansonsten unabhängig von AeroNexusAdvanced.

```bash
sudo apt install -y git rsync
cd ~/Projekte
git clone https://github.com/mason82-dotcom/MB-RF-Scan.git
bash ~/Projekte/MB-RF-Scan/scripts/pi/import-from-keyrfmonitor.sh
```

Das Skript führt Tests durch und verlangt die Bestätigung `IMPORT` vor Commit und Push. Für `git push` wird GitHub-Authentifizierung auf dem Pi benötigt (beispielsweise SSH-Key oder ein korrekt konfigurierter Credential Manager). Keine Passwörter oder Tokens in den Quellcode schreiben.

**Die erste Push-Synchronisierung kann ChatGPT nicht selbst vom Pi aus durchführen.** Bis die Ausgabe `git push origin main` Erfolg meldet, enthält GitHub noch nicht den gesamten produktiven Programmcode.

## 2. Webdienst auf den neuen Checkout umstellen

Erst nach erfolgreichem Import und bestätigten Tests:

```bash
cd ~/Projekte/MB-RF-Scan
/usr/bin/python3 -m unittest discover -s tests -q
mkdir -p ~/.config/systemd/user/keyrfmonitor.service.d
cp -a ~/.config/systemd/user/keyrfmonitor.service.d/override.conf \
  ~/.config/systemd/user/keyrfmonitor.service.d/override.conf.before-mb-rf-scan
cat > ~/.config/systemd/user/keyrfmonitor.service.d/override.conf <<EOF
[Service]
WorkingDirectory=$HOME/Projekte/MB-RF-Scan
ExecStart=
ExecStart=/usr/bin/python3 -m keyrfmonitor serve --host 127.0.0.1 --port 8890
EOF
systemctl --user daemon-reload
systemctl --user restart keyrfmonitor.service
systemctl --user is-active keyrfmonitor.service
curl -fsS --max-time 5 http://127.0.0.1:8890/ >/dev/null && echo 'Web-UI OK'
systemctl is-active dump1090.service
```

Die Datenbank liegt weiter unter `~/.local/share/keyrfmonitor` und wird nicht in Git übertragen. Der alte Projektordner bleibt als Rückfallebene erhalten. Falls die neue Webversion nicht startet, vorherige Override-Datei zurückkopieren, `daemon-reload` und Webdienst neu starten.

## 3. Zukünftige Updates (GitHub → Pi)

```bash
cd ~/Projekte/MB-RF-Scan
bash scripts/pi/sync-from-github.sh
```

Der Befehl ist **manuell und bewacht**: kein Update bei lokalen Git-Änderungen oder inaktivem ADS-B-Dienst; nur Fast-Forward von `origin/main`; Tests vor dem Webdienst-Neustart; Rollback bei fehlschlagenden Tests oder HTTP-Check. Wird die Weboberfläche noch aus dem alten Verzeichnis gestartet, wird sie nicht automatisch umgestellt.

`git status --short --branch` und `git log -1 --oneline` zeigen den lokalen Stand. `git rev-parse HEAD` und `git ls-remote origin refs/heads/main` vergleichen die Revisionen.

**Keine** automatischen Pushes von Pi-Messdaten, lokalen Datenbanken oder IQ-Aufnahmen. Keine automatische Aktualisierung während Funkmessungen. Ein regelmäßiger Update-Timer wird erst nach einem erfolgreich validierten manuellen Update empfohlen.

## 4. Entwicklung direkt auf dem Pi

Nach der Migration ausschließlich unter `~/Projekte/MB-RF-Scan` entwickeln. Vor jedem Push:
```bash
git status --short
/usr/bin/python3 -m unittest discover -s tests -q
git add <bewusst_gewaehlte_dateien>
git commit -m "Beschreibung"
git push origin main
```

Keine `git reset --hard`- oder `git clean`-Kommandos auf dem alten KeyRFMonitor-Verzeichnis ausführen. Die Weboberfläche bindet ausschließlich `127.0.0.1:8890`; keinen ungefilterten LAN-Port freigeben.
