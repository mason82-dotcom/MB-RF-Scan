"""Deterministic, receive-only edge-quality tests on synthetic CU8 power."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from keyrfmonitor.dsp import RFConfig
from keyrfmonitor.iq_envelope import (
    analyze_cu8_envelope, compare_envelope_reports,
)
from keyrfmonitor.iq_trace import TraceArchive


RATE = RFConfig().sample_rate
SAMPLES_PER_MS = RATE // 1000


def cu8_envelope(total_ms=230, start_ms=20, end_ms=200) -> bytes:
    if not 0 <= start_ms <= end_ms <= total_ms:
        raise ValueError("Invalid synthetic interval")
    before = b"\x80\x80" * (start_ms * SAMPLES_PER_MS)
    during = b"\xd0\x80" * ((end_ms - start_ms) * SAMPLES_PER_MS)
    after = b"\x80\x80" * ((total_ms - end_ms) * SAMPLES_PER_MS)
    return before + during + after


class EnvelopeAnalysisTests(unittest.TestCase):
    def test_clean_180ms_signal_edges_from_1ms_amplitude_bins(self):
        raw = cu8_envelope()
        result = analyze_cu8_envelope(
            raw, RATE, 20 * SAMPLES_PER_MS, 200 * SAMPLES_PER_MS)
        self.assertEqual(result["edge_status"], "estimated")
        self.assertEqual(result["estimated_active_duration_ms"], 180.0)
        self.assertEqual(result["estimated_start_offset_ms"], 0)
        self.assertEqual(result["estimated_end_offset_ms"], 0)
        self.assertEqual(result["duration_minus_detector_ms"], 0)
        self.assertEqual(result["resolution_ms"], 1)
        self.assertEqual(len(result["profile"]), 230)
        self.assertTrue(result["waveform_is_aggregated_only"])
        self.assertGreater(result["signal_above_reference_db"], 30)
        self.assertFalse(result["frame_verified"])

    def test_extra_10ms_in_detector_is_only_annotated_tail(self):
        first = analyze_cu8_envelope(
            cu8_envelope(), RATE, 20 * SAMPLES_PER_MS, 200 * SAMPLES_PER_MS)
        second = analyze_cu8_envelope(
            cu8_envelope(), RATE, 20 * SAMPLES_PER_MS, 210 * SAMPLES_PER_MS)
        diff = compare_envelope_reports(first, second)
        self.assertEqual(second["detector_duration_ms"], 190)
        self.assertEqual(second["estimated_active_duration_ms"], 180)
        self.assertEqual(second["estimated_end_offset_ms"], -10)
        self.assertEqual(diff["detector_duration_difference_ms"], 10)
        self.assertEqual(diff["estimated_active_difference_ms"], 0)
        self.assertEqual(
            diff["assessment"], "compatible_with_detector_boundary_difference")
        self.assertFalse(diff["interpretation_verified"])

    def test_truly_longer_synthetic_activity_remains_longer(self):
        first = analyze_cu8_envelope(
            cu8_envelope(), RATE, 20 * SAMPLES_PER_MS, 200 * SAMPLES_PER_MS)
        second = analyze_cu8_envelope(
            cu8_envelope(end_ms=210), RATE,
            20 * SAMPLES_PER_MS, 210 * SAMPLES_PER_MS)
        result = compare_envelope_reports(first, second)
        self.assertEqual(result["estimated_active_difference_ms"], 10)
        self.assertEqual(
            result["assessment"], "compatible_with_active_duration_difference")

    def test_no_quiet_guard_declines_edge_estimate(self):
        raw = cu8_envelope(total_ms=200, start_ms=0, end_ms=200)
        result = analyze_cu8_envelope(raw, RATE, 0, 200 * SAMPLES_PER_MS)
        self.assertEqual(result["edge_status"], "insufficient_reference")
        self.assertIsNone(result["estimated_active_duration_ms"])
        self.assertTrue(all(p["excess_db"] is None for p in result["profile"]))

    def test_flat_noise_is_not_reported_as_a_signal(self):
        raw = b"\x80\x80" * (230 * SAMPLES_PER_MS)
        report = analyze_cu8_envelope(
            raw, RATE, 20 * SAMPLES_PER_MS, 200 * SAMPLES_PER_MS)
        self.assertEqual(report["edge_status"], "weak_signal_contrast")
        self.assertIsNone(report["estimated_active_duration_ms"])
        self.assertLess(report["signal_above_reference_db"], 1)

    def test_fragmented_signal_is_not_misreported_as_full_burst(self):
        raw = (
            b"\x80\x80" * (20 * SAMPLES_PER_MS)
            + b"\xd0\x80" * (80 * SAMPLES_PER_MS)
            + b"\x80\x80" * (25 * SAMPLES_PER_MS)
            + b"\xd0\x80" * (75 * SAMPLES_PER_MS)
            + b"\x80\x80" * (30 * SAMPLES_PER_MS)
        )
        result = analyze_cu8_envelope(
            raw, RATE, 20 * SAMPLES_PER_MS, 200 * SAMPLES_PER_MS)
        self.assertEqual(result["edge_status"], "fragmented_envelope")
        self.assertIsNone(result["estimated_active_duration_ms"])
        self.assertEqual(result["detected_segments"], 2)

    def test_invalid_sample_sizes_and_intervals_rejected(self):
        with self.assertRaises(ValueError):
            analyze_cu8_envelope(b"\x80", RATE, 0, 1)
        with self.assertRaises(ValueError):
            analyze_cu8_envelope(b"\x80\x80" * 100, RATE, 0, 100)
        with self.assertRaises(ValueError):
            analyze_cu8_envelope(cu8_envelope(), RATE, 9999999, 9999999)
        with self.assertRaises(ValueError):
            analyze_cu8_envelope(cu8_envelope(), 1_024_001, 0, 5000)
        with self.assertRaises(ValueError):
            analyze_cu8_envelope(cu8_envelope(), RATE, True, 100)
        with self.assertRaises(ValueError):
            analyze_cu8_envelope(b"\x80\x80" * (RATE + 1024), RATE, 0, 1024)


class ArchiveEnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.archive = TraceArchive(Path(self.temp.name) / "iq-traces")
        self.config = RFConfig()

    def tearDown(self):
        self.temp.cleanup()

    def record(self, duration_ms=180, signal_end_ms=200):
        raw = cu8_envelope(end_ms=signal_end_ms)
        event = {
            "kind": "burst",
            "at": datetime.now(timezone.utc).isoformat(),
            "start_s": 0.02,
            "end_s": 0.02 + duration_ms / 1000,
            "duration_ms": duration_ms,
            "peak_snr_db": 37,
            "peak_rise_db": 25,
        }
        meta = self.archive.save(raw, event, self.config, 0)
        self.assertIsNotNone(meta)
        return meta["id"], raw

    def test_envelope_read_only_and_no_raw_bits_leaked(self):
        identifier, original = self.record()
        meta_path = self.archive.path / (identifier + ".json")
        old_metadata = meta_path.read_bytes()
        files_before = set(self.archive.path.iterdir())
        result = self.archive.envelope_summary(identifier)
        self.assertEqual(result["edge_status"], "estimated")
        self.assertEqual(result["source"], "rtl_sdr")
        self.assertEqual(
            (self.archive.path / (identifier + ".cu8")).read_bytes(), original)
        self.assertEqual(meta_path.read_bytes(), old_metadata)
        self.assertEqual(set(self.archive.path.iterdir()), files_before)
        for forbidden in ("bits", "decoder", "iq_format",
                          "raw_iq", "hex_preview", "confidence"):
            self.assertNotIn(forbidden, result)

    def test_missing_trace_returns_none_bad_trace_and_corrupt_length_fail(self):
        self.assertIsNone(self.archive.envelope_summary("0" * 32))
        with self.assertRaises(ValueError):
            self.archive.envelope_summary("../../secret")
        identifier, _ = self.record()
        with (self.archive.path / (identifier + ".cu8")).open("ab") as file:
            file.write(b"\x00\x00")
        with self.assertRaises(ValueError):
            self.archive.envelope_summary(identifier)


if __name__ == "__main__":
    unittest.main()
