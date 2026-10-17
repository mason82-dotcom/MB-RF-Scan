"""Minimal metadata store: records burst timing/levels, never IQ or payloads."""
from __future__ import annotations

from pathlib import Path
import os
import math
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

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
        self.db.execute("""CREATE TABLE IF NOT EXISTS scan_sessions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          started_at TEXT NOT NULL,
          finished_at TEXT,
          planned_seconds INTEGER NOT NULL CHECK(planned_seconds BETWEEN 20 AND 300),
          result TEXT NOT NULL CHECK(result IN ('running','completed','interrupted','error')),
          restoration TEXT NOT NULL CHECK(restoration IN ('pending','verified','failed','not_required')),
          burst_count INTEGER NOT NULL DEFAULT 0 CHECK(burst_count >= 0),
          pair_count INTEGER NOT NULL DEFAULT 0 CHECK(pair_count >= 0)
        )""")
        # Non-destructive migration of real v0.5 databases, including session #1.
        columns = {col[1] for col in self.db.execute("PRAGMA table_info(scan_sessions)")}
        analytics_columns = {
            "avg_duration_ms": "REAL",
            "min_duration_ms": "REAL",
            "max_duration_ms": "REAL",
            "avg_snr_db": "REAL",
            "peak_snr_db": "REAL",
            "avg_rise_db": "REAL",
            "peak_rise_db": "REAL",
            "stats_version": "INTEGER NOT NULL DEFAULT 0",
        }
        for column, definition in analytics_columns.items():
            if column not in columns:
                self.db.execute(f"ALTER TABLE scan_sessions ADD COLUMN {column} {definition}")
        # Only IDs are correlated; no additional RF payload or raw samples.
        self.db.execute("""CREATE TABLE IF NOT EXISTS scan_session_events (
            session_id INTEGER NOT NULL,
            event_id INTEGER NOT NULL PRIMARY KEY
        )""")
        self.db.execute("""CREATE INDEX IF NOT EXISTS idx_scan_events_session
                           ON scan_session_events(session_id)""")
        self.db.commit()
        self._active_session_id: int | None = None

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
            cursor = self.db.execute(sql, [event[k] for k in fields])
            if (self._active_session_id is not None
                    and event["source"] == "rtl_sdr" and not event["simulated"]):
                self.db.execute(
                    "INSERT INTO scan_session_events(session_id,event_id) VALUES (?,?)",
                    (self._active_session_id, cursor.lastrowid))
            self.db.commit()

    def recent(self, limit: int = 60) -> list[dict]:
        limit = max(1, min(int(limit), 200))
        with self.lock:
            cur = self.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
            columns = [field[0] for field in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]


    def summary(self, hours: int = 24, *, now: datetime | None = None) -> dict:
        """Real RTL-SDR burst statistics only; no simulated events or raw signals."""
        if type(hours) is not int or hours not in (6, 24, 168):
            raise ValueError("History window must be 6, 24 or 168 hours")
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError("History needs timezone-aware UTC timestamps")
        now = now.astimezone(timezone.utc)
        start = now - timedelta(hours=hours)
        parameters = ("rtl_sdr", start.isoformat(), now.isoformat())
        where = ("source=? AND simulated=0 AND "
                 "julianday(at)>=julianday(?) AND julianday(at)<=julianday(?)")
        with self.lock:
            summary = self.db.execute(f"""
                SELECT COUNT(CASE WHEN kind='burst' THEN 1 END),
                       COUNT(CASE WHEN kind='pair' THEN 1 END),
                       AVG(CASE WHEN kind='burst' THEN duration_ms END),
                       AVG(CASE WHEN kind='burst' THEN peak_snr_db END),
                       MAX(CASE WHEN kind='burst' THEN peak_snr_db END)
                FROM events WHERE {where}
            """, parameters).fetchone()
            grouped = self.db.execute(f"""
                SELECT strftime('%Y-%m-%dT%H:00:00Z', at),kind,COUNT(*)
                FROM events WHERE {where}
                GROUP BY strftime('%Y-%m-%dT%H:00:00Z', at),kind
            """, parameters).fetchall()
            latest = self.db.execute(f"""SELECT at FROM events WHERE {where}
                ORDER BY julianday(at) DESC,id DESC LIMIT 1""", parameters).fetchone()
        counts = {(hour, kind): count for hour,kind,count in grouped}
        floor = start.replace(minute=0,second=0,microsecond=0)
        final = now.replace(minute=0,second=0,microsecond=0)
        total = int((final-floor).total_seconds()//3600)+1
        buckets = []
        for i in range(total):
            hour = (floor+timedelta(hours=i)).strftime("%Y-%m-%dT%H:00:00Z")
            buckets.append({"hour":hour,"bursts":counts.get((hour,"burst"),0),
                            "pairs":counts.get((hour,"pair"),0)})
        return {"window_hours":hours,"burst_count":summary[0],
                "pair_count":summary[1],
                "avg_duration_ms":round(summary[2],1) if summary[2] is not None else None,
                "avg_snr_db":round(summary[3],1) if summary[3] is not None else None,
                "peak_snr_db":round(summary[4],1) if summary[4] is not None else None,
                "last_event_at":latest[0] if latest else None,
                "buckets":buckets,"source":"rtl_sdr","simulated":False}


    def start_session(self, planned_seconds: int) -> int:
        """Record an explicitly authorized CLI scan; never start the SDR."""
        if type(planned_seconds) is not int or not 20 <= planned_seconds <= 300:
            raise ValueError("Scan session duration outside 20..300 seconds")
        timestamp = datetime.now(timezone.utc).isoformat()
        with self.lock:
            if self._active_session_id is not None:
                raise ValueError("A CLI scan is already associated with this store")
            cursor = self.db.execute(
                "INSERT INTO scan_sessions(started_at,planned_seconds,result,restoration) "
                "VALUES (?,?, 'running','pending')", (timestamp, planned_seconds))
            self.db.commit()
            self._active_session_id = cursor.lastrowid
            return cursor.lastrowid

    def finish_session(self, session_id: int, *, result: str, restoration: str,
                       burst_count: int = 0, pair_count: int = 0) -> None:
        """Write final metadata, after ADS-B recovery was attempted and checked."""
        if type(session_id) is not int or session_id < 1:
            raise ValueError("Invalid scan session ID")
        if result not in ("completed", "interrupted", "error"):
            raise ValueError("Invalid scan result")
        if restoration not in ("verified", "failed", "not_required"):
            raise ValueError("Invalid restoration state")
        if result == "completed" and restoration != "verified":
            raise ValueError("Successful scan requires verified ADS-B restoration")
        if (type(burst_count) is not int or burst_count < 0 or
            type(pair_count) is not int or pair_count < 0):
            raise ValueError("Invalid event counts")
        ended_at = datetime.now(timezone.utc).isoformat()
        with self.lock:
            # Explicit association prevents simulator events and other sessions
            # from contaminating aggregates, even with concurrent SQLite writers.
            existing = self.db.execute(
                "SELECT result FROM scan_sessions WHERE id=?", (session_id,)).fetchone()
            if existing is None or existing[0] != "running":
                raise ValueError("Session missing or already finalized")
            aggregate = self.db.execute("""
                SELECT AVG(e.duration_ms), MIN(e.duration_ms), MAX(e.duration_ms),
                       AVG(e.peak_snr_db), MAX(e.peak_snr_db),
                       AVG(e.peak_rise_db), MAX(e.peak_rise_db)
                FROM scan_session_events a JOIN events e ON e.id=a.event_id
                WHERE a.session_id=? AND e.kind='burst'
                  AND e.source='rtl_sdr' AND e.simulated=0
            """, (session_id,)).fetchone()
            rounded = [round(value, 1) if value is not None else None
                       for value in aggregate]
            cursor = self.db.execute(
                """UPDATE scan_sessions SET finished_at=?, result=?, restoration=?,
                   burst_count=?, pair_count=?, avg_duration_ms=?, min_duration_ms=?,
                   max_duration_ms=?, avg_snr_db=?, peak_snr_db=?, avg_rise_db=?,
                   peak_rise_db=?, stats_version=1
                   WHERE id=? AND result='running'""",
                (ended_at, result, restoration, burst_count, pair_count,
                 *rounded, session_id))
            if cursor.rowcount != 1:
                self.db.rollback()
                raise ValueError("Session missing or already finalized")
            self.db.commit()
            if self._active_session_id == session_id:
                self._active_session_id = None

    def recent_sessions(self, limit: int = 30, *, now: datetime | None = None) -> list[dict]:
        """Return bounded, read-only scan summaries; an unfinished row stays unverified.

        A crash cannot falsely mark ADS-B as restored. Root watchdog remains the
        recovery mechanism, independent from SQLite bookkeeping.
        """
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Session limit must be between 1 and 100")
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError("Time must be timezone aware")
        now = now.astimezone(timezone.utc)
        with self.lock:
            cursor = self.db.execute(
                """SELECT id,started_at,finished_at,planned_seconds,result,restoration,
                          burst_count,pair_count,avg_duration_ms,min_duration_ms,
                          max_duration_ms,avg_snr_db,peak_snr_db,avg_rise_db,
                          peak_rise_db,stats_version FROM scan_sessions
                   ORDER BY id DESC LIMIT ?""", (limit,))
            fields = [field[0] for field in cursor.description]
            rows = [dict(zip(fields, row)) for row in cursor.fetchall()]
        for item in rows:
            start = datetime.fromisoformat(item["started_at"])
            end = datetime.fromisoformat(item["finished_at"]) if item["finished_at"] else now
            item["elapsed_seconds"] = max(0, round((end - start).total_seconds(), 1))
            item["stale"] = (item["result"] == "running" and
                             item["elapsed_seconds"] > item["planned_seconds"] + 90)
            item["metrics_available"] = item.pop("stats_version") > 0
            notices = []
            if item["restoration"] == "failed":
                notices.append("adsb_restore_failed")
            elif item["stale"]:
                notices.append("unfinished_scan")
            if item["result"] == "completed" and item["burst_count"] == 0:
                notices.append("no_bursts")
            elif item["result"] == "completed" and item["burst_count"] >= 2 and item["pair_count"] == 0:
                notices.append("no_pairs")
            if (item["metrics_available"] and item["burst_count"] > 0
                    and item["avg_snr_db"] is None):
                notices.append("snr_missing")
            item["diagnostics"] = notices
        return rows


    def session_timeline(self, session_id: int, bucket_seconds: int = 5) -> dict | None:
        """Summarize linked *metadata* into bounded relative-time buckets.

        This deliberately exposes no individual RF event timestamps, bitstreams,
        IQ samples or keys. Historic v0.5 sessions have no linkable events.
        """
        if type(session_id) is not int or not 1 <= session_id <= 2147483647:
            raise ValueError("Invalid scan ID")
        if type(bucket_seconds) is not int or bucket_seconds not in (5, 10):
            raise ValueError("Buckets must be 5 or 10 seconds")
        with self.lock:
            cursor = self.db.execute("""
                SELECT id,started_at,finished_at,planned_seconds,result,
                       restoration,burst_count,pair_count,stats_version
                FROM scan_sessions WHERE id=?
            """, (session_id,))
            row = cursor.fetchone()
            if row is None:
                return None
            fields = [description[0] for description in cursor.description]
            session = dict(zip(fields, row))
            event_cursor = self.db.execute("""
                SELECT e.at,e.kind,e.duration_ms,e.peak_snr_db
                FROM scan_session_events l
                JOIN events e ON e.id=l.event_id
                WHERE l.session_id=? AND e.source='rtl_sdr' AND e.simulated=0
                ORDER BY e.id ASC LIMIT 10001
            """, (session_id,))
            events = event_cursor.fetchall()

        start = datetime.fromisoformat(session["started_at"]).astimezone(timezone.utc)
        elapsed = (
            datetime.fromisoformat(session["finished_at"]).astimezone(timezone.utc) - start
        ).total_seconds() if session["finished_at"] else session["planned_seconds"]
        # Bounded to 360s regardless of an abandoned/stale session's age.
        window = min(360, max(session["planned_seconds"], math.ceil(max(0, elapsed))))
        bucket_count = math.ceil(window / bucket_seconds)
        bins = []
        for index in range(bucket_count):
            bins.append({"from_s": index * bucket_seconds,
                         "to_s": min(window, (index + 1) * bucket_seconds),
                         "bursts": 0, "pairs": 0,
                         "avg_snr_db": None, "avg_duration_ms": None})
        snr_edges = (10, 20, 30, 40)
        snr_labels = ("<10 dB", "10–19 dB", "20–29 dB", "30–39 dB", "≥40 dB")
        duration_edges = (100, 150, 200, 250)
        duration_labels = ("<100 ms", "100–149 ms", "150–199 ms",
                           "200–249 ms", "≥250 ms")
        snr_counts = [0] * len(snr_labels)
        duration_counts = [0] * len(duration_labels)
        sums = [[0.0, 0, 0.0, 0] for _ in bins]
        ignored = 0
        # Even under intentionally dense noise bursts, reading stays bounded.
        truncated = len(events) > 10000
        for at, kind, duration, snr in events[:10000]:
            try:
                offset = (datetime.fromisoformat(at).astimezone(timezone.utc) -
                          start).total_seconds()
            except (TypeError, ValueError, OverflowError):
                ignored += 1
                continue
            if not 0 <= offset < window:
                ignored += 1
                continue
            index = min(len(bins) - 1, int(offset // bucket_seconds))
            target = bins[index]
            if kind == "pair":
                target["pairs"] += 1
                continue
            if kind != "burst":
                ignored += 1
                continue
            target["bursts"] += 1
            if isinstance(snr, (int, float)) and math.isfinite(snr):
                sums[index][0] += snr
                sums[index][1] += 1
                snr_counts[next((j for j, edge in enumerate(snr_edges) if snr < edge),
                                len(snr_edges))] += 1
            if isinstance(duration, (int, float)) and math.isfinite(duration):
                sums[index][2] += duration
                sums[index][3] += 1
                duration_counts[next(
                    (j for j, edge in enumerate(duration_edges) if duration < edge),
                    len(duration_edges)
                )] += 1
        for bucket, (snr_sum, snr_total, duration_sum, duration_total) in zip(bins, sums):
            if snr_total:
                bucket["avg_snr_db"] = round(snr_sum / snr_total, 1)
            if duration_total:
                bucket["avg_duration_ms"] = round(duration_sum / duration_total, 1)

        # A newly started scan can have zero events and still have a valid
        # timeline. v0.5 sessions cannot be reconstructed after the fact.
        available = session["stats_version"] > 0 or session["result"] == "running"
        return {
            "session_id": session_id,
            "result": session["result"],
            "restoration": session["restoration"],
            "started_at": session["started_at"],
            "planned_seconds": session["planned_seconds"],
            "window_seconds": window,
            "bucket_seconds": bucket_seconds,
            "timeline_available": available,
            "source": "rtl_sdr",
            "simulated": False,
            "buckets": bins if available else [],
            "snr_distribution": [
                {"label": label, "count": count}
                for label, count in zip(snr_labels, snr_counts)
            ] if available else [],
            "duration_distribution": [
                {"label": label, "count": count}
                for label, count in zip(duration_labels, duration_counts)
            ] if available else [],
            "included_bursts": sum(b["bursts"] for b in bins),
            "included_pairs": sum(b["pairs"] for b in bins),
            "ignored_events": ignored,
            "truncated": truncated,
        }

    def close(self) -> None:
        with self.lock:
            self.db.close()
