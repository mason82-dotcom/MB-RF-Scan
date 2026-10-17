"""Opt-in real CU8 trace tests; synthetic RF input, no actual radio hardware."""
from datetime import datetime, timezone
from pathlib import Path
import math
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from keyrfmonitor.dsp import RFConfig
from keyrfmonitor.iq_trace import (
    MAX_ARCHIVE_BYTES, TraceArchive, TraceCollector, demodulate_fsk,
)


def synthetic_cu8(bits: str, cfg: RFConfig, *, amplitude: float = 55.) -> bytes:
    samples_per_symbol = round(cfg.sample_rate / 2000)
    freqs = np.repeat(
        np.array([cfg.high_offset_hz if bit == "1" else cfg.low_offset_hz
                  for bit in bits], dtype=np.float64),
        samples_per_symbol)
    phase = 2 * math.pi * np.cumsum(freqs) / cfg.sample_rate
    wave = amplitude * np.exp(1j * phase)
    samples = np.empty((len(freqs), 2), dtype=np.uint8)
    samples[:, 0] = np.clip(127.5 + wave.real, 0, 255).astype(np.uint8)
    samples[:, 1] = np.clip(127.5 + wave.imag, 0, 255).astype(np.uint8)
    return samples.tobytes()


class FskDemodTests(unittest.TestCase):
    def setUp(self):
        self.cfg = RFConfig()

    def test_realistic_cu8_recovers_nominal_fsk_symbol_hypothesis(self):
        expected = ("0100110101110001" * 16)
        data = synthetic_cu8(expected, self.cfg)
        result = demodulate_fsk(data, self.cfg, 0, len(data) // 2)
        self.assertGreaterEqual(len(result["bits"]), len(expected) - 1)
        self.assertGreater(result["bits"].count("1"), 50)
        self.assertFalse(result["verified"])
        self.assertEqual(result["baud_nominal"], 2000)
        # This only tests a known clean synthetic tone sequence, never a key protocol.
        decision = result["bits"]
        match = sum(a == b for a, b in zip(decision, expected))
        self.assertGreater(match / min(len(decision), len(expected)), .95)

    def test_noise_dc_is_marked_uncertain_not_as_verified_key(self):
        data = b"\x80\x80" * self.cfg.samples_per_frame * 2
        result = demodulate_fsk(data, self.cfg, 0, len(data) // 2)
        self.assertFalse(result["verified"])
        self.assertTrue(set(result["bits"]).issubset({"?"}))

    def test_demod_rejects_unaligned_samples_and_bad_ranges(self):
        with self.assertRaises(ValueError):
            demodulate_fsk(b"\x80", self.cfg, 0, 1)
        with self.assertRaises(ValueError):
            demodulate_fsk(b"\x80\x80" * 20, self.cfg, 10, 21)


class TraceArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "private-iq"
        self.archive = TraceArchive(self.root)
        self.cfg = RFConfig()
        self.event = {
            "kind": "burst", "at": datetime.now(timezone.utc).isoformat(),
            "start_s": .02, "end_s": .12, "duration_ms": 100.,
            "peak_snr_db": 30., "peak_rise_db": 19.,
        }
        self.raw = synthetic_cu8("01001101" * 25, self.cfg)

    def tearDown(self):
        self.temp.cleanup()

    def test_cu8_bytes_and_all_bit_decisions_are_saved_privately(self):
        trace = self.archive.save(self.raw, self.event, self.cfg, first_frame=0)
        self.assertIsNotNone(trace)
        identifier = trace["id"]
        self.assertEqual(self.archive.read(identifier)["id"], identifier)
        self.assertEqual((self.root.stat().st_mode & 0o777), 0o700)
        self.assertEqual((self.root / (identifier + ".cu8")).stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / (identifier + ".json")).stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / (identifier + ".cu8")).read_bytes(), self.raw)
        self.assertTrue(trace["decoder"]["bits"])
        self.assertEqual(len(trace["decoder"]["bits"]),
                         len(trace["decoder"]["confidence"]))
        self.assertFalse(trace["decoder"]["verified"])
        self.assertEqual(len(self.archive.list_recent()), 1)

    def test_paging_is_lossless_across_entire_recording(self):
        trace = self.archive.save(self.raw, self.event, self.cfg, 0)
        count = trace["sample_count"]
        samples = np.frombuffer(self.raw, dtype=np.uint8).reshape(-1, 2)
        rejoined_i, rejoined_q = [], []
        start = 0
        while start < count:
            page = self.archive.sample_page(trace["id"], start, 8192)
            rejoined_i.extend(page["i"])
            rejoined_q.extend(page["q"])
            self.assertLessEqual(len(page["spectrum"]), 256)
            start += page["count"]
        self.assertEqual(rejoined_i, samples[:, 0].tolist())
        self.assertEqual(rejoined_q, samples[:, 1].tolist())
        self.assertEqual(start, count)

    def test_trace_id_and_page_parameters_are_validated(self):
        for bad in ("../secret", "", "nothex", "F" * 32, "0" * 31):
            with self.assertRaises(ValueError):
                self.archive.read(bad)
        trace = self.archive.save(self.raw, self.event, self.cfg, 0)
        with self.assertRaises(ValueError):
            self.archive.sample_page(trace["id"], -1, 512)
        with self.assertRaises(ValueError):
            self.archive.sample_page(trace["id"], 0, 1000000)
        with self.assertRaises(ValueError):
            self.archive.sample_page(trace["id"], 0, True)
        with self.assertRaises(ValueError):
            self.archive.sample_page(trace["id"], trace["sample_count"], 512)
        self.assertIsNone(self.archive.sample_page("0" * 32, 0, 512))

    def test_archive_cap_does_not_delete_existing_data(self):
        first = self.archive.save(self.raw, self.event, self.cfg, 0)
        with patch("keyrfmonitor.iq_trace.MAX_ARCHIVE_BYTES", len(self.raw)):
            self.assertIsNone(self.archive.save(self.raw, self.event, self.cfg, 0))
        self.assertEqual(len(self.archive.list_recent()), 1)
        self.assertIsNotNone(self.archive.read(first["id"]))

    def test_collector_keeps_entire_burst_plus_before_after_frames(self):
        collector = TraceCollector(self.archive, self.cfg)
        size = self.cfg.samples_per_frame * 2
        for index in range(27):
            buf = b"\x80\x80" * self.cfg.samples_per_frame
            output = [self.event] if index == 25 else []
            collector.record_frame(index, buf, output)
        self.assertEqual(collector.saved, 1)
        data = self.archive.list_recent()[0]
        trace = self.archive.read(data["id"])
        self.assertLessEqual(trace["active_from_sample"], trace["active_to_sample"])
        self.assertGreater(trace["sample_count"],
                           trace["active_to_sample"] - trace["active_from_sample"])

    def test_collector_skips_when_per_session_cap_is_reached(self):
        collector = TraceCollector(self.archive, self.cfg, max_captures=1)
        for index in range(30):
            event = [self.event] if index in (25, 26) else []
            collector.record_frame(index, b"\x80\x80" * self.cfg.samples_per_frame, event)
        self.assertEqual(collector.saved, 1)
        self.assertEqual(collector.skipped, 1)


if __name__ == "__main__":
    unittest.main()
