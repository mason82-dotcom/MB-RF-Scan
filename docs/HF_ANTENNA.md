# HF-Antennenanschluss – MB-RF-Scan (CC1101 und RTL-SDR)

**Stand:** 20.11.2026, GitHub-`main` auf v0.10.0 (`020efde5`). Dieses Dokument beschreibt den **RF-Anschlusspfad** und trennt Hardware-Fähigkeit von tatsächlich implementierter Firmware. Es aktiviert **keinen Sender**.

## Eindeutige Begriffe

| Baugruppe | HF-Anschluss | Gegenwärtiger Betrieb | Bedeutung |
|---|---|---|---|
| Raspberry Pi 5 | **Kein** HF-Antennenanschluss | SPI1/CE0 zum CC1101 | GPIO/SPI führen digitale Signale, **keine HF-Antennensignale** |
| RTL2832U/R820T (RTL-SDR) | Antenneneingang, je nach Stick/Bauform anderer Steckverbinder | **Nur Empfang** | Das MB-RF-Scan-Projekt erzeugt über den RTL-SDR keine HF-Ausgabe |
| CC1101-Transceiver-IC | `RF_P` (IC-Pin 12), `RF_N` (IC-Pin 13), differenziell | **Nur Empfang** | Intern ist der HF-Pfad für RX/TX gemeinsam; **nicht direkt** an eine 50-Ω-Antenne anschließen |
| CC1101-Trägermodul | Antennenport **nach** dem bestückten 433-MHz-Balun-/Matching-/Filter-Netzwerk | **Empfang**, derzeit kein TX | Bei einem passenden Modul ist dies der *gemeinsame* 50-Ω-HF-Port; tatsächliche Bauform (SMA/IPEX/Lötpad) und Bestückung müssen am **konkreten Modul** geprüft werden |

**Signalpfad (funktional, kein Platinen-Schaltplan):**

```text
Raspberry Pi 5
  SPI1/CE0 (3,3-V-Digitalpegel)
       |
       v
CC1101-Trägermodul
  CC1101 RF_P / RF_N (differenzieller HF-Pfad)
       |
       v
  433-MHz-Balun + Anpass-/Filterglieder (auf dem Modul)
       |
       v
  Gemeinsamer 50-Ω-RF-Port (RX/TX-Hardware-Potenzial)
       |
       +---- 433-MHz-Empfangsantenne (aktueller RX-Betrieb)
```

**Nicht** GPIO18/19/20/21 oder GDO0/GDO2 als Antennenausgang verwenden. GDO0 (`GPIO25` am Pi) ist in v0.10.0 ein **digitales Carrier-Sense-Signal**, keine HF. Ein mit `ANT` beschriftetes Lötpad ist nur dann der korrekte Port, wenn die Boarddokumentation den Zusammenhang mit dem angepassten RF-Pfad bestätigt.

## Iststand aus dem Repository vs. offen

- **Belegt durch Code:** CC1101 auf SPI1/CE0; RX auf ca. 433,9155 MHz; 1 kHz RSSI; `SafeCC1101Bus` sperrt `STX`, `SFSTXON`, TX-FIFO und `PATABLE != 0x00`. Kein TX-Backend, kein Replay.
- **Laut Hardwareprotokoll vom 19.11.2026:** plausibler Chip (`PARTNUM=0x00`, `VERSION=0x14`), konfigurierter Empfang und Rauschreferenz ca. −94 dBm auf dem Pi geprüft. Dies bestätigt **keine Antennenanpassung**.
- **Nicht verifiziert:** exaktes CC1101-Trägermodell, RF-Buchsentyp, vorhandenes 433-MHz-Matching, Anschlussqualität/Antenne/Stehwellenverhältnis, tatsächliche Empfangsreichweite sowie Burst-/Paarwerte mit dem realen Handsender.
- **Keine Annahme:** 50 Ω ist die **HF-Nennimpedanz**, nicht ein DC-Widerstand, der zwingend mit einem Multimeter angezeigt werden muss.

## Technische Prüfung des Empfangsanschlusses (ohne Aussendung)

1. Pi ausschalten und von der Versorgung trennen. Aufdrucke und Fotos **beider Modulseiten** prüfen: exakter CC1101-Modultyp, 433-MHz-Variante, vorhandenes Matching, Lage und Typ des `ANT`-Ports. Keine willkürliche Brücke zwischen IC-Pins und Antenne setzen.
2. Eine für ca. **433,92 MHz** geeignete Antenne bzw. definiertes, zum Modul passendes 50-Ω-Antennensystem am ausgewiesenen HF-Port anschließen. Mechanik/Steckverbindertyp am Modul feststellen, nichts unter Spannung umstecken. Nicht von der Geometrie eines 868/915-MHz-Moduls ausgehen. Als Größenordnung beträgt `c/(4f)` bei 433,92 MHz ca. **17,3 cm** in Luft; eine reale Viertelwellenantenne braucht geeignete Gegengewichts-/Masseverhältnisse und Abstimmung.
3. Prüfen, ob SPI1/`/dev/spidev1.0` verfügbar ist; CC1101-Empfang bewusst starten. Die Raspberry-Pi-RGB-Nutzung auf SPI0 nicht ändern; ADS-B bleibt ungestört.
4. Im Dashboard **CC1101-Empfang** Rauschpegel, Abtastrate, Lücken und erkannte Bursts ansehen. Bei einem ausdrücklich autorisierten Test des eigenen Handsenders die Messwerte mit den bisherigen Näherungen (ca. 175 ms / 513 ms) vergleichen. **Keine** Bit-/Schlüsselcodes übertragen oder daraus Sendekommandos ableiten.
5. Für eine belastbare Aussage zur Anpassung und zum HF-Pfad ein geeignetes VNA-/HF-Messgerät und das genaue Modul-/Antennendatenblatt heranziehen. Ein plausibler SPI-Test oder Empfangspegel ist **kein** S11-/SWR-Nachweis.

## Falls ausdrücklich ein HF-Sendeausgang benötigt wird

Das ist ein **separater Entwicklungsauftrag**, kein Schalter an diesem Release. Der CC1101 kann als IC grundsätzlich senden, aber der vorhandene Programmcode ist bewusst **RX-only** und verhindert TX bereits am SPI-Transfer. Ein Anschluss an den 50-Ω-Port **aktiviert nichts**.

Vor jeder TX-Entwicklung erforderlich: zulässiger Anwendungsfall und Testsignal, genaue Modulrevision inklusive Filter/Anpassung, definierte Ausgangslast bzw. abgeschirmter HF-Prüfplatz, geeignete Messtechnik und überprüfte nationale Frequenzbedingungen. Sendeleistung am Chip und erlaubte abgestrahlte Leistung (ERP/EIRP) sind nicht identisch. **Keine Tests mit realen Fahrzeug-Schlüsseltelegrammen, keine Replay-Funktion und keine ungeprüfte Abstrahlung.** Keine Änderungen an `SafeCC1101Bus` oder TX-Konfiguration ohne eigenständiges Review.

## Quellen und weitere Projektdokumente

- TI CC1101, Datenblatt SWRS061I, Abbildung 10 (315/433-MHz-Balun und 50-Ω-Antennenanschluss): https://www.ti.com/lit/ds/symlink/cc1101.pdf
- Bundesnetzagentur, Allgemeinzuteilungen (aktuelle nationale Bedingungen vor jedem Sendebetrieb prüfen): https://www.bundesnetzagentur.de/Allgemeinzuteilungen
- [CC1101-Anschluss/SPI und Empfang](CC1101.md)
- [ADR-0001: RX-only-Entscheidung](adr/0001-cc1101-rx-only.md)
