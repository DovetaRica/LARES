import copy
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from threading import RLock
ON_STATES = frozenset({'on', 'home', 'present', 'occupied', 'detected'})
OFF_STATES = frozenset({'off', 'not_home', 'absent', 'unoccupied', 'clear', 'not_detected'})
DOOR_OPEN_STATES = frozenset({'open', 'opened', 'on', '开', '开门', '打开', '已打开'})
UNLOCK_STATES = frozenset({'unlock', 'unlocked', '已开锁', '开锁'})
STATE_KEY = 'arrival_tracker_v2'

class ArrivalTracker:

    def __init__(self, database, outside_entity, entry_entity, pair_window_seconds=120, outside_label='门外', entry_label='门内玄关', entry_door_entity=None, door_label='入户门/门锁辅助', short_trip_seconds=60):
        self.db = database
        self.outside_entity, self.entry_entity = (outside_entity, entry_entity)
        self.entry_door_entity = entry_door_entity or None
        self.pair_window, self.short_trip_seconds = (int(pair_window_seconds), int(short_trip_seconds))
        self.outside_label, self.entry_label, self.door_label = (outside_label, entry_label, door_label)
        self._lock = RLock()
        signature = [outside_entity, entry_entity, self.entry_door_entity, self.pair_window, self.short_trip_seconds]
        self.state = {'version': 2, 'signature': signature, 'presence': {outside_entity: None, entry_entity: None}, 'anchor': None, 'door': None, 'pending_departure': None, 'last_event_ts': None, 'short_trip_count': 0}
        raw = self.db.scalar('SELECT value FROM system_state WHERE key=?', (STATE_KEY,))
        try:
            saved = json.loads(raw or '{}')
        except (TypeError, ValueError):
            saved = {}
        if isinstance(saved, dict) and saved.get('version') == 2 and (saved.get('signature') == signature):
            self.state.update(saved)
        else:
            for entity in (outside_entity, entry_entity):
                rows = self.db.rows('SELECT new_state FROM events WHERE entity_id=? ORDER BY id DESC LIMIT 1', (entity,))
                if rows:
                    self.state['presence'][entity] = self._presence(rows[0]['new_state'])

    @staticmethod
    def _parse(value):
        try:
            result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            return result if result.tzinfo else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _presence(value):
        text = str(value).lower()
        return True if text in ON_STATES else False if text in OFF_STATES else None

    @contextmanager
    def _update(self):
        before = copy.deepcopy(self.state)
        try:
            with self.db.connect() as con:
                yield con
                con.execute('INSERT INTO system_state(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at', (STATE_KEY, json.dumps(self.state, ensure_ascii=False), datetime.now(timezone.utc).isoformat()))
        except Exception:
            self.state = before
            raise

    def seed_state(self, states):
        roster = {item.get('entity_id'): self._presence(item.get('state')) for item in states}
        with self._lock:
            changed = {entity: roster.get(entity) for entity in self.state['presence'] if roster.get(entity) != self.state['presence'][entity]}
            if changed:
                with self._update():
                    self.state['presence'].update(changed)
                    self.state['anchor'] = None

    def _door_evidence(self, now):
        door = self.state.get('door')
        stamp = self._parse((door or {}).get('ts'))
        if stamp and 0 <= (now - stamp).total_seconds() <= self.pair_window:
            return door['kind']
        return 'unavailable'

    def _record(self, con, candidate):
        trigger = [{'entity': candidate['first'], 'label': self.entry_label if candidate['first'] == self.entry_entity else self.outside_label, 'rule_version': 'inside-outside-v2', 'identity_confirmed': False, 'door_evidence': candidate['door_evidence']}, {'entity': candidate['second'], 'label': self.entry_label if candidate['second'] == self.entry_entity else self.outside_label}]
        if self.entry_door_entity and candidate['door_evidence'] != 'unavailable':
            trigger.append({'entity': self.entry_door_entity, 'label': self.door_label})
        cursor = con.execute('INSERT INTO movements(kind,ts,window_seconds,trigger_json,created_at) VALUES(?,?,?,?,?)', (candidate['kind'], candidate['ts'], candidate['gap'], json.dumps(trigger, ensure_ascii=False), datetime.now(timezone.utc).isoformat()))
        return cursor.lastrowid

    def _expire(self, con, now):
        pending = self.state.get('pending_departure')
        if pending and (now - self._parse(pending['ts'])).total_seconds() > self.short_trip_seconds:
            row_id = self._record(con, pending)
            self.state['pending_departure'] = None
            return row_id
        return None

    def flush_pending(self, now=None):
        now = now or datetime.now(timezone.utc)
        with self._lock:
            pending = self.state.get('pending_departure')
            if not pending or (now - self._parse(pending['ts'])).total_seconds() <= self.short_trip_seconds:
                return None
            with self._update() as con:
                return self._expire(con, now)

    def process(self, event):
        entity = event.get('entity_id')
        if not entity or entity not in (self.entry_entity, self.outside_entity, self.entry_door_entity):
            return None
        now = self._parse(event.get('ts'))
        if now is None:
            return None
        with self._lock:
            last = self._parse(self.state.get('last_event_ts'))
            if last and now < last:
                return None
            with self._update() as con:
                row_id = self._expire(con, now)
                self.state['last_event_ts'] = now.isoformat()
                if entity == self.entry_door_entity:
                    value = str(event.get('new_state')).lower()
                    kind = 'open' if value in DOOR_OPEN_STATES else 'unlock_only' if value in UNLOCK_STATES else 'other'
                    self.state['door'] = {'ts': now.isoformat(), 'kind': kind}
                    return row_id
                new, old = (self._presence(event.get('new_state')), self._presence(event.get('old_state')))
                prior = self.state['presence'][entity]
                self.state['presence'][entity] = new
                if new is None or (old is not None and prior is not None and (old != prior)):
                    self.state['anchor'] = None
                    return row_id
                if old is None:
                    self.state['anchor'] = None
                    return row_id
                if not (old is False and new is True and (prior is not True)):
                    return row_id
                anchor = self.state.get('anchor')
                self.state['anchor'] = {'entity': entity, 'ts': now.isoformat()}
                if not anchor or anchor['entity'] == entity:
                    return row_id
                gap = (now - self._parse(anchor['ts'])).total_seconds()
                if gap == 0:
                    self.state['anchor'] = None
                    return row_id
                if not 0 < gap <= self.pair_window:
                    return row_id
                candidate = {'kind': 'arrival' if entity == self.entry_entity else 'departure', 'ts': now.isoformat(), 'gap': round(gap), 'first': anchor['entity'], 'second': entity, 'door_evidence': self._door_evidence(now)}
                if candidate['kind'] == 'departure':
                    self.state['pending_departure'] = candidate
                    return row_id
                pending = self.state.get('pending_departure')
                if pending and 0 <= (now - self._parse(pending['ts'])).total_seconds() <= self.short_trip_seconds:
                    self.state['pending_departure'] = None
                    self.state['short_trip_count'] += 1
                    return row_id
                return self._record(con, candidate)
