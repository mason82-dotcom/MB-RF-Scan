"""Explicit, bounded scan session with fail-safe restoration of dump1090.

Only invoked from the interactive CLI. No web endpoint or autostart; raw
CU8 IQ is saved exclusively when the operator passes --iq.
The operator authorizes a single scan with sudo -v. A root-owned transient systemd
watchdog is armed *before* ADS-B is stopped, so SIGKILL of this process cannot
leave ADS-B off indefinitely. The watchdog only ever starts dump1090.service.
"""
from __future__ import annotations

from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

from .receiver import Monitor
from .storage import EventStore
from .iq_trace import TraceArchive, TraceCollector
from .dsp import RFConfig

ADS_B_UNIT = "dump1090.service"
WATCHDOG_UNIT = "keyrfmonitor-adsb-restore"
MIN_SECONDS = 20
MAX_SECONDS = 300


class ScanError(RuntimeError):
    pass


class ADSBControl:
    """All privileged commands have a fixed, non-user-controlled argv."""

    @staticmethod
    def run(argv: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(argv, text=True, capture_output=True,
                                  check=False, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ScanError(f"Befehl fehlgeschlagen: {argv[0]} ({exc})") from exc

    def is_active(self) -> bool:
        result = self.run(["systemctl", "is-active", "--quiet", ADS_B_UNIT], timeout=5)
        if result.returncode not in (0, 3):
            # systemctl 4 means unknown unit; do not operate on an unknown service.
            raise ScanError("ADS-B-Dienststatus nicht eindeutig. Bitte systemctl status dump1090.service prüfen.")
        return result.returncode == 0

    def authorize(self) -> None:
        if not shutil.which("sudo") or not shutil.which("systemd-run"):
            raise ScanError("sudo oder systemd-run fehlt; ADS-B wird nicht angehalten.")
        # Leave the terminal attached: sudo must display its password prompt.
        try:
            result = subprocess.run(["sudo", "-v"], check=False, timeout=90)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ScanError(f"sudo-Freigabe nicht möglich: {exc}") from exc
        if result.returncode != 0:
            raise ScanError("Keine sudo-Freigabe. ADS-B bleibt unverändert.")

    def root(self, *args: str) -> None:
        result = self.run(["sudo", "-n", *args])
        if result.returncode:
            message = (result.stderr or result.stdout or "Unbekannter Fehler").strip()
            raise ScanError(f"{args[0]} fehlgeschlagen: {message[:500]}")

    def arm(self, seconds: int) -> None:
        # A fixed name prevents a new scan from coexisting with an old restore timer.
        # --collect prevents persistent service/timer debris after the watchdog runs.
        self.root("systemd-run", "--unit=" + WATCHDOG_UNIT,
                  "--on-active=" + str(seconds + 45) + "s",
                  "--timer-property=AccuracySec=1s", "--collect",
                  "/usr/bin/systemctl", "start", ADS_B_UNIT)

    def pause(self) -> None:
        self.root("systemctl", "stop", ADS_B_UNIT)
        if self.is_active():
            raise ScanError("dump1090.service konnte nicht angehalten werden.")

    def restore(self) -> None:
        self.root("systemctl", "start", ADS_B_UNIT)
        if not self.is_active():
            raise ScanError("dump1090.service wurde nicht wieder aktiv. Watchdog bleibt scharf.")

    def disarm(self) -> None:
        self.root("systemctl", "stop", WATCHDOG_UNIT + ".timer")


def run_scan(seconds: int = 60, *, control: ADSBControl | None = None,
             database: str | Path | None = None,
             capture_iq: bool = False) -> int:
    """Pause active ADS-B, receive metadata for bounded time, then restore it.

    Refuses to manage service if it was not active beforehand. Does not touch
    CC1101 or change service enablement. A watchdog handles abrupt process death.
    """
    if type(capture_iq) is not bool:
        raise ScanError('capture_iq must be explicitly True or False')
    if not MIN_SECONDS <= seconds <= MAX_SECONDS:
        raise ScanError(f"Scan-Dauer muss zwischen {MIN_SECONDS} und {MAX_SECONDS} Sekunden liegen.")
    ctl = control or ADSBControl()
    if not ctl.is_active():
        raise ScanError("dump1090.service ist nicht aktiv. Automatische Wiederherstellung "
                        "nur aus dem vorher aktiven Zustand möglich; nichts geändert.")
    if not shutil.which("rtl_sdr"):
        raise ScanError("rtl_sdr nicht installiert; ADS-B bleibt aktiv.")
    # Interactive authorization before any actual change to systemd.
    ctl.authorize()
    store_path = Path(database) if database else Path.home() / ".local/share/keyrfmonitor/events.sqlite3"
    monitor = None
    store = None
    session_id = None
    armed = False
    failure: Exception | None = None
    restored = False
    interrupted = False
    old_sigterm = signal.getsignal(signal.SIGTERM)

    def on_term(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_term)
    burst_count = 0
    pair_count = 0
    trace = None
    try:
        # Validate/write session metadata before interrupting any ADS-B service.
        store = EventStore(store_path)
        session_id = store.start_session(seconds)
        if capture_iq:
            # Opt-in only: retain finite burst windows, not an open-ended stream.
            config = RFConfig()
            archive = TraceArchive(store_path.parent / "iq-traces")
            trace = TraceCollector(archive, config)
        # Arm the independent root-owned restoration timer BEFORE stopping ADS-B.
        ctl.arm(seconds)
        armed = True
        ctl.pause()
        monitor = Monitor(store, config=config, trace=trace) if capture_iq else Monitor(store)
        ok, msg = monitor.start()
        if not ok:
            raise ScanError(msg)
        print(f"KeyRFMonitor: Scan läuft {seconds} s; ADS-B wird danach automatisch wieder gestartet.", flush=True)
        if capture_iq:
            print("OPT-IN IQ: Echte CU8-Bursts und unbestätigte FSK-Bithypothesen "
                  "werden PRIVAT gespeichert; keine HF-Aussendung oder Replay.", flush=True)
            print("Begrenzung: max. 48 Burst-Traces je Scan, max. 64 MiB Archiv "
                  "und 128 MiB freier Speicher als Reserve.", flush=True)
        else:
            print("Nur Burst-Metadaten. Kein Senden, keine Codes, keine IQ-Speicherung.",
                  flush=True)
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            status = monitor.status()
            if status["state"] == "error":
                raise ScanError(status.get("error") or "SDR-Empfang abgebrochen")
            # An external restart of ADS-B requires us to release the SDR promptly.
            if ctl.is_active():
                raise ScanError("dump1090.service wurde während des Scans extern gestartet.")
            time.sleep(min(0.25, max(0, end - time.monotonic())))
    except KeyboardInterrupt:
        interrupted = True
        print("\nScan unterbrochen. Stelle ADS-B wieder her ...", file=sys.stderr, flush=True)
    except Exception as exc:
        failure = exc
    finally:
        try:
            if monitor is not None:
                monitor.close()  # SDR released before ADS-B is restarted.
                status = monitor.status()
                burst_count = int(status.get("burst_count", 0))
                pair_count = int(status.get("pair_count", 0))
                print(f"Ergebnis: {burst_count} Bursts, "
                      f"{pair_count} Paare.", flush=True)
                if trace is not None:
                    print(f"IQ-Traces: {trace.saved} gespeichert, "
                          f"{trace.skipped} nicht erfasst. Archiv: "
                          f"{trace.archive.path}", flush=True)
        except Exception as exc:
            failure = failure or exc
        try:
            if armed:
                ctl.restore()
                restored = True
                print("ADS-B-Wiederherstellung: dump1090.service aktiv.", flush=True)
        except Exception as exc:
            failure = failure or exc
            print("WARNUNG: ADS-B-Neustart fehlgeschlagen. Root-Watchdog bleibt aktiviert!",
                  file=sys.stderr, flush=True)
        if armed and restored:
            try:
                ctl.disarm()
            except Exception as exc:
                # ADS-B is already back; a redundant future watchdog start is harmless.
                print(f"Hinweis: Watchdog konnte nicht deaktiviert werden: {exc}",
                      file=sys.stderr, flush=True)
        # Final bookkeeping happens only AFTER the recovery attempt.
        # SIGKILL leaves a 'running/pending' row rather than a false success.
        try:
            if store is not None and session_id is not None:
                outcome = "error" if failure else "interrupted" if interrupted else "completed"
                recovery = "verified" if restored else "failed" if armed else "not_required"
                store.finish_session(session_id, result=outcome, restoration=recovery,
                                     burst_count=burst_count, pair_count=pair_count)
        except Exception as exc:
            failure = failure or exc
        finally:
            try:
                if store is not None:
                    store.close()
            except Exception as exc:
                failure = failure or exc
            signal.signal(signal.SIGTERM, old_sigterm)
    if failure:
        raise ScanError(str(failure)) from failure
    return 130 if interrupted else 0
