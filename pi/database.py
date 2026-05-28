"""SQLite persistence for sessions, transitions, distractions, and environment."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_time TEXT NOT NULL,
    end_time TEXT,
    focused_seconds INTEGER NOT NULL DEFAULT 0,
    total_seconds INTEGER NOT NULL DEFAULT 0,
    distraction_count INTEGER NOT NULL DEFAULT 0,
    break_count INTEGER NOT NULL DEFAULT 0,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS state_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER REFERENCES sessions(id),
    timestamp TEXT NOT NULL,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    trigger TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS distraction_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER REFERENCES sessions(id),
    timestamp TEXT NOT NULL,
    image_path TEXT,
    observation TEXT NOT NULL,
    confidence REAL NOT NULL,
    duration_seconds INTEGER
);

CREATE TABLE IF NOT EXISTS environment_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER REFERENCES sessions(id),
    timestamp TEXT NOT NULL,
    temperature REAL,
    humidity REAL,
    light_level INTEGER
);

CREATE INDEX IF NOT EXISTS idx_sessions_start ON sessions(start_time);
CREATE INDEX IF NOT EXISTS idx_transitions_session ON state_transitions(session_id);
CREATE INDEX IF NOT EXISTS idx_distractions_session ON distraction_events(session_id);
CREATE INDEX IF NOT EXISTS idx_env_session ON environment_log(session_id);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """Thread-safe SQLite wrapper. Single connection guarded by a lock — the
    write traffic is tiny (a few rows per second worst case) and this keeps the
    code simple and easy to reason about."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(sessions)")}
        if "break_count" not in cols:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN break_count INTEGER NOT NULL DEFAULT 0"
            )

    @contextmanager
    def _cursor(self):
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
            finally:
                cur.close()

    # ---------- sessions ----------
    def start_session(self) -> int:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO sessions(start_time) VALUES (?)", (_now_iso(),)
            )
            return cur.lastrowid

    def end_session(
        self,
        session_id: int,
        focused_seconds: int,
        total_seconds: int,
        distraction_count: int,
        break_count: int = 0,
        notes: str | None = None,
    ) -> None:
        with self._cursor() as cur:
            cur.execute(
                """UPDATE sessions
                      SET end_time = ?, focused_seconds = ?,
                          total_seconds = ?, distraction_count = ?,
                          break_count = ?, notes = ?
                    WHERE id = ?""",
                (
                    _now_iso(),
                    focused_seconds,
                    total_seconds,
                    distraction_count,
                    break_count,
                    notes,
                    session_id,
                ),
            )

    def update_session_progress(
        self, session_id: int, focused_seconds: int, total_seconds: int
    ) -> None:
        with self._cursor() as cur:
            cur.execute(
                """UPDATE sessions
                      SET focused_seconds = ?, total_seconds = ?
                    WHERE id = ?""",
                (focused_seconds, total_seconds, session_id),
            )

    def get_session(self, session_id: int) -> sqlite3.Row | None:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
            return cur.fetchone()

    def list_sessions(self, days: int = 7) -> list[sqlite3.Row]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM sessions WHERE start_time >= ? ORDER BY id DESC",
                (cutoff,),
            )
            return cur.fetchall()

    def mark_orphan_sessions_incomplete(self) -> None:
        """Called at startup — any session without end_time was killed mid-run."""
        with self._cursor() as cur:
            cur.execute(
                """UPDATE sessions
                      SET end_time = ?, notes =
                          COALESCE(notes, '') || ' [incomplete: pi restarted]'
                    WHERE end_time IS NULL""",
                (_now_iso(),),
            )

    # ---------- transitions ----------
    def log_transition(
        self,
        session_id: int | None,
        from_state: str,
        to_state: str,
        trigger: str,
    ) -> None:
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO state_transitions
                       (session_id, timestamp, from_state, to_state, trigger)
                   VALUES (?, ?, ?, ?, ?)""",
                (session_id, _now_iso(), from_state, to_state, trigger),
            )

    # ---------- distractions ----------
    def log_distraction(
        self,
        session_id: int,
        observation: str,
        confidence: float,
        image_path: str | None,
        duration_seconds: int | None = None,
    ) -> int:
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO distraction_events
                       (session_id, timestamp, image_path, observation,
                        confidence, duration_seconds)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    _now_iso(),
                    image_path,
                    observation,
                    confidence,
                    duration_seconds,
                ),
            )
            return cur.lastrowid

    def update_distraction_duration(self, event_id: int, duration_seconds: int) -> None:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE distraction_events SET duration_seconds = ? WHERE id = ?",
                (duration_seconds, event_id),
            )

    def list_distractions(self, days: int = 7) -> list[sqlite3.Row]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self._cursor() as cur:
            cur.execute(
                """SELECT d.*, s.start_time AS session_start
                     FROM distraction_events d
                     LEFT JOIN sessions s ON d.session_id = s.id
                    WHERE d.timestamp >= ?
                    ORDER BY d.id DESC""",
                (cutoff,),
            )
            return cur.fetchall()

    # ---------- environment ----------
    def log_environment(
        self,
        session_id: int | None,
        temperature: float | None,
        humidity: float | None,
        light_level: int | None,
    ) -> None:
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO environment_log
                       (session_id, timestamp, temperature, humidity, light_level)
                   VALUES (?, ?, ?, ?, ?)""",
                (session_id, _now_iso(), temperature, humidity, light_level),
            )

    # ---------- retention ----------
    def purge_old_images(self, days: int, image_dir: Path) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        purged = 0
        with self._cursor() as cur:
            cur.execute(
                """SELECT id, image_path FROM distraction_events
                    WHERE image_path IS NOT NULL AND timestamp < ?""",
                (cutoff,),
            )
            rows = cur.fetchall()
            for row in rows:
                p = Path(row["image_path"])
                try:
                    if p.exists():
                        p.unlink()
                    purged += 1
                except OSError:
                    pass
                cur.execute(
                    "UPDATE distraction_events SET image_path = NULL WHERE id = ?",
                    (row["id"],),
                )
        return purged

    def close(self) -> None:
        with self._lock:
            self._conn.close()
