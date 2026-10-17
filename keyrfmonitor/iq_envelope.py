"""Receive-only envelope quality metrics from bounded, archived CU8 bursts.

The report contains only 1 ms aggregate power estimates and edge offsets.
No I/Q samples, symbols, key codes, framing or transmission are returned.
Signal-on edges remain estimates, not hardware-timestamped ground truth.
"""
from __future__ import annotations

import math

import numpy as np

MAX_SIGNAL_SECONDS = 1
RESOLUTION_MS = 1
MIN_REFERENCE_WINDOWS = 8
MIN_ACTIVE_WINDOWS = 3


def analyze_cu8_envelope(raw: bytes, sample_rate: int, active_start: int,
                         active_end: int) -> dict:
    """Estimate energy on/off edges relative to the stored detector interval.

    Uses power from I/Q centred at 127.5, measured per 1 ms aggregate.
    The median of the quiet guard sections estimates the local reference.
    If guard coverage or signal contrast is inadequate, explicitly decline
    to estimate edges rather than manufacturing precision.
    """
    if (type(sample_rate) is not int or sample_rate <= 0
            or sample_rate % 1000 != 0):
        raise ValueError("Sample rate must support exact 1 ms intervals")
    if not isinstance(raw, bytes) or len(raw) % 2:
        raise ValueError("Invalid interleaved CU8 bytes")
    total_samples = len(raw) // 2
    if (total_samples > MAX_SIGNAL_SECONDS * sample_rate
            or type(active_start) is not int or type(active_end) is not int
            or not 0 <= active_start < active_end <= total_samples):
        raise ValueError("Invalid capture length or annotated bounds")
    per_bin = sample_rate // 1000
    n_bins = total_samples // per_bin
    if n_bins < 8:
        raise ValueError("Not enough CU8 samples for an envelope")
    data = np.frombuffer(raw, dtype=np.uint8)[:n_bins * per_bin * 2]
    data = data.reshape(n_bins, per_bin, 2).astype(np.float32)
    i = data[:, :, 0] - 127.5
    q = data[:, :, 1] - 127.5
    power = (np.mean(i * i + q * q, axis=1, dtype=np.float64))
    power_db = 10.0 * np.log10(np.maximum(power, 1e-10))
    declared_start_ms = active_start * 1000.0 / sample_rate
    declared_end_ms = active_end * 1000.0 / sample_rate
    start_bin = int(math.floor(declared_start_ms))
    end_bin = int(math.ceil(declared_end_ms))
    # Exclude the 2 ms near each annotated edge from the noise estimate.
    guard_head = power[:max(0, start_bin - 2)]
    guard_tail = power[min(n_bins, end_bin + 2):]
    guard_count = len(guard_head) + len(guard_tail)
    profile = [
        {"time_ms": round(index - declared_start_ms, 2),
         "excess_db": None}
        for index in range(n_bins)
    ]
    base = {
        "method": "cu8_1ms_rms_envelope",
        "resolution_ms": RESOLUTION_MS,
        "detector_duration_ms": round(declared_end_ms - declared_start_ms, 2),
        "capture_duration_ms": round(total_samples * 1000.0 / sample_rate, 2),
        "pre_guard_ms": round(declared_start_ms, 2),
        "post_guard_ms": round(total_samples * 1000.0 / sample_rate - declared_end_ms, 2),
        "reference_window_count": guard_count,
        "edge_status": "insufficient_reference",
        "noise_reference_db": None,
        "signal_above_reference_db": None,
        "threshold_above_reference_db": None,
        "estimated_start_offset_ms": None,
        "estimated_end_offset_ms": None,
        "estimated_active_duration_ms": None,
        "duration_minus_detector_ms": None,
        "detected_segments": 0,
        "edge_uncertainty_at_least_ms": RESOLUTION_MS,
        "profile": profile,
        "waveform_is_aggregated_only": True,
        "symbol_clock_verified": False,
        "frame_verified": False,
    }
    if guard_count < MIN_REFERENCE_WINDOWS:
        return base
    quiet_power = np.concatenate((guard_head, guard_tail))
    reference = float(np.median(quiet_power))
    reference_db = 10.0 * math.log10(max(reference, 1e-10))
    if end_bin <= start_bin or start_bin >= n_bins:
        return base
    peak_power = float(np.percentile(power[start_bin:min(n_bins, end_bin)], 95))
    peak_db = 10.0 * math.log10(max(peak_power, 1e-10))
    contrast_db = peak_db - reference_db
    for index in range(n_bins):
        profile[index]["excess_db"] = round(
            max(-30.0, min(65.0, float(power_db[index]) - reference_db)), 1)
    base.update({
        "noise_reference_db": round(reference_db, 2),
        "signal_above_reference_db": round(contrast_db, 2),
        "profile": profile,
    })
    if contrast_db < 10:
        base["edge_status"] = "weak_signal_contrast"
        return base
    threshold_db = max(reference_db + 8.0, peak_db - 15.0)
    base["threshold_above_reference_db"] = round(threshold_db - reference_db, 2)
    active = (power_db >= threshold_db).tolist()

    # Close isolated, one-bin negative decisions, but do not fuse larger gaps.
    for k in range(1, n_bins - 1):
        if active[k - 1] and not active[k] and active[k + 1]:
            active[k] = True

    runs = []
    index = 0
    while index < n_bins:
        if not active[index]:
            index += 1
            continue
        start = index
        while index < n_bins and active[index]:
            index += 1
        if index - start >= MIN_ACTIVE_WINDOWS:
            overlap = max(0, min(index, end_bin) - max(start, start_bin))
            runs.append((start, index, overlap))
    base["detected_segments"] = len(runs)
    relevant = [r for r in runs if r[2] > 0]
    if not relevant:
        base["edge_status"] = "no_sustained_activity"
        return base
    first, last, _ = max(relevant, key=lambda r: (r[2], r[1] - r[0]))
    if first == 0 or last == n_bins:
        base["edge_status"] = "capture_edge_clipped"
        return base
    # If an active segment does not span the overwhelming majority of the
    # interval, its boundaries cannot describe the entire burst.
    if (last - first < (declared_end_ms - declared_start_ms) * 0.7
            or len([r for r in relevant if r != (first, last, _)]) > 0):
        base["edge_status"] = "fragmented_envelope"
        return base
    base.update({
        "edge_status": "estimated",
        "estimated_start_offset_ms": round(first - declared_start_ms, 2),
        "estimated_end_offset_ms": round(last - declared_end_ms, 2),
        "estimated_active_duration_ms": round(float(last - first), 2),
        "duration_minus_detector_ms": round(
            float(last - first) - (declared_end_ms - declared_start_ms), 2),
    })
    return base


def compare_envelope_reports(first: dict, second: dict) -> dict:
    """Describe observable differences without asserting security protocol."""
    annotated_difference = round(
        second["detector_duration_ms"] - first["detector_duration_ms"], 2)
    result = {
        "detector_duration_difference_ms": annotated_difference,
        "estimated_active_difference_ms": None,
        "start_offset_difference_ms": None,
        "end_offset_difference_ms": None,
        "assessment": "insufficient_edge_evidence",
        "interpretation_verified": False,
    }
    if first["edge_status"] != "estimated" or second["edge_status"] != "estimated":
        return result
    active_difference = round(
        second["estimated_active_duration_ms"] - first["estimated_active_duration_ms"], 2)
    result.update({
        "estimated_active_difference_ms": active_difference,
        "start_offset_difference_ms": round(
            second["estimated_start_offset_ms"] - first["estimated_start_offset_ms"], 2),
        "end_offset_difference_ms": round(
            second["estimated_end_offset_ms"] - first["estimated_end_offset_ms"], 2),
    })
    if abs(annotated_difference) < 8.0:
        result["assessment"] = "detector_durations_similar"
    elif abs(active_difference) <= 3.0:
        result["assessment"] = "compatible_with_detector_boundary_difference"
    elif (abs(active_difference) >= 7.0
          and math.copysign(1, active_difference) == math.copysign(1, annotated_difference)):
        result["assessment"] = "compatible_with_active_duration_difference"
    else:
        result["assessment"] = "undetermined"
    return result
