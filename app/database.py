import json
import logging
import sqlite3
import threading
from contextlib import contextmanager, closing
from datetime import datetime, timezone
from pathlib import Path
log = logging.getLogger(__name__)
SQLITE_WINDOW_FUNCTIONS_VERSION = (3, 25, 0)

def supports_window_functions(version=None):
    current = version if version is not None else sqlite3.sqlite_version_info
    return tuple((int(part) for part in current[:3])) >= SQLITE_WINDOW_FUNCTIONS_VERSION
SCHEMA = "\nPRAGMA journal_mode=WAL;\nPRAGMA foreign_keys=ON;\nCREATE TABLE IF NOT EXISTS events (\n id INTEGER PRIMARY KEY, ts TEXT NOT NULL, entity_id TEXT, domain TEXT, category TEXT NOT NULL,\n event_type TEXT NOT NULL, old_state TEXT, new_state TEXT, source TEXT NOT NULL,\n context_id TEXT, parent_id TEXT, user_id_hash TEXT, area TEXT, attributes_json TEXT NOT NULL DEFAULT '{}',\n metadata_json TEXT NOT NULL DEFAULT '{}'\n);\nCREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);\nCREATE INDEX IF NOT EXISTS idx_events_entity_ts ON events(entity_id, ts);\nCREATE INDEX IF NOT EXISTS idx_events_type_ts ON events(event_type, ts);\nCREATE TABLE IF NOT EXISTS sessions (\n id INTEGER PRIMARY KEY, room TEXT NOT NULL, type TEXT NOT NULL, start_ts TEXT NOT NULL, end_ts TEXT,\n duration_seconds INTEGER, status TEXT NOT NULL DEFAULT 'open', evidence_json TEXT NOT NULL DEFAULT '[]'\n);\nCREATE TABLE IF NOT EXISTS patterns (\n id INTEGER PRIMARY KEY, type TEXT NOT NULL, fingerprint TEXT NOT NULL UNIQUE, description TEXT NOT NULL,\n confidence REAL NOT NULL, sample_count INTEGER NOT NULL, consistency REAL NOT NULL DEFAULT 0,\n counter_examples INTEGER NOT NULL DEFAULT 0, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,\n status TEXT NOT NULL CHECK(status IN ('candidate','confirmed','rejected','obsolete')),\n evidence_json TEXT NOT NULL DEFAULT '[]', reason TEXT NOT NULL DEFAULT ''\n);\nCREATE TABLE IF NOT EXISTS memories (\n id INTEGER PRIMARY KEY, pattern_id INTEGER, type TEXT NOT NULL, content TEXT NOT NULL,\n confidence REAL NOT NULL, sample_count INTEGER NOT NULL, status TEXT NOT NULL,\n created_at TEXT NOT NULL, updated_at TEXT NOT NULL, FOREIGN KEY(pattern_id) REFERENCES patterns(id)\n);\nCREATE TABLE IF NOT EXISTS feedback (\n id INTEGER PRIMARY KEY, pattern_id INTEGER NOT NULL, action TEXT NOT NULL CHECK(action IN ('confirm','reject')),\n note TEXT, created_at TEXT NOT NULL, FOREIGN KEY(pattern_id) REFERENCES patterns(id)\n);\nCREATE TABLE IF NOT EXISTS daily_summaries (\n day TEXT PRIMARY KEY, summary_json TEXT NOT NULL, created_at TEXT NOT NULL, llm_model TEXT, llm_duration_ms INTEGER\n);\nCREATE TABLE IF NOT EXISTS period_summaries (\n period_start TEXT PRIMARY KEY, period_end TEXT NOT NULL, summary_json TEXT NOT NULL,\n created_at TEXT NOT NULL, llm_model TEXT, llm_duration_ms INTEGER\n);\nCREATE INDEX IF NOT EXISTS idx_period_summaries_end ON period_summaries(period_end);\nCREATE TABLE IF NOT EXISTS ai_guidance (\n id INTEGER PRIMARY KEY, content TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),\n scope TEXT NOT NULL DEFAULT 'all', source TEXT NOT NULL DEFAULT 'owner',\n created_at TEXT NOT NULL, archived_at TEXT\n);\nCREATE INDEX IF NOT EXISTS idx_ai_guidance_active_created ON ai_guidance(active, created_at);\nCREATE TABLE IF NOT EXISTS advisor_submissions (\n id INTEGER PRIMARY KEY, external_id TEXT NOT NULL UNIQUE, source TEXT NOT NULL, scope TEXT NOT NULL,\n content TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),\n created_at TEXT NOT NULL, reviewed_at TEXT, guidance_id INTEGER,\n FOREIGN KEY(guidance_id) REFERENCES ai_guidance(id)\n);\nCREATE INDEX IF NOT EXISTS idx_advisor_submissions_status_created ON advisor_submissions(status, created_at);\nCREATE TABLE IF NOT EXISTS weekly_summaries (\n week_start TEXT PRIMARY KEY, summary_json TEXT NOT NULL, created_at TEXT NOT NULL, llm_model TEXT, llm_duration_ms INTEGER\n);\nCREATE TABLE IF NOT EXISTS automation_suggestions (\n id INTEGER PRIMARY KEY, pattern_id INTEGER, title TEXT NOT NULL, description TEXT NOT NULL, evidence_json TEXT NOT NULL,\n confidence REAL NOT NULL, sample_count INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'proposed', created_at TEXT NOT NULL,\n FOREIGN KEY(pattern_id) REFERENCES patterns(id)\n);\nCREATE TABLE IF NOT EXISTS system_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);\n-- Append-only operational accounting. No prompt, response, event or answer text.\nCREATE TABLE IF NOT EXISTS model_calls (\n id INTEGER PRIMARY KEY, scope TEXT NOT NULL, window_hash TEXT NOT NULL, model TEXT NOT NULL,\n started_at TEXT NOT NULL, attempts INTEGER NOT NULL, rejections INTEGER NOT NULL,\n queue_ms INTEGER NOT NULL, call_ms INTEGER NOT NULL, input_tokens_estimate INTEGER NOT NULL,\n prompt_tokens INTEGER, output_tokens INTEGER, outcome TEXT NOT NULL, error_type TEXT,\n output_items INTEGER NOT NULL\n);\nCREATE INDEX IF NOT EXISTS idx_model_calls_started ON model_calls(started_at);\n-- Every validated report a model has produced for a period, so one period can hold\n-- reports from several models side by side. Purely additive: nothing else reads or\n-- writes this table, and the row in *_summaries stays the current report.\nCREATE TABLE IF NOT EXISTS report_variants (\n scope TEXT NOT NULL, report_key TEXT NOT NULL, llm_model TEXT NOT NULL, summary_json TEXT NOT NULL,\n created_at TEXT NOT NULL, llm_duration_ms INTEGER,\n PRIMARY KEY (scope, report_key, llm_model)\n);\nCREATE INDEX IF NOT EXISTS idx_report_variants_key ON report_variants(scope, report_key);\n-- Questions the model wants the owner to settle. Additive: nothing else reads or\n-- writes this table, and an answer becomes ordinary owner guidance unless it was\n-- scoped to one specific window.\nCREATE TABLE IF NOT EXISTS ai_questions (\n id INTEGER PRIMARY KEY, question TEXT NOT NULL, why TEXT, source TEXT NOT NULL DEFAULT 'report',\n scope_key TEXT, status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','answered','skipped')),\n answer TEXT, answer_scope TEXT, answer_target TEXT, guidance_id INTEGER,\n created_at TEXT NOT NULL, answered_at TEXT, options_json TEXT\n);\nCREATE INDEX IF NOT EXISTS idx_ai_questions_status_created ON ai_questions(status, created_at);\n-- A condensed rule set proposed by an outside AI (see app/condense.py). Additive:\n-- the proposal is inert until the owner approves it, and approving only inserts\n-- rules and archives superseded entries - it never deletes anything.\nCREATE TABLE IF NOT EXISTS condense_proposals (\n id INTEGER PRIMARY KEY, external_id TEXT NOT NULL UNIQUE, source TEXT NOT NULL,\n rules_json TEXT NOT NULL, notes TEXT, status TEXT NOT NULL DEFAULT 'pending'\n CHECK(status IN ('pending','approved','rejected')), created_at TEXT NOT NULL,\n reviewed_at TEXT, guidance_ids TEXT, applied_json TEXT\n);\nCREATE INDEX IF NOT EXISTS idx_condense_proposals_status_created ON condense_proposals(status, created_at);\nCREATE INDEX IF NOT EXISTS idx_patterns_status_last_seen ON patterns(status, last_seen);\nCREATE INDEX IF NOT EXISTS idx_sessions_start_ts ON sessions(start_ts);\n-- One immutable memory segment: the AI summary of one frozen event-id range. The\n-- unique range index is the idempotency guard (a restart or repeated schedule tick\n-- can never create two segments for the same events), and a failed segment keeps\n-- its range so the report context can fall back to the deterministic compression.\nCREATE TABLE IF NOT EXISTS event_segments (\n id INTEGER PRIMARY KEY, period_start TEXT NOT NULL, period_end TEXT NOT NULL,\n start_event_id INTEGER NOT NULL, end_event_id INTEGER NOT NULL, event_count INTEGER NOT NULL,\n estimated_input_tokens INTEGER NOT NULL DEFAULT 0,\n summary_json TEXT, summary_text TEXT NOT NULL DEFAULT '',\n critical_events_json TEXT NOT NULL DEFAULT '[]',\n start_state_json TEXT NOT NULL DEFAULT '{}', end_state_json TEXT NOT NULL DEFAULT '{}',\n model TEXT, prompt_version TEXT NOT NULL DEFAULT 'segment_prompt_v1',\n summary_level TEXT NOT NULL DEFAULT 'segment',\n status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','succeeded','failed')),\n retry_count INTEGER NOT NULL DEFAULT 0, last_error TEXT,\n created_at TEXT NOT NULL, updated_at TEXT NOT NULL\n);\nCREATE UNIQUE INDEX IF NOT EXISTS idx_event_segments_range ON event_segments(start_event_id, end_event_id);\n-- Deterministic arrival/departure facts: paired on-edges of the entryway and\n-- outside-the-door presence sensors. Interpretation uses explicitly configured\n-- direction and timing rules; it does not establish personal identity.\nCREATE TABLE IF NOT EXISTS movements (\n id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('arrival','departure')),\n ts TEXT NOT NULL, window_seconds INTEGER, trigger_json TEXT NOT NULL DEFAULT '[]',\n created_at TEXT NOT NULL\n);\nCREATE INDEX IF NOT EXISTS idx_movements_ts ON movements(ts);\nCREATE INDEX IF NOT EXISTS idx_event_segments_period ON event_segments(period_start);\nCREATE INDEX IF NOT EXISTS idx_event_segments_status ON event_segments(status, updated_at);\n"

class Database:

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        if not supports_window_functions():
            log.error('SQLite %s lacks window functions (needs %s): the per-room 在场时段 page will fail', sqlite3.sqlite_version, '.'.join((str(part) for part in SQLITE_WINDOW_FUNCTIONS_VERSION)))
        with self.connect() as con:
            con.executescript(SCHEMA)
            columns = {row[1] for row in con.execute('PRAGMA table_info(ai_guidance)')}
            if 'scope' not in columns:
                con.execute("ALTER TABLE ai_guidance ADD COLUMN scope TEXT NOT NULL DEFAULT 'all'")
            if 'source' not in columns:
                con.execute("ALTER TABLE ai_guidance ADD COLUMN source TEXT NOT NULL DEFAULT 'owner'")
            qcols = {row[1] for row in con.execute('PRAGMA table_info(ai_questions)')}
            if 'options_json' not in qcols:
                con.execute('ALTER TABLE ai_questions ADD COLUMN options_json TEXT')

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA foreign_keys=ON')
        con.execute('PRAGMA busy_timeout=30000')
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def insert_event(self, event):
        cols = ['ts', 'entity_id', 'domain', 'category', 'event_type', 'old_state', 'new_state', 'source', 'context_id', 'parent_id', 'user_id_hash', 'area', 'attributes_json', 'metadata_json']
        vals = [event.get(c) for c in cols]
        vals[12] = json.dumps(vals[12] or {}, ensure_ascii=False)
        vals[13] = json.dumps(vals[13] or {}, ensure_ascii=False)
        with self._lock, self.connect() as con:
            cur = con.execute(f"INSERT INTO events ({','.join(cols)}) VALUES ({','.join(('?' for _ in cols))})", vals)
            return cur.lastrowid

    def set_state(self, key, value):
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self.connect() as con:
            con.execute('INSERT INTO system_state(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at', (key, str(value), now))

    def scalar(self, sql, params=()):
        with self.connect() as con:
            row = con.execute(sql, params).fetchone()
            return row[0] if row else None

    def rows(self, sql, params=()):
        with self.connect() as con:
            return [dict(r) for r in con.execute(sql, params).fetchall()]

    def backup(self, destination):
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.connect() as source, closing(sqlite3.connect(destination)) as target:
            source.backup(target)
