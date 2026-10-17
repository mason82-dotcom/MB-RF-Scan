"""Threshold sweep on archived RMS envelopes; never rewrite receiver output."""
import copy
import unittest

from keyrfmonitor.dsp import RFConfig
from keyrfmonitor.iq_envelope import analyze_cu8_envelope
from keyrfmonitor.envelope_sensitivity import summarize_threshold_sensitivity

RATE = RFConfig().sample_rate
BIN = RATE // 1000


def make_signal(*, dip_strength=129, cut=False):
    """180 ms annotated burst, 174 ms true activity, optional 19 ms dip.

    CU8 baseline I=Q=128 has power 0.5. Active I=131 gives ~14 dB
    above baseline; weak I=129 produces ~7.0 dB above baseline.
    """
    prefix = b"\x80\x80" * (25 * BIN)
    first = b"\x83\x80" * (45 * BIN)
    middle = bytes((128 if cut else dip_strength, 128)) * (19 * BIN)
    second = b"\x83\x80" * (110 * BIN)
    suffix = b"\x80\x80" * (31 * BIN)
    return prefix + first + middle + second + suffix


def examine(raw):
    return analyze_cu8_envelope(raw, RATE, 20 * BIN, 200 * BIN)


class SensitivityTests(unittest.TestCase):
    def test_field_observed_like_threshold_artifact_is_tentative(self):
        report = examine(make_signal())
        self.assertEqual(report["edge_status"], "fragmented_envelope")
        result = summarize_threshold_sensitivity(report)
        self.assertEqual(result["status"], "threshold_sensitive_fragmentation")
        self.assertEqual(result["candidate_threshold_db"], 7.0)
        self.assertEqual(result["candidate_active_duration_ms"], 174.0)
        self.assertEqual(result["candidate_start_offset_ms"], 5.0)
        self.assertEqual(result["candidate_end_offset_ms"], -1.0)
        by_threshold = {
            item["threshold_above_reference_db"]: item
            for item in result["rows"]
        }
        for level in (6.0, 6.5, 7.0):
            self.assertEqual(by_threshold[level]["segment_count"], 1)
            self.assertEqual(by_threshold[level]["longest_segment_ms"], 174)
        self.assertEqual(by_threshold[8.0]["segment_count"], 2)
        self.assertEqual(by_threshold[8.0]["largest_gap_ms"], 19)
        self.assertTrue(result["original_edge_status_preserved"])
        self.assertFalse(result["automatic_threshold_change"])
        self.assertFalse(result["transmission_continuity_verified"])

    def test_true_19ms_pause_does_not_get_joined(self):
        result = summarize_threshold_sensitivity(examine(make_signal(cut=True)))
        self.assertEqual(result["status"], "fragmentation_not_resolved")
        self.assertIsNone(result["candidate_active_duration_ms"])
        self.assertEqual(result["rows"][0]["segment_count"], 2)
        self.assertEqual(result["rows"][0]["largest_gap_ms"], 19)

    def test_original_8db_result_stays_untouched(self):
        original = examine(make_signal(dip_strength=131))
        before = copy.deepcopy(original)
        sweep = summarize_threshold_sensitivity(original)
        self.assertEqual(original, before)
        self.assertEqual(original["edge_status"], "estimated")
        self.assertEqual(sweep["status"], "original_edges_estimated")
        self.assertIsNone(sweep["candidate_active_duration_ms"])
        self.assertEqual(len(sweep["rows"]), 7)

    def test_no_adequate_quiet_guard_yields_no_candidate(self):
        raw = b"\x83\x80" * (200 * BIN)
        report = analyze_cu8_envelope(raw, RATE, 0, 200 * BIN)
        self.assertEqual(report["edge_status"], "insufficient_reference")
        data = summarize_threshold_sensitivity(report)
        self.assertEqual(data["rows"], [])
        self.assertEqual(data["status"], "not_assessable")

    def test_low_contrast_cannot_create_tentative_claim(self):
        raw = b"\x80\x80" * (230 * BIN)
        report = examine(raw)
        self.assertEqual(report["edge_status"], "weak_signal_contrast")
        data = summarize_threshold_sensitivity(report)
        self.assertEqual(data["rows"], [])
        self.assertIsNone(data["candidate_active_duration_ms"])

    def test_guardrail_rejects_nonfinite_and_oversized_profiles(self):
        normal = examine(make_signal())
        for bad in (
            dict(normal, profile=[dict(time_ms=float("nan"), excess_db=12)]),
            dict(normal, profile=[dict(time_ms=0, excess_db=float("inf"))]),
            dict(normal, profile=[{"time_ms": 0, "excess_db": 0}] * 1001),
            dict(normal, reference_window_count=0),
            dict(normal, signal_above_reference_db=float("inf")),
        ):
            with self.subTest(bad=str(list(bad.keys()))):
                report = summarize_threshold_sensitivity(bad)
                self.assertEqual(report["status"], "not_assessable")
                self.assertFalse(report["automatic_threshold_change"])

    def test_result_has_no_iq_symbols_or_codes(self):
        report = examine(make_signal())
        result = summarize_threshold_sensitivity(report)
        contents = str(result)
        for secret in ("raw_iq", "symbols", "key_code", "hex", "decoder", "bits"):
            self.assertNotIn(secret, contents)


if __name__ == "__main__":
    unittest.main()
