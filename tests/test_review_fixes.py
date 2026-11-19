"""Regression tests for the review fixes of v0.9.5.

No RF hardware, no SDR, no systemd service and no network beyond loopback.
"""
from datetime import datetime, timedelta, timezone
import errno
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

from keyrfmonitor import server
from keyrfmonitor.dsp import PassiveDetector, RFConfig
from keyrfmonitor.iq_trace import TraceArchive, TraceCollector
from keyrfmonitor.receiver import Monitor
from keyrfmonitor.storage import EventStore

from test_summary_http import SummaryHTTPTests

ROOT = Path(__file__).resolve().parents[1]


ORIGIN = datetime.now(timezone.utc)


def _event(offset_ms=0, duration=18.0):
    return {
        "kind": "burst",
        "at": (ORIGIN + timedelta(milliseconds=offset_ms)).isoformat(),
        "start_s": .001, "end_s": .001 + duration / 1000,
        "duration_ms": duration, "peak_snr_db": 26, "peak_rise_db": 19,
    }


class ArchiveRobustnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.archive = TraceArchive(Path(self.temp.name) / "iq-traces")
        self.config = RFConfig()
        self.raw = b"\x70\x90" * (self.config.samples_per_frame * 3)
        self.saved = self.archive.save(self.raw, _event(), self.config, 0)

    def tearDown(self):
        self.temp.cleanup()

    def _write_meta(self, identity, payload):
        path = self.archive.path / (identity + ".json")
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_non_object_or_null_decoder_metadata_is_skipped(self):
        self._write_meta("a" * 32, [1, 2])
        broken = dict(self.saved, id="b" * 32, decoder=None)
        self._write_meta("b" * 32, broken)
        listing = self.archive.list_recent()
        self.assertEqual([entry["id"] for entry in listing], [self.saved["id"]])
        with self.assertRaises(ValueError):
            self.archive.read("a" * 32)

    def test_listing_cache_follows_file_changes(self):
        first = self.archive.list_recent()
        self.assertEqual(len(first), 1)
        second = self.archive.save(self.raw, _event(600), self.config, 0)
        ids = {entry["id"] for entry in self.archive.list_recent()}
        self.assertEqual(ids, {self.saved["id"], second["id"]})
        (self.archive.path / (second["id"] + ".json")).unlink()
        ids = {entry["id"] for entry in self.archive.list_recent()}
        self.assertEqual(ids, {self.saved["id"]})
        # Returned entries are copies; callers cannot corrupt the cache.
        self.archive.list_recent()[0]["id"] = "x"
        self.assertEqual(self.archive.list_recent()[0]["id"], self.saved["id"])

    def test_fifo_instead_of_cu8_does_not_block(self):
        cu8 = self.archive.path / (self.saved["id"] + ".cu8")
        cu8.unlink()
        os.mkfifo(cu8, 0o600)
        result = {}

        def attempt(name, call):
            try:
                call()
                result[name] = "returned"
            except ValueError:
                result[name] = "rejected"

        calls = {
            "page": lambda: self.archive.sample_page(self.saved["id"], 0, 512),
            "decode": lambda: self.archive.decode_hypothesis(self.saved["id"]),
            "envelope": lambda: self.archive.envelope_summary(self.saved["id"]),
        }
        threads = [threading.Thread(target=attempt, args=item, daemon=True)
                   for item in calls.items()]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)
        self.assertEqual(result, {name: "rejected" for name in calls})

    def test_archive_write_error_does_not_stop_reception(self):
        collector = TraceCollector(self.archive, self.config)
        frame = b"\x80\x80" * self.config.samples_per_frame
        for index in range(4):
            collector.record_frame(index, frame, [])
        event = dict(_event(), start_s=.01, end_s=.02)
        with patch.object(self.archive, "save", side_effect=OSError(errno.ENOSPC, "full")):
            collector.record_frame(4, frame, [event])
        self.assertEqual((collector.saved, collector.skipped, collector.errors), (0, 1, 1))
        self.assertTrue(collector.archive_full)
        collector.reset_scan()
        for index in range(4):
            collector.record_frame(index, frame, [])
        with patch.object(self.archive, "save", side_effect=ValueError("outside")):
            collector.record_frame(4, frame, [event])
        self.assertEqual((collector.skipped, collector.errors), (1, 1))
        self.assertFalse(collector.archive_full)


class ReceiverStatusTests(unittest.TestCase):
    def test_status_does_not_hold_lock_during_adsb_query(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "events.sqlite3")
            monitor = Monitor(store)
            free = []

            def slow_query():
                # Simulates the SDR reader thread needing the lock meanwhile.
                probe = threading.Thread(target=lambda: free.append(
                    monitor.lock.acquire(timeout=0.5) and (monitor.lock.release() or True)))
                probe.start()
                probe.join()
                return False

            with patch.object(Monitor, "adsb_active", side_effect=slow_query):
                monitor.status()
            self.assertEqual(free, [True])
            # A second poll within the TTL uses the cached value.
            with patch.object(Monitor, "adsb_active", side_effect=AssertionError("not cached")):
                self.assertFalse(monitor.status()["adsb_active"])
            store.close()

    def test_detector_keeps_bounded_event_history(self):
        detector = PassiveDetector(RFConfig())
        for number in range(1000):
            detector._emit({"kind": "burst" if number % 2 else "pair"}, [])
        self.assertLessEqual(len(detector.events), 256)
        self.assertEqual(detector.status()["burst_count"], 500)


class LanListenerRetryTests(unittest.TestCase):
    def test_missing_lan_address_keeps_loopback_and_retries(self):
        calls = []
        loopback_done = threading.Event()

        class Dummy:
            def __init__(self, address, handler):
                self.address = address
                self.stop = threading.Event()

            def serve_forever(self, **kwargs):
                if self.address[0] == "127.0.0.1":
                    deadline = time.time() + 3
                    while time.time() < deadline and ("lan", "ok") not in calls:
                        time.sleep(0.02)
                    loopback_done.set()
                    return
                self.stop.wait()

            def shutdown(self):
                self.stop.set()

            def server_close(self):
                return

        attempts = {"lan": 0}

        def create(address, handler):
            if address[0] == server.LAN_IP:
                attempts["lan"] += 1
                if attempts["lan"] == 1:
                    calls.append(("lan", "fail"))
                    raise OSError(errno.EADDRNOTAVAIL, "Cannot assign requested address")
                calls.append(("lan", "ok"))
            else:
                calls.append(("loopback", "ok"))
            return Dummy(address, handler)

        real_wait = threading.Event.wait

        def fast_wait(event, timeout=None):
            return real_wait(event, 0.01 if timeout == 5 else timeout)

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(server, "ThreadingHTTPServer", side_effect=create), \
                patch.object(threading.Event, "wait", fast_wait):
            server.serve("127.0.0.1", 8890, str(Path(directory) / "events.sqlite3"),
                         lan_host=server.LAN_IP)
        self.assertTrue(loopback_done.is_set())
        self.assertEqual(calls, [("loopback", "ok"), ("lan", "fail"), ("lan", "ok")])

    def test_other_lan_bind_errors_still_fail(self):
        def create(address, handler):
            if address[0] == server.LAN_IP:
                raise OSError(errno.EADDRINUSE, "in use")
            return type("D", (), {"server_close": lambda self: None})()

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(server, "ThreadingHTTPServer", side_effect=create), \
                self.assertRaises(OSError):
            server.serve("127.0.0.1", 8890, str(Path(directory) / "events.sqlite3"),
                         lan_host=server.LAN_IP)


class IQErrorHTTPTests(unittest.TestCase):
    setUp = SummaryHTTPTests.setUp
    tearDown = SummaryHTTPTests.tearDown

    def _status(self, path):
        try:
            with urlopen(self.base + path, timeout=5) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, json.loads(error.read())

    def test_corrupt_trace_files_get_http_answers(self):
        archive = TraceArchive(self.db.parent / "iq-traces")
        config = RFConfig()
        saved = archive.save(b"\x70\x90" * (config.samples_per_frame * 3),
                             _event(), config, 0)
        (archive.path / ("c" * 32 + ".json")).write_text("[1,2]", encoding="utf-8")
        status, data = self._status("/api/iq/traces")
        self.assertEqual(status, 200)
        self.assertEqual([t["id"] for t in data["traces"]], [saved["id"]])
        self.assertEqual(self._status("/api/iq/trace?id=" + "c" * 32)[0], 400)
        (archive.path / (saved["id"] + ".cu8")).unlink()
        status, _ = self._status(f"/api/iq/samples?id={saved['id']}&start=0&count=512")
        self.assertEqual(status, 409)
        meta = json.loads((archive.path / (saved["id"] + ".json")).read_text())
        meta["sample_count"] = "many"
        (archive.path / (saved["id"] + ".json")).write_text(json.dumps(meta))
        status, _ = self._status(f"/api/iq/samples?id={saved['id']}&start=0&count=512")
        self.assertIn(status, (400, 409))

    def test_pair_quality_rejects_changed_pair_ids(self):
        archive = TraceArchive(self.db.parent / "iq-traces")
        config = RFConfig()
        one_ms = config.sample_rate // 1000
        raw = (b"\x80\x80" * (one_ms * 20) + b"\xd0\x80" * (one_ms * 180)
               + b"\x80\x80" * (one_ms * 30))
        ids = []
        for index in range(2):
            event = dict(_event(index * 510, 180), start_s=0.02, end_s=0.2)
            ids.append(archive.save(raw, event, config, 0)["id"])
        _, pairs = self._status("/api/iq/pairs")
        pair = pairs["pairs"][0]
        good = (f"/api/iq/pair-quality?pair=1&first={pair['first']['id']}"
                f"&second={pair['second']['id']}")
        self.assertEqual(self._status(good)[0], 200)
        stale = f"/api/iq/pair-quality?pair=1&first={'d' * 32}&second={'e' * 32}"
        status, data = self._status(stale)
        self.assertEqual(status, 412)
        self.assertTrue(data["stale_pair"])
        self.assertEqual(self._status("/api/iq/pair-quality?pair=1&first=" + "d" * 32)[0], 400)


class SyncScriptTests(unittest.TestCase):
    """Run sync-from-github.sh against a local bare repo with mocked systemd."""

    def _git(self, *args, cwd):
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                       env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"})

    def _setup(self, directory):
        base = Path(directory)
        origin = base / "origin.git"
        seed = base / "seed"
        live = base / "live"
        home = base / "home"
        bin_dir = base / "bin"
        for folder in (seed, home, bin_dir):
            folder.mkdir()
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        self._git("init", "-q", "-b", "main", cwd=seed)
        (seed / "tests").mkdir()
        (seed / "tests" / "test_ok.py").write_text(
            "import unittest\nclass T(unittest.TestCase):\n    def test(self): pass\n")
        script = (ROOT / "scripts/pi/sync-from-github.sh").read_text(encoding="utf-8")
        # Accept the local test remote instead of GitHub; everything else unchanged.
        script = script.replace(
            "https://github.com/mason82-dotcom/MB-RF-Scan.git|", f"{origin}|", 1)
        (seed / "scripts" / "pi").mkdir(parents=True)
        (seed / "scripts" / "pi" / "sync-from-github.sh").write_text(script)
        self._git("add", "-A", cwd=seed)
        self._git("commit", "-q", "-m", "base", cwd=seed)
        self._git("remote", "add", "origin", str(origin), cwd=seed)
        self._git("push", "-q", "origin", "main", cwd=seed)
        subprocess.run(["git", "clone", "-q", str(origin), str(live)], check=True)
        log = base / "systemctl.log"
        (bin_dir / "systemctl").write_text(
            "#!/bin/sh\n"
            f'echo "$@" >> "{log}"\n'
            'case "$*" in *"show keyrfmonitor.service"*) echo "$TEST_LIVE";; esac\n'
            "exit 0\n")
        (bin_dir / "curl").write_text("#!/bin/sh\n[ -f \"$TEST_HEALTH\" ]\n")
        (bin_dir / "sleep").write_text("#!/bin/sh\nexit 0\n")
        (bin_dir / "journalctl").write_text("#!/bin/sh\nexit 0\n")
        for executable in bin_dir.iterdir():
            executable.chmod(0o755)
        env = {**os.environ, "HOME": str(home), "TEST_LIVE": str(live),
               "TEST_HEALTH": str(base / "healthy"),
               "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
               "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}
        return seed, live, home, env, log

    def _run(self, live, env):
        return subprocess.run(["/usr/bin/bash", str(live / "scripts/pi/sync-from-github.sh")],
                              cwd=live, env=env, capture_output=True, text=True, timeout=60)

    def _head(self, path):
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, check=True,
                              capture_output=True, text=True).stdout.strip()

    def test_failing_tests_leave_live_tree_untouched_and_are_not_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            seed, live, home, env, log = self._setup(directory)
            before = self._head(live)
            (seed / "tests" / "test_bad.py").write_text(
                "import unittest\nclass T(unittest.TestCase):\n"
                "    def test(self): self.fail('x')\n")
            self._git("add", "-A", cwd=seed)
            self._git("commit", "-q", "-m", "bad", cwd=seed)
            self._git("push", "-q", "origin", "main", cwd=seed)
            first = self._run(live, env)
            self.assertEqual(first.returncode, 1, first.stderr)
            self.assertEqual(self._head(live), before)
            self.assertFalse((live / "tests" / "test_bad.py").exists())
            self.assertNotIn("restart", log.read_text() if log.exists() else "")
            second = self._run(live, env)
            self.assertEqual(second.returncode, 1)
            self.assertIn("uebersprungen", second.stderr)

    def test_unhealthy_restart_rolls_back_and_remembers_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            seed, live, home, env, log = self._setup(directory)
            before = self._head(live)
            (seed / "NEW").write_text("x")
            self._git("add", "-A", cwd=seed)
            self._git("commit", "-q", "-m", "new", cwd=seed)
            self._git("push", "-q", "origin", "main", cwd=seed)
            result = self._run(live, env)  # no "healthy" marker: dashboard never ready
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(self._head(live), before)
            self.assertIn("Rollback", result.stderr)
            bad = home / ".local/share/keyrfmonitor/mb-rf-scan-sync.bad"
            self.assertTrue(bad.exists())
            Path(env["TEST_HEALTH"]).write_text("1")
            retry = subprocess.run(
                ["/usr/bin/bash", str(live / "scripts/pi/sync-from-github.sh")],
                cwd=live, env={**env, "MB_RF_SCAN_SYNC_RETRY": "1"},
                capture_output=True, text=True, timeout=60)
            self.assertEqual(retry.returncode, 0, retry.stderr)
            self.assertNotEqual(self._head(live), before)
            self.assertFalse(bad.exists())


if __name__ == "__main__":
    unittest.main()
