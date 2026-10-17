"""Read-only v0.7 scan timelines: metadata-only, bounded, session-isolated."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from keyrfmonitor.storage import EventStore


class TimelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "events.sqlite3"
        self.store = EventStore(self.path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add(self, start, offset, *, kind="burst", simulated=False,
            duration_ms=180, snr_db=25):
        sample = {
            "kind": kind,
            "at": (start + timedelta(seconds=offset)).isoformat(),
            "duration_ms": duration_ms if kind == "burst" else None,
            "peak_snr_db": snr_db if kind == "burst" else None,
        }
        if simulated:
            sample.update(source="cc1101_sim", simulated=True)
        self.store.insert(sample)

    def test_five_second_buckets_and_distributions(self):
        session = self.store.start_session(20)
        start = datetime.fromisoformat(self.store.recent_sessions()[0]["started_at"])
        self.add(start, 1, duration_ms=85, snr_db=7)
        self.add(start, 6, duration_ms=185, snr_db=25)
        self.add(start, 7, kind="pair")
        self.add(start, 16, duration_ms=260, snr_db=43)
        self.add(start, 3, simulated=True, duration_ms=300, snr_db=88)
        self.store.finish_session(
            session, result="completed", restoration="verified",
            burst_count=3, pair_count=1)
        result = self.store.session_timeline(session, 5)
        self.assertTrue(result["timeline_available"])
        self.assertEqual((result["window_seconds"], result["bucket_seconds"]), (20, 5))
        self.assertEqual(len(result["buckets"]), 4)
        self.assertEqual([b["bursts"] for b in result["buckets"]], [1, 1, 0, 1])
        self.assertEqual([b["pairs"] for b in result["buckets"]], [0, 1, 0, 0])
        self.assertEqual(result["buckets"][1]["avg_snr_db"], 25)
        self.assertEqual(result["included_bursts"], 3)
        self.assertEqual(result["included_pairs"], 1)
        self.assertEqual([x["count"] for x in result["snr_distribution"]],
                         [1, 0, 1, 0, 1])
        self.assertEqual([x["count"] for x in result["duration_distribution"]],
                         [1, 0, 1, 0, 1])
        self.assertFalse(result["simulated"])
        self.assertNotIn("events", result)
        self.assertNotIn("key_bits", result)
        ten = self.store.session_timeline(session, 10)
        self.assertEqual([x["bursts"] for x in ten["buckets"]], [2, 1])
        self.assertEqual(len(ten["buckets"]), 2)

    def test_individual_scans_and_other_store_are_isolated(self):
        first = self.store.start_session(20)
        first_at = datetime.fromisoformat(self.store.recent_sessions()[0]["started_at"])
        self.add(first_at, 2, snr_db=18)
        # Unlinked event from a separate database writer must not leak in.
        outside = EventStore(self.path)
        try:
            outside.insert({"kind": "burst", "at": (first_at + timedelta(seconds=4)).isoformat(),
                            "peak_snr_db": 99, "duration_ms": 175})
        finally:
            outside.close()
        self.store.finish_session(first, result="completed", restoration="verified",
                                  burst_count=1)
        second = self.store.start_session(20)
        second_at = datetime.fromisoformat(self.store.recent_sessions()[0]["started_at"])
        self.add(second_at, 3, snr_db=37)
        self.store.finish_session(second, result="completed", restoration="verified",
                                  burst_count=1)
        self.assertEqual(self.store.session_timeline(first)["buckets"][0]["avg_snr_db"], 18)
        self.assertEqual(self.store.session_timeline(second)["buckets"][0]["avg_snr_db"], 37)

    def test_legacy_unlinked_session_cannot_be_reconstructed(self):
        with self.store.lock:
            cursor = self.store.db.execute(
                "INSERT INTO scan_sessions(started_at,planned_seconds,result,restoration,"
                "burst_count,pair_count,stats_version) VALUES (?,?,?,?,?,?,0)",
                (datetime.now(timezone.utc).isoformat(), 20, "completed", "verified", 9, 4))
            self.store.db.commit()
            old_id = cursor.lastrowid
        result = self.store.session_timeline(old_id)
        self.assertFalse(result["timeline_available"])
        self.assertEqual(result["buckets"], [])
        self.assertEqual(result["snr_distribution"], [])

    def test_running_scan_uses_bounded_planned_window(self):
        session = self.store.start_session(300)
        result = self.store.session_timeline(session)
        self.assertTrue(result["timeline_available"])
        self.assertEqual(len(result["buckets"]), 60)
        self.assertEqual(result["included_bursts"], 0)
        self.assertEqual(self.store.recent_sessions()[0]["metrics_available"], False)

    def test_bad_dates_are_ignored_without_crashing(self):
        session = self.store.start_session(20)
        start = datetime.fromisoformat(self.store.recent_sessions()[0]["started_at"])
        self.add(start, -1)
        self.add(start, 22)
        self.store.insert({"kind": "burst", "at": "not-a-timestamp",
                           "duration_ms": 180, "peak_snr_db": 25})
        self.add(start, 2, duration_ms=175, snr_db=19)
        self.store.finish_session(session, result="completed", restoration="verified",
                                  burst_count=4)
        result = self.store.session_timeline(session)
        self.assertEqual(result["ignored_events"], 3)
        self.assertEqual(result["included_bursts"], 1)
        self.assertEqual(result["snr_distribution"][1]["count"], 1)

    def test_invalid_input_and_missing_session(self):
        for sid in (0, -1, True, 1.0, "1", 2147483648):
            with self.assertRaises(ValueError):
                self.store.session_timeline(sid)
        for size in (0, 3, 300, True, 5.0, "5"):
            with self.assertRaises(ValueError):
                self.store.session_timeline(1, size)
        self.assertIsNone(self.store.session_timeline(9876))

    def test_reopened_store_keeps_timeline(self):
        session = self.store.start_session(20)
        start = datetime.fromisoformat(self.store.recent_sessions()[0]["started_at"])
        self.add(start, 2, snr_db=31)
        self.store.finish_session(session, result="completed", restoration="verified",
                                  burst_count=1)
        reopened = EventStore(self.path)
        try:
            data = reopened.session_timeline(session)
            self.assertEqual(data["included_bursts"], 1)
            self.assertEqual(data["buckets"][0]["avg_snr_db"], 31)
        finally:
            reopened.close()


if __name__ == "__main__":
    unittest.main()
