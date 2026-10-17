"""Isolated synthetic FSK lab: local generated codes and virtual SDR replay only.

No radio transmission, hardware driver calls, vehicle capture import or over-air replay.
The generated pseudo-random symbols are not extracted from any received RF signal.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import os
import secrets
import sqlite3
import threading

import numpy as np

from .dsp import PassiveDetector, RFConfig

CODE_BITS = 350


class LabStore:
    """Persistent local store for application-generated *synthetic* test vectors."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        if not self.path.exists():
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        os.chmod(self.path, 0o600)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.execute("""CREATE TABLE IF NOT EXISTS lab_codes (
            id INTEGER PRIMARY KEY,
            created_at TEXT NOT NULL,
            origin TEXT NOT NULL CHECK(origin='synthetic'),
            bits TEXT NOT NULL,
            bit_count INTEGER NOT NULL CHECK(bit_count=350)
        )""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS lab_runs (
            id INTEGER PRIMARY KEY,
            code_id INTEGER NOT NULL REFERENCES lab_codes(id),
            created_at TEXT NOT NULL,
            bursts INTEGER NOT NULL,
            pairs INTEGER NOT NULL,
            simulated INTEGER NOT NULL CHECK(simulated=1)
        )""")
        self.db.commit()

    @staticmethod
    def _public(row) -> dict:
        id_, created_at, origin, bits = row
        return {"id": id_, "created_at": created_at, "origin": origin,
                "bit_count": len(bits), "hex_preview": format(int(bits, 2), '088x')[:20] + '…'}

    def create(self) -> dict:
        # Random test vector is generated internally; there is deliberately no
        # API to import captured or external security-device bit streams.
        bits = format(secrets.randbits(CODE_BITS), f'0{CODE_BITS}b')
        now = datetime.now(timezone.utc).isoformat()
        with self.lock:
            cur = self.db.execute("SELECT COUNT(*) FROM lab_codes")
            if cur.fetchone()[0] >= 100:
                raise ValueError("Maximal 100 synthetische Testcodes. Bitte zuerst bereinigen.")
            cur = self.db.execute("INSERT INTO lab_codes(created_at,origin,bits,bit_count) VALUES (?,?,?,?)",
                                  (now, 'synthetic', bits, CODE_BITS))
            self.db.commit()
            return {"id": cur.lastrowid, "created_at": now, "origin": "synthetic",
                    "bit_count": CODE_BITS, "hex_preview": format(int(bits,2), '088x')[:20] + '…'}

    def list_codes(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT id,created_at,origin,bits FROM lab_codes ORDER BY id DESC LIMIT 100").fetchall()
        return [self._public(r) for r in rows]

    def read_bits(self, code_id: int) -> str:
        if isinstance(code_id, bool) or not isinstance(code_id, int) or code_id < 1:
            raise ValueError("Ungültige Testcode-ID")
        with self.lock:
            row = self.db.execute("SELECT bits FROM lab_codes WHERE id=? AND origin='synthetic'", (code_id,)).fetchone()
        if row is None:
            raise KeyError("Testcode nicht gefunden")
        bits = row[0]
        if len(bits) != CODE_BITS or any(bit not in '01' for bit in bits):
            raise ValueError("Ungültiger Testcode in Datenbank")
        return bits

    def record_run(self, code_id: int, bursts: int, pairs: int) -> None:
        with self.lock:
            self.db.execute("INSERT INTO lab_runs(code_id,created_at,bursts,pairs,simulated) VALUES (?,?,?,?,1)",
                            (code_id, datetime.now(timezone.utc).isoformat(), bursts, pairs))
            self.db.commit()

    def close(self):
        with self.lock:
            self.db.close()


def virtual_replay(bits: str, config: RFConfig | None = None) -> dict:
    """Send a synthetic test sequence *only* to the in-process virtual receiver.

    Returns burst/pair counts; does not yield an IQ file or payload output.
    Never invokes rtl_sdr, a TX driver or a physical transmitter.
    """
    if len(bits) != CODE_BITS or any(x not in '01' for x in bits):
        raise ValueError("Only application-generated 350-bit lab vectors are supported")
    cfg = config or RFConfig()
    detector = PassiveDetector(cfg, started_at=0)
    sr = cfg.sample_rate
    samples = cfg.samples_per_frame
    rng = np.random.default_rng(2026)
    payload = np.fromiter((c == '1' for c in bits), dtype=np.bool_, count=CODE_BITS)
    start_times = (1.75, 2.263)
    duration = CODE_BITS / 2000
    phase_end = 0.0
    total_frames = int(3.2 * 1000 / cfg.frame_ms)
    for i in range(total_frames):
        t = (i * samples + np.arange(samples)) / sr
        active = np.zeros(samples, dtype=np.bool_)
        freq = np.zeros(samples, dtype=np.float32)
        for start in start_times:
            window = (t >= start) & (t < start + duration)
            if not np.any(window):
                continue
            symbol = np.minimum(((t[window] - start) * 2000).astype(np.int32), CODE_BITS-1)
            freq[window] = np.where(payload[symbol], cfg.high_offset_hz, cfg.low_offset_hz)
            active |= window
        phase = phase_end + np.cumsum(freq, dtype=np.float64) * (2 * np.pi / sr)
        phase_end = float(phase[-1])
        wave = 45 * active * np.exp(1j * phase)
        channels = np.empty((samples, 2), dtype=np.uint8)
        channels[:, 0] = np.clip(127.5 + wave.real + rng.normal(0, 1.5, samples), 0, 255).astype(np.uint8)
        channels[:, 1] = np.clip(127.5 + wave.imag + rng.normal(0, 1.5, samples), 0, 255).astype(np.uint8)
        detector.process(channels.tobytes())
    detector.finish()
    events = detector.events
    return {"medium": "virtual_only", "rf_transmitted": False,
            "bursts": sum(event['kind']=='burst' for event in events),
            "pairs": sum(event['kind']=='pair' for event in events),
            "duration_s": round(total_frames * cfg.frame_ms/1000, 3),
            "test_bits": CODE_BITS}
