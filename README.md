# KeyRFMonitor

**Eigenständiger passiver RF-Monitor für Raspberry Pi 5 und RTL-SDR.**

**Version:** 0.3.0 (befristeter ADS-B-Scanmodus; CC1101-Simulator und SPI-Vorbereitung, kein CC1101-Hardwarebetrieb). Dieses Projekt ist **vollständig unabhängig von AeroNexusAdvanced**: kein Import, kein gemeinsamer Dienst, kein gemeinsames Repository, kein gemeinsamer Speicher und kein Zugriff auf dessen Dateien.

## Funktionsumfang

- Lokales Web-Dashboard (nur `127.0.0.1:8765`, ohne öffentliches Netzwerkinterface)
- Passive Überwachung der **zwei gemessenen FSK-Frequenzlagen** ohne Bit- oder Protokolldekodierung
- Automatische Burst-Erkennung anhand 10-ms-Spektralfenstern und adaptiver Rauschreferenz
- Erkennung von Burst-Paaren anhand des **Abstands der Burst-Anfänge (400–650 ms)**
- Anzeigen von Pegelanstieg, SNR und Burst-Dauern
- Lokale SQLite-Historie nur mit technischen **Metadaten** (keine Rohsignale, kein Key-Material)
- Offline-Analyse bestehender RTL-SDR-IQ-Dateien (CU8, 1,024 MS/s) **ohne Kopie der Eingabedatei**
- Kein RTL-SDR-Autostart: Webdienst kann bei aktivem ADS-B laufen, SDR-Messung aber nicht gestartet werden
- Der **Web-Empfang** verändert ADS-B nicht und verweigert den Start, solange `dump1090.service` läuft
- Neuer ausdrücklicher **CLI-Scanmodus**: stoppt `dump1090.service` befristet, misst lokal und stellt ADS-B danach wieder her – mit unabhängiger systemd-Wiederherstellung als Ausfallschutz
- Gegenseitige Verriegelung des RTL-SDR zwischen Webempfang und CLI-Scan mittels Dateisperre
- Unit-Tests mit künstlichen FSK-Daten
- **CC1101 vorbereitet:** Pinbelegung am Pi 5, optionaler SPI-Register-Probe und getrennte Hardware-Modulstruktur; kein CC1101-RX/TX-Betrieb

## Referenzparameter aus der Messreihe (nicht kalibriert)

| Parameter | Eingestellter bzw. näherungsweise gemessener Wert |
|---|---|
| Frequenz | 433,920 MHz |
| SDR | RTL2832U / R820T |
| Gain | 7,7 dB |
| Abtastrate | 1.024.000 Samples/s (CU8) |
| Modulation | 2-FSK (durch Phasenanalyse gut gestützt) |
| Frequenzlagen relativ | −20,12 kHz / +11,35 kHz |
| Frequenzabstand | rund 31,47 kHz |
| Symbolrate | circa 2.000 Bd (geschätzt, **wird nicht dekodiert**) |
| Burst | rund 175 ms |
| Pair-Abstand | rund 513 ms zwischen Startpunkten |

Die Kalibrierung des SDR-Oszillators ist nicht nachgewiesen. Ergebnisse gelten zunächst für die gemessene Kombination und die geprüften Aussendungen. Der Monitor ersetzt keinen kalibrierten HF-Messplatz. Automatische Erkennung kann bei Störungen Fehlalarme auslösen oder schwache Signale übersehen.

## Installation auf Raspberry Pi OS

Das ZIP z. B. nach `/home/mason/Projekte/KeyRFMonitor` entpacken. Es wird **keine** bestehende `AeroNexusAdvanced`-Datei benötigt oder verändert.

```bash
sudo apt update
sudo apt install -y rtl-sdr python3-numpy
cd ~/Projekte/KeyRFMonitor
/usr/bin/python3 -m unittest discover -s tests -v
/usr/bin/python3 -m keyrfmonitor serve
```

Lokale Anzeige auf dem Pi: `http://127.0.0.1:8765`

Per SSH-Tunnel von einem anderen Rechner (Beispiel, auf dem **Client** ausführen):

```bash
ssh -L 8765:127.0.0.1:8765 mason@rpi5
```

Danach auf dem Client `http://127.0.0.1:8765` aufrufen. Nur auf einem vertrauenswürdigen Rechner und in einer vertrauenswürdigen SSH-Sitzung öffnen.

### Optional: Autostart der lokalen Weboberfläche

```bash
cd ~/Projekte/KeyRFMonitor
bash scripts/install-user-service.sh
```

Das startet **nur den lokalen Webserver**, niemals den SDR selbst. Der SDR startet ausschließlich nach einem aktiven Klick auf **Messung starten**. Der Dienst stoppt oder startet `dump1090.service` nicht.

```bash
systemctl --user status keyrfmonitor.service
systemctl --user stop keyrfmonitor.service
```

## CC1101-Hardwarevorbereitung (noch kein physischer Sender)

**Wichtig:** Die Vorbereitung verursacht keine HF-Aussendung und setzt weder einen CC1101-Chip noch eine freie SPI-Schnittstelle voraus. Der vorhandene RTL-SDR-Monitor funktioniert unverändert weiter.

```bash
cd ~/Projekte/KeyRFMonitor
/usr/bin/python3 -m keyrfmonitor cc1101 wiring
```

**Erst nachdem ein CC1101-Modul korrekt angeschlossen wurde:** SPI gezielt auf dem Pi aktivieren, `python3-spidev` installieren und den Registertest manuell ausführen:

```bash
sudo raspi-config nonint do_spi 0
sudo apt install -y python3-spidev
ls -l /dev/spidev0.0
/usr/bin/python3 -m keyrfmonitor cc1101 probe
```

Die Anschlussbelegung, 3,3-V-Vorsichtsmaßnahmen und die Grenzen des Tests sind in **[docs/CC1101.md](docs/CC1101.md)** beschrieben. Das Modul wird nur über SPI ausgelesen; in Version 0.2.0 gibt es **keine** CC1101-RX-Konfiguration, keinen HF-TX-Treiber und keine physische Replay-Funktion.

## Befristeter Scanmodus (Neu in 0.3.0)

Für eine Messung ohne dauerhaftes Deaktivieren des Flugzeugempfangs auf deinem Pi:

```bash
cd ~/Projekte/KeyRFMonitor
/usr/bin/python3 -m keyrfmonitor scan --seconds 60
```

Dieser Befehl wird **als Benutzer `mason`, nicht mit `sudo python`** gestartet. Der Modus prüft erst, ob `dump1090.service` aktiv ist, verifiziert die Anwesenheit von `rtl_sdr`, fordert eine einmalige `sudo`-Berechtigung für die systemd-Aktionen an und installiert mit `systemd-run` **vor dem Stoppen** einen befristeten systemweiten Rückschalt-Timer. Dann wird `dump1090.service` kontrolliert gestoppt, der RTL-SDR exklusiv für die passive 433-MHz-Erkennung übernommen und nur Burst-Metadaten protokolliert. Nach 60 Sekunden (oder `Strg+C`, Fehler oder SIGTERM) gibt der Monitor den Stick frei, startet ADS-B wieder und entfernt den Timer. Wird der Scanprozess hart beendet, stellt der unabhängige Timer ADS-B spätestens circa 45 Sekunden nach der Soll-Laufzeit wieder her. Ein Neustart des Pi aktiviert den zuvor aktivierten regulären ADS-B-Dienst wieder; der Timer ersetzt keine Systemsicherung gegen alle Fehler.

**Sicherheitsgrenzen:** Ohne erfolgreichen Timer-Start wird ADS-B nicht angehalten. Ist ADS-B bereits inaktiv, verweigert der Modus die Aktion (kein fälschliches Wiedereinschalten fremd verwalteter Dienste). Dauer **20–300 Sekunden**. Es werden weder der Enable-/Autostartstatus der Unit noch die Konfiguration geändert. Die Weboberfläche darf niemals `sudo` aufrufen und bietet deshalb nur eine Kopierhilfe für den Konsolenbefehl. Vor dem ersten Betrieb auf dem Pi muss die lokale Funktion von `systemd-run` geprüft werden. Auf einem nicht mit systemd betriebenen Host funktioniert dieser Modus nicht.

**Manuelle Kontrolle:**

```bash
systemctl is-active dump1090.service
systemctl list-timers --all | grep keyrfmonitor || true
```

Wenn ADS-B wider Erwarten nicht startet:

```bash
sudo systemctl start dump1090.service
sudo systemctl status dump1090.service --no-pager
```

## Umgang mit dem gemeinsam genutzten RTL-SDR

Der R820T/RTL2832U kann nicht gleichzeitig vom ADS-B-Empfänger (`dump1090.service`, 1090 MHz) und dem 433-MHz-Monitor verwendet werden.

**Vor einer Messung (bewusste manuelle Aktion):**

```bash
sudo systemctl stop dump1090.service
```

1. Weboberfläche öffnen und **Messung starten** wählen.
2. Ca. 2 Sekunden zur Ruhekalibrierung abwarten. In dieser Zeit keine Schlüsselbetätigung.
3. Signalereignisse beobachten.
4. **Messung beenden** wählen; auf Status „Gestoppt“ warten.
5. ADS-B **manuell** wieder aktivieren:

```bash
sudo systemctl start dump1090.service
systemctl is-active dump1090.service
```

**Hinweis:** Im manuellen Webmodus bleiben Dienständerungen ausdrücklich ausgeschlossen. Nur der getrennte, vom Operator aufgerufene CLI-Scanmodus verändert ADS-B kurzzeitig.

## Vorhandene IQ-Aufnahme offline analysieren

```bash
cd ~/Projekte/KeyRFMonitor
/usr/bin/python3 -m keyrfmonitor analyze /tmp/mercedes_433_iq.cu8
```

Das Tool liest die Aufnahme nur und druckt Burst-Dauern sowie Abstände aus. Es erstellt keine neuen IQ-Dateien und gibt weder Schlüsseldaten noch demodulierte Bits aus. Offline-Messungen werden nicht in die Live-Historie übernommen.

## Architektur

```text
KeyRFMonitor/
  keyrfmonitor/
    hardware/cc1101.py  Vorbereitete SPI-Pinbelegung/Chiperkennung
    dsp.py          DSP: nur FFT-Energien, keine Code-Demodulation
    receiver.py     rtl_sdr-Subprozess, prozessübergreifende SDR-Sperre
    adsb_session.py Kontrollierter ADS-B-Scan mit Root-Watchdog
    storage.py      SQLite-Metadaten mit Spalten-Whitelist
    server.py       HTTP-API nur auf Loopback, CSRF-Schutz
    __main__.py     CLI: serve / analyze
    web/            HTML/CSS/JS, keine CDNs
  scripts/
    install-user-service.sh
  docs/CC1101.md
  tests/
    test_detector.py
  README.md
  requirements.txt
```

Die Historie liegt unter `~/.local/share/keyrfmonitor/events.sqlite3` mit Dateirechten `0600`. Alle Daten werden lokal verarbeitet, keine externe Übermittlung. Technische Zeitstempel der Detektionen können weiterhin Rückschlüsse auf Anwesenheit und Nutzung ermöglichen; Backup/Weitergabe entsprechend behandeln.

## Sicherheitsgrenzen und bewusst nicht enthaltene Funktionen

- **Kein** physisches Senden, HF-Replay, Rolling-Code-Nachbau oder Fahrzeugzugriff. Die getrennte synthetische Laborsimulation sendet nichts aus.
- **Keine** Speicherung von IQ-Rohdaten durch den Live-Monitor
- **Keine** Speicherung bzw. Ausgabe von Symbol- oder Schlüsselbitfolgen
- **Keine** Änderung der Konfiguration oder des Enable-Status von `dump1090.service`; befristetes Stop/Start nur per bewusstem CLI-Scan; keine Änderungen an AeroNexusAdvanced
- **Keine** automatische CC1101-Probe beim Start, keine SPI-Zugriffe über die Weboberfläche
- **Kein** LAN-Listener ohne Authentifizierung (bewusst nur `127.0.0.1`)
- Kein Firmware-Flash oder Kernel-Treiberänderungen
- Kein Anspruch auf sichere Erkennung aller Schlüsseltypen; andere Frequenzen benötigen explizite Konfigurationsanpassungen im Quellcode

## Prüfung und Fehlerdiagnose

```bash
/usr/bin/python3 -m unittest discover -s tests -v
rtl_test -t
systemctl is-active dump1090.service
```

`rtl_test -t` kann bei einem R820T den Hinweis "No E4000 tuner found" ausgeben; das ist kein Nachweis eines Defekts. Ein `usb_claim_interface error -6` bedeutet in der Regel, dass ein anderer Prozess den Stick belegt. Die Startwarnung `[R82XX] PLL not locked!` ist zu protokollieren, wenn die Abstimmung anschließend dennoch funktioniert.

**Hinweis:** Für einen Raspberry Pi auf reinem SSH-Betrieb die Benutzer-Systemd-Unit verwenden. Ein Browser auf einem anderen Gerät benötigt einen SSH-Tunnel oder einen später separat entwickelten, authentifizierten TLS-Zugang. Bitte den Webserver nicht über einen ungeschützten Port-Forward ins LAN/Internet veröffentlichen.

## GitHub-Synchronisierung auf dem Raspberry Pi

Der produktive Quellcode liegt unter `~/Projekte/MB-RF-Scan` auf Branch `main`. Das Dashboard läuft auf `127.0.0.1:8890` (SSH-Tunnel erforderlich, kein offener LAN-Zugang). Die bereits auf GitHub importierte Version v0.3.0 hat 25 lokale Unit-Tests bestanden.

Manuell aktualisieren:

```bash
cd ~/Projekte/MB-RF-Scan
bash scripts/pi/sync-from-github.sh
```

Optional stündlichen, abgesicherten **GitHub → Pi**-Abgleich einrichten:

```bash
bash scripts/pi/install-sync-timer.sh
```

Anleitung, Prüfung, Rollback und Deaktivierung: [docs/PI_SYNC.md](docs/PI_SYNC.md). Der Abgleich führt keine SDR-Messungen oder ADS-B-Unterbrechungen aus und pusht keine Messdaten nach GitHub.

## v0.4.0: Echtzeitdiagnose und Verlauf

Die Weboberfläche zeigt einen Pegeltrend aus den Statusmetadaten des aktiven Web-Empfängers sowie einen Verlauf der realen RTL-SDR-Ereignisse über 6, 24 oder 168 Stunden. Der Endpunkt `GET /api/summary?hours=24` liefert aggregierte SQLite-Metadaten (keine IQ-Daten, Schlüsselbits oder Senderbefehle). Simulierte CC1101-Ereignisse werden in der Ereignistabelle gekennzeichnet, jedoch nicht in der Statistik mitgezählt. Bei zeitlich begrenzten Konsolenscans bleibt der Livepegel des Web-Monitors inaktiv; die gespeicherten Burst-Ereignisse aktualisieren den Verlauf. Die HTML-Oberfläche stoppt ADS-B nicht und führt kein sudo aus.

## v0.5.0: Scan-Sitzungsverlauf

Bei jedem autorisierten Terminalscan (`/usr/bin/python3 -m keyrfmonitor scan --seconds 60`) wird ein lokaler Datensatz angelegt. Nach dem Scan werden Ausführungsdauer, Burst- und Paarzahlen und das Ergebnis der ADS-B-Wiederherstellungsprüfung nachgetragen.

Die API `GET /api/sessions` zeigt höchstens 30 der jüngsten Sitzungen und speichert ausschließlich Metadaten. Nicht beendete Datensätze behalten `running/pending` und können nach Ablauf der geplanten Dauer als unvollständig markiert werden. **Dies ist kein zuverlässiger Hinweis auf den aktuellen Dienstzustand:** Der unabhängige Root-Watchdog bleibt für die ADS-B-Rückschaltung zuständig. Alte Burst-Ereignisse aus v0.3/v0.4 werden nicht rückwirkend Sitzungen zugeordnet.

Auch v0.5.0 startet weder Scans noch `sudo`-Kommandos aus dem Browser. Der Webserver zeigt Sitzungen nur lesend an.

## v0.6.0: Scanvergleich und Qualitätskennzahlen

Neue CLI-Scans erhalten zusätzliche, rein lokale Qualitätskennzahlen aus den
bereits gespeicherten Burst-Metadaten: mittlere/minimale/maximale Burstdauer,
mittlerer/maximaler Signal-Rausch-Abstand (SNR) und mittlerer/maximaler Pegelanstieg.
Nur während der aktiven CLI-Sitzung erfasste **echte RTL-SDR-Ereignisse** werden
berücksichtigt; `scan_session_events` ordnet dafür ausschließlich Datenbank-IDs
zu. Synthetische CC1101-Ereignisse werden nie für diesen Vergleich verwendet.

Das Dashboard zeigt einen Vergleich der jüngsten abgeschlossenen Scans,
mit Burst- und Paarzahlen sowie optional SNR und Burstdauer. Diagnosehinweise
beruhen nur auf objektiven Kriterien (z. B. **keine Bursts**, **keine Burst-Paare**,
**fehlende SNR-Metadaten**, **unvollständig protokolliert**,
**ADS-B-Wiederherstellungsversuch fehlgeschlagen**). Diese Aussagen sind weder
eine Senderidentifikation noch eine Bewertung der Fahrzeug- oder Schlüssel-Sicherheit.

Alte v0.5-Sitzungen bleiben erhalten und zeigen `metrics_available: false`,
da sie nicht nachträglich mit Messereignissen verknüpft werden. Neue Sessions
liefern selbst bei null Bursts `metrics_available: true` und null-SNR/-Dauer.
Die `/api/sessions`-Schnittstelle bleibt schreibgeschützt und liefert maximal
30 Sitzungen. Die Browseranwendung kann weiterhin weder `dump1090.service`
anhalten noch einen privilegierten Terminalscan auslösen.
