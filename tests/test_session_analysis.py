"""v0.6.0: per-session aggregates are safe, isolated, and backwards-compatible."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from keyrfmonitor.storage import EventStore


class ScanMetricsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "events.sqlite3"
        self.store = EventStore(self.path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add(self, kind="burst", *, source="rtl_sdr", **measurements):
        item = {"kind": kind, "at": datetime.now(timezone.utc).isoformat(),
                **measurements}
        if source == "cc1101_sim":
            item.update({"source": "cc1101_sim", "simulated": True})
        self.store.insert(item)

    def test_metrics_from_real_bursts_only(self):
        sid = self.store.start_session(20)
        self.add(duration_ms=170, peak_snr_db=40, peak_rise_db=15)
        self.add(duration_ms=190, peak_snr_db=44, peak_rise_db=19)
        self.add("pair", start_gap_ms=510)
        self.add(duration_ms=500, peak_snr_db=99, peak_rise_db=99,
                 source="cc1101_sim")
        # Another SQLite writer that did not join this CLI session.
        other = EventStore(self.path)
        try:
            other.insert({"kind": "burst",
                          "at": datetime.now(timezone.utc).isoformat(),
                          "duration_ms": 50, "peak_snr_db": 5,
                          "peak_rise_db": 5})
        finally:
            other.close()
        self.store.finish_session(sid, result="completed",
                                  restoration="verified",
                                  burst_count=2, pair_count=1)
        row = self.store.recent_sessions()[0]
        self.assertTrue(row["metrics_available"])
        self.assertEqual(row["avg_duration_ms"], 180)
        self.assertEqual(row["min_duration_ms"], 170)
        self.assertEqual(row["max_duration_ms"], 190)
        self.assertEqual(row["avg_snr_db"], 42)
        self.assertEqual(row["peak_snr_db"], 44)
        self.assertEqual(row["avg_rise_db"], 17)
        self.assertEqual(row["peak_rise_db"], 19)
        self.assertEqual(row["diagnostics"], [])
        self.assertEqual(len(self.store.recent()), 5)

    def test_two_sessions_are_disjoint(self):
        first = self.store.start_session(20)
        self.add(duration_ms=160, peak_snr_db=20, peak_rise_db=11)
        self.store.finish_session(first, result="completed",
                                  restoration="verified", burst_count=1)
        second = self.store.start_session(20)
        self.add(duration_ms=200, peak_snr_db=30, peak_rise_db=21)
        self.store.finish_session(second, result="completed",
                                  restoration="verified", burst_count=1)
        newer, older = self.store.recent_sessions()
        self.assertEqual(newer["id"], second)
        self.assertEqual((newer["avg_duration_ms"], newer["avg_snr_db"]), (200, 30))
        self.assertEqual((older["avg_duration_ms"], older["avg_snr_db"]), (160, 20))

    def test_empty_session_has_known_missing_metrics(self):
        sid = self.store.start_session(20)
        self.store.finish_session(sid, result="completed", restoration="verified")
        row = self.store.recent_sessions()[0]
        self.assertTrue(row["metrics_available"])
        self.assertIsNone(row["avg_duration_ms"])
        self.assertIsNone(row["peak_snr_db"])
        self.assertIn("no_bursts", row["diagnostics"])

    def test_no_pairs_and_missing_snr_get_specific_hints(self):
        sid = self.store.start_session(20)
        self.add(duration_ms=150)
        self.add(duration_ms=150)
        self.store.finish_session(sid, result="completed",
                                  restoration="verified", burst_count=2)
        row = self.store.recent_sessions()[0]
        self.assertIn("no_pairs", row["diagnostics"])
        self.assertIn("snr_missing", row["diagnostics"])

    def test_recovery_failure_and_unfinished_are_not_silently_successful(self):
        sid = self.store.start_session(20)
        self.store.finish_session(sid, result="error", restoration="failed")
        result = self.store.recent_sessions()[0]
        self.assertEqual(result["diagnostics"], ["adsb_restore_failed"])
        pending = self.store.start_session(20)
        future = datetime.now(timezone.utc) + timedelta(seconds=120)
        row = self.store.recent_sessions(now=future)[0]
        self.assertEqual(row["id"], pending)
        self.assertEqual(row["diagnostics"], ["unfinished_scan"])
        self.assertEqual(row["restoration"], "pending")

    def test_duplicate_active_session_disallowed(self):
        self.store.start_session(20)
        with self.assertRaises(ValueError):
            self.store.start_session(20)

    def test_migration_preserves_v05_sessions_without_fabricating_metrics(self):
        db = Path(self.tmp.name) / "legacy-v05.sqlite3"
        conn = sqlite3.connect(db)
        conn.execute("""CREATE TABLE scan_sessions (
          id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL,
          finished_at TEXT, planned_seconds INTEGER NOT NULL,
          result TEXT NOT NULL, restoration TEXT NOT NULL,
          burst_count INTEGER NOT NULL DEFAULT 0,
          pair_count INTEGER NOT NULL DEFAULT 0)""")
        conn.execute("""INSERT INTO scan_sessions
                       (started_at,finished_at,planned_seconds,result,restoration,
                        burst_count,pair_count) VALUES (?,?,?,?,?,?,?)""",
                     ("2026-10-17T16:15:15+00:00","2026-10-17T16:15:36+00:00",
                      20,"completed","verified",9,4))
        conn.commit()
        conn.close()
        legacy = EventStore(db)
        try:
            rows = legacy.recent_sessions()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["burst_count"], 9)
            self.assertEqual(rows[0]["pair_count"], 4)
            self.assertFalse(rows[0]["metrics_available"])
            self.assertIsNone(rows[0]["avg_snr_db"])
            self.assertIsNone(rows[0]["avg_duration_ms"])
            # Idempotent on another process startup.
            other = EventStore(db)
            other.close()
            self.assertEqual(legacy.recent_sessions()[0]["burst_count"], 9)
        finally:
            legacy.close()


if __name__ == "__main__":
    unittest.main()
