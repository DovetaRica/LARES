import json
import sqlite3
import threading
from contextlib import contextmanager, closing
from datetime import datetime, timezone
from pathlib import Path


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY, ts TEXT NOT NULL, entity_id TEXT, domain TEXT, category TEXT NOT NULL,
 event_type TEXT NOT NULL, old_state TEXT, new_state TEXT, source TEXT NOT NULL,
 context_id TEXT, parent_id TEXT, user_id_hash TEXT, area TEXT, attributes_json TEXT NOT NULL DEFAULT '{}',
 metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_entity_ts ON events(entity_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_type_ts ON events(event_type, ts);
CREATE TABLE IF NOT EXISTS sessions (
 id INTEGER PRIMARY KEY, room TEXT NOT NULL, type TEXT NOT NULL, start_ts TEXT NOT NULL, end_ts TEXT,
 duration_seconds INTEGER, status TEXT NOT NULL DEFAULT 'open', evidence_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS patterns (
 id INTEGER PRIMARY KEY, type TEXT NOT NULL, fingerprint TEXT NOT NULL UNIQUE, description TEXT NOT NULL,
 confidence REAL NOT NULL, sample_count INTEGER NOT NULL, consistency REAL NOT NULL DEFAULT 0,
 counter_examples INTEGER NOT NULL DEFAULT 0, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('candidate','confirmed','rejected','obsolete')),
 evidence_json TEXT NOT NULL DEFAULT '[]', reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS memories (
 id INTEGER PRIMARY KEY, pattern_id INTEGER, type TEXT NOT NULL, content TEXT NOT NULL,
 confidence REAL NOT NULL, sample_count INTEGER NOT NULL, status TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, FOREIGN KEY(pattern_id) REFERENCES patterns(id)
);
CREATE TABLE IF NOT EXISTS feedback (
 id INTEGER PRIMARY KEY, pattern_id INTEGER NOT NULL, action TEXT NOT NULL CHECK(action IN ('confirm','reject')),
 note TEXT, created_at TEXT NOT NULL, FOREIGN KEY(pattern_id) REFERENCES patterns(id)
);
CREATE TABLE IF NOT EXISTS daily_summaries (
 day TEXT PRIMARY KEY, summary_json TEXT NOT NULL, created_at TEXT NOT NULL, llm_model TEXT, llm_duration_ms INTEGER
);
CREATE TABLE IF NOT EXISTS weekly_summaries (
 week_start TEXT PRIMARY KEY, summary_json TEXT NOT NULL, created_at TEXT NOT NULL, llm_model TEXT, llm_duration_ms INTEGER
);
CREATE TABLE IF NOT EXISTS automation_suggestions (
 id INTEGER PRIMARY KEY, pattern_id INTEGER, title TEXT NOT NULL, description TEXT NOT NULL, evidence_json TEXT NOT NULL,
 confidence REAL NOT NULL, sample_count INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'proposed', created_at TEXT NOT NULL,
 FOREIGN KEY(pattern_id) REFERENCES patterns(id)
);
CREATE TABLE IF NOT EXISTS system_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_patterns_status_last_seen ON patterns(status, last_seen);
CREATE INDEX IF NOT EXISTS idx_sessions_start_ts ON sessions(start_ts);
"""


class Database:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._local = threading.local()
        with self.connect() as con:
            con.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        active = getattr(self._local, "connection", None)
        if active is not None:
            yield active
            return
        with self._lock:
            con = sqlite3.connect(self.path, timeout=30)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA foreign_keys=ON")
            try:
                yield con
                con.commit()
            except BaseException:
                con.rollback()
                raise
            finally:
                con.close()

    @contextmanager
    def batch(self):
        """One connection and atomic transaction; no per-event fsync in a replay."""
        if getattr(self._local, "connection", None) is not None:
            raise RuntimeError("Nested batch transactions are unsupported")
        with self.connect() as con:
            con.execute("BEGIN")
            self._local.connection = con
            try:
                yield self
            finally:
                self._local.connection = None

    def insert_event(self, event):
        cols = ["ts","entity_id","domain","category","event_type","old_state","new_state","source",
                "context_id","parent_id","user_id_hash","area","attributes_json","metadata_json"]
        vals = [event.get(c) for c in cols]
        vals[12] = json.dumps(vals[12] or {}, ensure_ascii=False)
        vals[13] = json.dumps(vals[13] or {}, ensure_ascii=False)
        with self._lock, self.connect() as con:
            cur = con.execute(f"INSERT INTO events ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})", vals)
            return cur.lastrowid

    def set_state(self, key, value):
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self.connect() as con:
            con.execute("INSERT INTO system_state(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (key, str(value), now))

    def scalar(self, sql, params=()):
        with self.connect() as con:
            row = con.execute(sql, params).fetchone()
            return row[0] if row else None

    def rows(self, sql, params=()):
        with self.connect() as con:
            return [dict(r) for r in con.execute(sql, params).fetchall()]

    def backup(self, destination):
        if getattr(self._local, "connection", None) is not None:
            raise RuntimeError("Back up only committed transactions")
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.connect() as source, closing(sqlite3.connect(destination)) as target:
            source.backup(target)
