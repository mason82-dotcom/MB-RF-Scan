"""Loopback-only HTTP smoke tests for read-only event summaries."""
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen
from unittest.mock import patch

from keyrfmonitor import server
from keyrfmonitor.storage import EventStore


class SummaryHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "events.sqlite3"
        store = EventStore(self.db)
        store.insert({"kind": "burst", "at": datetime.now(timezone.utc).isoformat(),
                      "duration_ms": 175.0, "peak_snr_db": 17.0})
        store.insert({"kind": "burst", "at": datetime.now(timezone.utc).isoformat(),
                      "duration_ms": 185.0, "source": "cc1101_sim", "simulated": True})
        store.close()

        self.ready = threading.Event()
        self.httpd = None
        self.failure = None
        def factory(address, handler):
            httpd = ThreadingHTTPServer(address, handler)
            self.httpd = httpd
            self.ready.set()
            return httpd
        self.patcher = patch.object(server, "ThreadingHTTPServer", side_effect=factory)
        self.patcher.start()
        def serve():
            try:
                server.serve("127.0.0.1", 0, str(self.db))
            except Exception as exc:
                self.failure = exc
                self.ready.set()
        self.thread = threading.Thread(target=serve, daemon=True)
        self.thread.start()
        if not self.ready.wait(timeout=5) or self.httpd is None:
            self.tearDown()
            self.fail(f"Test HTTP server did not start: {self.failure}")
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self):
        if getattr(self, "httpd", None) is not None:
            self.httpd.shutdown()
            self.thread.join(timeout=5)
        if getattr(self, "patcher", None) is not None:
            self.patcher.stop()
            self.patcher = None
        if getattr(self, "temp", None) is not None:
            self.temp.cleanup()
            self.temp = None

    def test_summary_only_counts_real_events(self):
        with urlopen(self.base + "/api/summary?hours=6", timeout=3) as response:
            result = json.load(response)
            self.assertEqual(response.status, 200)
        self.assertEqual(result["window_hours"], 6)
        self.assertEqual((result["burst_count"], result["pair_count"]), (1, 0))
        self.assertEqual(result["avg_duration_ms"], 175)
        self.assertEqual(sum(b["bursts"] for b in result["buckets"]), 1)
        self.assertEqual(len(result["buckets"]), 7)
        self.assertEqual(result["source"], "rtl_sdr")

    def test_bad_time_windows_are_rejected(self):
        for query in ("hours=0", "hours=12", "hours=6&extra=x", "hours=6&hours=24"):
            with self.subTest(query=query):
                with self.assertRaises(HTTPError) as raised:
                    urlopen(self.base + "/api/summary?" + query, timeout=3)
                self.assertEqual(raised.exception.code, 400)
 
    def test_session_readonly_endpoint(self):
        store = EventStore(self.db)
        session_id = store.start_session(30)
        store.finish_session(session_id, result="completed", restoration="verified",
                             burst_count=10, pair_count=5)
        store.close()
        with urlopen(self.base + "/api/sessions", timeout=3) as response:
            data = json.load(response)
            self.assertEqual(response.status, 200)
        self.assertEqual(data["source"], "cli_scan")
        self.assertEqual(len(data["sessions"]), 1)
        session = data["sessions"][0]
        self.assertEqual(session["id"], session_id)
        self.assertEqual(session["result"], "completed")
        self.assertEqual(session["restoration"], "verified")
        self.assertEqual((session["burst_count"], session["pair_count"]), (10, 5))
        self.assertNotIn("key_bits", session)
        self.assertNotIn("iq_data", session)

    def test_v06_analytics_visible_via_read_only_sessions_api(self):
        store = EventStore(self.db)
        sid = store.start_session(20)
        now = datetime.now(timezone.utc).isoformat()
        store.insert({"kind": "burst", "at": now, "duration_ms": 160,
                      "peak_snr_db": 32, "peak_rise_db": 18})
        store.insert({"kind": "burst", "at": now, "duration_ms": 180,
                      "peak_snr_db": 36, "peak_rise_db": 22})
        store.insert({"kind": "pair", "at": now, "start_gap_ms": 500})
        store.insert({"kind": "burst", "at": now, "duration_ms": 120,
                      "peak_snr_db": 99, "source": "cc1101_sim",
                      "simulated": True})
        store.finish_session(sid, result="completed", restoration="verified",
                             burst_count=2, pair_count=1)
        store.close()
        with urlopen(self.base + "/api/sessions", timeout=3) as response:
            payload = json.load(response)
            self.assertEqual(response.status, 200)
        item = payload["sessions"][0]
        self.assertTrue(item["metrics_available"])
        self.assertEqual(item["avg_duration_ms"], 170)
        self.assertEqual(item["avg_snr_db"], 34)
        self.assertEqual(item["peak_snr_db"], 36)
        self.assertEqual(item["avg_rise_db"], 20)
        self.assertEqual(item["burst_count"], 2)
        self.assertEqual(item["pair_count"], 1)
        self.assertNotIn("payload", item)
        self.assertNotIn("raw_iq", item)

    def test_timeline_api_returns_only_aggregated_metadata(self):
        store = EventStore(self.db)
        session = store.start_session(20)
        start = datetime.fromisoformat(store.recent_sessions()[0]["started_at"])
        from datetime import timedelta
        store.insert({
            "kind": "burst",
            "at": (start + timedelta(seconds=1)).isoformat(),
            "duration_ms": 185, "peak_snr_db": 30,
        })
        store.finish_session(session, result="completed", restoration="verified",
                             burst_count=1)
        store.close()
        with urlopen(self.base + f"/api/session-detail?id={session}&bucket=5", timeout=3) as response:
            result = json.load(response)
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(result["session_id"], session)
        self.assertEqual(result["included_bursts"], 1)
        self.assertEqual(result["buckets"][0]["avg_snr_db"], 30)
        self.assertTrue(result["timeline_available"])
        self.assertNotIn("events", result)
        self.assertNotIn("raw_iq", result)
        self.assertNotIn("key_codes", result)

    def test_timeline_api_rejects_bad_ids_buckets_and_unknown_sessions(self):
        bad = (
            "id=1&bucket=2", "id=1&bucket=10&other=1", "id=-1&bucket=5",
            "id=abc&bucket=5", "id=1&bucket=5&id=2", "id=1&bucket=",
            "bucket=5", "id=1", "id=%C3%A4&bucket=5",
        )
        for query in bad:
            with self.subTest(query=query), self.assertRaises(HTTPError) as raised:
                urlopen(self.base + "/api/session-detail?" + query, timeout=3)
            self.assertEqual(raised.exception.code, 400)
        with self.assertRaises(HTTPError) as missing:
            urlopen(self.base + "/api/session-detail?id=999999&bucket=5", timeout=3)
        self.assertEqual(missing.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
