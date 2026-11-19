"""Receive-only SPI safety layer for the CC1101 (see docs/adr/0001-cc1101-rx-only.md).

Every byte sent to the chip passes ``check_payload`` BEFORE it reaches spidev.
The allowlist permits exactly what a passive RSSI receiver needs:

* write single configuration registers 0x00..0x2E (with value guards),
* write PATABLE (0x3E) only with 0x00 (no output power),
* read single configuration registers and the status registers PARTNUM,
  VERSION, RSSI and MARCSTATE,
* the strobes SRES, SCAL, SRX, SIDLE, SPWD, SFRX and SNOP.

Everything else is rejected, in particular STX (0x35), SFSTXON (0x31), any
write to the TX FIFO (0x3F), any RX FIFO read (payload data), PKTSTATUS
(mirrors the GDO pin levels) and every GDO configuration except an explicit
allowlist without data or symbol-clock outputs.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
from typing import Callable, Iterator

READ = 0x80
BURST = 0x40

# Command strobes (write access, address 0x30..0x3D).
SRES, SFSTXON, SXOFF, SCAL, SRX, STX, SIDLE = 0x30, 0x31, 0x32, 0x33, 0x34, 0x35, 0x36
SWOR, SPWD, SFRX, SFTX, SWORRST, SNOP = 0x38, 0x39, 0x3A, 0x3B, 0x3C, 0x3D
ALLOWED_STROBES = frozenset({SRES, SCAL, SRX, SIDLE, SPWD, SFRX, SNOP})

# Status registers (read access with BURST bit set).
PARTNUM, VERSION, FREQEST, LQI, RSSI, MARCSTATE = 0x30, 0x31, 0x32, 0x33, 0x34, 0x35
PKTSTATUS = 0x38
STATUS_REGISTERS = frozenset(range(0x30, 0x3E))
READABLE_STATUS = frozenset({PARTNUM, VERSION, RSSI, MARCSTATE})

PATABLE = 0x3E
FIFO = 0x3F
LAST_CONFIG_REGISTER = 0x2E

IOCFG2, IOCFG1, IOCFG0, MCSM1 = 0x00, 0x01, 0x02, 0x17
# Allowlist (not a denylist): many GDOx_CFG values expose demodulated bits or
# their clock (0x0B-0x0D serial data/clock, 0x16/0x17 RX_HARD_DATA,
# 0x1D RX_SYMBOL_TICK, ...). Only these carry no data:
#   0x0E carrier sense, 0x29 CHIP_RDYn, 0x2E high impedance.
ALLOWED_GDO_VALUES = frozenset({0x0E, 0x29, 0x2E})

MARCSTATE_IDLE = 0x01
MARCSTATE_RX = 0x0D
MARCSTATE_RXFIFO_OVERFLOW = 0x11


class UnsafeCC1101Command(ValueError):
    """A byte sequence outside the receive-only allowlist was requested."""


def check_payload(payload: list[int]) -> None:
    """Raise UnsafeCC1101Command unless the SPI transfer is receive-only safe."""
    if not isinstance(payload, list) or not 1 <= len(payload) <= 2:
        raise UnsafeCC1101Command("Only single-byte strobes and single-register access")
    if any(type(b) is not int or not 0 <= b <= 0xFF for b in payload):
        raise UnsafeCC1101Command("SPI payload must consist of bytes")
    header = payload[0]
    read = bool(header & READ)
    burst = bool(header & BURST)
    address = header & 0x3F

    if address == FIFO:
        raise UnsafeCC1101Command("FIFO access is forbidden (no TX data, no RX payload)")

    if read:
        if len(payload) != 2 or payload[1] != 0x00:
            raise UnsafeCC1101Command("A register read clocks exactly one dummy zero byte")
        if address <= LAST_CONFIG_REGISTER and not burst:
            return
        if address in READABLE_STATUS and burst:
            return
        raise UnsafeCC1101Command(f"Read of address 0x{address:02X} not allowed")

    if address in STATUS_REGISTERS:
        # Writes to 0x30..0x3D are command strobes regardless of the burst bit.
        if len(payload) != 1:
            raise UnsafeCC1101Command("A strobe is a single byte")
        if address not in ALLOWED_STROBES:
            raise UnsafeCC1101Command(f"Strobe 0x{address:02X} is not receive-only")
        return

    if burst:
        raise UnsafeCC1101Command("Burst writes are not allowed")
    if len(payload) != 2:
        raise UnsafeCC1101Command("A register write needs exactly one value")
    value = payload[1]
    if address == PATABLE:
        if value != 0x00:
            raise UnsafeCC1101Command("PATABLE may only be set to 0x00 (no output power)")
        return
    if address > LAST_CONFIG_REGISTER:
        raise UnsafeCC1101Command(f"Write to address 0x{address:02X} not allowed")
    if address in (IOCFG2, IOCFG1, IOCFG0) and (value & 0xBF) not in ALLOWED_GDO_VALUES:
        # Bit 6 (output inversion) is harmless; bit 7 (temperature sensor /
        # drive strength) and every data or clock output are rejected.
        raise UnsafeCC1101Command("GDO pins may only output carrier sense, CHIP_RDYn or high-Z")
    if address == MCSM1 and (value >> 2) & 0x03 == 0x02:
        raise UnsafeCC1101Command("RXOFF_MODE must never switch to TX")


def spi_device_path(bus: int, device: int) -> Path:
    return Path(f"/dev/spidev{bus}.{device}")


@contextmanager
def cc1101_device_lock(lock_path: Path) -> Iterator[None]:
    """Advisory flock on the SPI device node itself.

    Locking /dev/spidevB.D (not a file in a data directory) means probe, CLI
    listener and web receiver exclude each other regardless of database
    path, HOME or user.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(lock_path, flags)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CC1101Busy("Der CC1101 wird bereits von einem anderen Prozess verwendet.") from exc
        yield
    finally:
        os.close(fd)


class CC1101Busy(RuntimeError):
    """Another process holds the CC1101 device lock."""


class SafeCC1101Bus:
    """spidev wrapper that only performs allowlisted receive-only transfers."""

    def __init__(self, bus: int, device: int, speed_hz: int = 2_000_000,
                 spi_factory: Callable | None = None):
        if type(bus) is not int or not 0 <= bus <= 9:
            raise ValueError("SPI-Bus muss 0 bis 9 sein")
        if type(device) is not int or device not in (0, 1):
            raise ValueError("SPI-Geraet muss CE0 oder CE1 sein")
        if type(speed_hz) is not int or not 10_000 <= speed_hz <= 5_000_000:
            raise ValueError("SPI-Takt 10 kHz bis 5 MHz")
        if spi_factory is None:
            import spidev  # Optional system dependency on the Pi only.
            spi_factory = spidev.SpiDev
        self._spi = spi_factory()
        self._spi.open(bus, device)
        self._spi.mode = 0
        self._spi.bits_per_word = 8
        self._spi.max_speed_hz = speed_hz
        self.transfers = 0

    def _xfer(self, payload: list[int]) -> list[int]:
        check_payload(payload)
        result = self._spi.xfer2(list(payload))
        self.transfers += 1
        if (not isinstance(result, list) or len(result) != len(payload)
                or any(type(b) is not int or not 0 <= b <= 0xFF for b in result)):
            raise OSError("Ungueltige Antwort vom SPI-Controller")
        return result

    def strobe(self, command: int) -> int:
        return self._xfer([command])[0]

    def write_register(self, address: int, value: int) -> None:
        self._xfer([address, value])

    def read_register(self, address: int) -> int:
        return self._xfer([READ | address, 0x00])[1]

    def read_status(self, address: int) -> int:
        return self._xfer([READ | BURST | address, 0x00])[1]

    def close(self) -> None:
        self._spi.close()
