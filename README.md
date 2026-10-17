# MB-RF-Scan

Eigenständiges Raspberry-Pi-Projekt für **passive Funkdiagnose**, basierend auf dem funktionierenden lokalen **KeyRFMonitor v0.3.0**. Keine Verbindung zu AeroNexusAdvanced.

## Stand und Erstimport

Das GitHub-Repository enthält zunächst die sicheren Import- und Sync-Werkzeuge. **Der eigentliche Programmcode muss einmalig aus der auf dem Pi laufenden Installation übernommen werden.** So bleibt der auf dem Pi getestete Entwicklungsstand maßgeblich.

Auf dem Raspberry Pi:

```bash
sudo apt install -y git rsync
cd ~/Projekte
git clone https://github.com/mason82-dotcom/MB-RF-Scan.git
bash ~/Projekte/MB-RF-Scan/scripts/pi/import-from-keyrfmonitor.sh
```

Das Skript prüft die vorhandene Installation unter `~/Projekte/KeyRFMonitor`, kopiert nur Projektquellen (keine lokalen Aufnahmen, Datenbanken, Schlüssel oder Zugangsdaten), führt die Unit-Tests aus, zeigt die vorgesehenen Änderungen und fragt **vor dem Commit und Push** nochmals nach. Die bisherige Installation bleibt unangetastet.

Danach: [Pi-Synchronisation und Webdienst-Migration](docs/PI_SYNC.md).

## Zielkonfiguration

| Bereich | Einstellung |
| --- | --- |
| Repository | `mason82-dotcom/MB-RF-Scan` |
| Branch | `main` |
| Pi-Verzeichnis | `~/Projekte/MB-RF-Scan` |
| Dashboard | `127.0.0.1:8890` (nur lokal/SSH-Tunnel) |
| ADS-B | `dump1090.service` – im Normalbetrieb aktiv |
| Empfänger | RTL2832U/R820T; CC1101 nur vorbereitet |
| Betriebsart | Nur passive Erfassung und technische Ereignis-Metadaten |

**Wichtig:** Es wird weder ein SDR automatisch übernommen noch ein Fahrzeug-Schlüsselcode gespeichert oder ausgesendet. Der befristete CLI-Scan verwaltet ADS-B ausschließlich nach ausdrücklichem Start auf dem Pi. Synchronisierung führt keine automatischen Scans durch.

## Entwicklung und Tests

```bash
cd ~/Projekte/MB-RF-Scan
/usr/bin/python3 -m unittest discover -s tests -q
bash scripts/pi/sync-from-github.sh
```

Der Sync verweigert Änderungen bei lokalen Git-Modifikationen und wenn der ADS-B-Dienst nicht aktiv ist. Bei Testfehlern wird auf den vorherigen Git-Commit zurückgestellt. Die GitHub Actions testen das Python-Projekt nach dem Erstimport.
