"""Minimal metadata store: records burst timing/levels, never IQ or payloads."""
from __future__ import annotations

from pathlib import Path
import os
import sqlite3
import threading

_ALLOWED = {
    "kind", "at", "start_s", "end_s", "duration_ms", "peak_rise_db",
    "peak_snr_db", "low_offset_hz", "high_offset_hz", "second_start_s",
    "start_gap_ms", "first_duration_ms", "second_duration_ms", "sequence",
    "source", "simulated", "rssi_dbm", "lqi"
}


class EventStore:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        self.lock = threading.RLock()
        # Do not create world-readable log files even with a permissive user's umask.
        if not self.path.exists():
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        os.chmod(self.path, 0o600)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.execute("""CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          kind TEXT NOT NULL CHECK(kind IN ('burst','pair')),
          at TEXT NOT NULL,
          start_s REAL,
          end_s REAL,
          duration_ms REAL,
          peak_rise_db REAL,
          peak_snr_db REAL,
          low_offset_hz REAL,
          high_offset_hz REAL,
          second_start_s REAL,
          start_gap_ms REAL,
          first_duration_ms REAL,
          second_duration_ms REAL,
          sequence INTEGER,
          source TEXT NOT NULL DEFAULT 'rtl_sdr',
          simulated INTEGER NOT NULL DEFAULT 0,
          rssi_dbm REAL,
          lqi INTEGER
        )""")
        # Non-destructive migration for v0.2.0 databases with existing events.
        present = {col[1] for col in self.db.execute("PRAGMA table_info(events)")}
        migrations = {
            "source": "TEXT NOT NULL DEFAULT 'rtl_sdr'",
            "simulated": "INTEGER NOT NULL DEFAULT 0",
            "rssi_dbm": "REAL",
            "lqi": "INTEGER",
        }
        for column, definition in migrations.items():
            if column not in present:
                self.db.execute(f"ALTER TABLE events ADD COLUMN {column} {definition}")
        self.db.commit()

    def insert(self, event: dict) -> None:
        if event.get("kind") not in ("burst", "pair"):
            raise ValueError("Unsupported event kind")
        if set(event) - _ALLOWED:
            raise ValueError("Unapproved data: raw data and codes are forbidden")
        event = event.copy()
        event.setdefault("source", "rtl_sdr")
        event.setdefault("simulated", False)
        if event["source"] not in ("rtl_sdr", "cc1101_sim"):
            raise ValueError("Unknown receiver source")
        if bool(event["simulated"]) != (event["source"] == "cc1101_sim"):
            raise ValueError("Simulation flag must match receiver source")
        if event.get("lqi") is not None and not (type(event["lqi"]) is int and 0 <= event["lqi"] <= 255):
            raise ValueError("LQI must be a byte-sized number")
        event["simulated"] = int(event["simulated"])
        fields = list(event)
        sql = ("INSERT INTO events (" + ",".join(fields) + ") VALUES (" +
               ",".join("?" for _ in fields) + ")")
        with self.lock:
            self.db.execute(sql, [event[k] for k in fields])
            self.db.commit()

    def recent(self, limit: int = 60) -> list[dict]:
        limit = max(1, min(int(limit), 200))
        with self.lock:
            cur = self.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
            columns = [field[0] for field in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]

    def close(self) -> None:
        with self.lock:
            self.db.close()
