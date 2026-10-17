"""Scan-session persistence and metadata safety without hardware access."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from keyrfmonitor.storage import EventStore


class SessionMetadataTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=EventStore(Path(self.tmp.name)/"events.sqlite3")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_successful_session_and_bounded_list(self):
        session=self.store.start_session(60)
        self.store.finish_session(session,result="completed",restoration="verified",
                                  burst_count=10,pair_count=5)
        item=self.store.recent_sessions()[0]
        self.assertEqual(item["id"],session)
        self.assertEqual(item["result"],"completed")
        self.assertEqual(item["restoration"],"verified")
        self.assertEqual((item["burst_count"],item["pair_count"]),(10,5))
        self.assertIsNotNone(item["finished_at"])
        self.assertGreaterEqual(item["elapsed_seconds"],0)

    def test_pending_stays_unverified(self):
        session=self.store.start_session(20)
        item=self.store.recent_sessions()[0]
        self.assertEqual(item["restoration"],"pending")
        self.assertEqual(item["result"],"running")
        self.assertFalse(item["stale"])
        later=datetime.now(timezone.utc)+timedelta(seconds=120)
        item=self.store.recent_sessions(now=later)[0]
        self.assertTrue(item["stale"])
        self.assertIsNone(item["finished_at"])
        self.assertEqual(item["restoration"],"pending")

    def test_failed_restoration_is_not_reported_as_success(self):
        session=self.store.start_session(60)
        self.store.finish_session(session,result="error",restoration="failed")
        item=self.store.recent_sessions()[0]
        self.assertEqual(item["result"],"error")
        self.assertEqual(item["restoration"],"failed")
        with self.assertRaises(ValueError):
            self.store.finish_session(session,result="completed",restoration="verified")

    def test_validation(self):
        for duration in (0,19,301,60.0,True,"60"):
            with self.assertRaises(ValueError):
                self.store.start_session(duration)
        session=self.store.start_session(60)
        with self.assertRaises(ValueError):
            self.store.finish_session(session,result="completed",restoration="failed")
        with self.assertRaises(ValueError):
            self.store.finish_session(session,result="missing",restoration="verified")
        with self.assertRaises(ValueError):
            self.store.finish_session(session,result="error",restoration="not_required",burst_count=-1)
        self.store.finish_session(session,result="interrupted",restoration="verified")
        for limit in (0,101,1.0,True):
            with self.assertRaises(ValueError):
                self.store.recent_sessions(limit)

    def test_existing_database_events_still_available(self):
        self.store.insert({"kind":"burst","at":datetime.now(timezone.utc).isoformat(),
                           "duration_ms":182.0})
        session=self.store.start_session(30)
        self.store.finish_session(session,result="error",restoration="not_required")
        self.assertEqual(len(self.store.recent()),1)
        self.assertEqual(len(self.store.recent_sessions()),1)


if __name__=="__main__":
    unittest.main()
