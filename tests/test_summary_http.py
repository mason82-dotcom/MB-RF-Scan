"""Loopback-only HTTP smoke tests for read-only event summaries."""
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from keyrfmonitor import server
from keyrfmonitor.storage import EventStore
from keyrfmonitor.iq_trace import TraceArchive
from keyrfmonitor.dsp import RFConfig
import re


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

    def test_iq_apis_are_available_without_token_on_loopback(self):
        archive = TraceArchive(self.db.parent / "iq-traces")
        config = RFConfig()
        raw = b"\x70\x90" * (config.samples_per_frame * 2)
        event = {
            "kind": "burst", "at": datetime.now(timezone.utc).isoformat(),
            "start_s": .001, "end_s": .015, "duration_ms": 14.,
            "peak_snr_db": 25., "peak_rise_db": 18.,
        }
        trace = archive.save(raw, event, config, 0)
        self.assertIsNotNone(trace)
        identifier = trace["id"]
        with urlopen(self.base + "/api/iq/traces", timeout=4) as response:
            self.assertEqual(response.status, 200)
            inventory = json.load(response)
        self.assertEqual(len(inventory["traces"]), 1)
        self.assertEqual(inventory["traces"][0]["id"], identifier)
        with urlopen(self.base + "/api/iq/trace?id=" + identifier, timeout=4) as response:
            meta = json.load(response)["trace"]
        self.assertFalse(meta["decoder"]["verified"])
        self.assertEqual(meta["sample_count"], len(raw) // 2)
        with urlopen(
                self.base + "/api/iq/samples?id=" + identifier
                + "&start=0&count=512", timeout=4) as response:
            page = json.load(response)
        self.assertEqual(page["count"], 512)
        self.assertEqual(page["i"], [0x70]*512)
        self.assertEqual(page["q"], [0x90]*512)
        self.assertTrue(page["spectrum"])

        for query in (
            "/api/iq/trace?id=../../passwd",
            "/api/iq/samples?id="+identifier+"&start=0&count=9999999",
            "/api/iq/samples?id="+identifier+"&start=-1&count=512",
            "/api/iq/samples?id="+identifier+"&start=0&count=512&extra=1",
        ):
            with self.subTest(bad=query), self.assertRaises(HTTPError) as err:
                urlopen(self.base + query, timeout=4)
            self.assertEqual(err.exception.code, 400)
        with self.assertRaises(HTTPError) as missing:
            urlopen(self.base + "/api/iq/trace?id="+"0"*32, timeout=4)
        self.assertEqual(missing.exception.code, 404)

    def test_untrusted_http_host_header_is_rejected(self):
        with self.assertRaises(HTTPError) as raised:
            urlopen(Request(self.base + "/api/status",
                            headers={"Host": "untrusted.example"}), timeout=4)
        self.assertEqual(raised.exception.code, 403)

    def test_csrf_protection_for_web_writes_remains_enabled(self):
        with self.assertRaises(HTTPError) as raised:
            urlopen(Request(self.base + "/api/stop", data=b"",
                            method="POST"), timeout=4)
        self.assertEqual(raised.exception.code, 403)
    def test_web_listener_prepares_passive_iq_capture_by_default(self):
        with urlopen(self.base + "/api/status", timeout=3) as response:
            status = json.load(response)
        self.assertTrue(status["iq_trace_enabled"])
        self.assertEqual(status["iq_trace_saved"], 0)
        self.assertEqual(status["state"], "stopped")


    def test_readonly_fsk_decode_api_exposes_selectable_hypotheses(self):
        archive = TraceArchive(self.db.parent / "iq-traces")
        config = RFConfig()
        # Mock RF tones are not needed to test the HTTP security/shape contract:
        raw = b"\x80\x80" * (config.samples_per_frame * 20)
        event = {
            "kind": "burst", "at": datetime.now(timezone.utc).isoformat(),
            "start_s": 0, "end_s": .2, "duration_ms": 200,
            "peak_snr_db": 25.0, "peak_rise_db": 19.0,
        }
        item = archive.save(raw, event, config, 0)
        trace_id = item["id"]
        base = "/api/iq/decode?id=" + trace_id
        nrz = base + "&baud=2000&coding=nrz&polarity=normal&alignment=0"
        with urlopen(self.base + nrz, timeout=6) as response:
            decoded = json.load(response)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(decoded["baud"], 2000)
        self.assertEqual(decoded["coding"], "nrz")
        self.assertEqual(decoded["source"], "rtl_sdr")
        self.assertEqual(decoded["interpretation"], "hypothesis_only")
        self.assertFalse(decoded["frame_verified"])
        self.assertFalse(decoded["security_protocol_verified"])
        with urlopen(self.base + base +
                     "&baud=2000&coding=manchester&polarity=inverted&alignment=1",
                     timeout=6) as response:
            manchester = json.load(response)
        self.assertEqual(manchester["coding"], "manchester")
        self.assertEqual(manchester["alignment"], 1)
        self.assertEqual(manchester["polarity"], "inverted")
        self.assertLessEqual(manchester["bit_count"], decoded["bit_count"] // 2)

    def test_fsk_api_rejects_invalid_query_and_unknown_traces(self):
        identity = "0" * 32
        valid = "id=" + identity + "&baud=2000&coding=nrz&polarity=normal&alignment=0"
        for query in (
            "id=" + identity + "&baud=15000&coding=nrz&polarity=normal&alignment=0",
            "id=" + identity + "&baud=2000&coding=nrz&polarity=normal&alignment=1",
            "id=" + identity + "&baud=2000&coding=rot13&polarity=normal&alignment=0",
            "id=" + identity + "&baud=2000&coding=nrz&polarity=normal&alignment=0&extra=1",
            "id=" + identity + "&baud=2000&coding=nrz&polarity=normal",
            "id=../../etc&baud=2000&coding=nrz&polarity=normal&alignment=0",
            valid + "&id=" + identity,
        ):
            with self.subTest(query=query), self.assertRaises(HTTPError) as bad:
                urlopen(self.base + "/api/iq/decode?" + query, timeout=3)
            self.assertEqual(bad.exception.code, 400)
        with self.assertRaises(HTTPError) as missing:
            urlopen(self.base + "/api/iq/decode?" + valid, timeout=3)
        self.assertEqual(missing.exception.code, 404)


    def test_archive_pair_timing_endpoint_groups_real_trace_metadata(self):
        from datetime import timedelta
        archive = TraceArchive(self.db.parent / "iq-traces")
        config = RFConfig()
        raw = b"\x80\x80" * (config.samples_per_frame * 2)
        origin = datetime.now(timezone.utc)
        # Four bursts in newest-to-oldest archive ordering, two real-time gaps.
        for index, start_ms in enumerate((0, 513, 2500, 3013)):
            event = {
                "kind": "burst",
                "at": (origin + timedelta(milliseconds=start_ms)).isoformat(),
                "start_s": 0.0, "end_s": 0.02, "duration_ms": 20.0,
                "peak_snr_db": 20 + index, "peak_rise_db": 15.0,
            }
            self.assertIsNotNone(archive.save(raw, event, config, 0))
        with urlopen(self.base + "/api/iq/pairs", timeout=4) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            payload = json.load(response)
        self.assertEqual(payload["trace_count"], 4)
        self.assertEqual(payload["pair_count"], 2)
        self.assertEqual(payload["unpaired_count"], 0)
        self.assertEqual([p["start_gap_ms"] for p in payload["pairs"]],
                         [513.0, 513.0])
        self.assertFalse(payload["button_mapping_verified"])
        self.assertTrue(payload["no_raw_bits_or_iq"])
        serialized = json.dumps(payload)
        for secret in ("raw_symbols", "raw_iq", "hex_preview", "key_code",
                       "bit_confidence", "iq_format", "decoder"):
            self.assertNotIn(secret, serialized)

    def test_pair_timing_empty_archive_is_read_only(self):
        with urlopen(self.base + "/api/iq/pairs", timeout=4) as response:
            self.assertEqual(response.status, 200)
            payload = json.load(response)
        self.assertEqual(payload["pair_count"], 0)
        self.assertEqual(payload["trace_count"], 0)
        self.assertIsNone(payload["duration_median_ms"])


    def test_iq_pair_envelope_endpoint_is_read_only_and_explains_outlier(self):
        from datetime import timedelta
        archive = TraceArchive(self.db.parent / "iq-traces")
        config = RFConfig()
        one_ms = config.sample_rate // 1000
        # Two archived 180-ms active windows, the second was labelled
        # 190 ms by a coarse detector. No actual RF hardware is touched.
        raw = (b"\x80\x80" * (one_ms * 20)
               + b"\xd0\x80" * (one_ms * 180)
               + b"\x80\x80" * (one_ms * 30))
        origin = datetime.now(timezone.utc)
        for index, duration in enumerate((180, 190)):
            event = {
                "kind": "burst",
                "at": (origin + timedelta(milliseconds=index * 510)).isoformat(),
                "start_s": 0.02, "end_s": .02 + duration / 1000,
                "duration_ms": duration, "peak_snr_db": 36 + index,
                "peak_rise_db": 25.0,
            }
            self.assertIsNotNone(archive.save(raw, event, config, 0))
        files_before = {
            p.name: p.read_bytes() for p in archive.path.iterdir()
        }
        with urlopen(self.base + "/api/iq/pair-quality?pair=1", timeout=10) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            data = json.load(response)
        self.assertEqual(data["pair_index"], 1)
        self.assertEqual(data["start_gap_ms"], 510.0)
        self.assertEqual(data["comparison"]["detector_duration_difference_ms"], 10.0)
        self.assertEqual(data["comparison"]["estimated_active_difference_ms"], 0.0)
        self.assertEqual(
            data["comparison"]["assessment"],
            "compatible_with_detector_boundary_difference")
        self.assertEqual(data["second"]["estimated_end_offset_ms"], -10.0)
        self.assertEqual(len(data["first"]["profile"]), 230)
        sensitivity = data["second"]["threshold_sensitivity"]
        self.assertEqual(len(sensitivity["rows"]), 7)
        self.assertEqual(sensitivity["status"], "original_edges_estimated")
        self.assertIsNone(sensitivity["candidate_active_duration_ms"])
        self.assertTrue(sensitivity["original_edge_status_preserved"])
        self.assertFalse(sensitivity["automatic_threshold_change"])
        self.assertFalse(sensitivity["transmission_continuity_verified"])
        self.assertTrue(data["read_only"])
        self.assertFalse(data["contains_raw_iq_or_key_bits"])
        self.assertEqual(
            {p.name: p.read_bytes() for p in archive.path.iterdir()},
            files_before,
        )
        serialized = json.dumps(data)
        for sensitive in ("raw_symbols", "hex_preview", "key_code",
                          "bit_confidence", "decoder", "iq_format"):
            self.assertNotIn(sensitive, serialized)

    def test_iq_pair_quality_rejects_invalid_indices(self):
        for query in (
            "pair=0", "pair=25", "pair=-1", "pair=foo",
            "pair=1&pair=2", "pair=1&other=1", "foo=1",
            "pair=", "",
        ):
            with self.subTest(query=query), self.assertRaises(HTTPError) as error:
                urlopen(self.base + "/api/iq/pair-quality?" + query, timeout=4)
            self.assertEqual(error.exception.code, 400)
        with self.assertRaises(HTTPError) as empty:
            urlopen(self.base + "/api/iq/pair-quality?pair=1", timeout=4)
        self.assertEqual(empty.exception.code, 404)


    def test_iq_pair_threshold_sensitivity_reports_7db_as_tentative(self):
        from datetime import timedelta
        archive = TraceArchive(self.db.parent / "iq-traces")
        cfg = RFConfig()
        ms = cfg.sample_rate // 1000
        # Pair 2 has a constant-energy first burst and a weak, but nonzero,
        # 19 ms section that falls below 8 dB, not below 7 dB.
        for index in range(2):
            signal = (
                b"\x80\x80" * (25 * ms)
                + b"\x83\x80" * (45 * ms)
                + (b"\x83\x80" if index == 0 else b"\x81\x80") * (19 * ms)
                + b"\x83\x80" * (110 * ms)
                + b"\x80\x80" * (31 * ms)
            )
            event = {
                "kind": "burst",
                "at": (datetime.now(timezone.utc) +
                       timedelta(milliseconds=index * 510)).isoformat(),
                "start_s": 0.020, "end_s": 0.200,
                "duration_ms": 180.0, "peak_snr_db": 30.0,
                "peak_rise_db": 20.0,
            }
            self.assertIsNotNone(archive.save(signal, event, cfg, 0))
        with urlopen(self.base + "/api/iq/pair-quality?pair=1", timeout=12) as r:
            payload = json.load(r)
        second = payload["second"]
        sensitivity = second["threshold_sensitivity"]
        self.assertEqual(second["edge_status"], "fragmented_envelope")
        self.assertEqual(sensitivity["status"], "threshold_sensitive_fragmentation")
        self.assertEqual(sensitivity["candidate_active_duration_ms"], 174.0)
        self.assertEqual(sensitivity["candidate_threshold_db"], 7.0)
        self.assertEqual(sensitivity["rows"][4]["largest_gap_ms"], 19)
        self.assertFalse(sensitivity["automatic_threshold_change"])
        self.assertFalse(sensitivity["transmission_continuity_verified"])
        self.assertNotIn("bits", json.dumps(sensitivity))
        self.assertNotIn("decoder", json.dumps(sensitivity))



if __name__ == "__main__":
    unittest.main()
