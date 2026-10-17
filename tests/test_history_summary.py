"""Passive summary API backing store uses metadata only; synthetic sources stay separate."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from keyrfmonitor.storage import EventStore

class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=EventStore(Path(self.temp.name)/"events.sqlite3")
        self.now=datetime(2026,10,17,14,25,tzinfo=timezone.utc)
    def tearDown(self):
        self.store.close()
        self.temp.cleanup()
    def add(self, age, kind="burst", simulated=False, **kwargs):
        event={"kind":kind,"at":(self.now-timedelta(hours=age)).isoformat(),**kwargs}
        if simulated:event.update(source="cc1101_sim",simulated=True)
        self.store.insert(event)
    def test_empty_window(self):
        s=self.store.summary(24,now=self.now)
        self.assertEqual((s["burst_count"],s["pair_count"]),(0,0))
        self.assertEqual(len(s["buckets"]),25)
        self.assertIsNone(s["avg_duration_ms"])
        self.assertIsNone(s["last_event_at"])
    def test_real_bursts_and_pairs(self):
        self.add(1,duration_ms=170,peak_snr_db=12)
        self.add(1,duration_ms=180,peak_snr_db=16)
        self.add(1,kind="pair",start_gap_ms=510)
        s=self.store.summary(24,now=self.now)
        self.assertEqual((s["burst_count"],s["pair_count"]),(2,1))
        self.assertEqual(s["avg_duration_ms"],175)
        self.assertEqual(s["avg_snr_db"],14)
        self.assertEqual(sum(b["bursts"] for b in s["buckets"]),2)
        self.assertEqual(sum(b["pairs"] for b in s["buckets"]),1)
    def test_excludes_simulation_old_and_future(self):
        self.add(25,duration_ms=190)
        self.add(-.01,duration_ms=200)
        self.add(2,simulated=True,duration_ms=185)
        self.add(2,kind="pair",simulated=True,start_gap_ms=530)
        s=self.store.summary(24,now=self.now)
        self.assertEqual((s["burst_count"],s["pair_count"]),(0,0))
        self.assertEqual(len(self.store.recent()),4)
    def test_windows_validate(self):
        self.add(7,duration_ms=180)
        self.assertEqual(self.store.summary(6,now=self.now)["burst_count"],0)
        self.assertEqual(self.store.summary(168,now=self.now)["burst_count"],1)
        self.assertEqual(len(self.store.summary(168,now=self.now)["buckets"]),169)
        for n in (1,0,12,169,24.0,True,"24"):
            with self.assertRaises(ValueError):self.store.summary(n,now=self.now)
    def test_timestamps_and_timezone(self):
        self.store.insert({"kind":"burst","at":"2026-10-17T13:00:00Z","duration_ms":175})
        self.add(1,duration_ms=180)
        s=self.store.summary(6,now=self.now.astimezone(timezone(timedelta(hours=2))))
        self.assertEqual(s["burst_count"],2)
        self.assertTrue(all(x["hour"].endswith("Z") for x in s["buckets"]))
        with self.assertRaises(ValueError):
            self.store.summary(24,now=datetime(2026,10,17))
if __name__=="__main__":
    unittest.main()
