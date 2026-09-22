import json
from datetime import datetime, timedelta, timezone
logger = __import__('logging').getLogger(__name__)
ON_STATES = frozenset({'on', 'home', 'present', 'occupied', 'detected'})
DOOR_OPEN_STATES = frozenset({'open', 'opened', 'unlock', 'unlocked', 'on', '开', '开门', '打开', '已打开', '已开锁', '开锁'})

class ArrivalTracker:

    def __init__(self, database, outside_entity, entry_entity, pair_window_seconds=120, outside_label='门外', entry_label='玄关', entry_door_entity=None, door_label='入户门', short_trip_seconds=60):
        self.db = database
        self.outside_entity = outside_entity
        self.entry_entity = entry_entity
        self.entry_door_entity = entry_door_entity or None
        self.pair_window = int(pair_window_seconds)
        self.short_trip_seconds = int(short_trip_seconds)
        self.outside_label = outside_label
        self.entry_label = entry_label
        self.door_label = door_label
        self._last_on = {self.outside_entity: None, self.entry_entity: None}
        self._last_door_open = None
        self._last_departure_at = None
        self._resume_from_events()

    def _resume_from_events(self):
        since = (datetime.now(timezone.utc) - timedelta(seconds=self.pair_window * 4)).isoformat()
        for entity in (self.outside_entity, self.entry_entity):
            row = self.db.rows("SELECT ts FROM events WHERE entity_id=? AND new_state='on' AND datetime(ts)>=datetime(?) ORDER BY id DESC LIMIT 1", (entity, since))
            if row:
                try:
                    self._last_on[entity] = datetime.fromisoformat(row[0]['ts'].replace('Z', '+00:00'))
                except ValueError:
                    pass
        if self.entry_door_entity:
            rows = self.db.rows('SELECT ts,new_state FROM events WHERE entity_id=? AND datetime(ts)>=datetime(?) ORDER BY id DESC LIMIT 40', (self.entry_door_entity, since))
            for row in rows:
                if str(row['new_state']).lower() in DOOR_OPEN_STATES:
                    try:
                        self._last_door_open = datetime.fromisoformat(row['ts'].replace('Z', '+00:00'))
                        break
                    except ValueError:
                        pass

    def _parse(self, value):
        return datetime.fromisoformat(str(value).replace('Z', '+00:00'))

    def _door_recent(self, when):
        if not self.entry_door_entity:
            return True
        if self._last_door_open is None:
            return False
        gap = (when - self._last_door_open).total_seconds()
        return -self.pair_window <= gap <= self.pair_window * 2

    def process(self, event):
        entity = event.get('entity_id')
        now = None
        try:
            now = self._parse(event.get('ts'))
        except (TypeError, ValueError):
            return None
        if self.entry_door_entity and entity == self.entry_door_entity:
            if str(event.get('new_state')).lower() in DOOR_OPEN_STATES:
                self._last_door_open = now
            return None
        if entity not in self._last_on:
            return None
        if str(event.get('new_state')).lower() not in ON_STATES:
            return None
        other_entity = self.entry_entity if entity == self.outside_entity else self.outside_entity
        other_ts = self._last_on.get(other_entity)
        if other_ts is not None:
            gap = (now - other_ts).total_seconds()
            if 0 <= gap <= self.pair_window:
                kind = 'arrival' if entity == self.entry_entity else 'departure'
                self._last_on[other_entity] = None
                self._last_on[entity] = now
                if not self._door_recent(now):
                    logger.info('[Movement] %s pair ignored ts=%s (no %s open in window)', kind, event.get('ts'), self.door_label)
                    return None
                if kind == 'arrival' and self._last_departure_at is not None:
                    away = (now - self._last_departure_at).total_seconds()
                    if 0 <= away <= self.short_trip_seconds:
                        logger.info('[Movement] short trip suppressed ts=%s away=%.0fs (<%ss)', event.get('ts'), away, self.short_trip_seconds)
                        self._last_departure_at = None
                        return None
                if kind == 'departure':
                    self._last_departure_at = now
                else:
                    self._last_departure_at = None
                movement_id = self._record(kind, event.get('ts'), gap, other_entity, entity)
                return movement_id
        self._last_on[entity] = now
        return None

    def _record(self, kind, ts, gap_seconds, first_entity, second_entity):
        now = datetime.now(timezone.utc).isoformat()
        trigger = [{'entity': first_entity, 'label': self.entry_label if first_entity == self.entry_entity else self.outside_label}, {'entity': second_entity, 'label': self.entry_label if second_entity == self.entry_entity else self.outside_label}]
        if self.entry_door_entity:
            trigger.append({'entity': self.entry_door_entity, 'label': self.door_label})
        with self.db.connect() as con:
            cur = con.execute('INSERT INTO movements(kind,ts,window_seconds,trigger_json,created_at) VALUES(?,?,?,?,?)', (kind, ts, int(gap_seconds), json.dumps(trigger, ensure_ascii=False), now))
        logger.info('[Movement] %s recorded ts=%s window=%ss (%s -> %s%s)', kind, ts, int(gap_seconds), trigger[0]['label'], trigger[1]['label'], f' + {self.door_label}' if self.entry_door_entity else '')
        return cur.lastrowid
