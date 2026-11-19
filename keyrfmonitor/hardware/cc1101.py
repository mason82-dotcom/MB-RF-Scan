"""CC1101 hardware *preparation*: Raspberry Pi pinout and non-transmitting SPI probe.

This module deliberately does NOT contain CC1101 configuration writes, command
strobes, FIFO operations, packet transmission, or received payload handling.
The probe only reads the PARTNUM and VERSION status registers. No RF mode is set.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Callable


# Default wiring: SPI1 / CE0. On this Pi, SPI0 (/dev/spidev0.0, GPIO10 MOSI)
# drives the Pironman 5 RGB LEDs; a CC1101 on SPI0/CE0 would see every LED
# update as SPI traffic with its chip select asserted.
DEFAULT_SPI_BUS = 1
DEFAULT_SPI_DEVICE = 0
SPI1_OVERLAY = "dtoverlay=spi1-1cs"
PIRONMAN5_CONFIG = Path("/opt/pironman5/config.json")


@dataclass(frozen=True)
class Wiring:
    """Raspberry Pi 40-pin header <-> 3.3V CC1101 module, default SPI1 CE0."""

    vcc: str = "3V3: Pin 17 (oder 1) -> CC1101 VCC (niemals 5 V)"
    gnd: str = "GND: Pin 39 (oder 6/9/14) -> CC1101 GND"
    sclk: str = "GPIO21/SPI1 SCLK: Pin 40 -> CC1101 SCLK"
    mosi: str = "GPIO20/SPI1 MOSI: Pin 38 -> CC1101 SI/MOSI"
    miso: str = "GPIO19/SPI1 MISO: Pin 35 -> CC1101 SO/MISO"
    csn: str = "GPIO18/SPI1 CE0: Pin 12 -> CC1101 CSn"
    gdo0: str = "GPIO25: Pin 22 -> CC1101 GDO0 (optional, spaeter)"
    gdo2: str = "GPIO24: Pin 18 -> CC1101 GDO2 (optional, spaeter)"
    overlay: str = f"/boot/firmware/config.txt: {SPI1_OVERLAY} -> /dev/spidev1.0"


def wiring_lines() -> tuple[str, ...]:
    return tuple(asdict(Wiring()).values())


def spi_device_path(bus: int, device: int) -> Path:
    return Path(f"/dev/spidev{bus}.{device}")


def spi0_used_by_pironman(config_path: Path = PIRONMAN5_CONFIG) -> bool:
    """True if the Pironman 5 service drives its WS2812 LEDs over SPI0."""
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    # The setting is stored either top-level or under "system".
    for section in (data, data.get("system")):
        if isinstance(section, dict) and section.get("rgb_enable") is True:
            return True
    return False


class CC1101ProbeError(RuntimeError):
    """No hardware probe result available (never guess that a device is present)."""


def _valid_address(bus: int, device: int, speed_hz: int) -> None:
    if type(bus) is not int or bus < 0 or bus > 9:
        raise ValueError("SPI-Bus muss eine ganze Zahl zwischen 0 und 9 sein")
    if type(device) is not int or device not in (0, 1):
        raise ValueError("SPI-Geraet muss CE0 (0) oder CE1 (1) sein")
    if type(speed_hz) is not int or not 10_000 <= speed_hz <= 1_000_000:
        raise ValueError("SPI-Takt fuer Diagnose muss 10 kHz bis 1 MHz betragen")


def probe(bus: int = DEFAULT_SPI_BUS, device: int = DEFAULT_SPI_DEVICE,
          speed_hz: int = 500_000,
          spi_factory: Callable | None = None) -> dict:
    """Read two CC1101 status registers; NEVER change radio state or transmit.

    CC1101 status registers require the SPI READ and BURST flags, even for a
    single-register read: PARTNUM (0x30) => 0xF0, VERSION (0x31) => 0xF1.
    The dummy second byte clocks out the value. Chip-select is handled by SPI.

    A plausible SPI answer does not constitute successful RF reception, and an
    unknown VERSION does not warrant identifying the connected chip as CC1101.
    """
    _valid_address(bus, device, speed_hz)
    if spi_factory is None:
        node = spi_device_path(bus, device)
        if not node.exists():
            hint = (f" SPI1 aktivieren: '{SPI1_OVERLAY}' in /boot/firmware/config.txt, dann neu starten."
                    if bus == 1 else "")
            raise CC1101ProbeError(f"{node} fehlt.{hint}")
        try:
            import spidev  # Optional system dependency, import only for explicit probe.
        except ImportError as exc:
            raise CC1101ProbeError(
                "python3-spidev fehlt im System-Python. Installieren: sudo apt install python3-spidev"
            ) from exc
        spi_factory = spidev.SpiDev

    spi = None
    try:
        spi = spi_factory()
        spi.open(bus, device)
        spi.mode = 0
        spi.max_speed_hz = speed_hz
        spi.bits_per_word = 8
        part = spi.xfer2([0xF0, 0x00])
        version = spi.xfer2([0xF1, 0x00])
        if (len(part) != 2 or len(version) != 2 or
                any(type(x) is not int or not 0 <= x <= 255 for x in (part[1], version[1]))):
            raise CC1101ProbeError("Ungueltige Antwort vom SPI-Controller")
    except (OSError, IOError) as exc:
        raise CC1101ProbeError(
            f"SPI-Zugriff fehlgeschlagen: {exc}. SPI, Verkabelung und Benutzerrechte pruefen."
        ) from exc
    finally:
        if spi is not None:
            spi.close()

    partnum, ver = part[1], version[1]
    plausible = partnum == 0x00 and ver not in (0x00, 0xFF)
    return {
        "spi_bus": bus,
        "spi_device": device,
        "partnum": f"0x{partnum:02X}",
        "version": f"0x{ver:02X}",
        "cc1101_plausible": plausible,
        "result": ("Plausible CC1101-Registerantwort; RF-Funktion noch ungeprueft"
                   if plausible else "Keine belastbare CC1101-Erkennung (Verdrahtung/Modul pruefen)"),
        "probe_mode": "SPI-Statusregister lesen, keine Funk-Konfigurationsbefehle",
        "rx_implemented": False,
        "tx_implemented": False,
    }
