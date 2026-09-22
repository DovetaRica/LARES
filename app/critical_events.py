from datetime import datetime
from .event_preprocessor import importance, _attrs, _parse_ts
SPIKE_DELTAS = {'temperature': 5.0, 'humidity': 20.0, 'carbon_dioxide': 500.0, 'pm25': 35.0, 'illuminance': 2000.0, 'power': 500.0, 'voltage': 20.0}
DEFAULT_SPIKE_DELTA = 10000.0
LONG_PRESENCE_MINUTES = 120
NIGHT_START_HOUR, NIGHT_END_HOUR = (23, 6)

def _local_hour(ts):
    return _parse_ts(ts).hour

def _device_class(event):
    return str(_attrs(event).get('device_class') or '')

def rule_opening(event):
    if event.get('category') == 'opening':
        return {'rule': 'opening', 'importance': 70}
    return None

def rule_lock(event):
    if event.get('domain') == 'lock' and event.get('new_state') in ('unlocked', 'locked'):
        return {'rule': 'lock', 'importance': 75}
    return None

def rule_unavailable(event):
    if event.get('new_state') == 'unavailable' or event.get('old_state') == 'unavailable':
        return {'rule': 'unavailable', 'importance': 80, 'direction': 'lost' if event.get('new_state') == 'unavailable' else 'restored'}
    return None

def rule_automation_correction(event):
    if event.get('event_type') == 'automation_correction':
        return {'rule': 'automation_correction', 'importance': 90}
    return None

def rule_env_spike(event):
    if event.get('event_type') not in ('environment_aggregate', 'appliance_metric_aggregate'):
        return None
    stats = event.get('metadata_json')
    if isinstance(stats, str):
        try:
            import json
            stats = json.loads(stats or '{}')
        except ValueError:
            stats = {}
    try:
        delta = abs(float((stats or {}).get('delta')))
    except (TypeError, ValueError):
        return None
    device_class = _device_class(event)
    threshold = SPIKE_DELTAS.get(device_class, DEFAULT_SPIKE_DELTA)
    if delta >= threshold:
        return {'rule': 'env_spike', 'importance': 60, 'delta': delta, 'unit': _attrs(event).get('unit_of_measurement')}
    return None

def rule_flapping(event):
    if event.get('kind') == 'flap':
        return {'rule': 'flapping', 'importance': 55}
    return None

def rule_long_presence(item):
    if item.get('kind') == 'interval' and item.get('state') == '有人':
        minutes = item.get('minutes') or 0
        if minutes >= LONG_PRESENCE_MINUTES:
            return {'rule': 'long_presence', 'importance': 45, 'minutes': minutes}
    return None

def rule_night_activity(event):
    if event.get('category') not in ('opening', 'presence', 'device_tracker'):
        return None
    if event.get('new_state') in ('off', 'not_home', 'away', 'closed', 'locked'):
        return None
    hour = _local_hour(event.get('ts') or '')
    if hour >= NIGHT_START_HOUR or hour < NIGHT_END_HOUR:
        return {'rule': 'night_activity', 'importance': 65}
    return None
RULES = (rule_opening, rule_lock, rule_unavailable, rule_automation_correction, rule_env_spike, rule_flapping)

def detect_events(events):
    found = []
    for event in events:
        score = importance(event)
        for rule in RULES:
            hit = rule(event)
            if hit:
                found.append({'ts': event.get('ts'), 'entity_id': event.get('entity_id'), 'area': event.get('area'), 'score': max(hit.pop('importance', score), score), **hit})
                break
    return found

def detect_items(items):
    found = []
    for item in items:
        hit = rule_long_presence(item)
        if hit:
            found.append({'ts': item.get('start'), 'entity_id': item.get('entity_id'), 'score': hit.pop('importance', 45), **hit})
    return found

def detect(events, preprocessed=None):
    found = detect_events(events) + detect_items((preprocessed or {}).compressed if preprocessed else [])
    seen, unique = (set(), [])
    for item in sorted(found, key=lambda entry: (-entry.get('score', 0), entry.get('ts') or '')):
        key = (item.get('rule'), item.get('entity_id'), item.get('ts'))
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique
