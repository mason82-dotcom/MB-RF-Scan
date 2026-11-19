"""One-shot conducted RF lab beacon for CC1101, separate from the RX-only bus.

SECURITY BOUNDARY: This module never reads recordings or accepts a caller-defined
frequency, modulation, payload, power or repetition count.  No web endpoints.
The legacy SafeCC1101Bus remains RX-only.  See docs/CC1101_LAB_TX.md.

A software guard cannot detect an antenna vs. a 50-ohm termination.  TX must be
performed only in an RF-shielded or adequately attenuated conducted test setup.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from .cc1101 import DEFAULT_SPI_BUS, DEFAULT_SPI_DEVICE
from .cc1101_bus import CC1101Busy, cc1101_device_lock, spi_device_path

# Intentionally immutable: not Mercedes 433.920 MHz and not its measured timing.
FREQUENCY_HZ = 433_400_000
FXOSC_HZ = 26_000_000
FREQ_WORD = round(FREQUENCY_HZ * 65536 / FXOSC_HZ)
PAYLOAD = b"MBRF-LAB-ONLY-01"          # Fixed, public, synthetic, non-automotive.
SYNC_WORD = (0xA6, 0x5C)             # Dedicated laboratory sync, not learned.
PA_BYTE = 0x12                       # TI CC1101 reference, approx. -30 dBm.
DATA_RATE_BPS = (256 + 0x83) * 2**8 * FXOSC_HZ / 2**28
MAX_TX_WAIT_S = 0.25                 # Hardware must return to IDLE after packet.
SPI_HZ = 500_000
BUS, DEVICE = DEFAULT_SPI_BUS, DEFAULT_SPI_DEVICE

# Registers are a single audited fixed configuration, not caller-supplied.
# GDO0/GDO1/GDO2 high-Z; packet mode, fixed length, CRC, no address filtering,
# GFSK, approx 9.6 kBd and 20.6 kHz frequency deviation at 26 MHz XOSC.
TX_REGISTERS = (
    (0x00, 0x2E), (0x01, 0x2E), (0x02, 0x2E),
    (0x03, 0x47), (0x04, SYNC_WORD[0]), (0x05, SYNC_WORD[1]),
    (0x06, len(PAYLOAD)), (0x07, 0x00), (0x08, 0x04),
    (0x0A, 0x00), (0x0B, 0x06), (0x0C, 0x00),
    (0x0D, (FREQ_WORD >> 16) & 0xFF),
    (0x0E, (FREQ_WORD >> 8) & 0xFF), (0x0F, FREQ_WORD & 0xFF),
    (0x10, 0xC8), (0x11, 0x83), (0x12, 0x12),
    (0x13, 0x22), (0x14, 0xF8), (0x15, 0x35),
    (0x16, 0x07), (0x17, 0x00), (0x18, 0x18),
    (0x19, 0x16), (0x1A, 0x6C), (0x1B, 0x43),
    (0x1C, 0x40), (0x1D, 0x91), (0x21, 0x56),
    (0x22, 0x10), (0x23, 0xE9), (0x24, 0x2A),
    (0x25, 0x00), (0x26, 0x1F),
    (0x2C, 0x81), (0x2D, 0x35), (0x2E, 0x09),
)
TX_REGISTER_MAP = dict(TX_REGISTERS)
assert len(TX_REGISTER_MAP) == len(TX_REGISTERS)
assert BUS == 1 and DEVICE == 0

SRES, SCAL, STX, SIDLE, SFTX, SPWD, SNOP = 0x30, 0x33, 0x35, 0x36, 0x3B, 0x39, 0x3D
PARTNUM, VERSION, MARCSTATE = 0x30, 0x31, 0x35
STATE_IDLE, STATE_TX, STATE_UNDERFLOW = 0x01, 0x13, 0x16


class LabTxError(RuntimeError):
    """Fail-closed conducted-test error, including incomplete cleanup."""


def plan() -> dict:
    """No imports of hardware dependencies, SPI access, or device checks."""
    duration_ms = (4 + 2 + len(PAYLOAD) + 2) * 8 / DATA_RATE_BPS * 1000
    return {
        "mode": "CONDUCTED_LAB_ONLY",
        "hardware_tx_verified": False,
        "frequency_hz": FREQUENCY_HZ,
        "modulation": "GFSK",
        "data_rate_bps_approx": round(DATA_RATE_BPS),
        "fixed_payload_ascii": PAYLOAD.decode("ascii"),
        "payload_bytes": len(PAYLOAD),
        "nominal_pa_dbm_reference": -30,
        "pa_byte": f"0x{PA_BYTE:02X}",
        "expected_packet_airtime_ms_approx": round(duration_ms, 1),
        "repetition_count": 1,
        "rf_load": "50-ohm load + attenuation / shielded conducted setup",
        "no_replay": True,
        "no_web_tx": True,
        "rx_bus_unchanged": True,
        "warning": "No actual emissions without --execute AND interactive confirmation.",
    }


class _FixedPacketBus:
    """Very narrow SPI interface: only fixed register writes and one fixed FIFO.

    Deliberately NOT a subclass of SafeCC1101Bus, whose RX-only allowlist
    continues to reject TX instructions.  No raw SPI method is exposed.
    """

    def __init__(self, spi_factory: Callable):
        self._spi = spi_factory()
        self._spi.open(BUS, DEVICE)
        self._spi.mode = 0
        self._spi.bits_per_word = 8
        self._spi.max_speed_hz = SPI_HZ
        self._sent = False

    def _xfer(self, raw: list[int]) -> list[int]:
        reply = self._spi.xfer2(raw)
        if not isinstance(reply, list) or len(reply) != len(raw):
            raise LabTxError("SPI-Rueckgabe ungueltig")
        if any(type(x) is not int or not 0 <= x <= 255 for x in reply):
            raise LabTxError("SPI-Rueckgabe enthaelt ungueltige Bytes")
        return reply

    def _strobe(self, command: int) -> None:
        if command not in (SRES, SCAL, STX, SIDLE, SFTX, SPWD, SNOP):
            raise LabTxError("Nicht freigegebener Strobe")
        if command == STX:
            if self._sent:
                raise LabTxError("Mehr als ein Paket pro Lauf verboten")
            self._sent = True          # Set BEFORE touching the SPI hardware.
        self._xfer([command])

    def _write(self, address: int, value: int) -> None:
        if address == 0x3E:
            if value not in (PA_BYTE, 0x00):
                raise LabTxError("Nicht freigegebene PA-Leistung")
        elif TX_REGISTER_MAP.get(address) != value:
            raise LabTxError("Nicht freigegebene Registerkonfiguration")
        self._xfer([address, value])

    def _read_reg(self, address: int) -> int:
        if address not in TX_REGISTER_MAP:
            raise LabTxError("Registerlesezugriff nicht freigegeben")
        return self._xfer([0x80 | address, 0x00])[1]

    def _read_status(self, address: int) -> int:
        if address not in (PARTNUM, VERSION, MARCSTATE):
            raise LabTxError("Statusregister nicht freigegeben")
        return self._xfer([0xC0 | address, 0x00])[1]

    def _load_fixed_fifo(self) -> None:
        if self._sent:
            raise LabTxError("TX-Start bereits erfolgt")
        # Only this hard-coded 16-byte payload ever reaches TX FIFO.
        self._xfer([0x7F, *PAYLOAD])

    def close(self) -> None:
        self._spi.close()

    def shutdown(self) -> list[str]:
        """Always force IDLE, flush FIFO, disarm PA and power down."""
        errors: list[str] = []
        for name, action in (
            ("SIDLE", lambda: self._strobe(SIDLE)),
            ("SFTX", lambda: self._strobe(SFTX)),
            ("PA=0", lambda: self._write(0x3E, 0x00)),
            ("SPWD", lambda: self._strobe(SPWD)),
        ):
            try:
                action()
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        try:
            self.close()
        except Exception as exc:
            errors.append(f"SPI close: {exc}")
        return errors


def run_one_shot(*, confirmed: bool,
                 spi_factory: Callable | None = None,
                 device_path: Path | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic) -> dict:
    """Sends at most one fixed packet, only after CLI confirms the RF load.

    The CLI additionally requires a TTY and a human typed one-shot phrase.
    A fake SPI factory can be provided only for hardware-free tests.
    """
    if confirmed is not True:
        raise LabTxError("Leitungsgebundenes 50-Ohm-Labor-Setup nicht bestaetigt")
    path = device_path if device_path is not None else spi_device_path(BUS, DEVICE)
    if not path.exists():
        raise LabTxError(f"{path} nicht vorhanden")
    if spi_factory is None:
        try:
            import spidev
        except ImportError as exc:
            raise LabTxError("python3-spidev fehlt") from exc
        spi_factory = spidev.SpiDev

    try:
        with cc1101_device_lock(path):
            bus = _FixedPacketBus(spi_factory)
            try:
                bus._strobe(SNOP)                 # Wake from SPWD.
                sleep(0.002)
                bus._strobe(SRES)
                sleep(0.002)
                if (bus._read_status(PARTNUM) != 0x00 or
                        bus._read_status(VERSION) in (0x00, 0xFF)):
                    raise LabTxError("CC1101-ID nicht plausibel")
                for address, value in TX_REGISTERS:
                    bus._write(address, value)
                bad = [f"0x{a:02X}" for a, v in TX_REGISTERS
                       if bus._read_reg(a) != v]
                if bad:
                    raise LabTxError("Register-Readback fehlgeschlagen: " + ", ".join(bad))
                bus._strobe(SIDLE)
                bus._strobe(SFTX)
                bus._write(0x3E, PA_BYTE)
                bus._strobe(SCAL)
                sleep(0.003)
                if (bus._read_status(MARCSTATE) & 0x1F) != STATE_IDLE:
                    raise LabTxError("CC1101 nicht in IDLE nach Kalibrierung")
                bus._load_fixed_fifo()
                bus._strobe(STX)
                start = monotonic()
                saw_tx = False
                while monotonic() - start < MAX_TX_WAIT_S:
                    state = bus._read_status(MARCSTATE) & 0x1F
                    if state == STATE_TX:
                        saw_tx = True
                    elif state == STATE_IDLE and saw_tx:
                        break
                    elif state == STATE_UNDERFLOW:
                        raise LabTxError("CC1101 TXFIFO-Unterlauf")
                    sleep(0.002)
                else:
                    raise LabTxError("TX nicht als abgeschlossen beobachtet (Timeout)")
            finally:
                problems = bus.shutdown()
                if problems:
                    raise LabTxError("Sichere Abschaltung unvollstaendig: " + "; ".join(problems))
    except CC1101Busy as exc:
        raise LabTxError("CC1101 durch RX oder Probe belegt") from exc

    return {
        "result": "one_shot_completed_in_chip_state_machine",
        "note": "Kein Nachweis einer normkonformen oder gemessenen HF-Aussendung",
        "frequency_hz": FREQUENCY_HZ,
        "packets_requested": 1,
        "tx_disabled_after": True,
    }
