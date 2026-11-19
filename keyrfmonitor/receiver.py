"""Subprocess-owned live RTL-SDR receiver, with explicit operator start/stop."""
from __future__ import annotations

from collections import deque
from pathlib import Path
import fcntl
import os
import shutil
import subprocess
import threading
import time

from .dsp import PassiveDetector, RFConfig
from .storage import EventStore
from .iq_trace import TraceCollector


class Monitor:
    def __init__(self, store: EventStore, config: RFConfig | None = None,
                 trace: TraceCollector | None = None):
        self.store = store
        self.config = config or RFConfig()
        self.trace = trace
        self.lock = threading.RLock()
        self.process: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None
        self.state = "stopped"
        self.last_error: str | None = None
        self.stderr_tail = deque(maxlen=10)
        self.detector: PassiveDetector | None = None
        self.started_at = None
        self._device_lock_fd: int | None = None
        self._adsb_cache: tuple[float, bool] | None = None

    def _claim_device(self) -> bool:
        """Process-wide advisory lock: dashboard and CLI must not race for SDR."""
        path = self.store.path.parent / "rtl-sdr.lock"
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        except Exception:
            os.close(fd)
            raise
        self._device_lock_fd = fd
        return True

    def _release_device(self) -> None:
        if self._device_lock_fd is not None:
            os.close(self._device_lock_fd)
            self._device_lock_fd = None

    @staticmethod
    def adsb_active() -> bool:
        # A deliberately read-only systemctl query. Nothing here stops ADS-B.
        try:
            p = subprocess.run(["systemctl", "is-active", "--quiet", "dump1090.service"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=3, check=False)
            return p.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def start(self) -> tuple[bool, str]:
        with self.lock:
            if self.state in ("starting", "running", "stopping"):
                return False, "Empfang läuft bereits oder wird gerade beendet."
            if self.adsb_active():
                return False, ("ADS-B verwendet den RTL-SDR. Stoppe dump1090.service "
                               "bewusst vor dem Start der RF-Messung.")
            if not shutil.which("rtl_sdr"):
                return False, "rtl_sdr fehlt. Bitte das Debian-Paket rtl-sdr installieren."
            if not self._claim_device():
                return False, "Ein anderer KeyRFMonitor-Prozess verwendet den RTL-SDR bereits."
            self.last_error = None
            self.stderr_tail.clear()
            if self.trace is not None:
                self.trace.reset_scan()
            try:
                self.detector = PassiveDetector(self.config, on_event=self.store.insert)
            except Exception:
                self._release_device()
                raise
            argv = ["rtl_sdr", "-d", "0", "-f", str(self.config.center_hz),
                    "-s", str(self.config.sample_rate), "-g", str(self.config.gain_db), "-"]
            try:
                self.process = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                                bufsize=0)
            except OSError as e:
                self.state = "error"
                self.last_error = str(e)
                self._release_device()
                return False, self.last_error
            self.state = "starting"
            self.started_at = time.time()
            self.thread = threading.Thread(target=self._collect, name="rtl-sdr-reader", daemon=True)
            self.thread.start()
            threading.Thread(target=self._read_errors, args=(self.process,),
                             name="rtl-sdr-stderr", daemon=True).start()
            return True, "SDR-Messung gestartet. 2 Sekunden für die Ruhekalibrierung warten."

    def _read_errors(self, process: subprocess.Popen) -> None:
        try:
            for raw in process.stderr:
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    with self.lock:
                        self.stderr_tail.append(line[:400])
        except (OSError, ValueError):
            return

    def _collect(self) -> None:
        proc = self.process
        detector = self.detector
        assert proc is not None and detector is not None
        size = self.config.samples_per_frame * 2
        try:
            while True:
                # Pull exactly one 10ms chunk, read-only and in memory.
                parts = []
                remaining = size
                while remaining > 0:
                    buf = proc.stdout.read(remaining)
                    if not buf:
                        break
                    parts.append(buf)
                    remaining -= len(buf)
                if remaining:
                    break
                chunk = b"".join(parts)
                events = detector.process(chunk)
                if self.trace is not None:
                    self.trace.record_frame(detector.frame_index - 1, chunk, events)
                with self.lock:
                    if self.state == "starting" and detector.status()["baseline_ready"]:
                        self.state = "running"
        except Exception as e:  # Explicitly report unexpected backend errors.
            with self.lock:
                self.last_error = f"Empfangsfehler: {type(e).__name__}: {e}"
        finally:
            detector.finish()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
            with self.lock:
                intentional = self.state == "stopping"
                if intentional:
                    self.state = "stopped"
                else:
                    self.state = "error"
                    if not self.last_error:
                        self.last_error = ("SDR-Programm beendet. Ist das Gerät belegt? "
                                           f"Exit-Code {proc.returncode}.")
                self.process = None
                self._release_device()

    def stop(self) -> tuple[bool, str]:
        with self.lock:
            if self.process is None or self.state in ("stopped", "error"):
                return False, "Keine aktive SDR-Messung."
            proc = self.process
            self.state = "stopping"
            proc.terminate()
        return True, "SDR-Messung wird beendet. ADS-B wird nicht automatisch gestartet."

    def _device_in_use_elsewhere(self) -> bool:
        if self._device_lock_fd is not None:
            return False
        path = self.store.path.parent / "rtl-sdr.lock"
        try:
            fd = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return False
        except OSError:
            return True
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return False
            except BlockingIOError:
                return True
        finally:
            os.close(fd)

    ADSB_STATUS_TTL_S = 1.0

    def _adsb_active_cached(self) -> bool:
        """Short-lived cache for status polls; start() always queries fresh."""
        now = time.monotonic()
        cached = self._adsb_cache
        if cached is not None and now - cached[0] < self.ADSB_STATUS_TTL_S:
            return cached[1]
        active = self.adsb_active()
        self._adsb_cache = (time.monotonic(), active)
        return active

    def status(self) -> dict:
        # systemctl (up to 3 s) and the lock-file probe run OUTSIDE self.lock,
        # so the 10 ms SDR reader thread is never blocked by status polling.
        adsb = self._adsb_active_cached()
        external = self._device_in_use_elsewhere()
        with self.lock:
            obj = {"state": self.state, "adsb_active": adsb,
                   "error": self.last_error, "stderr": list(self.stderr_tail)[-4:],
                   "frequency_hz": self.config.center_hz,
                   "gain_db": self.config.gain_db,
                   "iq_saved": bool(self.trace and self.trace.saved),
                   "key_codes_saved": False,
                   "iq_trace_enabled": self.trace is not None,
                   "iq_trace_saved": self.trace.saved if self.trace else 0,
                   "iq_trace_skipped": self.trace.skipped if self.trace else 0,
                   "iq_trace_errors": getattr(self.trace, "errors", 0) if self.trace else 0,
                   "external_scan_active": external}
            if self.detector is not None:
                obj.update(self.detector.status())
            return obj

    def close(self) -> None:
        with self.lock:
            process = self.process
            if process is not None:
                self.state = "stopping"
                process.terminate()
            thread = self.thread
        if thread:
            thread.join(timeout=5)
        with self.lock:
            if self.process is not None:
                self.process.kill()
                self.process.wait(timeout=2)
                self.process = None
            self.state = "stopped"
            self._release_device()
