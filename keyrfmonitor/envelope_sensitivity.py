"""Conservative threshold sensitivity on already-aggregated CU8 RMS power.

Receive-only and metadata-only. Never modifies the original 8 dB+ envelope
decision, joins radio segments or exposes I/Q samples / key payloads.
The 0.1 dB quantization of the displayed profile limits accuracy.
"""
from __future__ import annotations

import math

SWEEP_DB = (6.0, 6.5, 7.0, 7.5, 8.0, 8.5, 9.0)
LOWER_CONFIRM_DB = (6.5, 7.0)
MIN_RUN_MS = 3
MAX_PROFILE_BINS = 1000


def _summarize_at_threshold(profile: list[dict], threshold: float,
                            detector_duration_ms: float) -> dict:
    """Describe consecutive >=threshold windows, no arbitrary gap bridging.

    One isolated below-threshold 1-ms bin is closed exactly as in the
    original 1-ms envelope estimator; wider gaps remain observable.
    """
    starts = [p["time_ms"] for p in profile]
    active = [p["excess_db"] is not None
              and p["excess_db"] >= threshold for p in profile]
    for i in range(1, len(active) - 1):
        if active[i - 1] and not active[i] and active[i + 1]:
            active[i] = True
    all_runs = []
    start = None
    for i, on in enumerate(active + [False]):
        if on and start is None:
            start = i
        elif not on and start is not None:
            if i - start >= MIN_RUN_MS:
                # Only count runs overlapping the stored detector interval.
                if starts[start] < detector_duration_ms and starts[i - 1] + 1 > 0:
                    all_runs.append((start, i))
            start = None
    gaps = [all_runs[i + 1][0] - all_runs[i][1]
            for i in range(len(all_runs) - 1)]
    longest_ms = max((b - a for a, b in all_runs), default=0)
    contiguous_ms = None
    start_offset = None
    end_offset = None
    if len(all_runs) == 1:
        a, b = all_runs[0]
        if a > 0 and b < len(profile) and b - a >= detector_duration_ms * 0.7:
            contiguous_ms = round(float(b - a), 1)
            start_offset = round(float(starts[a]), 1)
            end_offset = round(float(starts[b - 1] + 1 - detector_duration_ms), 1)
    return {
        "threshold_above_reference_db": threshold,
        "segment_count": len(all_runs),
        "longest_segment_ms": longest_ms,
        "largest_gap_ms": max(gaps, default=0),
        "contiguous_candidate_duration_ms": contiguous_ms,
        "start_offset_ms": start_offset,
        "end_offset_ms": end_offset,
    }


def summarize_threshold_sensitivity(envelope: dict) -> dict:
    """Compare a small fixed threshold sweep; retain original edge_status.

    This is an *additional* diagnostic, never a replacement edge estimator.
    A tentative contiguous estimate is returned only if two neighboring
    lower thresholds yield similar edge offsets, the noise reference exists
    and the nominal threshold is close to 8 dB. The source signal may still
    contain amplitude drops or intentional transmission gaps.
    """
    result = {
        "method": "relative_rms_threshold_sweep",
        "levels_db": list(SWEEP_DB),
        "profile_precision_db": 0.1,
        "rows": [],
        "status": "not_assessable",
        "candidate_threshold_db": None,
        "candidate_active_duration_ms": None,
        "candidate_start_offset_ms": None,
        "candidate_end_offset_ms": None,
        "confidence": "unverified",
        "original_edge_status_preserved": True,
        "automatic_threshold_change": False,
        "transmission_continuity_verified": False,
    }
    if envelope.get("edge_status") in (
        "insufficient_reference", "weak_signal_contrast", "no_sustained_activity",
    ):
        return result
    duration = envelope.get("detector_duration_ms")
    profile = envelope.get("profile")
    contrast = envelope.get("signal_above_reference_db")
    reference_count = envelope.get("reference_window_count")
    nominal = envelope.get("threshold_above_reference_db")
    if (not isinstance(profile, list) or not 8 <= len(profile) <= MAX_PROFILE_BINS
            or type(reference_count) is not int or reference_count < 8
            or type(duration) not in (int, float) or not math.isfinite(duration)
            or duration <= 0 or duration > 1000
            or type(contrast) not in (int, float) or not math.isfinite(contrast)
            or type(nominal) not in (int, float) or not math.isfinite(nominal)):
        return result
    last_time = -float("inf")
    for point in profile:
        if not isinstance(point, dict):
            return result
        t, v = point.get("time_ms"), point.get("excess_db")
        if (type(t) not in (int, float) or not math.isfinite(t)
                or t <= last_time or
                (v is not None and
                 (type(v) not in (int, float) or not math.isfinite(v)))):
            return result
        last_time = t
    # Empty/null profiles are not reinterpreted as valid signal activity.
    if not any(p["excess_db"] is not None for p in profile):
        return result
    rows = [_summarize_at_threshold(profile, level, duration)
            for level in SWEEP_DB]
    result["rows"] = rows
    by_level = {row["threshold_above_reference_db"]: row for row in rows}
    if envelope["edge_status"] == "estimated":
        result["status"] = "original_edges_estimated"
        return result
    if envelope["edge_status"] == "capture_edge_clipped":
        result["status"] = "capture_edge_clipped"
        return result
    if abs(nominal - 8.0) > 0.25:
        result["status"] = "nominal_threshold_outside_sweep"
        return result
    low, high = (by_level[level] for level in LOWER_CONFIRM_DB)
    nominal_row = by_level[8.0]
    stable_lower = (
        contrast >= 12.0 and
        envelope["edge_status"] == "fragmented_envelope" and
        nominal_row["segment_count"] >= 2 and
        low["contiguous_candidate_duration_ms"] is not None and
        high["contiguous_candidate_duration_ms"] is not None and
        abs(low["contiguous_candidate_duration_ms"] -
            high["contiguous_candidate_duration_ms"]) <= 2.0 and
        abs(low["start_offset_ms"] - high["start_offset_ms"]) <= 2.0 and
        abs(low["end_offset_ms"] - high["end_offset_ms"]) <= 2.0
    )
    if stable_lower:
        result.update({
            "status": "threshold_sensitive_fragmentation",
            "candidate_threshold_db": 7.0,
            "candidate_active_duration_ms": high["contiguous_candidate_duration_ms"],
            "candidate_start_offset_ms": high["start_offset_ms"],
            "candidate_end_offset_ms": high["end_offset_ms"],
        })
    elif envelope["edge_status"] == "fragmented_envelope":
        result["status"] = "fragmentation_not_resolved"
    else:
        result["status"] = "inconclusive"
    return result
