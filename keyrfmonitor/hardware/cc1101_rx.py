"""Receive-only CC1101 burst monitor based on 1 kHz RSSI sampling.

Design: docs/adr/0001-cc1101-rx-only.md. The chip is configured for 2-FSK
reception in asynchronous serial mode WITHOUT any data output (GDO0 carries
carrier sense, GDO2/GDO1 are tri-stated, the FIFO is never read). Only the
RSSI status register is sampled; the resulting events contain metadata
(timing, level) and never bits, frames or key material.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import queue
from statistics import median
import threading
import time
from typing import Callable

from . import cc1101_bus as b
from .cc1101 import DEFAULT_SPI_BUS, DEFAULT_SPI_DEVICE

FXOSC_HZ = 26_000_000
RSSI_OFFSET_DB = 74  # Datasheet value at 433 MHz; levels are uncalibrated.

# 433.9156 MHz = midpoint of the measured tones (-20.12 / +11.35 kHz around
# 433.920 MHz); 2-FSK, ~2 kBd, 15.9 kHz deviation, 101.6 kHz channel filter.
RX_REGISTERS: tuple[tuple[int, int, str], ...] = (
    (0x00, 0x2E, "IOCFG2: GDO2 hochohmig"),
    (0x01, 0x2E, "IOCFG1: GDO1 hochohmig"),
    (0x02, 0x0E, "IOCFG0: GDO0 = Carrier Sense (keine Daten)"),
    (0x03, 0x47, "FIFOTHR"),
    (0x07, 0x00, "PKTCTRL1"),
    (0x08, 0x30, "PKTCTRL0: asynchroner Modus, FIFO ungenutzt"),
    (0x0B, 0x06, "FSCTRL1: ZF 152 kHz"),
    (0x0C, 0x00, "FSCTRL0"),
    (0x0D, 0x10, "FREQ2"),
    (0x0E, 0xB0, "FREQ1"),
    (0x0F, 0x66, "FREQ0: 433,9155 MHz"),
    (0x10, 0xC6, "MDMCFG4: Kanalbandbreite 101,6 kHz"),
    (0x11, 0x43, "MDMCFG3: ca. 2,0 kBd"),
    (0x12, 0x00, "MDMCFG2: 2-FSK, kein Sync-Wort"),
    (0x13, 0x22, "MDMCFG1"),
    (0x14, 0xF8, "MDMCFG0"),
    (0x15, 0x32, "DEVIATN: 15,9 kHz"),
    (0x16, 0x07, "MCSM2"),
    (0x17, 0x0C, "MCSM1: nach RX in RX bleiben"),
    (0x18, 0x18, "MCSM0: Kalibrierung beim Wechsel aus IDLE"),
    (0x19, 0x16, "FOCCFG"),
    (0x1A, 0x6C, "BSCFG"),
    (0x1B, 0x43, "AGCCTRL2"),
    (0x1C, 0x40, "AGCCTRL1"),
    (0x1D, 0x91, "AGCCTRL0"),
    (0x21, 0x56, "FREND1"),
    (0x22, 0x10, "FREND0: PA-Index 0"),
    (0x23, 0xE9, "FSCAL3"),
    (0x24, 0x2A, "FSCAL2"),
    (0x25, 0x00, "FSCAL1"),
    (0x26, 0x1F, "FSCAL0"),
    (0x2C, 0x81, "TEST2"),
    (0x2D, 0x35, "TEST1"),
    (0x2E, 0x09, "TEST0"),
)


def carrier_frequency_hz(freq2: int = 0x10, freq1: int = 0xB0, freq0: int = 0x66) -> float:
    return ((freq2 << 16) | (freq1 << 8) | freq0) * FXOSC_HZ / 2 ** 16


def rssi_to_dbm(raw: int) -> float:
    if type(raw) is not int or not 0 <= raw <= 0xFF:
        raise ValueError("RSSI must be a byte")
    signed = raw - 256 if raw >= 128 else raw
    return signed / 2 - RSSI_OFFSET_DB


@dataclass(frozen=True)
class RssiDetectorConfig:
    sample_period_ms: float = 1.0
    calibration_ms: float = 500.0
    min_rise_db: float = 10.0
    hysteresis_db: float = 3.0
    quiet_ms_to_finish: float = 15.0
    min_burst_ms: float = 80.0
    max_burst_ms: float = 350.0
    pair_start_min_s: float = 0.40
    pair_start_max_s: float = 0.65
    baseline_window: int = 2000
    baseline_update_every: int = 100


class RssiBurstDetector:
    """Burst and pair detection on an RSSI series (dBm, monotonic seconds).

    Produces the same event shape as the RTL-SDR PassiveDetector, with
    source "cc1101". peak_rise_db is the rise above the adaptive noise floor;
    peak_snr_db reports the same value since RSSI has no separate SNR.
    """

    def __init__(self, config: RssiDetectorConfig | None = None,
                 on_event: Callable[[dict], None] | None = None,
                 started_wall: float | None = None):
        self.config = config or RssiDetectorConfig()
        self.on_event = on_event
        self.started_wall = time.time() if started_wall is None else started_wall
        self.origin: float | None = None
        self.calibration: list[float] = []
        self.idle: deque[float] = deque(maxlen=self.config.baseline_window)
        self.baseline_dbm: float | None = None
        self.idle_count = 0
        self.candidate: dict | None = None
        self.quiet_since: float | None = None
        self.long_carrier = False
        self.long_carrier_count = 0
        self.last_unpaired: dict | None = None
        self.burst_count = 0
        self.pair_count = 0
        self.last_rssi_dbm: float | None = None
        self.events: deque[dict] = deque(maxlen=256)

    def _emit(self, event: dict, out: list[dict]) -> None:
        self.events.append(event)
        out.append(event)
        if self.on_event:
            self.on_event(event.copy())

    def _at(self, relative_s: float) -> str:
        return datetime.fromtimestamp(self.started_wall + relative_s, timezone.utc).isoformat()

    def process(self, t: float, rssi_dbm: float) -> list[dict]:
        out: list[dict] = []
        if self.origin is None:
            self.origin = t
        rel = t - self.origin
        self.last_rssi_dbm = rssi_dbm
        cfg = self.config
        if self.baseline_dbm is None:
            self.calibration.append(rssi_dbm)
            if rel * 1000 >= cfg.calibration_ms and len(self.calibration) >= 50:
                self.baseline_dbm = median(self.calibration)
                self.idle.extend(self.calibration)
            return out

        rise = rssi_dbm - self.baseline_dbm
        threshold = cfg.min_rise_db if self.candidate is None else cfg.min_rise_db - cfg.hysteresis_db
        if self.long_carrier:
            # A carrier longer than any burst (or a risen noise floor) is fed
            # into the noise reference so detection resumes on its own.
            if rise < cfg.min_rise_db - cfg.hysteresis_db:
                self.long_carrier = False
            self._track_idle(rssi_dbm)
            return out
        if self.candidate is not None and (rel - self.candidate["start"]) * 1000 > cfg.max_burst_ms:
            self.candidate = None
            self.quiet_since = None
            self.long_carrier = True
            self.long_carrier_count += 1
            self._track_idle(rssi_dbm)
            return out
        if rise >= threshold:
            if self.candidate is None:
                self.candidate = {"start": rel, "end": rel, "peak_rise": rise, "peak_dbm": rssi_dbm}
            c = self.candidate
            c["end"] = rel
            c["peak_rise"] = max(c["peak_rise"], rise)
            c["peak_dbm"] = max(c["peak_dbm"], rssi_dbm)
            self.quiet_since = None
            return out

        if self.candidate is not None:
            if self.quiet_since is None:
                self.quiet_since = rel
            if (rel - self.quiet_since) * 1000 >= cfg.quiet_ms_to_finish:
                self._finish(out)
            return out

        self._track_idle(rssi_dbm)
        return out

    def _track_idle(self, rssi_dbm: float) -> None:
        self.idle.append(rssi_dbm)
        self.idle_count += 1
        if self.idle_count % self.config.baseline_update_every == 0:
            self.baseline_dbm = median(self.idle)

    def interrupt(self) -> None:
        """Signal gap (e.g. RX recovery): drop a running burst, keep the pair state."""
        self.candidate = None
        self.quiet_since = None

    def _finish(self, out: list[dict]) -> None:
        c = self.candidate
        self.candidate = None
        self.quiet_since = None
        cfg = self.config
        duration_ms = (c["end"] - c["start"]) * 1000 + cfg.sample_period_ms
        if not cfg.min_burst_ms <= duration_ms <= cfg.max_burst_ms:
            return
        start, end = c["start"], c["end"] + cfg.sample_period_ms / 1000
        burst = {
            "kind": "burst", "source": "cc1101", "simulated": False,
            "at": self._at(start), "start_s": round(start, 3), "end_s": round(end, 3),
            "duration_ms": round(duration_ms, 1),
            "peak_rise_db": round(c["peak_rise"], 1),
            "peak_snr_db": round(c["peak_rise"], 1),
            "rssi_dbm": round(c["peak_dbm"], 1),
        }
        self.burst_count += 1
        self._emit(burst, out)
        prev = self.last_unpaired
        if prev is not None and cfg.pair_start_min_s <= start - prev["start_s"] <= cfg.pair_start_max_s:
            self.pair_count += 1
            self._emit({
                "kind": "pair", "source": "cc1101", "simulated": False,
                "at": burst["at"], "start_s": prev["start_s"],
                "second_start_s": burst["start_s"],
                "start_gap_ms": round((start - prev["start_s"]) * 1000, 1),
                "first_duration_ms": prev["duration_ms"],
                "second_duration_ms": burst["duration_ms"],
                "sequence": self.pair_count,
            }, out)
            self.last_unpaired = None
        else:
            self.last_unpaired = burst

    def finish(self) -> None:
        """Discard an unfinished burst instead of guessing its end."""
        self.candidate = None
        self.quiet_since = None

    def status(self) -> dict:
        return {
            "baseline_ready": self.baseline_dbm is not None,
            "baseline_dbm": None if self.baseline_dbm is None else round(self.baseline_dbm, 1),
            "last_rssi_dbm": None if self.last_rssi_dbm is None else round(self.last_rssi_dbm, 1),
            "burst_active": self.candidate is not None,
            "long_carrier": self.long_carrier,
            "long_carriers": self.long_carrier_count,
            "burst_count": self.burst_count,
            "pair_count": self.pair_count,
        }


class CC1101SetupError(RuntimeError):
    """The chip did not accept the receive-only configuration."""


def wake(bus: b.SafeCC1101Bus, sleep: Callable[[float], None] = time.sleep) -> None:
    """After SPWD the crystal needs ~150-300 µs once CSn goes low; spidev cannot
    wait for SO, so a no-op strobe wakes the chip and the first real access
    follows 2 ms later (otherwise it reads 0xFF or a strobe gets lost)."""
    bus.strobe(b.SNOP)
    sleep(0.002)


class CC1101Cancelled(RuntimeError):
    """A stop was requested while the chip was being configured."""


def configure_rx(bus: b.SafeCC1101Bus, *, sleep: Callable[[float], None] = time.sleep,
                 cancelled: Callable[[], bool] = lambda: False) -> None:
    """Reset, write and verify the receive-only configuration, then enter RX."""
    wake(bus, sleep)
    bus.strobe(b.SRES)
    sleep(0.002)
    if bus.read_status(b.PARTNUM) != 0x00 or bus.read_status(b.VERSION) in (0x00, 0xFF):
        raise CC1101SetupError("Kein plausibler CC1101 an SPI (PARTNUM/VERSION)")
    for address, value, _ in RX_REGISTERS:
        bus.write_register(address, value)
    mismatches = [f"0x{a:02X}" for a, v, _ in RX_REGISTERS if bus.read_register(a) != v]
    if mismatches:
        raise CC1101SetupError("Register nicht übernommen: " + ", ".join(mismatches))
    bus.write_register(b.PATABLE, 0x00)
    bus.strobe(b.SIDLE)
    bus.strobe(b.SFRX)
    bus.strobe(b.SCAL)
    _wait_state(bus, b.MARCSTATE_IDLE, sleep)
    if cancelled():
        raise CC1101Cancelled()
    bus.strobe(b.SRX)
    _wait_state(bus, b.MARCSTATE_RX, sleep)


def read_marcstate(bus: b.SafeCC1101Bus) -> int:
    """Status registers can change during the SPI read (CC1101 errata):
    read until two consecutive values agree."""
    previous = bus.read_status(b.MARCSTATE) & 0x1F
    for _ in range(4):
        current = bus.read_status(b.MARCSTATE) & 0x1F
        if current == previous:
            return current
        previous = current
    raise OSError("MARCSTATE nicht stabil lesbar")


def _wait_state(bus: b.SafeCC1101Bus, state: int, sleep: Callable[[float], None],
                timeout_s: float = 0.2) -> None:
    for _ in range(int(timeout_s / 0.002)):
        if read_marcstate(bus) == state:
            return
        sleep(0.002)
    raise CC1101SetupError(f"CC1101 erreicht Zustand 0x{state:02X} nicht")


def power_down(bus: b.SafeCC1101Bus) -> None:
    bus.strobe(b.SIDLE)
    bus.strobe(b.SPWD)


def describe_error(exc: BaseException) -> str:
    """Operator-facing message with a recovery step; technical text stays separate."""
    if isinstance(exc, ImportError):
        return ("Das SPI-Modul fehlt im System-Python. python3-spidev installieren "
                "und den Empfang erneut starten.")
    if isinstance(exc, b.CC1101Busy):
        return ("Der CC1101 wird gerade von einem anderen Prozess verwendet "
                "(Konsolenmessung oder Probe). Diesen beenden und erneut starten.")
    if isinstance(exc, CC1101SetupError):
        return f"{exc}. Verkabelung an SPI1 prüfen und erneut starten."
    if isinstance(exc, PermissionError):
        return ("Keine Berechtigung für /dev/spidev1.0. Den Dienstbenutzer in die Gruppe "
                "spi aufnehmen, neu anmelden und erneut starten.")
    if isinstance(exc, FileNotFoundError) or "spidev" in str(exc):
        return ("Die SPI1-Schnittstelle /dev/spidev1.0 ist nicht verfügbar. "
                "Overlay dtoverlay=spi1-1cs prüfen und erneut starten.")
    if isinstance(exc, OSError):
        return "Die SPI-Verbindung zum CC1101 ist abgebrochen. Verkabelung prüfen und erneut starten."
    return "Der CC1101-Empfang wurde unerwartet beendet. Erneut starten; Details kopieren, falls es wieder passiert."


class CC1101Receiver:
    """Background RSSI sampler; started explicitly, never on service start.

    Each start() gets a new generation number; a finishing thread from an
    older generation never overwrites the state of a newer one. Events are
    handed to a separate writer thread so SQLite commits cannot stall the
    1 kHz sampling.
    """

    MARCSTATE_CHECK_EVERY = 500
    GAP_MS = 3.0
    SETTLE_SAMPLES = 5  # samples ignored after an RX recovery (recalibration)

    def __init__(self, on_event: Callable[[dict], None], *,
                 lock_path: Path | None = None,
                 bus_factory: Callable[[], b.SafeCC1101Bus] | None = None,
                 detector_config: RssiDetectorConfig | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.on_event = on_event
        self.lock_path = Path(lock_path) if lock_path else b.spi_device_path(
            DEFAULT_SPI_BUS, DEFAULT_SPI_DEVICE)
        self.bus_factory = bus_factory or (
            lambda: b.SafeCC1101Bus(DEFAULT_SPI_BUS, DEFAULT_SPI_DEVICE))
        self.detector_config = detector_config or RssiDetectorConfig()
        self.clock = clock
        self.sleep = sleep
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._generation = 0
        self._state = "stopped"
        self._error: str | None = None
        self._error_detail: str | None = None
        self._detector: RssiBurstDetector | None = None
        self._samples = 0
        self._gaps = 0
        self._recoveries = 0
        self._store_errors = 0
        self._started: float | None = None
        self._elapsed = 0.0

    def start(self) -> tuple[bool, str]:
        with self._lock:
            if self._state in ("starting", "running", "stopping"):
                return False, "CC1101-Empfang läuft bereits oder wird gerade beendet."
            if self._thread is not None and self._thread.is_alive():
                return False, "Der vorherige CC1101-Lauf wird noch beendet. Gleich erneut starten."
            self._generation += 1
            generation = self._generation
            self._stop = threading.Event()
            self._ready = threading.Event()
            self._error = self._error_detail = None
            self._samples = self._gaps = self._recoveries = self._store_errors = 0
            self._elapsed = 0.0
            self._started = None
            events: queue.SimpleQueue = queue.SimpleQueue()
            self._detector = RssiBurstDetector(self.detector_config, on_event=events.put)
            self._state = "starting"
            self._thread = threading.Thread(
                target=self._run, args=(generation, self._stop, self._ready, events),
                name="cc1101-rssi", daemon=True)
            self._thread.start()
            ready = self._ready
        ready.wait(timeout=3)
        with self._lock:
            if generation != self._generation:
                return False, "CC1101-Start wurde durch eine neuere Aktion abgelöst."
            if self._state == "error":
                return False, self._error or "CC1101-Empfang konnte nicht starten."
            if self._state in ("stopping", "stopped"):
                return False, "CC1101-Start wurde abgebrochen."
        return True, "CC1101-Empfang gestartet (nur Empfang, RSSI-Metadaten, kein Senden)."

    def _current(self, generation: int) -> bool:
        return generation == self._generation

    def _writer(self, events: "queue.SimpleQueue", done: threading.Event) -> None:
        while True:
            try:
                event = events.get(timeout=0.1)
            except queue.Empty:
                if done.is_set():
                    return
                continue
            try:
                self.on_event(event)
            except Exception:
                with self._lock:
                    self._store_errors += 1

    def _run(self, generation: int, stop: threading.Event, ready: threading.Event,
             events: "queue.SimpleQueue") -> None:
        done = threading.Event()
        writer = threading.Thread(target=self._writer, args=(events, done),
                                  name="cc1101-events", daemon=True)
        writer.start()
        detector = self._detector
        try:
            with b.cc1101_device_lock(self.lock_path):
                bus = self.bus_factory()
                try:
                    try:
                        configure_rx(bus, sleep=self.sleep, cancelled=stop.is_set)
                    except CC1101Cancelled:
                        return
                    with self._lock:
                        if not self._current(generation) or stop.is_set():
                            return
                        self._state = "running"
                        self._started = self.clock()
                    ready.set()
                    self._sample_loop(bus, generation, stop, detector)
                finally:
                    try:
                        power_down(bus)
                    finally:
                        bus.close()
        except Exception as exc:
            with self._lock:
                if self._current(generation):
                    self._error = describe_error(exc)
                    self._error_detail = f"{type(exc).__name__}: {exc}"
                    self._state = "error"
        finally:
            detector.finish()
            done.set()
            writer.join(timeout=5)
            with self._lock:
                if self._current(generation) and self._state != "error":
                    self._state = "stopped"
            ready.set()

    def _recover_rx(self, bus: b.SafeCC1101Bus, state: int) -> None:
        bus.strobe(b.SIDLE)
        _wait_state(bus, b.MARCSTATE_IDLE, self.sleep)
        if state == b.MARCSTATE_RXFIFO_OVERFLOW:
            bus.strobe(b.SFRX)  # only valid in IDLE / overflow
        bus.strobe(b.SRX)
        _wait_state(bus, b.MARCSTATE_RX, self.sleep)

    def _sample_loop(self, bus: b.SafeCC1101Bus, generation: int,
                     stop: threading.Event, detector: RssiBurstDetector) -> None:
        period = self.detector_config.sample_period_ms / 1000
        next_t = self.clock()
        last_t: float | None = None
        settle = 0
        while not stop.is_set():
            now = self.clock()
            if now < next_t:
                self.sleep(next_t - now)
            t = self.clock()
            raw = bus.read_status(b.RSSI)
            if settle:
                settle -= 1  # recalibration after recovery: not a signal
            else:
                detector.process(t, rssi_to_dbm(raw))
            with self._lock:
                self._samples += 1
                if last_t is not None and (t - last_t) * 1000 > self.GAP_MS:
                    self._gaps += 1
                self._elapsed = t - (self._started or t)
                check = self._samples % self.MARCSTATE_CHECK_EVERY == 0
            last_t = t
            next_t += period
            if t - next_t > 0.05:  # fell far behind: resynchronise, gap counted
                next_t = t + period
            if check:
                state = read_marcstate(bus)
                if state != b.MARCSTATE_RX:
                    self._recover_rx(bus, state)
                    detector.interrupt()
                    settle = self.SETTLE_SAMPLES
                    with self._lock:
                        self._recoveries += 1

    def stop(self, timeout: float = 3.0) -> tuple[bool, str]:
        with self._lock:
            if self._state not in ("starting", "running"):
                return False, "Kein laufender CC1101-Empfang."
            self._state = "stopping"
            self._stop.set()
            thread = self._thread
        if thread:
            thread.join(timeout=timeout)
            if thread.is_alive():
                return False, ("CC1101-Empfang wird noch beendet. Status in wenigen "
                               "Sekunden erneut prüfen.")
        return True, "CC1101-Empfang beendet, Chip im Stromsparmodus."

    def status(self) -> dict:
        with self._lock:
            detector = self._detector.status() if self._detector else {}
            rate = self._samples / self._elapsed if self._elapsed > 0 else None
            return {
                "state": self._state, "error": self._error,
                "error_detail": self._error_detail,
                "source": "cc1101", "spi": f"SPI{DEFAULT_SPI_BUS}/CE{DEFAULT_SPI_DEVICE}",
                "frequency_hz": round(carrier_frequency_hz()),
                "samples": self._samples,
                "sample_rate_hz": None if rate is None else round(rate),
                "sample_gaps": self._gaps, "rx_recoveries": self._recoveries,
                "store_errors": self._store_errors,
                "rx_implemented": True, "tx_implemented": False,
                "levels_calibrated": False,
                **detector,
            }

    def close(self) -> None:
        """Stop and wait for the thread so the event store is not closed under it."""
        self.stop(timeout=5)
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
