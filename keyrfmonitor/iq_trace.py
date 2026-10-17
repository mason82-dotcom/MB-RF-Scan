"""Opt-in, bounded storage and visualization of real received RTL-SDR CU8 I/Q.

This module is receive-only. It never drives hardware, transmits, replays
signals or interprets proprietary security protocols. Bit decisions are
uncertified hypotheses from a nominal FSK symbol clock, not validated codes.
Only explicitly started scans capture bursts; --no-iq disables raw recording.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import uuid

import numpy as np

from .dsp import RFConfig
from .bit_analysis import interpret_symbols, validate_options
from .iq_envelope import analyze_cu8_envelope
from .envelope_sensitivity import summarize_threshold_sensitivity

TRACE_NAME = re.compile(r"^[0-9a-f]{32}$")
MAX_CAPTURES_PER_SCAN = 48
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_FILES = 128
MIN_FREE_BYTES = 128 * 1024 * 1024
MAX_PAGE_SAMPLES = 8192
NOMINAL_BAUD = 2000


def demodulate_fsk(raw: bytes, config: RFConfig, active_start: int,
                   active_end: int, baud: int = NOMINAL_BAUD) -> dict:
    """Noncoherent phase-difference FSK hard decisions, with symbol uncertainty.

    A best-effort clock-phase search cannot establish proprietary framing,
    cryptographic validity or exact original symbol timing.
    """
    samples = np.frombuffer(raw, dtype=np.uint8)
    if len(samples) % 2:
        raise ValueError("Odd number of I/Q bytes")
    total = len(samples) // 2
    if not (0 <= active_start < active_end <= total):
        raise ValueError("Invalid active burst bounds")
    if type(baud) is not int or not 100 <= baud <= 20000:
        raise ValueError("Invalid nominal symbol rate")
    per_symbol = int(round(config.sample_rate / baud))
    data = samples.reshape(-1, 2)[active_start:active_end].astype(np.float32)
    z = (data[:, 0] - 127.5) + 1j * (data[:, 1] - 127.5)
    if len(z) < per_symbol * 2:
        return {"bits": "", "confidence": [], "timing_offset_samples": 0,
                "samples_per_symbol": per_symbol, "estimated_low_hz": None,
                "estimated_high_hz": None, "baud_nominal": baud,
                "verified": False, "method": "phase_discriminator_fsk"}
    frequency = (np.angle(z[1:] * np.conj(z[:-1])).astype(np.float32)
                 * (config.sample_rate / (2 * np.pi)))
    sums = np.concatenate(([0.0], np.cumsum(frequency, dtype=np.float64)))
    middle = (config.low_offset_hz + config.high_offset_hz) * 0.5
    halfspan = abs(config.high_offset_hz - config.low_offset_hz) * 0.5

    def phase_metric(phase: int):
        count = (len(frequency) - phase) // per_symbol
        if count < 1:
            return -1., np.zeros(0, dtype=float)
        starts = phase + np.arange(count, dtype=np.int64) * per_symbol
        means = (sums[starts + per_symbol] - sums[starts]) / per_symbol
        quality = np.clip(np.abs(means - middle) / max(halfspan, 1.0), 0, 1)
        return float(quality.mean()), means

    stride = max(1, per_symbol // 32)
    offsets = list(range(0, per_symbol, stride))
    best = max(offsets, key=lambda phase: phase_metric(phase)[0])
    lower = max(0, best - stride)
    upper = min(per_symbol, best + stride + 1)
    best = max(range(lower, upper), key=lambda phase: phase_metric(phase)[0])
    _, means = phase_metric(best)
    quality = np.clip(np.abs(means - middle) / max(halfspan, 1.0), 0, 1)
    bits = "".join(
        "?" if strength < 0.35 else "1" if value >= middle else "0"
        for value, strength in zip(means, quality)
    )
    low = means[(means < middle) & (quality >= 0.35)]
    high = means[(means >= middle) & (quality >= 0.35)]
    return {
        "bits": bits,
        "confidence": [round(float(x), 2) for x in quality],
        "timing_offset_samples": int(best),
        "samples_per_symbol": per_symbol,
        "estimated_low_hz": round(float(np.median(low)), 1) if len(low) else None,
        "estimated_high_hz": round(float(np.median(high)), 1) if len(high) else None,
        "baud_nominal": baud,
        "verified": False,
        "method": "phase_discriminator_fsk",
    }


class TraceArchive:
    """Private disk archive; API access is restricted to the configured LAN."""

    def __init__(self, path: Path):
        self.path = Path(path).expanduser()
        if self.path.is_symlink():
            raise ValueError("Trace directory must not be a symlink")
        self.path.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path, 0o700)

    @staticmethod
    def _id(value: str) -> str:
        if not isinstance(value, str) or not TRACE_NAME.fullmatch(value):
            raise ValueError("Invalid trace ID")
        return value

    @staticmethod
    def _private_write(path: Path, content: bytes) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    def _inventory(self) -> tuple[int, int]:
        files = list(self.path.glob("*.cu8"))
        total = sum(p.stat().st_size for p in files if p.is_file() and not p.is_symlink())
        return len(files), total

    def save(self, raw: bytes, event: dict, config: RFConfig,
             first_frame: int) -> dict | None:
        if event.get("kind") != "burst":
            return None
        if len(raw) % 2:
            raise ValueError("Invalid CU8 sample count")
        if len(raw) > config.sample_rate * 2:  # At most one second per burst
            raise ValueError("Trace exceeds one second")
        nfiles, used = self._inventory()
        free = shutil.disk_usage(self.path).free
        if (nfiles >= MAX_ARCHIVE_FILES or used + len(raw) > MAX_ARCHIVE_BYTES
                or free < len(raw) + MIN_FREE_BYTES):
            return None
        sample_start = first_frame * config.samples_per_frame
        start = max(0, round(event["start_s"] * config.sample_rate) - sample_start)
        end = min(len(raw) // 2, round(event["end_s"] * config.sample_rate) - sample_start)
        if end <= start:
            raise ValueError("Burst outside captured sample region")
        decisions = demodulate_fsk(raw, config, start, end)
        trace_id = uuid.uuid4().hex
        meta = {
            "id": trace_id,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "event_at": event["at"],
            "center_hz": config.center_hz,
            "sample_rate": config.sample_rate,
            "gain_db": config.gain_db,
            "low_offset_hz": config.low_offset_hz,
            "high_offset_hz": config.high_offset_hz,
            "sample_count": len(raw) // 2,
            "active_from_sample": start,
            "active_to_sample": end,
            "duration_ms": event["duration_ms"],
            "peak_rise_db": event["peak_rise_db"],
            "peak_snr_db": event["peak_snr_db"],
            "iq_format": "rtl_sdr_cu8_interleaved",
            "iq_bytes": len(raw),
            "bytes_saved": True,
            "decoder": decisions,
            "security_protocol_verified": False,
            "source": "rtl_sdr",
        }
        binary = self.path / (trace_id + ".cu8")
        temporary = self.path / (trace_id + ".json.tmp")
        final = self.path / (trace_id + ".json")
        try:
            self._private_write(binary, raw)
            self._private_write(temporary, json.dumps(
                meta, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8"))
            os.replace(temporary, final)
        except BaseException:
            binary.unlink(missing_ok=True)
            temporary.unlink(missing_ok=True)
            raise
        return meta

    def read(self, trace_id: str) -> dict | None:
        trace_id = self._id(trace_id)
        path = self.path / (trace_id + ".json")
        if not path.is_file() or path.is_symlink():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("id") != trace_id or data.get("source") != "rtl_sdr":
            raise ValueError("Trace metadata inconsistent")
        return data

    def list_recent(self, limit: int = 48) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid trace list size")
        results = []
        for entry in self.path.glob("*.json"):
            if entry.is_symlink() or not TRACE_NAME.fullmatch(entry.stem):
                continue
            try:
                meta = self.read(entry.stem)
                if meta:
                    results.append({
                        "id": meta["id"], "saved_at": meta["saved_at"],
                        "event_at": meta["event_at"], "duration_ms": meta["duration_ms"],
                        "peak_snr_db": meta["peak_snr_db"],
                        "sample_count": meta["sample_count"],
                        "symbol_count": len(meta["decoder"]["bits"]),
                    })
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                continue
        return sorted(results, key=lambda e: e["saved_at"], reverse=True)[:limit]

    def decode_hypothesis(self, trace_id: str, *, baud: int = NOMINAL_BAUD,
                          coding: str = "nrz", polarity: str = "normal",
                          alignment: int = 0) -> dict | None:
        """Inspect a bounded stored CU8 burst without modifying archived bytes.

        Only 1 second of samples is ever accepted. Uncertified symbols and
        unknown bytes remain explicitly uncertain in the response.
        """
        validate_options(baud, coding, polarity, alignment)
        meta = self.read(trace_id)
        if meta is None:
            return None
        rate = meta["sample_rate"]
        total = meta["sample_count"]
        if (type(rate) is not int or rate != RFConfig().sample_rate
                or type(total) is not int or not 2 <= total <= rate):
            raise ValueError("Unsupported I/Q sampling format or capture size")
        begin, end = meta["active_from_sample"], meta["active_to_sample"]
        if (type(begin) is not int or type(end) is not int
                or not 0 <= begin < end <= total):
            raise ValueError("Invalid active signal bounds")
        filename = self.path / (self._id(trace_id) + ".cu8")
        descriptor = os.open(filename, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            details = os.fstat(stream.fileno())
            if not stat.S_ISREG(details.st_mode) or details.st_size != total * 2:
                raise ValueError("IQ file and stored metadata differ")
            raw = stream.read(total * 2)
        config = RFConfig(
            center_hz=meta["center_hz"], sample_rate=rate,
            gain_db=meta["gain_db"], low_offset_hz=meta["low_offset_hz"],
            high_offset_hz=meta["high_offset_hz"])
        physical = demodulate_fsk(raw, config, begin, end, baud=baud)
        output = interpret_symbols(
            physical, baud=baud, coding=coding, polarity=polarity,
            alignment=alignment)
        output.update({
            "id": trace_id,
            "event_at": meta["event_at"],
            "duration_ms": meta["duration_ms"],
            "active_samples": end - begin,
            "sample_rate": rate,
            "source": "rtl_sdr",
            "modulation_hypothesis": "2-FSK",
        })
        return output

    def envelope_summary(self, trace_id: str) -> dict | None:
        """Read at most 1 second of CU8 and return aggregate energy metrics.

        Archived metadata and raw samples are never modified. A malformed
        or inconsistent file is rejected rather than silently reinterpreted.
        """
        meta = self.read(trace_id)
        if meta is None:
            return None
        rate = meta["sample_rate"]
        total = meta["sample_count"]
        first = meta["active_from_sample"]
        last = meta["active_to_sample"]
        if (type(rate) is not int or rate != RFConfig().sample_rate
                or type(total) is not int or not 2 <= total <= rate
                or type(first) is not int or type(last) is not int
                or not 0 <= first < last <= total):
            raise ValueError("Incompatible CU8 archive metadata")
        filename = self.path / (self._id(trace_id) + ".cu8")
        descriptor = os.open(filename, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            details = os.fstat(stream.fileno())
            if not stat.S_ISREG(details.st_mode) or details.st_size != total * 2:
                raise ValueError("Stored CU8 file size differs from archive metadata")
            raw = stream.read(total * 2)
        if len(raw) != total * 2:
            raise ValueError("Incomplete archived CU8 file")
        envelope = analyze_cu8_envelope(raw, rate, first, last)
        # Preserve the original threshold and edge diagnosis unchanged;
        # attach a separate, explicitly unverified sensitivity assessment.
        envelope["threshold_sensitivity"] = summarize_threshold_sensitivity(envelope)
        envelope.update({
            "id": self._id(trace_id),
            "event_at": meta["event_at"],
            "duration_ms": meta["duration_ms"],
            "peak_snr_db": meta["peak_snr_db"],
            "source": "rtl_sdr",
        })
        return envelope

    def sample_page(self, trace_id: str, start: int, count: int) -> dict | None:
        if type(start) is not int or start < 0 or type(count) is not int \
                or count not in (512, 2048, 8192):
            raise ValueError("Expected nonnegative start and count 512, 2048 or 8192")
        meta = self.read(trace_id)
        if meta is None:
            return None
        total = meta["sample_count"]
        if start >= total:
            raise ValueError("Sample start outside captured trace")
        expected = min(count, total - start)
        filename = self.path / (trace_id + ".cu8")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(filename, flags)
        try:
            with os.fdopen(descriptor, "rb") as stream:
                stream.seek(start * 2)
                raw = stream.read(expected * 2)
        finally:
            pass
        if len(raw) != expected * 2:
            raise ValueError("Stored CU8 trace has an unexpected length")
        arr = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 2)
        z = ((arr[:, 0].astype(np.float32) - 127.5)
             + 1j * (arr[:, 1].astype(np.float32) - 127.5))
        fft_size = min(1024, len(z))
        if fft_size >= 16:
            spectrum = np.fft.fftshift(np.fft.fft(z[:fft_size] * np.hanning(fft_size)))
            levels = 20 * np.log10(np.maximum(np.abs(spectrum), 1e-6))
            bins = min(256, fft_size)
            frequency = np.fft.fftshift(np.fft.fftfreq(fft_size, 1 / meta["sample_rate"]))
            idx = np.linspace(0, fft_size - 1, bins, dtype=np.int64)
            plot_spectrum = [{"khz": round(float(frequency[j] / 1000), 2),
                              "db": round(float(levels[j]), 2)} for j in idx]
        else:
            plot_spectrum = []
        return {
            "id": trace_id,
            "start": start,
            "count": expected,
            "total": total,
            "i": arr[:, 0].tolist(),
            "q": arr[:, 1].tolist(),
            "spectrum": plot_spectrum,
            "format": "unsigned_8_bit_cu8",
        }


class TraceCollector:
    """Ring buffer of 10-ms CU8 frames, retaining full observed event windows."""

    def __init__(self, archive: TraceArchive, config: RFConfig,
                 max_captures: int = MAX_CAPTURES_PER_SCAN):
        if type(max_captures) is not int or not 1 <= max_captures <= MAX_CAPTURES_PER_SCAN:
            raise ValueError("Invalid per-scan trace cap")
        self.archive = archive
        self.config = config
        self.max_captures = max_captures
        self.frames: deque[tuple[int, bytes]] = deque(maxlen=64)
        self.saved = 0
        self.skipped = 0
        self.archive_full = False

    def reset_scan(self) -> None:
        """Reset per-scan quota and ring without deleting stored captures."""
        self.frames.clear()
        self.saved = 0
        self.skipped = 0
        self.archive_full = False

    def record_frame(self, index: int, raw: bytes, events: list[dict]) -> None:
        if len(raw) != self.config.samples_per_frame * 2:
            raise ValueError("Wrong CU8 frame size")
        self.frames.append((index, raw))
        for event in events:
            if event.get("kind") != "burst":
                continue
            if self.saved >= self.max_captures or self.archive_full:
                self.skipped += 1
                continue
            start_frame = round(event["start_s"] * 1000 / self.config.frame_ms)
            end_frame = round(event["end_s"] * 1000 / self.config.frame_ms)
            first_needed = max(0, start_frame - 2)
            last_needed = min(index, end_frame + 2)
            selected = [(j, data) for j, data in self.frames
                        if first_needed <= j <= last_needed]
            if not selected or selected[0][0] > start_frame or selected[-1][0] < end_frame - 1:
                self.skipped += 1
                continue
            first_frame = selected[0][0]
            raw_capture = b"".join(data for _, data in selected)
            outcome = self.archive.save(raw_capture, event, self.config, first_frame)
            if outcome is None:
                self.archive_full = True
                self.skipped += 1
            else:
                self.saved += 1
