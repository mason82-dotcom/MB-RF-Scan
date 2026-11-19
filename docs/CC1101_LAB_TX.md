# CC1101: leitungsgebundener HF-Laborsender (ein Testpaket)

**Status:** Entwicklungsstand, nicht am realen TX-Ausgang abgenommen. Die bestehende
RX-only-Schnittstelle SafeCC1101Bus bleibt unverändert. Es gibt keine
Fahrzeugsteuerung und keine Wiedergabe empfangener Funktelegramme.

## Hardwareanschluss

Der Raspberry Pi 5 nutzt SPI1/CE0 bei 3,3 V (Gerät /dev/spidev1.0).
Die vollständige Pinbelegung steht in [CC1101.md](CC1101.md); SPI0
ist für die Pironman-5-LEDs reserviert. GDO0, GDO1 und GDO2 werden im
Labor-TX-Modus hochohmig geschaltet.

**Chip-RF_P und Chip-RF_N sind kein direkter 50-Ohm-Antennenausgang.**
Nur den fertig angepassten 50-Ohm-HF-Anschluss einer ausdrücklich für
433 MHz bestückten CC1101-Trägerplatine verwenden (SMA/U.FL und
HF-Matching). 868/915-MHz-Trägerplatinen sind nicht automatisch geeignet.

Leitungsgebundener, abgeschirmter Aufbau:

    Raspberry Pi 5 (3,3 V / SPI1)
            |
        CC1101 433-MHz-Modul
            |
       HF-SMA / 50 Ohm
            |
       20-30 dB Festdaempfung
            |
    50-Ohm-Messgeraet oder 50-Ohm-Abschluss

- **Keine Antenne, kein offener HF-Anschluss und kein freier Sendebetrieb.**
- Koaxialleitung, Schirmung, 50-Ohm-Abschluss und bei Bedarf DC-Block
  prüfen; das Messgerät muss die verbleibende Leistung vertragen.
- Den Pi vor Umverdrahtung stromlos machen; niemals 5 V an den CC1101.
- Der 26-MHz-Quarz und das 433-MHz-Matching müssen zur Registertabelle
  passen. Die tatsächliche RF-Leistung und Oberwellen sind nicht gemessen.
- Software kann einen fehlenden Abschlusswiderstand **nicht** erkennen.

## Fest hinterlegte Labor-Parameter

| Größe | Wert |
|---|---|
| Mittenfrequenz | 433,400 MHz; Rasterwert ca. 433,400116 MHz |
| Modulation | GFSK im festen Paketmodus |
| Datenrate | ca. 9,596 kBd |
| Modulationshub | ca. ±20,6 kHz |
| Syncwort | 0xA65C, 16 Bit |
| Inhalt | MBRF-LAB-ONLY-01 (16 öffentliche ASCII-Bytes) |
| Präambel | 4 Bytes |
| CRC | aktiviert |
| Sendevorgänge | genau einer je interaktivem Aufruf |
| Erwartete Paketdauer | rund 20 ms |
| PATABLE | 0x12, am TI-Referenzdesign nominal −30 dBm |
| TX-Abschluss | IDLE, TX-FIFO leeren, PA=0, SPWD |
| TX-Zeitlimit | 250 ms |

Diese Daten sind feste Treiberkonstanten. Es existieren keine Optionen
für eigene Nutzdaten, Frequenzen, Sendeleistung, Wiederholungen oder
Dauerträger. Die Frequenz ist absichtlich von den aufgezeichneten
433,920-MHz-Fahrzeugmessungen getrennt.

## Bedienung (standardmäßig ohne Sendung)

Die Vorschau berührt keine SPI-Hardware:

~~~bash
cd ~/Projekte/MB-RF-Scan
/usr/bin/python3 -m keyrfmonitor cc1101 lab-tx
~~~

**Erst nach überprüftem, abgeschirmtem 50-Ohm-Messaufbau:**

~~~bash
/usr/bin/python3 -m keyrfmonitor cc1101 lab-tx --execute --confirm-conducted
~~~

Zusätzlich ist ein interaktives Terminal mit zweiter, wörtlich
einzugebender Freigabe erforderlich: SEND 1 LAB PACKET.

Es wird nur ein Paket ausgelöst. Ohne die Schalter, ohne TTY oder bei
falscher Freigabe wird kein TX-Befehl ausgeführt. Nicht in Cron, systemd
oder das Web-Dashboard einbauen; ein Neustart löst niemals einen Sendetest aus.

## Trennung und Fehlerschutz

1. Die bewährte RX-only-Whitelist verbietet weiterhin STX, SFTX, TXFIFO
   und PA-Leistung ungleich null. Sie wird nicht erweitert.
2. Die separate TX-Klasse enthält nur exakt geprüfte Registerwerte und
   einen einzigen, unveränderlichen Testinhalt.
3. Der gemeinsame Dateilock auf /dev/spidev1.0 verhindert konkurrierende
   CC1101-RX-, Probe- und TX-Prozesse. ADS-B und RTL-SDR werden nicht verändert.
4. Vor STX müssen Chip-ID und alle geschriebenen Register plausibel sein.
5. Der Chip wird in festen Packet-Mode mit TXOFF_MODE=IDLE versetzt,
   sodass ein intakter CC1101 nach dem Paket selbsttätig stoppt.
6. Der Treiber erkennt Unterlauf und einen nicht beobachteten TX->IDLE-
   Übergang. Im finally-Pfad versucht er SIDLE, SFTX, PA=0 und SPWD.
   Abschaltfehler werden angezeigt.
7. Software-Timeout und Paketmodus garantieren **keine** Abschaltung
   bei defektem Chip, einem blockierenden SPI-Treiber oder Stromausfall.
   Abschirmung, Abschlusslast und gegebenenfalls eine physisch
   trennbare Versorgung sind deshalb zusätzliche Schutzebenen.

## Rechtlicher Rahmen

Deutschland: [Bundesnetzagentur Vfg. 91/2025](https://www.bundesnetzagentur.de/DE/Fachthemen/Telekommunikation/Frequenzen/Allgemeinzuteilungen/_DL/vfg91_2025.pdf?__blob=publicationFile&v=3).
Das SRD-Band 433,05–434,79 MHz hat Nutzungsbedingungen insbesondere zu
ERP und teilweise Duty-Cycle. Nationale Sonderregelungen finden sich in
Tabelle 3. Die bloße Lage innerhalb des Bandes belegt **keine**
Rechtskonformität. ERP, Spektrum, Bandbreite, Oberwellen, Störungen und
Konformität der konkreten Funkanlage sind vor freiem Sendebetrieb zu prüfen.

Chip-Referenz: [Texas Instruments CC1101, Datenblatt](https://www.ti.com/lit/ds/symlink/cc1101.pdf).

## Testmatrix: erst Offline-CI, danach leitungsgebundene HF-Abnahme

| Test | Erwartung | Aktueller Hardwarestatus |
|---|---|---|
| P01 Unittests mit Fake-SPI | keine HF-Aussendung, Tests grün | UNBEKANNT |
| P02 Plan ohne --execute | JSON ohne SPI-Zugriff | UNBEKANNT |
| P03 Fehlende Freigabe/TTY | TX verweigert | UNBEKANNT |
| P04 Falsche Freigabephrase | TX verweigert | UNBEKANNT |
| P05 Parallel laufender CC1101-RX | Verriegelung, kein STX | UNBEKANNT |
| P06 Falsche Chip-ID / Readback | kein STX, PA=0 | UNBEKANNT |
| H01 Module/Quarz/Matching prüfen | 433 MHz, 26 MHz, 3,3 V | UNBEKANNT |
| H02 HF-Last und Abschirmung prüfen | 50 Ohm, definierte Dämpfung, keine Antenne | UNBEKANNT |
| H03 Messung in Zero-Span/Max-Hold | ein Paket 433,400 MHz, kein Replay | UNBEKANNT |
| H04 HF-Leistung/Bandbreite/Oberwellen | protokollierte Messwerte, keine Daueremission | UNBEKANNT |
| H05 IDLE- und Abschaltprüfung | danach kein Träger, PA=0, SPWD | UNBEKANNT |
| H06 Regression RX/ADS-B | RX wieder startbar, dump1090 unverändert | UNBEKANNT |
| H07 Fehler/Timeout unter Messgerät | Abschaltpfad verifiziert | UNBEKANNT |

**Ein grüner Fake-SPI-Test ersetzt keine echte HF-Hardwareabnahme.**
