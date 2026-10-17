"""CC1101 development simulator, metadata-only and entirely independent of SPI.

No payloads, IQ samples, captured code words, GPIO or RF device accesses.
Simulated 175-ms bursts with a 513-ms start-to-start gap are *fixtures*, not
observations from a connected CC1101. Hardware RX is not implemented.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import Event, RLock, Thread
from typing import Callable
import time

SIMULATED_PAIR_STARTS_S = (0.30, 1.55, 2.80)
SIMULATED_PAIR_GAP_S = 0.513
SIMULATED_BURST_MS = 175.0


def simulated_events(started_at: datetime | None = None) -> list[dict]:
    """Generate metadata-only, deterministic 3-pair test scenario."""
    base = started_at or datetime.now(timezone.utc)
    if base.tzinfo is None:
        raise ValueError("A timezone-aware timestamp is required")
    result = []
    for sequence, first_start in enumerate(SIMULATED_PAIR_STARTS_S, 1):
        second_start = first_start + SIMULATED_PAIR_GAP_S
        for start in (first_start, second_start):
            at = (base + timedelta(seconds=start)).isoformat()
            result.append({
                "kind": "burst", "source": "cc1101_sim", "simulated": True,
                "at": at, "start_s": round(start, 3),
                "end_s": round(start + SIMULATED_BURST_MS / 1000, 3),
                "duration_ms": SIMULATED_BURST_MS,
            })
        result.append({
            "kind": "pair", "source": "cc1101_sim", "simulated": True,
            "at": (base + timedelta(seconds=second_start)).isoformat(),
            "start_s": round(first_start, 3),
            "second_start_s": round(second_start, 3),
            "start_gap_ms": round(SIMULATED_PAIR_GAP_S * 1000, 1),
            "first_duration_ms": SIMULATED_BURST_MS,
            "second_duration_ms": SIMULATED_BURST_MS,
            "sequence": sequence,
        })
    return sorted(result, key=lambda item: (item["second_start_s"] if item["kind"] == "pair" else item["start_s"], item["kind"] == "pair"))


class CC1101Simulator:
    """Manually triggered simulated backend; never opens spidev or GPIO."""

    def __init__(self, on_event: Callable[[dict], None]):
        self.on_event = on_event
        self._lock = RLock()
        self._stop = Event()
        self._thread: Thread | None = None
        self._state = "stopped"
        self._count = 0
        self._error: str | None = None

    def start(self) -> tuple[bool, str]:
        with self._lock:
            if self._state in ("starting", "running", "stopping"):
                return False, "CC1101-Simulation läuft bereits."
            self._stop.clear()
            self._count = 0
            self._error = None
            self._state = "starting"
            self._thread = Thread(target=self._run, name="cc1101-metadata-simulation", daemon=True)
            self._thread.start()
            return True, "Simulationslauf gestartet; keine CC1101-Hardware und keine Funkaussendung."

    def _run(self) -> None:
        now = datetime.now(timezone.utc)
        events = simulated_events(now)
        origin = time.monotonic()
        try:
            with self._lock:
                self._state = "running"
            for event in events:
                timestamp = event.get("second_start_s", event.get("start_s"))
                remaining = max(0.0, origin + timestamp - time.monotonic())
                if self._stop.wait(remaining):
                    break
                self.on_event(event.copy())
                with self._lock:
                    self._count += 1
        except Exception as exc:
            with self._lock:
                self._error = f"Simulation fehlgeschlagen: {type(exc).__name__}: {exc}"
                self._state = "error"
        finally:
            with self._lock:
                if self._state != "error":
                    self._state = "stopped"

    def stop(self) -> tuple[bool, str]:
        with self._lock:
            if self._state not in ("starting", "running"):
                return False, "Keine laufende CC1101-Simulation."
            self._state = "stopping"
            self._stop.set()
        return True, "Simulation wird beendet."

    def status(self) -> dict:
        with self._lock:
            return {
                "state": self._state, "events_emitted": self._count,
                "error": self._error, "source": "cc1101_sim",
                "simulated": True, "hardware_connected": None,
                "spi_accessed": False, "rx_implemented": False, "tx_implemented": False,
            }

    def close(self) -> None:
        with self._lock:
            self._stop.set()
            thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=5)
