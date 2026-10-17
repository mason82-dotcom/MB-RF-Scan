"""Read-only grouping from stored RF event metadata; no key bits inspected."""
from datetime import datetime, timedelta, timezone
import unittest

from keyrfmonitor.pair_timing import (
    MAX_ANALYZED_TRACES, summarize_pair_timing,
)


START = datetime(2026, 10, 17, 18, 0, tzinfo=timezone.utc)


def entry(index: int, start_ms: int, duration: float = 180.0,
          snr: float = 39.0) -> dict:
    return {
        "id": f"{index:032x}",
        "event_at": (START + timedelta(milliseconds=start_ms)).isoformat(),
        "saved_at": (START + timedelta(seconds=index)).isoformat(),
        "duration_ms": duration,
        "peak_snr_db": snr,
        "sample_count": 235520,
        "symbol_count": 359 if duration == 180 else 379,
        "decoder": {"bits": "0101" * 90},
        "private_iq_bytes": b"private_example_never_return",
        "key_code": "never_return",
    }


class PairTimingTests(unittest.TestCase):
    def test_eight_bursts_from_four_short_press_like_events(self):
        # Metadata deliberately arrived in reverse order (as /api/iq/traces).
        times = [0, 513, 2350, 2863, 5100, 5613, 7950, 8463]
        files = [
            entry(i + 1, start, duration=190.0 if i == 6 else 180.0)
            for i, start in enumerate(times)
        ]
        payload = summarize_pair_timing(list(reversed(files)))
        self.assertEqual(payload["trace_count"], 8)
        self.assertEqual(payload["pair_count"], 4)
        self.assertEqual(payload["unpaired_count"], 0)
        self.assertEqual(payload["duration_median_ms"], 180.0)
        self.assertEqual(payload["duration_range_ms"], [180.0, 190.0])
        self.assertEqual(
            [pair["start_gap_ms"] for pair in payload["pairs"]],
            [513.0, 513.0, 513.0, 513.0])
        self.assertEqual(payload["pairs"][0]["first"]["id"], files[0]["id"])
        self.assertEqual(payload["pairs"][3]["first"]["id"], files[6]["id"])
        self.assertEqual(payload["pairs"][3]["duration_difference_ms"], -10)
        self.assertEqual(payload["pairs"][3]["quiet_gap_ms"], 323.0)
        self.assertFalse(payload["button_mapping_verified"])
        self.assertFalse(payload["radio_protocol_verified"])
        self.assertTrue(payload["no_raw_bits_or_iq"])
        raw = str(payload)
        for secret in ("01010101", "private_example", "never_return",
                       "key_code", "decoder", "hex_preview"):
            self.assertNotIn(secret, raw)

    def test_unpaired_single_burst_and_no_traces(self):
        empty = summarize_pair_timing([])
        self.assertEqual(empty["pair_count"], 0)
        self.assertEqual(empty["trace_count"], 0)
        self.assertIsNone(empty["duration_median_ms"])
        one = summarize_pair_timing([entry(1, 0)])
        self.assertEqual(one["pair_count"], 0)
        self.assertEqual(one["unpaired_count"], 1)
        self.assertEqual(one["unpaired"][0]["id"], entry(1, 0)["id"])

    def test_only_adjacent_start_gaps_400_through_650_milliseconds(self):
        sequence = [
            entry(1, 0), entry(2, 399),  # Too early
            entry(3, 1049),              # Exactly 650 ms from #2
            entry(4, 1730),              # Too late
        ]
        data = summarize_pair_timing(sequence)
        self.assertEqual(data["pair_count"], 1)
        self.assertEqual(data["pairs"][0]["first"]["id"], sequence[1]["id"])
        self.assertEqual(data["pairs"][0]["second"]["id"], sequence[2]["id"])
        self.assertEqual(data["unpaired_count"], 2)

    def test_overlapping_candidates_choose_only_disjoint_pairs(self):
        # A greedy algorithm might consume an edge that blocks two pairs.
        files = [entry(1, 0), entry(2, 513), entry(3, 1026),
                 entry(4, 1539)]
        result = summarize_pair_timing(files)
        self.assertEqual(result["pair_count"], 2)
        self.assertEqual(result["pairs"][0]["first"]["id"], files[0]["id"])
        self.assertEqual(result["pairs"][1]["first"]["id"], files[2]["id"])

    def test_duplicate_and_malformed_timestamps_are_ignored(self):
        valid = entry(1, 0)
        bad = [
            dict(entry(3, 1500), event_at="2026-10-17T18:00:01"),
            dict(entry(4, 1500), event_at="garbled"),
            dict(entry(5, 1500), duration_ms=float("nan")),
            dict(entry(6, 1500), symbol_count=-1),
            valid.copy(),
        ]
        result = summarize_pair_timing([valid, *bad, entry(2, 513)])
        self.assertEqual(result["trace_count"], 2)
        self.assertEqual(result["ignored_invalid_or_duplicate"], 5)
        self.assertEqual(result["pair_count"], 1)

    def test_timezones_normalized(self):
        x = entry(1, 0)
        y = entry(2, 513)
        y["event_at"] = "2026-10-17T20:00:00.513+02:00"
        result = summarize_pair_timing([x, y])
        self.assertEqual(result["pair_count"], 1)

    def test_limit_and_bad_input_rejected(self):
        with self.assertRaises(ValueError):
            summarize_pair_timing(None)
        with self.assertRaises(ValueError):
            summarize_pair_timing([entry(i, i * 1000) for i in range(49)])
        self.assertEqual(MAX_ANALYZED_TRACES, 48)


if __name__ == "__main__":
    unittest.main()
