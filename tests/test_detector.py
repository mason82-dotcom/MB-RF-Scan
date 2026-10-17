"""Deterministic synthetic RF tests; test waveforms are not real key captures."""
import tempfile
import unittest
from pathlib import Path

import numpy as np

from keyrfmonitor.dsp import PassiveDetector, RFConfig
from keyrfmonitor.storage import EventStore


class DetectorTests(unittest.TestCase):
    def setUp(self):
        self.cfg = RFConfig()

    def feed_synthetic(self, spans, total_seconds=4.8, *, amplitude=45.0, noise=1.5):
        detector = PassiveDetector(self.cfg, started_at=0)
        rng = np.random.default_rng(42)
        sr = self.cfg.sample_rate
        nsamp = self.cfg.samples_per_frame
        output = []
        for step in range(round(total_seconds * 1000 / self.cfg.frame_ms)):
            t = (step * nsamp + np.arange(nsamp)) / sr
            active = np.zeros(nsamp, dtype=bool)
            for a, b in spans:
                active |= ((t >= a) & (t < b))
            # 500us FSK symbol states (alternating plus occasional duplicate symbols).
            symbol = np.floor(t / .0005).astype(np.int64)
            state = np.isin(symbol % 7, (0, 1, 4))
            freq = np.where(state, 11_350.0, -20_120.0)
            phase = 2 * np.pi * np.cumsum(freq) / sr
            wave = amplitude * active * np.exp(1j * phase)
            real = 127.5 + wave.real + rng.normal(0, noise, nsamp)
            imag = 127.5 + wave.imag + rng.normal(0, noise, nsamp)
            iq = np.empty((nsamp, 2), dtype=np.uint8)
            iq[:, 0] = np.clip(real, 0, 255).astype(np.uint8)
            iq[:, 1] = np.clip(imag, 0, 255).astype(np.uint8)
            output.extend(detector.process(iq.tobytes()))
        detector.finish()
        return detector, output

    def test_synthetic_fsk_finds_two_pairs_and_four_bursts(self):
        intervals = [(1.8, 1.975), (2.315, 2.49), (3.5, 3.675), (4.007, 4.182)]
        detector, output = self.feed_synthetic(intervals)
        bursts = [x for x in output if x["kind"] == "burst"]
        pairs = [x for x in output if x["kind"] == "pair"]
        self.assertEqual(len(bursts), 4, bursts)
        self.assertEqual(len(pairs), 2, pairs)
        self.assertTrue(all(155 <= b["duration_ms"] <= 190 for b in bursts), bursts)
        self.assertTrue(all(495 <= p["start_gap_ms"] <= 530 for p in pairs))
        self.assertTrue(all(abs(b["low_offset_hz"] + 20120) < 2500 for b in bursts))
        self.assertTrue(all(abs(b["high_offset_hz"] - 11350) < 2500 for b in bursts))

    def test_noise_only_produces_no_events(self):
        detector, output = self.feed_synthetic([], 2.8)
        self.assertEqual(output, [])
        self.assertEqual(detector.pair_count, 0)

    def test_short_glitch_is_not_a_valid_burst(self):
        _, output = self.feed_synthetic([(1.9, 1.92)], 2.7)
        self.assertEqual(output, [])

    def test_chunk_aggregation(self):
        detector = PassiveDetector(self.cfg)
        self.assertEqual(detector.process(b'\x80\x80' * 300), [])
        self.assertEqual(detector.frame_index, 0)
        detector.process(b'\x80\x80' * (self.cfg.samples_per_frame - 300))
        self.assertEqual(detector.frame_index, 1)


class StorageTests(unittest.TestCase):
    def test_only_explicit_metadata_allowed(self):
        with tempfile.TemporaryDirectory() as temp:
            store = EventStore(Path(temp) / "private" / "events.db")
            store.insert({"kind": "burst", "at": "2026-10-17T14:00:00Z",
                          "duration_ms": 175.0})
            self.assertEqual(store.recent()[0]["duration_ms"], 175.0)
            with self.assertRaises(ValueError):
                store.insert({"kind": "burst", "at": "now", "raw_key": "deadbeef"})
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)
            store.close()


if __name__ == "__main__":
    unittest.main()
