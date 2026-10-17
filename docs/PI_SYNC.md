# Raspberry Pi 5 ↔ GitHub: MB-RF-Scan synchron halten

> **Aktueller Installationsstand:** Die Erstübernahme wurde am 17.10.2026 auf `main` als Commit `5bd63b8` erfolgreich übertragen; 25 Unit-Tests bestanden. Die produktive Weboberfläche des Pi läuft aus `~/Projekte/MB-RF-Scan` auf `127.0.0.1:8890`, während `dump1090.service` aktiv bleibt. Die nachfolgenden Erstimport-Schritte sind nur für Neuinstallationen erforderlich.

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

## 5. Optional: GitHub → Raspberry Pi stündlich automatisch synchronisieren

Die Einrichtung erfolgt **bewusst einmalig am Pi**. Neue Commits auf GitHub-`main` werden dann ungefähr stündlich (mit bis zu fünf Minuten zufälliger Verzögerung) abgerufen. Der Timer startet **keine Funkmessung** und **stoppt dump1090 nicht**. Bei lokalen Änderungen, einem laufenden ADS-B-Pausenfenster oder divergierender Git-Historie wird der Abgleich abgebrochen; damit gibt es keinen erzwungenen Merge. Bei fehlschlagenden Tests oder einem fehlgeschlagenen HTTP-Test erfolgt ein Git-Rollback.

Zuerst einmalig prüfen und den Timer aktivieren:

```bash
cd ~/Projekte/MB-RF-Scan
bash scripts/pi/sync-from-github.sh
bash scripts/pi/install-sync-timer.sh
systemctl --user is-enabled mb-rf-scan-sync.timer
systemctl --user list-timers --all mb-rf-scan-sync.timer
```

Status und Protokoll:

```bash
systemctl --user status mb-rf-scan-sync.timer --no-pager
journalctl --user -u mb-rf-scan-sync.service -n 60 --no-pager
```

Bei Bedarf einmal sofort synchronisieren: `systemctl --user start mb-rf-scan-sync.service`. Dauerhaft beenden: `systemctl --user disable --now mb-rf-scan-sync.timer`.

**Synchronisierungsrichtung:** GitHub → Pi automatisch, lokale Codeänderungen → GitHub weiterhin bewusst nach Tests mit `git commit` und `git push`. Es gibt keine Hintergrundübertragung von Messdaten, Datenbanken, Zugangstokens oder IQ-Dateien. User-Timer laufen nur solange der systemd-Benutzermanager aktiv ist; bei ausgeschaltetem Pi wird kein Update ausgeführt und nach dem Wiederanlaufen entsprechend der Timer-Konfiguration nachgeholt. Für durchgehende Ausführung ohne Login wäre gesondert systemd-Linger zu konfigurieren.

**Hinweis:** Ein grüner Unit-Test-Lauf ersetzt keine manuelle Prüfung kritischer Funk-/Hardwarefunktionen bei größeren Änderungen; der HTTP-Test prüft lediglich die Erreichbarkeit des Dashboards.

## 6. Dashboard-Neustart: Readiness-Check und Fehlerdiagnose

Der Webdienst ist eine systemd-`Type=simple`-Unit. `systemctl --user restart` kann erfolgreich zurückkehren, bevor `127.0.0.1:8890` gebunden ist. Deshalb wartet `sync-from-github.sh` ab dem nächsten Commit bis zu 20 Sekunden auf **Dienststatus aktiv** und die HTTP-API `/api/status`. Ein einzelnes `curl: (7) Failed to connect` löst keinen sofortigen Rollback mehr aus.

Wenn der Webtest weiterhin fehlschlägt, werden Dienststatus und letzte Journalzeilen ausgegeben, anschließend erfolgt ein Git-Rollback und ein erneuter Bereitschaftstest des alten Webdienstes. Ein Git-Rollback behebt keine Probleme wie Portkonflikte, fehlende Rechte oder unvollständige systemd-Konfiguration.

Sicher prüfen:

```bash
systemctl --user show keyrfmonitor.service -p WorkingDirectory -p ExecStart -p NRestarts
systemctl --user status keyrfmonitor.service --no-pager -l
journalctl --user -u keyrfmonitor.service -n 60 --no-pager -l
ss -ltnp '( sport = :8890 )'
curl -sS --max-time 3 http://127.0.0.1:8890/api/status
systemctl is-active dump1090.service
```

Nach einem Rollback auf `5bd63b8` fehlt `install-sync-timer.sh` lokal wieder; zuerst die Webdienstursache klären. Zum einmaligen Einspielen der neuen Version bei stabiler Weboberfläche `git fetch origin main && git merge --ff-only origin/main` ausführen, Unit-Tests starten und anschließend den Webdienst kontrolliert neu starten. Den Timer erst aktivieren, wenn dieser Schritt erfolgreich ist.
