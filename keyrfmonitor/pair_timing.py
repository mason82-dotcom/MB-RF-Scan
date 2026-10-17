"""Passive metadata-only timing analysis for *already saved* IQ burst windows.

No bitstream reconstruction, replay, key-command inference or SDR access.
The pairing is a timing heuristic, never a proof of transmitter identity.
"""
from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
import math
from statistics import median

# Calibrated from passive measurements and the existing burst-pair detector.
MIN_START_GAP_MS = 400
MAX_START_GAP_MS = 650
EXPECTED_START_GAP_MS = 513
MAX_ANALYZED_TRACES = 48


def _read_time(text: str) -> datetime:
    if not isinstance(text, str):
        raise ValueError("event_at must be a string")
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("event_at must contain a timezone")
    return value.astimezone(timezone.utc)


def _sanitize(entry: dict) -> dict:
    """Expose non-sensitive metrics only; never include stored bits or raw IQ."""
    if not isinstance(entry, dict):
        raise ValueError("Bad trace metadata")
    event_at = _read_time(entry["event_at"])
    duration = float(entry["duration_ms"])
    snr = float(entry["peak_snr_db"])
    count = entry["sample_count"]
    symbol_count = entry["symbol_count"]
    identity = entry["id"]
    if (not math.isfinite(duration) or duration <= 0 or duration > 1000
            or not math.isfinite(snr)
            or type(count) is not int or count <= 0
            or type(symbol_count) is not int or symbol_count < 0
            or not isinstance(identity, str) or len(identity) != 32
            or any(c not in "0123456789abcdef" for c in identity)):
        raise ValueError("Invalid trace summary")
    return {
        "id": identity,
        "event_at": event_at.isoformat(),
        "duration_ms": round(duration, 3),
        "peak_snr_db": round(snr, 2),
        "sample_count": count,
        "symbol_count": symbol_count,
        "_timestamp": event_at.timestamp(),
    }


def summarize_pair_timing(traces: list[dict]) -> dict:
    """Group chronologically adjacent bursts by start-to-start gap.

    A dynamic program maximizes disjoint pairs, then prefers candidate gaps
    closest to the preexisting 513 ms reference. It cannot identify actions,
    rolling codes, buttons, senders or cryptographic payloads.
    """
    if not isinstance(traces, list) or len(traces) > MAX_ANALYZED_TRACES:
        raise ValueError("Too many candidate traces")
    valid = []
    ignored = 0
    seen = set()
    for trace in traces:
        try:
            entry = _sanitize(trace)
            if entry["id"] in seen:
                ignored += 1
                continue
            seen.add(entry["id"])
            valid.append(entry)
        except (ValueError, KeyError, TypeError, OverflowError):
            ignored += 1
    valid.sort(key=lambda item: (item["_timestamp"], item["id"]))
    n = len(valid)
    starts = [item["_timestamp"] for item in valid]
    candidate_gap_ms = {}
    for index in range(n - 1):
        gap = (starts[index + 1] - starts[index]) * 1000
        if MIN_START_GAP_MS - 0.0001 <= gap <= MAX_START_GAP_MS + 0.0001:
            candidate_gap_ms[index] = gap

    @lru_cache(None)
    def solve(index: int) -> tuple[int, float, tuple[int, ...]]:
        if index >= n:
            return 0, 0.0, ()
        best = solve(index + 1)
        if index in candidate_gap_ms:
            later = solve(index + 2)
            candidate = (
                later[0] + 1,
                later[1] - abs(candidate_gap_ms[index] - EXPECTED_START_GAP_MS),
                (index,) + later[2],
            )
            # More pairs first, smaller total timing error second.
            if candidate[:2] > best[:2]:
                best = candidate
        return best

    pair_indices = solve(0)[2]
    member_indices = set()
    pairs = []
    for index in pair_indices:
        first = valid[index]
        second = valid[index + 1]
        member_indices.update((index, index + 1))
        gap_ms = round(candidate_gap_ms[index], 1)
        first_duration = first["duration_ms"]
        second_duration = second["duration_ms"]
        fields = ("id", "event_at", "duration_ms",
                  "peak_snr_db", "sample_count", "symbol_count")
        a = {field: first[field] for field in fields}
        b = {field: second[field] for field in fields}
        pairs.append({
            "index": len(pairs) + 1,
            "first": a,
            "second": b,
            "start_gap_ms": gap_ms,
            "quiet_gap_ms": round(max(0.0, gap_ms - first_duration), 1),
            "duration_difference_ms": round(second_duration - first_duration, 1),
            "pairing_basis": "adjacent_start_gap_only",
            "button_action": None,
            "verified": False,
        })
    fields = ("id", "event_at", "duration_ms",
              "peak_snr_db", "sample_count", "symbol_count")
    unpaired = [
        {key: trace[key] for key in fields}
        for i, trace in enumerate(valid) if i not in member_indices
    ]
    durations = [item["duration_ms"] for item in valid]
    duration_median = median(durations) if durations else None
    return {
        "source": "rtl_sdr",
        "analysis_mode": "archived_event_metadata_only",
        "ordering": "chronological_event_at",
        "trace_count": n,
        "ignored_invalid_or_duplicate": ignored,
        "pair_count": len(pairs),
        "unpaired_count": len(unpaired),
        "expected_start_gap_ms": EXPECTED_START_GAP_MS,
        "minimum_start_gap_ms": MIN_START_GAP_MS,
        "maximum_start_gap_ms": MAX_START_GAP_MS,
        "duration_median_ms": round(float(duration_median), 1)
            if duration_median is not None else None,
        "duration_range_ms": (
            [min(durations), max(durations)] if durations else None
        ),
        "pairs": pairs,
        "unpaired": unpaired,
        "button_mapping_verified": False,
        "radio_protocol_verified": False,
        "no_raw_bits_or_iq": True,
    }
