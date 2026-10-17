import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from keyrfmonitor.receiver import Monitor
from keyrfmonitor.storage import EventStore


class ReceiverSafetyTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = EventStore(Path(self.dir.name) / "events.sqlite3")
        self.monitor = Monitor(self.store)

    def tearDown(self):
        self.monitor.close()
        self.store.close()
        self.dir.cleanup()

    @patch.object(Monitor, "adsb_active", return_value=True)
    @patch("keyrfmonitor.receiver.subprocess.Popen")
    def test_does_not_take_sdr_while_adsb_active(self, popen, adsb):
        ok, msg = self.monitor.start()
        self.assertFalse(ok)
        self.assertIn("ADS-B", msg)
        popen.assert_not_called()

    @patch.object(Monitor, "adsb_active", return_value=False)
    @patch("keyrfmonitor.receiver.shutil.which", return_value=None)
    def test_missing_rtl_sdr_explained(self, which, adsb):
        ok, msg = self.monitor.start()
        self.assertFalse(ok)
        self.assertIn("rtl_sdr", msg)

    def test_exclusive_lock_blocks_second_instance(self):
        second = Monitor(self.store)
        try:
            self.assertTrue(self.monitor._claim_device())
            self.assertFalse(second._claim_device())
            self.assertTrue(second.status()["external_scan_active"])
            self.monitor._release_device()
            self.assertTrue(second._claim_device())
        finally:
            second.close()

    def test_stop_no_running_receiver(self):
        ok, msg = self.monitor.stop()
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
