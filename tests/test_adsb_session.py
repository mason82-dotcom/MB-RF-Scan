"""Unit tests for bounded ADS-B pauses: all commands mocked, never touch systemd."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from keyrfmonitor import __main__
from keyrfmonitor.adsb_session import ADSBControl, ScanError, run_scan
from keyrfmonitor.storage import EventStore


class FakeControl:
    def __init__(self, *, active=True, fail_at=None):
        self.active = active
        self.fail_at = fail_at
        self.commands = []

    def _call(self, name):
        self.commands.append(name)
        if self.fail_at == name:
            raise ScanError("simulierter Fehler: " + name)

    def is_active(self):
        self.commands.append("is_active")
        return self.active

    def authorize(self):
        self._call("authorize")

    def arm(self, seconds):
        self._call("arm")
        self.seconds = seconds

    def pause(self):
        self._call("pause")
        self.active = False

    def restore(self):
        self._call("restore")
        self.active = True

    def disarm(self):
        self._call("disarm")


class FakeMonitor:
    instances = []
    fail_start = False
    def __init__(self, store):
        self.store = store
        self.closed = False
        self.__class__.instances.append(self)

    def start(self):
        if self.__class__.fail_start:
            return False, "SDR bereits belegt"
        return True, "started"

    def status(self):
        return {"state": "running", "burst_count": 2, "pair_count": 1}

    def close(self):
        self.closed = True


class ScanModeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "events.sqlite3")
        FakeMonitor.instances.clear()
        FakeMonitor.fail_start = False

    def tearDown(self):
        self.temp.cleanup()

    def test_inactive_adsb_unchanged(self):
        ctrl = FakeControl(active=False)
        with self.assertRaisesRegex(ScanError, "nicht aktiv"):
            run_scan(60, control=ctrl, database=self.path)
        self.assertEqual(ctrl.commands, ["is_active"])

    def test_duration_limits_no_change(self):
        for n in (0, 19, 301, 999):
            ctrl = FakeControl()
            with self.assertRaises(ScanError):
                run_scan(n, control=ctrl, database=self.path)
            self.assertEqual(ctrl.commands, [])

    def test_successful_pause_scan_restore(self):
        ctrl = FakeControl()
        with patch("keyrfmonitor.adsb_session.shutil.which", return_value="/usr/bin/rtl_sdr"), \
             patch("keyrfmonitor.adsb_session.Monitor", FakeMonitor), \
             patch("keyrfmonitor.adsb_session.time.monotonic", side_effect=[0, 61]), \
             contextlib.redirect_stdout(io.StringIO()):
            rc = run_scan(60, control=ctrl, database=self.path)
        self.assertEqual(rc, 0)
        self.assertEqual(ctrl.commands,
                         ["is_active", "authorize", "arm", "pause", "restore", "disarm"])
        self.assertEqual(ctrl.seconds, 60)
        self.assertTrue(ctrl.active)
        self.assertTrue(FakeMonitor.instances[-1].closed)
        store = EventStore(self.path)
        session = store.recent_sessions()[0]
        self.assertEqual(session["result"], "completed")
        self.assertEqual(session["restoration"], "verified")
        self.assertEqual((session["burst_count"], session["pair_count"]), (2, 1))
        store.close()

    def test_start_failure_restores_and_disarms(self):
        FakeMonitor.fail_start = True
        ctrl = FakeControl()
        with patch("keyrfmonitor.adsb_session.shutil.which", return_value="/usr/bin/rtl_sdr"), \
             patch("keyrfmonitor.adsb_session.Monitor", FakeMonitor), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ScanError, "SDR bereits belegt"):
                run_scan(60, control=ctrl, database=self.path)
        self.assertEqual(ctrl.commands[-2:], ["restore", "disarm"])
        self.assertTrue(ctrl.active)
        store = EventStore(self.path)
        session = store.recent_sessions()[0]
        self.assertEqual((session["result"], session["restoration"]), ("error", "verified"))
        store.close()

    def test_pause_failure_recovers(self):
        ctrl = FakeControl(fail_at="pause")
        with patch("keyrfmonitor.adsb_session.shutil.which", return_value="/usr/bin/rtl_sdr"), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(ScanError):
                run_scan(60, control=ctrl, database=self.path)
        self.assertEqual(ctrl.commands[-2:], ["restore", "disarm"])

    def test_arming_failure_never_stops_adsb(self):
        ctrl = FakeControl(fail_at="arm")
        with patch("keyrfmonitor.adsb_session.shutil.which", return_value="/usr/bin/rtl_sdr"), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(ScanError):
                run_scan(60, control=ctrl, database=self.path)
        self.assertNotIn("pause", ctrl.commands)
        self.assertNotIn("restore", ctrl.commands)
        self.assertTrue(ctrl.active)
        store = EventStore(self.path)
        session = store.recent_sessions()[0]
        self.assertEqual((session["result"], session["restoration"]), ("error", "not_required"))
        store.close()

    def test_restore_failure_retains_watchdog(self):
        ctrl = FakeControl(fail_at="restore")
        with patch("keyrfmonitor.adsb_session.shutil.which", return_value="/usr/bin/rtl_sdr"), \
             patch("keyrfmonitor.adsb_session.Monitor", FakeMonitor), \
             patch("keyrfmonitor.adsb_session.time.monotonic", side_effect=[0, 61]), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(ScanError):
                run_scan(60, control=ctrl, database=self.path)
        self.assertNotIn("disarm", ctrl.commands)
        store = EventStore(self.path)
        session = store.recent_sessions()[0]
        self.assertEqual((session["result"], session["restoration"]), ("error", "failed"))
        store.close()

    def test_cli_invalid_seconds(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = __main__.main(["scan", "--seconds", "301"])
        self.assertEqual(rc, 2)
        self.assertIn("20 und 300", err.getvalue())

    def test_systemd_commands_fixed_and_watchdog_first(self):
        ctl = ADSBControl()
        calls = []
        def runner(args, **kw):
            calls.append(args)
            return type("Result", (), {"returncode": 0, "stderr": "", "stdout": ""})()
        with patch.object(ctl, "run", side_effect=runner):
            ctl.arm(60)
            ctl.disarm()
        self.assertEqual(calls[0], ["sudo", "-n", "systemd-run",
                                    "--unit=keyrfmonitor-adsb-restore", "--on-active=105s",
                                    "--timer-property=AccuracySec=1s", "--collect",
                                    "/usr/bin/systemctl", "start", "dump1090.service"])
        self.assertEqual(calls[1], ["sudo", "-n", "systemctl", "stop",
                                    "keyrfmonitor-adsb-restore.timer"])


if __name__ == "__main__":
    unittest.main()
