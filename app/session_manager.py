import json
import logging
import re
from datetime import datetime, timedelta, timezone
ON_STATES = frozenset({'on', 'home', 'present', 'occupied', 'detected', 'motion'})
OFF_STATES = frozenset({'off', 'not_home', 'away', 'clear'})
ENTITY_ID_LIKE_RE = re.compile('[a-z][a-z0-9_]*\\.[a-z0-9_]+', re.IGNORECASE)
log = logging.getLogger(__name__)

class SessionManager:

    def __init__(self, database, noise_entities=(), non_occupancy_entities=(), merge_gap_seconds=180, pass_by_seconds=45):
        self.db = database
        self.noise_entities = set(noise_entities or ())
        self.non_occupancy_entities = set(non_occupancy_entities or ())
        self.merge_gap_seconds = int(merge_gap_seconds)
        self.pass_by_seconds = int(pass_by_seconds)
        self._entity_state = {}
        self._room_entities = {}
        self._entity_room = {}
        self._unknown_state_logged = set()

    @staticmethod
    def _parse_ts(value):
        try:
            parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    def _presence_room(self, entity_id, event, meta):
        room = event.get('area') or (meta.get('life_area') if isinstance(meta, dict) else None) or ''
        room = str(room).strip()
        if room:
            return room
        return self._entity_room.get(entity_id) or str(entity_id or 'unknown')

    def _register(self, entity_id, room):
        previous = self._entity_room.get(entity_id)
        if previous != room and previous:
            peers = self._room_entities.get(previous)
            if peers is not None:
                peers.discard(entity_id)
                if not peers:
                    self._room_entities.pop(previous, None)
        self._entity_room[entity_id] = room
        self._room_entities.setdefault(room, set()).add(entity_id)

    def seed_state(self, states=(), presence_classes=(), profile=None):
        classes = {str(item).lower() for item in presence_classes or ()}
        for state in states or ():
            entity_id = state.get('entity_id')
            if not entity_id or entity_id in self.noise_entities or entity_id in self.non_occupancy_entities:
                continue
            attributes = state.get('attributes') or {}
            if str(attributes.get('device_class') or '').lower() not in classes:
                continue
            metadata = {}
            describe = getattr(profile, 'metadata', None)
            if describe is not None:
                metadata = describe(entity_id) or {}
            self._register(entity_id, self._presence_room(entity_id, {'area': attributes.get('area_id')}, metadata))
            current = str(state.get('state') or '').lower()
            if current in ON_STATES:
                self._entity_state[entity_id] = 'on'
            elif current in OFF_STATES:
                self._entity_state[entity_id] = 'off'
        if self._room_entities:
            log.info('[SessionManager] seeded rooms=%s entities=%s', len(self._room_entities), len(self._entity_room))
        return len(self._room_entities)

    def _room_still_occupied(self, room, except_entity=None):
        for entity in self._room_entities.get(room, ()):
            if entity == except_entity:
                continue
            if self._entity_state.get(entity) == 'on':
                return True
        return False

    def process(self, event):
        if event.get('category') != 'presence':
            return
        entity_id = event.get('entity_id')
        if entity_id in self.noise_entities or entity_id in self.non_occupancy_entities:
            return
        meta = event.get('metadata_json') or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta or '{}')
            except ValueError:
                meta = {}
        room = self._presence_room(entity_id, event, meta)
        state = str(event.get('new_state', '')).lower()
        self._register(entity_id, room)
        with self.db.connect() as con:
            opened = con.execute("SELECT * FROM sessions WHERE room=? AND status='open' ORDER BY id DESC LIMIT 1", (room,)).fetchone()
            if state in ON_STATES:
                self._entity_state[entity_id] = 'on'
                if opened:
                    return
                last_closed = con.execute("SELECT * FROM sessions WHERE room=? AND status='closed' AND end_ts IS NOT NULL ORDER BY datetime(end_ts) DESC, id DESC LIMIT 1", (room,)).fetchone()
                if last_closed:
                    last_end = self._parse_ts(last_closed['end_ts'])
                    this_start = self._parse_ts(event['ts'])
                    if last_end and this_start is not None:
                        gap = (this_start - last_end).total_seconds()
                        if gap <= self.merge_gap_seconds:
                            con.execute("UPDATE sessions SET status='open', end_ts=NULL, duration_seconds=NULL WHERE id=?", (last_closed['id'],))
                            return
                con.execute("INSERT INTO sessions(room,type,start_ts,status,evidence_json) VALUES(?,?,?,'open',?)", (room, 'room_activity', event['ts'], json.dumps([event.get('entity_id')], ensure_ascii=False)))
            elif state in OFF_STATES:
                self._entity_state[entity_id] = 'off'
                if self._room_still_occupied(room, except_entity=entity_id):
                    return
                if not opened:
                    return
                start = self._parse_ts(opened['start_ts'])
                end = self._parse_ts(event['ts'])
                duration = max(0, int((end - start).total_seconds())) if start and end else 0
                session_type = 'pass_by' if duration < self.pass_by_seconds else 'room_activity'
                con.execute("UPDATE sessions SET end_ts=?,duration_seconds=?,status='closed',type=? WHERE id=?", (event['ts'], duration, session_type, opened['id']))
            elif entity_id not in self._unknown_state_logged:
                self._unknown_state_logged.add(entity_id)
                log.info('[SessionManager] presence state is neither on nor off state=%s room=%s', state, room)

    def close_stale_open_sessions(self, max_age_hours=12):
        cutoff = datetime.now(timezone.utc) - timedelta(hours=int(max_age_hours))
        closed = 0
        with self.db.connect() as con:
            rows = con.execute("SELECT id, room, type, start_ts FROM sessions WHERE status='open'").fetchall()
            for row in rows:
                room = str(row['room'] or '')
                if row['type'] == 'sleep':
                    continue
                start = self._parse_ts(row['start_ts'])
                pseudo_room = bool(ENTITY_ID_LIKE_RE.fullmatch(room.strip()))
                too_old = row['type'] in {'room_activity', 'pass_by'} and start is not None and (start.astimezone(timezone.utc) < cutoff)
                if not (pseudo_room or too_old):
                    continue
                last = con.execute("SELECT MAX(ts) FROM events WHERE category='presence' AND area=?", (room,)).fetchone()
                end_text = last[0] if last and last[0] else row['start_ts']
                end = self._parse_ts(end_text) or start
                duration = max(0, int((end - start).total_seconds())) if start and end else 0
                con.execute("UPDATE sessions SET end_ts=?, duration_seconds=?, status='closed' WHERE id=?", (end_text, duration, row['id']))
                closed += 1
        if closed:
            log.info('[SessionManager] closed %s stale open session(s) max_age_hours=%s', closed, max_age_hours)
        return closed
