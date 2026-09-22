from copy import deepcopy
from datetime import datetime, timezone
from threading import RLock
from .automation_corrections import CONTROL_ATTRIBUTES, UNKNOWN_STATES, changed_dimensions
from .telemetry_stats import TelemetryWindow, timestamp

class EventAggregator:

    def __init__(self, database, correction_window_seconds=180):
        self.db = database
        self.environment = {}
        self.recent_automated = {}
        self.correction_window = correction_window_seconds
        self._lock = RLock()

    def process(self, event):
        with self._lock:
            if event['category'] in {'environment', 'appliance_metric'}:
                self.environment.setdefault(event['entity_id'], TelemetryWindow()).update(event)
                return None
            stored, context = (deepcopy(event), None)
            now, entity = (timestamp(event.get('ts')), event.get('entity_id'))
            domain = event.get('domain')
            actionable = domain in CONTROL_ATTRIBUTES and event.get('event_type') == 'state_changed' and (str(event.get('new_state')).lower() not in UNKNOWN_STATES)
            prior = self.recent_automated.get(entity)
            if now is not None and prior is not None:
                age = (now - prior[0]).total_seconds()
                if age > self.correction_window:
                    self.recent_automated.pop(entity, None)
                    prior = None
                elif age < 0:
                    return self.db.insert_event(stored)
            if now is not None and actionable:
                attrs = event.get('attributes_json') or {}
                if event['source'] == 'automation':
                    if prior is None or changed_dimensions(domain, prior[1], event.get('new_state'), prior[2], attrs):
                        context = (now, event.get('new_state'), deepcopy(attrs))
                elif prior is not None:
                    changed = changed_dimensions(domain, prior[1], event.get('new_state'), prior[2], attrs)
                    if changed and event['source'] == 'manual':
                        stored['event_type'] = 'automation_correction'
                        stored['category'] = 'automation_correction'
                        stored['metadata_json'] = {**(stored.get('metadata_json') or {}), 'automated_state': prior[1], 'automated_attributes': prior[2], 'manual_state': event.get('new_state'), 'seconds_after': int((now - prior[0]).total_seconds()), 'changed_dimensions': changed, 'correction_version': 'observed-override-v1'}
                    if changed:
                        context = False
            elif prior is not None and now is not None:
                context = False
            event_id = self.db.insert_event(stored)
            if context is False:
                self.recent_automated.pop(entity, None)
            elif context is not None:
                self.recent_automated[entity] = context
            return event_id

    def flush_environment(self):
        count = 0
        with self._lock:
            for entity, window in list(self.environment.items()):
                aggregate = window.aggregate()
                if aggregate is not None:
                    self.db.insert_event(aggregate)
                    count += 1
                del self.environment[entity]
            now = datetime.now(timezone.utc)
            self.recent_automated = {entity: item for entity, item in self.recent_automated.items() if (now - item[0]).total_seconds() <= self.correction_window}
        return count
