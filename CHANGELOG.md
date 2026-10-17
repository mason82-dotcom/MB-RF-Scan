# Änderungsprotokoll

## 0.6.0 – Sitzungsvergleich und Empfangsdiagnose (2026-10-17)

- Echte Burst-Metadaten werden einer aktiven CLI-Scan-Sitzung über Ereignis-IDs zugeordnet; synthetische CC1101-Ereignisse werden ausgeschlossen.
- Nichtdestruktive SQLite-Erweiterung: mittlere/minimale/maximale Burstdauer sowie mittlere/maximale SNR- und Pegelanstiegswerte pro neu abgeschlossenem Scan.
- Read-only `/api/sessions` ergänzt um `metrics_available` und neutrale Diagnosekennzeichen wie »keine Bursts«, »keine Paare«, »abgebrochene Sitzung« oder »ADS-B-Wiederherstellung fehlgeschlagen«.
- Sitzungsvergleich mit Verlaufsgrafiken für Burst-Anzahl, Paare, SNR und Dauer; nur Metadaten, keine Rohdaten.
- Bereits vorhandene v0.5-Sitzungen bleiben unverändert erhalten; fehlende historische Qualitätswerte werden als »nicht verfügbar« gekennzeichnet.
- Die ADS-B-Umschaltung und Root-Watchdog-Logik bleiben unverändert, ebenso die Bindung an Loopback.


## 0.5.0 – Protokoll befristeter Scan-Sitzungen (2026-10-17)

- Neue SQLite-Tabelle `scan_sessions` mit automatisch protokolliertem Beginn, Soll-/Ist-Dauer, Ergebnis, Burst- und Paarzahlen sowie separat verifiziertem ADS-B-Wiederherstellungsstatus.
- Bei SIGKILL oder Stromausfall bleibt der Status unbestätigt; die Root-Wiederherstellungs-Unit ist weiterhin das unabhängige Fail-Safe.
- Schreibgeschützte API `GET /api/sessions` und Sitzungstabelle im Dashboard.
- Keine Rückdatierung alter Messungen und keine Verknüpfung mit dekodierten Telegrammdaten, IQ-Aufnahmen oder Schlüsselcodes.
- Keine Änderung des befristeten Scan-Fensters (20–300 Sekunden), der Privilegien oder des SDR-Exklusivzugriffs.


## 0.4.0 – Live-Diagnose und Verlauf (2026-10-17)

- Schreibgeschützte Statistik-API `/api/summary?hours=6|24|168` mit UTC-Stundenwerten, Burst-/Paarzählern, durchschnittlicher Dauer und SNR.
- Nur reale RTL-SDR-Ereignisse fließen in die Statistik ein; synthetische CC1101-Simulationen werden ausgeschlossen.
- Neue Verlaufsgrafik mit Zeitraumauswahl und Live-Pegeltrend aus Statusmetadaten; verbesserte Kennzahlen und Quellkennzeichnung.
- Konsolen-Scanbefehl nutzt `~/Projekte/MB-RF-Scan`; weiterhin keine privilegierte Browsersteuerung und keine IQ-/Schlüsselcode-Erfassung.


## 0.3.0 – Befristeter ADS-B-Scanmodus (2026-10-17)

- Neuer Operator-Befehl `keyrfmonitor scan --seconds 60` (20–300 Sekunden).
- `dump1090.service` wird nur pausiert, wenn er vorher aktiv war; normale Webmessung stoppt keinen Dienst.
- Systemd-Rückschalt-Timer wird **vor** der Pause scharfgeschaltet und nach erfolgreicher Wiederherstellung entfernt.
- Bei `Strg+C`, SIGTERM, Fehler und regulärem Timeout wird der SDR zuerst freigegeben und ADS-B wieder gestartet.
- Nach SIGKILL/Prozessabsturz übernimmt der unabhängige Wiederherstellungstimer.
- Laufzeitbegrenzung, feste systemd-Befehle, keine Passwortspeicherung, keine Code-/IQ-Speicherung.
- Prozessübergreifende exklusive Dateisperre gegen parallele Empfängernutzung.
- Dashboard-Hinweis und Kopierhilfe für den CLI-Scanmodus, keine privilegierten HTTP-Endpunkte.
- Unveränderte CC1101-SPI-Vorbereitung und synthetische Laborsimulation; kein Hardwarebetrieb erforderlich.

## 0.2.0 – CC1101 vorbereiten (2026-10-17)

- Eigene Hardwarestruktur `keyrfmonitor/hardware/cc1101.py`.
- Raspberry-Pi-5-SPI0/CE0-Verdrahtung über `cc1101 wiring` abrufbar.
- Optionaler, nicht sendender Registertest `cc1101 probe` (PARTNUM / VERSION).
- Neue CC1101-Dokumentation, 3,3-V-Sicherheit und getrennte Schnittstelle.
- Keine Änderungen am RTL-SDR-Backend, ADS-B oder AeroNexusAdvanced.
