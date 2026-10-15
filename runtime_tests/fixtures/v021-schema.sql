-- Frozen public v0.2.1 SQL schema, comments removed; synthetic fixture only.

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
CREATE TABLE IF NOT EXISTS period_summaries (
 period_start TEXT PRIMARY KEY, period_end TEXT NOT NULL, summary_json TEXT NOT NULL,
 created_at TEXT NOT NULL, llm_model TEXT, llm_duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_period_summaries_end ON period_summaries(period_end);
CREATE TABLE IF NOT EXISTS ai_guidance (
 id INTEGER PRIMARY KEY, content TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
 scope TEXT NOT NULL DEFAULT 'all', source TEXT NOT NULL DEFAULT 'owner',
 created_at TEXT NOT NULL, archived_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_ai_guidance_active_created ON ai_guidance(active, created_at);
CREATE TABLE IF NOT EXISTS advisor_submissions (
 id INTEGER PRIMARY KEY, external_id TEXT NOT NULL UNIQUE, source TEXT NOT NULL, scope TEXT NOT NULL,
 content TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
 created_at TEXT NOT NULL, reviewed_at TEXT, guidance_id INTEGER,
 FOREIGN KEY(guidance_id) REFERENCES ai_guidance(id)
);
CREATE INDEX IF NOT EXISTS idx_advisor_submissions_status_created ON advisor_submissions(status, created_at);
CREATE TABLE IF NOT EXISTS weekly_summaries (
 week_start TEXT PRIMARY KEY, summary_json TEXT NOT NULL, created_at TEXT NOT NULL, llm_model TEXT, llm_duration_ms INTEGER
);
CREATE TABLE IF NOT EXISTS automation_suggestions (
 id INTEGER PRIMARY KEY, pattern_id INTEGER, title TEXT NOT NULL, description TEXT NOT NULL, evidence_json TEXT NOT NULL,
 confidence REAL NOT NULL, sample_count INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'proposed', created_at TEXT NOT NULL,
 FOREIGN KEY(pattern_id) REFERENCES patterns(id)
);
CREATE TABLE IF NOT EXISTS system_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS model_calls (
 id INTEGER PRIMARY KEY, scope TEXT NOT NULL, window_hash TEXT NOT NULL, model TEXT NOT NULL,
 started_at TEXT NOT NULL, attempts INTEGER NOT NULL, rejections INTEGER NOT NULL,
 queue_ms INTEGER NOT NULL, call_ms INTEGER NOT NULL, input_tokens_estimate INTEGER NOT NULL,
 prompt_tokens INTEGER, output_tokens INTEGER, outcome TEXT NOT NULL, error_type TEXT,
 output_items INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_model_calls_started ON model_calls(started_at);
CREATE TABLE IF NOT EXISTS report_variants (
 scope TEXT NOT NULL, report_key TEXT NOT NULL, llm_model TEXT NOT NULL, summary_json TEXT NOT NULL,
 created_at TEXT NOT NULL, llm_duration_ms INTEGER,
 PRIMARY KEY (scope, report_key, llm_model)
);
CREATE INDEX IF NOT EXISTS idx_report_variants_key ON report_variants(scope, report_key);
CREATE TABLE IF NOT EXISTS ai_questions (
 id INTEGER PRIMARY KEY, question TEXT NOT NULL, why TEXT, source TEXT NOT NULL DEFAULT 'report',
 scope_key TEXT, status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','answered','skipped')),
 answer TEXT, answer_scope TEXT, answer_target TEXT, guidance_id INTEGER,
 created_at TEXT NOT NULL, answered_at TEXT, options_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_ai_questions_status_created ON ai_questions(status, created_at);
CREATE TABLE IF NOT EXISTS condense_proposals (
 id INTEGER PRIMARY KEY, external_id TEXT NOT NULL UNIQUE, source TEXT NOT NULL,
 rules_json TEXT NOT NULL, notes TEXT, status TEXT NOT NULL DEFAULT 'pending'
 CHECK(status IN ('pending','approved','rejected')), created_at TEXT NOT NULL,
 reviewed_at TEXT, guidance_ids TEXT, applied_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_condense_proposals_status_created ON condense_proposals(status, created_at);
CREATE INDEX IF NOT EXISTS idx_patterns_status_last_seen ON patterns(status, last_seen);
CREATE INDEX IF NOT EXISTS idx_sessions_start_ts ON sessions(start_ts);
CREATE TABLE IF NOT EXISTS event_segments (
 id INTEGER PRIMARY KEY, period_start TEXT NOT NULL, period_end TEXT NOT NULL,
 start_event_id INTEGER NOT NULL, end_event_id INTEGER NOT NULL, event_count INTEGER NOT NULL,
 estimated_input_tokens INTEGER NOT NULL DEFAULT 0,
 summary_json TEXT, summary_text TEXT NOT NULL DEFAULT '',
 critical_events_json TEXT NOT NULL DEFAULT '[]',
 start_state_json TEXT NOT NULL DEFAULT '{}', end_state_json TEXT NOT NULL DEFAULT '{}',
 model TEXT, prompt_version TEXT NOT NULL DEFAULT 'segment_prompt_v1',
 summary_level TEXT NOT NULL DEFAULT 'segment',
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','succeeded','failed')),
 retry_count INTEGER NOT NULL DEFAULT 0, last_error TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_event_segments_range ON event_segments(start_event_id, end_event_id);
CREATE TABLE IF NOT EXISTS movements (
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('arrival','departure')),
 ts TEXT NOT NULL, window_seconds INTEGER, trigger_json TEXT NOT NULL DEFAULT '[]',
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_movements_ts ON movements(ts);
CREATE INDEX IF NOT EXISTS idx_event_segments_period ON event_segments(period_start);
CREATE INDEX IF NOT EXISTS idx_event_segments_status ON event_segments(status, updated_at);
