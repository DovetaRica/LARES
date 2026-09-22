import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from .event_taxonomy import classify, polling_info
from .telemetry_stats import summarize_samples
FLAP_COUNT = 8
FLAP_SPAN_MINUTES = 15
PRESENCE_ON = frozenset({'on', 'home', 'present', 'occupied', 'detected', 'motion'})
IDLE_STATES = frozenset({'off', 'not_home', 'away', 'clear', 'idle', 'standby', 'unavailable'})
INTERVAL_CATEGORIES = frozenset({'device', 'media', 'presence', 'device_tracker'})
PRESENCE_MIN_VALID_SECONDS = 90
FLIP_WINDOW_MINUTES = 10
FLIP_NOISE_THRESHOLD = 6
SYNC_ON_SPREAD_SECONDS = 120
SYNC_OFF_SPREAD_SECONDS = 120
SYNC_CLUSTER_MIN_ENTITIES = 3

def _parse_ts(value):
    return datetime.fromisoformat(str(value).replace('Z', '+00:00'))

def _parse_ts_safe(value):
    try:
        return _parse_ts(value)
    except (TypeError, ValueError):
        return None

def _attrs(event):
    value = event.get('attributes_json')
    if isinstance(value, str):
        try:
            value = json.loads(value or '{}')
        except ValueError:
            value = {}
    return value if isinstance(value, dict) else {}

def _meta(event):
    value = event.get('metadata_json')
    if isinstance(value, str):
        try:
            value = json.loads(value or '{}')
        except ValueError:
            value = {}
    return value if isinstance(value, dict) else {}

def importance(event):
    metric_class = event.get('metric_class') or classify(event)
    if event.get('event_type') == 'automation_correction':
        return 90
    if event.get('new_state') == 'unavailable' or event.get('old_state') == 'unavailable':
        return 80
    if event.get('category') == 'opening':
        return 70
    if event.get('source') == 'manual':
        return 50
    if metric_class == 'automation_run':
        return 40
    if event.get('category') == 'appliance':
        return 35
    if event.get('category') in ('presence', 'device_tracker'):
        return 30
    if event.get('category') == 'media':
        return 25
    if metric_class == 'device_action':
        return 20
    if metric_class == 'sensor_telemetry':
        return 10
    return 5

@dataclass
class PreprocessedSegment:
    raw_count: int
    compressed: list = field(default_factory=list)
    automation_runs: list = field(default_factory=list)
    sensor_noise: list = field(default_factory=list)
    incomplete_presence: list = field(default_factory=list)
    non_occupancy: list = field(default_factory=list)
    systemic_windows: list = field(default_factory=list)
    start_state: dict = field(default_factory=dict)
    end_state: dict = field(default_factory=dict)

    @property
    def compressed_count(self):
        return len(self.compressed)

    @property
    def compression_ratio(self):
        return 1.0 - self.compressed_count / self.raw_count if self.raw_count else 0.0

def _mark_class(item, event):
    item['class'] = event.get('metric_class') or classify(event)
    return item

def _merge_intervals(events, range_end, category=None):
    is_presence = category in ('presence', 'device_tracker')
    intervals = []
    open_item = None
    for event in events:
        state = str(event.get('new_state'))
        brightness = _attrs(event).get('brightness')
        previous_end = event.get('ts')
        event_old = event.get('old_state')
        if open_item is not None and event_old is not None and (str(event_old) != open_item['state']):
            open_item['end'] = open_item.get('last_ts') or open_item['start']
            open_item['stream_incomplete'] = True
            intervals.append(open_item)
            open_item = None
        if open_item is not None and state != open_item['state']:
            open_item['end'] = event.get('ts')
            intervals.append(open_item)
            open_item = None
        if open_item is None:
            open_item = {'kind': 'interval', 'entity': event.get('entity_id'), 'state': state, 'start': event.get('ts'), 'importance': importance(event)}
        open_item['last_ts'] = event.get('ts')
        if brightness is not None:
            try:
                value = float(brightness)
                low, high = open_item.get('brightness', (value, value))
                open_item['brightness'] = (min(low, value), max(high, value))
            except (TypeError, ValueError):
                pass
    if open_item is not None:
        open_item['end'] = range_end
        open_item['ongoing'] = True
        intervals.append(open_item)
    result = []
    for item in intervals:
        if item.get('state') in IDLE_STATES:
            continue
        start, end = (_parse_ts_safe(item.get('start')), _parse_ts_safe(item.get('end')))
        if start is not None and end is not None:
            item['duration_seconds'] = (end - start).total_seconds()
            item['minutes'] = round(item['duration_seconds'] / 60, 1)
        if is_presence and item.get('state') in PRESENCE_ON:
            item['state'] = '有人'
        if 'brightness' in item:
            item['brightness'] = [round(item['brightness'][0], 1), round(item['brightness'][1], 1)]
        if not item.get('ongoing'):
            item.pop('ongoing', None)
        result.append(item)
    return result

def _is_state_edge(event):
    old_state = event.get('old_state')
    new_state = event.get('new_state')
    if old_state is None or new_state is None:
        return True
    return str(old_state) != str(new_state)

def _gate_occupancy(group, known_noisy):
    total = len(group)
    flags = [known_noisy] * total
    if not known_noisy:
        edge_indices = [index for index, event in enumerate(group) if _is_state_edge(event)]
        for pos, index in enumerate(edge_indices):
            start = _parse_ts_safe(group[index].get('ts'))
            if start is None:
                continue
            window = 0
            for later in edge_indices[pos:]:
                other_ts = _parse_ts_safe(group[later].get('ts'))
                if other_ts is None or other_ts - start > timedelta(minutes=FLIP_WINDOW_MINUTES):
                    break
                window += 1
            if window > FLIP_NOISE_THRESHOLD:
                for offset in range(window):
                    flags[edge_indices[pos + offset]] = True
    clean = [event for event, noisy in zip(group, flags) if not noisy]
    noise_entries = []
    excluded = sum((1 for flag in flags if flag))
    if excluded:
        noise_entries.append({'kind': 'sensor_noise', 'entity': group[0].get('entity_id'), 'excluded_flips': excluded, 'transitions': total, 'first': group[0].get('ts'), 'last': group[-1].get('ts'), 'known_noisy': known_noisy, 'importance': 15, 'note': '存在传感器原始翻转经去抖/噪声门控剔除，不用于占用推断'})
    return (clean, noise_entries)

def _phantom_filter(intervals, entity):
    kept, phantoms = ([], 0)
    for item in intervals:
        seconds = item.get('duration_seconds')
        if seconds is None:
            minutes = item.get('minutes')
            seconds = minutes * 60 if minutes is not None else None
        if item.get('state') == '有人' and seconds is not None and (seconds < PRESENCE_MIN_VALID_SECONDS):
            phantoms += 1
            continue
        kept.append(item)
    return (kept, phantoms)

def _noise_entry(entity, group, known_noisy, excluded):
    return {'kind': 'sensor_noise', 'entity': entity, 'excluded_flips': excluded, 'transitions': len(group), 'first': group[0].get('ts'), 'last': group[-1].get('ts'), 'known_noisy': known_noisy, 'importance': 15, 'note': '存在传感器原始翻转经去抖/噪声门控剔除，不用于占用推断'}

def _env_summary(event):
    stats = _meta(event)
    item = {'kind': 'env', 'entity': event.get('entity_id'), 'start': stats.get('period_start'), 'end': event.get('ts'), 'importance': importance(event)}
    for key in ('min', 'max', 'avg', 'delta', 'count'):
        if stats.get(key) is not None:
            item[key] = stats[key]
    unit = _attrs(event).get('unit_of_measurement')
    if unit:
        item['unit'] = unit
    try:
        delta = float(stats.get('delta'))
        item['trend'] = '上升' if delta > 0 else '下降' if delta < 0 else '平稳'
    except (TypeError, ValueError):
        pass
    return _mark_class(item, event)

def _telemetry_summary(entity, group):
    first, last = (group[0], group[-1])
    item = {'kind': 'telemetry', 'entity': entity, 'start': first.get('ts'), 'end': last.get('ts'), 'count': len(group), 'first': first.get('new_state'), 'last': last.get('new_state'), 'importance': 10}
    statistics = summarize_samples(group, _meta)
    for key in ('first', 'last'):
        if key in statistics:
            statistics[key] = str(statistics[key]).removesuffix('.0')
    item.update(statistics)
    unit = _attrs(last).get('unit_of_measurement')
    if unit:
        item['unit'] = unit
    return _mark_class(item, last)

def _discrete_event(event):
    item = {'kind': 'state', 'entity': event.get('entity_id'), 'ts': event.get('ts'), 'from': event.get('old_state'), 'to': event.get('new_state'), 'source': event.get('source'), 'importance': importance(event)}
    if event.get('event_type') == 'automation_correction':
        stats = _meta(event)
        item.update({'kind': 'correction', 'automated': stats.get('automated_state'), 'manual': stats.get('manual_state'), 'after_s': stats.get('seconds_after')})
    return _mark_class(item, event)

def _automation_summary(entity, group):
    polling = polling_info(entity)
    item = {'kind': 'automation', 'entity': entity, 'count': len(group), 'first': group[0].get('ts'), 'last': group[-1].get('ts'), 'importance': 40}
    name = (polling or {}).get('name') or _meta(group[0]).get('name')
    if name:
        item['name'] = name
    if polling:
        item['polling'] = {'period_seconds': polling.get('period_seconds'), 'kind': polling.get('kind'), 'expected_per_day': round(86400 / polling['period_seconds']) if polling.get('period_seconds') else None, 'note': '定时巡检/看门狗节拍，属设计行为，与人员活动无关'}
    else:
        item['registered'] = False
        item['note'] = '触发来源未登记，不得用于行为或异常推断'
    return item

def _flapping_entry(entity, group):
    first, last = (group[0], group[-1])
    span = (_parse_ts(last.get('ts')) - _parse_ts(first.get('ts'))).total_seconds() / 60
    return {'kind': 'flap', 'entity': entity, 'first': first.get('ts'), 'last': last.get('ts'), 'transitions': len(group), 'span_min': round(span, 1), 'states': sorted({str(e.get('new_state')) for e in group}), 'importance': 55}

def detect_incomplete_entities(events):
    previous = {}
    suspects = set()
    for event in events:
        entity = event.get('entity_id')
        if not entity:
            continue
        old_state = event.get('old_state')
        prior = previous.get(entity)
        if old_state is not None and prior is not None and (str(old_state) != prior):
            suspects.add(entity)
        new_state = event.get('new_state')
        if new_state is not None:
            previous[entity] = str(new_state)
    return suspects

def _gate_synchronized_locks(presence_intervals):
    on_items = [item for item in presence_intervals if item.get('state') == '有人' and _parse_ts_safe(item.get('start'))]
    on_items.sort(key=lambda item: _parse_ts(item['start']))
    excluded, windows = (set(), [])
    cluster = []
    for item in on_items + [None]:
        if item is not None and cluster:
            spread = (_parse_ts(item['start']) - _parse_ts(cluster[0]['start'])).total_seconds()
            if spread <= SYNC_ON_SPREAD_SECONDS:
                cluster.append(item)
                continue
        if len(cluster) >= SYNC_CLUSTER_MIN_ENTITIES:
            entities = {item['entity'] for item in cluster}
            ends = [_parse_ts_safe(item.get('end')) for item in cluster]
            ongoing = sum((1 for item in cluster if item.get('ongoing')))
            real_ends = [ts for ts in ends if ts is not None]
            if len(entities) >= SYNC_CLUSTER_MIN_ENTITIES and ongoing <= 1 and real_ends and ((max(real_ends) - min(real_ends)).total_seconds() <= SYNC_OFF_SPREAD_SECONDS):
                window_start = min((_parse_ts(item['start']) for item in cluster))
                window_end = max(real_ends)
                tz = window_start.tzinfo
                windows.append({'window_start': window_start.astimezone(tz).isoformat(), 'window_end': window_end.astimezone(tz).isoformat(), 'entities': sorted(entities), 'count': len(cluster), 'note': '多房间存在传感器同步锁定（同时进入有人、同时恢复），判定为系统性误报，已从占用记录剔除'})
                excluded.update((id(item) for item in cluster))
        cluster = [item] if item is not None else []
    kept = [item for item in presence_intervals if id(item) not in excluded]
    return (kept, windows)

def preprocess(events, range_end=None, state_before=None, noise_entities=(), identities=None, suspect_entities=(), non_occupancy_entities=()):
    events = [event for event in events if event.get('entity_id')]
    range_end = range_end or (events[-1].get('ts') if events else None)
    noise_entities = set(noise_entities or ())
    suspect_entities = set(suspect_entities or ())
    non_occupancy_entities = set(non_occupancy_entities or ())
    by_entity = {}
    for event in events:
        by_entity.setdefault(event['entity_id'], []).append(event)
    compressed, automation_runs, sensor_noise = ([], [], [])
    presence_intervals, incomplete_presence = ([], [])
    non_occupancy_legs = []
    for entity, group in by_entity.items():
        classes = {classify(event) for event in group}
        if classes == {'sensor_telemetry'}:
            compressed.append(_telemetry_summary(entity, group))
            continue
        if classes == {'automation_run'}:
            automation_runs.append(_automation_summary(entity, group))
            continue
        first = group[0]
        if classify(first) == 'presence_raw' and first.get('category') == 'opening':
            for event in group:
                compressed.append(_discrete_event(event))
            continue
        if classify(first) == 'presence_raw':
            if entity in non_occupancy_entities:
                legs = _merge_intervals(group, range_end, category=first.get('category'))
                active = [item for item in legs if item.get('state') == '有人']
                if active:
                    non_occupancy_legs.append({'entity': entity, 'legs': len(active), 'note': '非房间占用信号（门外/电子围栏），仅供到离与关联分析'})
                continue
            if entity in suspect_entities:
                suspect_legs = _merge_intervals(group, range_end, category=first.get('category'))
                for item in suspect_legs:
                    item['stream_incomplete'] = True
                incomplete_presence.extend(suspect_legs)
                continue
            clean, noise_entries = _gate_occupancy(group, entity in noise_entities)
            sensor_noise.extend(noise_entries)
            if clean:
                intervals = _merge_intervals(clean, range_end, category=first.get('category'))
                incomplete = [item for item in intervals if item.get('stream_incomplete')]
                intervals = [item for item in intervals if not item.get('stream_incomplete')]
                incomplete_presence.extend(incomplete)
                kept, phantoms = _phantom_filter(intervals, entity)
                if phantoms:
                    entry = next((item for item in sensor_noise if item.get('entity') == entity), None)
                    if entry is None:
                        entry = _noise_entry(entity, group, entity in noise_entities, 0)
                        sensor_noise.append(entry)
                    entry['excluded_flips'] = entry.get('excluded_flips', 0) + phantoms
                presence_intervals.extend(kept)
            continue
        state_changes = [event for event in group if classify(event) != 'sensor_telemetry']
        if len(state_changes) >= FLAP_COUNT and (_parse_ts_safe(state_changes[-1]['ts']) - _parse_ts_safe(state_changes[0]['ts'])).total_seconds() <= FLAP_SPAN_MINUTES * 60:
            compressed.append(_flapping_entry(entity, state_changes))
            continue
        if classify(first) == 'device_action' and first.get('category') in INTERVAL_CATEGORIES | {'media'}:
            compressed.extend((_mark_class(item, group[0]) for item in _merge_intervals(group, range_end, category=first.get('category'))))
            continue
        for event in group:
            compressed.append(_discrete_event(event))
    kept_presence, systemic_windows = _gate_synchronized_locks(presence_intervals)
    compressed.extend(kept_presence)
    if identities:
        for bucket in (compressed, sensor_noise, automation_runs, incomplete_presence):
            for item in bucket:
                label = identities.get(item.get('entity')) if item.get('entity') else None
                if label:
                    for key in ('device', 'area', 'name'):
                        if label.get(key):
                            item.setdefault(key, label[key])
    compressed.sort(key=lambda item: item.get('start') or item.get('ts') or '')
    automation_runs.sort(key=lambda item: item.get('first') or '')
    entities = sorted({event['entity_id'] for event in events})
    state_before = state_before or {}
    start_state = {entity: state_before[entity] for entity in entities if entity in state_before}
    end_state = {}
    for entity in entities:
        for event in reversed(by_entity[entity]):
            if event.get('new_state') is not None:
                end_state[entity] = event.get('new_state')
                break
    return PreprocessedSegment(raw_count=len(events), compressed=compressed, automation_runs=automation_runs, sensor_noise=sensor_noise, incomplete_presence=incomplete_presence, non_occupancy=non_occupancy_legs, systemic_windows=systemic_windows, start_state=start_state, end_state=end_state)
