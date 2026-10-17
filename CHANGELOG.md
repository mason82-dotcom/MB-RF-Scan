# Änderungsprotokoll

## v0.9.4 – Bytegenauer Rohdatenexport im Heimnetz-Dashboard

- Zwei explizite Download-Schaltflächen für originale `.cu8`-Samples und `.json`-Metadaten einer ausgewählten bestehenden Aufnahme.
- Neue GET-Schnittstelle `/api/iq/download?id=…&format=cu8|json` mit `Content-Disposition: attachment`, `no-store`, `nosniff` und byteidentischem Inhalt.
- Strenge ID-/Formatprüfung, Symlink- und FIFO-Abwehr, prüfbare Dateigrößen- und Archivkonsistenzgrenzen; keine Manipulation von gespeicherten Dateien.
- Auch JSON-Dateien können Bitentscheidungen enthalten. Nur im vertrauenswürdigen Heimnetz ohne Portfreigabe nutzen; bestehendes HTTP bleibt unverschlüsselt.
- Kein neuer Scan, keine Aussendung, kein Replay und keinerlei Einfluss auf ADS-B.


## v0.9.3 – Schwellenwert-Sensitivität für archivierte I/Q-Signale

- Separate, rein lesende Schwellenwertprüfung bei 6,0 bis 9,0 dB über lokalem Rauschpegel; Hüllkurvenabschnitte, längster Abschnitt und größte Lücke.
- Bei ausreichend stabilem Befund an zwei niedrigeren Schwellen vorläufige Kandidaten-Schätzung, ohne die ursprünglichen `edge_status`-Werte zu überschreiben.
- Dashboard vergleicht beide Bursts eines ausgewählten Paares in einer Sensitivitätstabelle und kennzeichnet hypothetische Aussagen.
- Synthetische Regressionstests für 19-ms-Amplitudeneinbruch gegenüber echter Sendeunterbrechung, unsichere Referenz und unveränderte Daten.
- Keine Übertragung, Replay-Funktion, automatische ADS-B-Umschaltung oder neue Speicherung von Schlüsselbits.


## v0.9.2 – Gespeicherte CU8-Hüllkurven statt Bitcode-Raten

- Read-only Analyse zweier gespeicherter I/Q-Bursts mit 1-ms-RMS-Energieraster und Rauschreferenz aus dem aufgenommenen Vor-/Nachlauf.
- Schätzt Signalbeginn, Ende und aktive Dauer; vergleicht diese vorsichtig mit den 10-ms-Detektorgrenzen, besonders bei 180-/190-ms-Abweichungen.
- Meldet unzureichende Referenz, geringe Kontraste, abgeschnittene Ränder und unterbrochene Signale als Unsicherheit.
- Dashboard mit überlagerten Leistungshüllkurven beider Bursts und paarweise auswählbarer Qualitätsbewertung.
- Neue LAN-begrenzte GET-API `/api/iq/pair-quality?pair=N` ohne Rohdaten, Bits, Replay, automatischen SDR-Start oder ADS-B-Eingriffe; Daten unverändert.


## v0.9.1 – Zeitliche Gruppierung archivierter I/Q-Bursts

- Chronologische, schreibgeschützte Gruppierung der letzten höchstens 48 gespeicherten Burst-Metadaten zu disjunkten Paaren bei 400–650 ms Startabstand, Referenz 513 ms.
- Live-Dashboard zeigt Paarnummer, Zeiten, Ruhepausen, Burstdauer, SNR, Ausreißer/ungepaarte Bursts und direkte Navigation in die I/Q-Ansicht.
- Keine Annahme, welche Taste oder welches Fahrzeugkommando zu einem Paar gehört; externe Reihenfolge 2x Auf, 2x Zu bleibt unbestätigter Bedienhinweis.
- Kein Zugriff auf Bitstreams oder IQ-Samples für die Paaranalyse; keine SDR-Aktion, kein ADS-B-Stop/Start und keine automatische Aufzeichnung durch diesen Endpunkt.


## 0.9.0 – Decoder-Hypothesen aus realen Burst-I/Q-Fenstern

- Zusätzliche rein lesende FSK-Bitinterpretation mit zehn wählbaren Symbolraten, NRZ/Manchester, normaler/invertierter Polarität und Manchester-Halbbit-Phase.
- Hexansicht ohne erfundene unbekannte Bits (`??`); Symbolunsicherheit, mögliche alternierende Pegelfolgen, Taktversatz und geschätzte Tonfrequenzen.
- Statische Original-CU8-Aufnahmen bleiben unverändert, keine Änderung der Scanzeit, ADS-B-Watchdog-Logik, HF-Übertragung oder des LAN-Zugriffs.
- Neue read-only Decoder-API mit strikter Parameterprüfung und maximal zwei gleichzeitig laufenden DSP-Aufträgen.
- Zusätzliche Tests für Symbolhypothesen, Archiv-Unveränderlichkeit, unbekannte Bits, HTTP-API und UI-Verträge.


## v0.8.1 – Echte I/Q- und Bitansichten sofort bei regulären Scans

- Explizit gestartete Terminalscans speichern standardmäßig echte CU8-I/Q-Bursts samt unsicheren FSK-Bitentscheidungen; `--iq` ist kompatibel, `--no-iq` deaktiviert die Aufnahme.
- Bei bewusst gestarteten Browser-Messungen werden auch I/Q-Burstfenster erfasst, ohne ADS-B automatisch anzuhalten.
- Dashboard aktualisiert die I/Q-Aufnahmeliste automatisch und zeigt neue Bursts sofort, ohne manuelle Aktualisierung oder erneute Auswahl; Navigationsposition bleibt bei unverändertem Bestand erhalten.
- Bereits vorhandene Sitzungen und Datenbank bleiben unverändert; die 20–300-s-CLI-Watchdoggrenzen sowie Archiv- und Speichergrenzen bleiben bestehen.
- LAN-Zugriff weiterhin nur über `192.168.178.63` und Loopback, keine zusätzliche IQ-Tokenabfrage, keine HF-Aussendung oder Replay-Funktion.


## 0.8.0 LAN-Zugriff ohne IQ-Token (192.168.178.63)

- Nach ausdrücklicher Freischaltung zwei IP-gebundene Weblistener `127.0.0.1:8890` und `192.168.178.63:8890`; niemals `0.0.0.0`.
- Zugriff ohne separate IQ-Tokenabfrage; HTTP-GETs/POSTs werden auf Quellnetz `192.168.178.0/24` und Loopback sowie gültigen Host-Header beschränkt.
- Schreibaktionen behalten CSRF-Token; keine Änderung am RTL-SDR-/ADS-B-Watchdog.
- Einmaliges sicheres Einrichten mittels `scripts/pi/enable-lan-dashboard.sh` mit Dienst-Rollback bei Fehlern.
- **HTTP im Heimnetz ohne Verschlüsselung:** Alle vertrauenswürdigen LAN-Clients können IQ/Bits lesen, keine Router-Portweiterleitung vorsehen.


## 0.8.0 – Opt-in-Erfassung echter I/Q-Burst-Signale und FSK-Bit-Hypothesen (2026-10-17)

- `scan --iq` aktiviert lokal und ausdrücklich begrenzte CU8-I/Q-Aufzeichnung real empfangener Burst-Signale; normale Scans speichern weiterhin nur Metadaten.
- Phasendiskriminator für vorläufige 2.000-Bd-FSK-Bitentscheidungen (0/1/?) mit Symbolkonfidenz und unbekannter Protokollgültigkeit.
- Private Dateiablage mit 0600/0700, maximal 48 Aufnahmen je Scan, Archivlimit 64 MiB / 128 Dateien und 128 MiB freie Speicherreserve.
- Token-geschützte, nur auf Loopback erreichbare IQ-API; Darstellung der vollständigen Samplefolge in seitenweisen I/Q-, Konstellations- und FFT-Grafiken.
- Weiterhin keine TX-/Replay-Funktion und keine Erweiterung der ADS-B-Root-Berechtigungen oder Scanlaufzeit.


## 0.7.0 – Detaillierte Analyse einzelner Scans (2026-10-17)

- Neue schreibgeschützte API `GET /api/session-detail?id=N&bucket=5|10` für reine Ereignis-Metadaten aus einer ausgewählten Sitzung.
- Fünf- oder zehnsekündiger Burst-/Paarverlauf mit durchschnittlicher SNR und Burstdauer je Zeitfenster.
- Grob gruppierte SNR- und Burstdauerverteilungen statt Einzelereignis-Timestamps, IQ-Rohdaten oder Symbolfolgen.
- Maximale Auswertezeit 360 Sekunden, maximal 10.000 zugeordnete Ereignisse; zusätzliche Daten werden als `truncated` gekennzeichnet.
- Alte v0.5-Sitzungen ohne Ereignis-ID-Zuordnung bleiben erhalten, werden aber explizit als nicht rekonstruierbar ausgewiesen.
- Keine Änderung am exklusiven SDR-Zugriff, ADS-B-Dienst, sudo-Flow oder Root-Watchdog.


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
