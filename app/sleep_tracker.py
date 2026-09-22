import json
from datetime import datetime, timezone
logger = __import__('logging').getLogger(__name__)
CLOSED_STATES = frozenset({'closed'})
OPEN_STATES = frozenset({'open', 'opening'})

class SleepTracker:

    def __init__(self, database, curtain_entity, bed_entities, room='主卧'):
        self.db = database
        self.curtain_entity = curtain_entity
        self.bed_entities = tuple(bed_entities)
        self.room = room
        self._open_id = None
        self._bed_on_events = {entity: 0 for entity in self.bed_entities}
        self._resume_open_session()

    def _resume_open_session(self):
        row = self.db.rows("SELECT id,evidence_json FROM sessions WHERE type='sleep' AND status='open' ORDER BY start_ts DESC LIMIT 1")
        if row:
            self._open_id = row[0]['id']
            try:
                evidence = json.loads(row[0]['evidence_json'] or '{}')
            except ValueError:
                evidence = {}
            counts = evidence.get('bed_on_events') or {}
            for entity in self.bed_entities:
                self._bed_on_events[entity] = int(counts.get(entity, 0))
            logger.info('[SleepSession] resumed open id=%s from database', self._open_id)

    def seed_state(self, states):
        if self._open_id is not None:
            return None
        for state in states or []:
            if state.get('entity_id') != self.curtain_entity:
                continue
            if state.get('state') in CLOSED_STATES:
                last_changed = state.get('last_changed') or datetime.now(timezone.utc).isoformat()
                return self._open(last_changed)
        return None

    def process(self, event):
        entity = event.get('entity_id')
        if entity == self.curtain_entity:
            return self._on_curtain(event)
        if entity in self._bed_on_events and self._open_id is not None:
            return self._on_bed(event)
        return None

    def _on_curtain(self, event):
        state = str(event.get('new_state'))
        if state in CLOSED_STATES:
            if self._open_id is None:
                return self._open(event.get('ts'))
            return None
        if state in OPEN_STATES and self._open_id is not None:
            return self._close(event.get('ts'))
        return None

    def _on_bed(self, event):
        if str(event.get('new_state')).lower() == 'on':
            self._bed_on_events[event['entity_id']] = self._bed_on_events.get(event['entity_id'], 0) + 1
            self._write_evidence()
        return None

    def _open(self, start_ts):
        now = datetime.now(timezone.utc).isoformat()
        with self.db.connect() as con:
            cur = con.execute("INSERT INTO sessions(room,type,start_ts,end_ts,duration_seconds,status,evidence_json) VALUES(?,?,?,NULL,NULL,'open',?)", (self.room, 'sleep', start_ts, self._evidence_json()))
            self._open_id = cur.lastrowid
        logger.info('[SleepSession] opened id=%s start=%s curtain=%s', self._open_id, start_ts, self.curtain_entity)
        return None

    def _close(self, end_ts):
        session_id, start_ts = (self._open_id, None)
        row = self.db.rows('SELECT start_ts FROM sessions WHERE id=?', (session_id,))
        start_ts = row[0]['start_ts'] if row else None
        duration = None
        try:
            from .event_preprocessor import _parse_ts
            duration = max(0, int((_parse_ts(end_ts) - _parse_ts(start_ts)).total_seconds()))
        except (TypeError, ValueError):
            duration = None
        with self.db.connect() as con:
            con.execute("UPDATE sessions SET end_ts=?, duration_seconds=?, status='closed', evidence_json=? WHERE id=?", (end_ts, duration, self._evidence_json(), session_id))
        self._open_id = None
        for entity in self.bed_entities:
            self._bed_on_events[entity] = 0
        logger.info('[SleepSession] closed id=%s end=%s duration_s=%s', session_id, end_ts, duration)
        return session_id

    def _evidence_json(self):
        return json.dumps({'definition': 'curtain closed -> opened', 'curtain_entity': self.curtain_entity, 'bed_on_events': self._bed_on_events, 'bed_entities': list(self.bed_entities)}, ensure_ascii=False)

    def _write_evidence(self):
        if self._open_id is None:
            return
        with self.db.connect() as con:
            con.execute('UPDATE sessions SET evidence_json=? WHERE id=?', (self._evidence_json(), self._open_id))
