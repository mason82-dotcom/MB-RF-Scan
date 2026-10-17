"""Passive FSK energy monitor. Never extracts, stores or reconstructs key bits.

Only FFT energy summaries are retained, at 10 ms resolution. Raw complex IQ
samples are discarded immediately after a frame is processed.
"""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import math
import time
from typing import Callable

import numpy as np


@dataclass(frozen=True)
class RFConfig:
    center_hz: int = 433_920_000
    sample_rate: int = 1_024_000
    gain_db: float = 7.7
    low_offset_hz: float = -20_120.0
    high_offset_hz: float = 11_350.0
    tone_halfwidth_hz: float = 3_500.0
    fft_size: int = 1_024
    frame_ms: int = 10
    calibration_seconds: float = 1.5
    min_rise_db: float = 12.0
    min_snr_db: float = 8.0
    quiet_frames_to_finish: int = 3
    min_burst_ms: float = 80.0
    max_burst_ms: float = 350.0
    pair_start_min_s: float = 0.40
    pair_start_max_s: float = 0.65

    @property
    def samples_per_frame(self) -> int:
        count = int(self.sample_rate * self.frame_ms / 1000)
        if count <= 0 or count % self.fft_size:
            raise ValueError("sample_rate*frame_ms must be divisible by fft_size")
        return count


class PassiveDetector:
    """Classifies bursts from tone energy; no demodulation of the payload."""

    def __init__(self, config: RFConfig | None = None,
                 on_event: Callable[[dict], None] | None = None,
                 started_at: float | None = None):
        self.config = config or RFConfig()
        self.on_event = on_event
        self.started_at = started_at if started_at is not None else time.time()
        self.window = np.hanning(self.config.fft_size).astype(np.float32)
        freqs = np.fft.fftfreq(self.config.fft_size, 1 / self.config.sample_rate)
        self.low_mask = abs(freqs - self.config.low_offset_hz) <= self.config.tone_halfwidth_hz
        self.high_mask = abs(freqs - self.config.high_offset_hz) <= self.config.tone_halfwidth_hz
        self.noise_mask = ((abs(freqs) >= 45_000) & (abs(freqs) <= 150_000))
        if not self.low_mask.any() or not self.high_mask.any():
            raise ValueError("configured FSK tones outside FFT coverage")
        self.freqs = freqs
        self.pending_bytes = bytearray()
        self.frame_index = 0
        self.baseline_samples: list[float] = []
        self.calibration_frames = int(self.config.calibration_seconds * 1000 / self.config.frame_ms)
        self.baseline_db: float | None = None
        self.idle_levels: deque[float] = deque(maxlen=600)
        self.candidate: dict | None = None
        self.quiet_frames = 0
        self.events: list[dict] = []
        self.last_unpaired: dict | None = None
        self.pair_count = 0
        self.last_metric: dict = {}

    def process(self, raw_bytes: bytes) -> list[dict]:
        """Feed interleaved unsigned 8-bit I/Q bytes; return newly completed events."""
        self.pending_bytes.extend(raw_bytes)
        frame_bytes = self.config.samples_per_frame * 2
        new_events: list[dict] = []
        while len(self.pending_bytes) >= frame_bytes:
            frame = bytes(self.pending_bytes[:frame_bytes])
            del self.pending_bytes[:frame_bytes]
            metric = self._measure(frame)
            new_events.extend(self._update(metric))
        return new_events

    def _measure(self, frame: bytes) -> dict:
        a = np.frombuffer(frame, dtype=np.uint8).astype(np.float32).reshape(-1, 2)
        z = (a[:, 0] - 127.5) + 1j * (a[:, 1] - 127.5)
        frames = z.reshape(-1, self.config.fft_size)
        frames = frames - frames.mean(axis=1, keepdims=True)
        fft = np.fft.fft(frames * self.window, axis=1)
        psd = np.abs(fft).astype(np.float64) ** 2
        spec = psd.mean(axis=0)
        low = spec[self.low_mask]
        high = spec[self.high_mask]
        # Integrated tone energy; no per-symbol waveform, states or payload stored.
        total = float(low.sum() + high.sum())
        noise_per_bin = float(np.median(spec[self.noise_mask]))
        noise_equiv = noise_per_bin * (len(low) + len(high))
        signal_db = 10.0 * math.log10(max(total, 1e-12))
        snr_db = 10.0 * math.log10(max(total / max(noise_equiv, 1e-12), 1e-12))
        low_freq = float(np.sum(self.freqs[self.low_mask] * low) / max(low.sum(), 1e-12))
        high_freq = float(np.sum(self.freqs[self.high_mask] * high) / max(high.sum(), 1e-12))
        return {"power_db": signal_db, "snr_db": snr_db,
                "low_offset_hz": low_freq, "high_offset_hz": high_freq}

    def _emit(self, event: dict, result: list[dict]) -> None:
        self.events.append(event)
        result.append(event)
        if self.on_event:
            self.on_event(event.copy())

    def _update(self, m: dict) -> list[dict]:
        i = self.frame_index
        self.frame_index += 1
        result: list[dict] = []
        if self.baseline_db is None:
            self.baseline_samples.append(m["power_db"])
            if len(self.baseline_samples) >= self.calibration_frames:
                self.baseline_db = float(np.median(self.baseline_samples))
                self.idle_levels.extend(self.baseline_samples)
            self.last_metric = {"calibrating": True,
                                "progress": min(1, len(self.baseline_samples) / self.calibration_frames)}
            return result

        rise = m["power_db"] - self.baseline_db
        active = rise >= self.config.min_rise_db and m["snr_db"] >= self.config.min_snr_db
        self.last_metric = {"calibrating": False, "rise_db": round(rise, 1),
                            "snr_db": round(m["snr_db"], 1), "active": active}

        if active:
            if self.candidate is None:
                self.candidate = {"start_frame": i, "end_frame": i,
                                  "peak_rise_db": rise, "peak_snr_db": m["snr_db"],
                                  "low_weighted_sum": 0.0, "high_weighted_sum": 0.0,
                                  "weight_sum": 0.0}
            candidate = self.candidate
            candidate["end_frame"] = i
            candidate["peak_rise_db"] = max(candidate["peak_rise_db"], rise)
            candidate["peak_snr_db"] = max(candidate["peak_snr_db"], m["snr_db"])
            weight = max(rise, 0.1)
            candidate["low_weighted_sum"] += m["low_offset_hz"] * weight
            candidate["high_weighted_sum"] += m["high_offset_hz"] * weight
            candidate["weight_sum"] += weight
            self.quiet_frames = 0
        elif self.candidate is not None:
            self.quiet_frames += 1
            if self.quiet_frames >= self.config.quiet_frames_to_finish:
                candidate = self.candidate
                self.candidate = None
                self.quiet_frames = 0
                duration = (candidate["end_frame"] - candidate["start_frame"] + 1) * self.config.frame_ms
                if self.config.min_burst_ms <= duration <= self.config.max_burst_ms:
                    start = candidate["start_frame"] * self.config.frame_ms / 1000
                    end = (candidate["end_frame"] + 1) * self.config.frame_ms / 1000
                    weight = candidate["weight_sum"]
                    burst = {"kind": "burst", "start_s": round(start, 3),
                             "end_s": round(end, 3), "duration_ms": round(duration, 1),
                             "peak_rise_db": round(candidate["peak_rise_db"], 1),
                             "peak_snr_db": round(candidate["peak_snr_db"], 1),
                             "low_offset_hz": round(candidate["low_weighted_sum"] / weight),
                             "high_offset_hz": round(candidate["high_weighted_sum"] / weight),
                             "at": datetime.fromtimestamp(self.started_at + start,
                                                          timezone.utc).isoformat()}
                    self._emit(burst, result)
                    prev = self.last_unpaired
                    if prev is not None:
                        gap = start - prev["start_s"]
                        if self.config.pair_start_min_s <= gap <= self.config.pair_start_max_s:
                            self.pair_count += 1
                            pair = {"kind": "pair", "at": burst["at"],
                                    "start_s": prev["start_s"],
                                    "second_start_s": burst["start_s"],
                                    "start_gap_ms": round(gap * 1000, 1),
                                    "first_duration_ms": prev["duration_ms"],
                                    "second_duration_ms": burst["duration_ms"],
                                    "sequence": self.pair_count}
                            self._emit(pair, result)
                            self.last_unpaired = None
                        else:
                            self.last_unpaired = burst
                    else:
                        self.last_unpaired = burst
        else:
            # Slowly track changing background levels, never from active frames.
            self.idle_levels.append(m["power_db"])
            if len(self.idle_levels) >= 50 and i % 100 == 0:
                self.baseline_db = float(np.median(self.idle_levels))
        return result

    def finish(self) -> None:
        """Discard a partially captured event instead of guessing its duration."""
        self.candidate = None
        self.pending_bytes.clear()

    def status(self) -> dict:
        return {"processed_seconds": round(self.frame_index * self.config.frame_ms / 1000, 2),
                "baseline_ready": self.baseline_db is not None,
                "baseline_db": round(self.baseline_db, 1) if self.baseline_db is not None else None,
                "burst_count": sum(e["kind"] == "burst" for e in self.events),
                "pair_count": self.pair_count,
                "signal": self.last_metric.copy()}
