import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from .event_taxonomy import classify
from .llm_schema import compact_json, conservative_token_estimate

def parsed(value):
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None

def clipped_sessions(rows, start, end, room_label=lambda value: value):
    buckets = defaultdict(list)
    for row in rows:
        left, right = (parsed(row.get('start_ts')), parsed(row.get('end_ts')))
        room = room_label(row.get('room'))
        if not left or not room or re.fullmatch('[a-z_]+\\.[a-z0-9_]+', str(room)):
            continue
        right = min(right or end, end)
        left = max(left, start)
        if right <= left:
            continue
        kind = row.get('type')
        family = 'occupancy' if kind in ('room_activity', 'presence', 'pass_by') else kind
        item = dict(row, room=room, start_ts=left.astimezone(start.tzinfo).isoformat(), end_ts=right.astimezone(start.tzinfo).isoformat(), duration_seconds=int((right - left).total_seconds()), status='open' if not parsed(row.get('end_ts')) or parsed(row['end_ts']) > end else 'closed')
        buckets[room, family].append((left, right, item))
    result = []
    for (room, family), legs in buckets.items():
        merged = []
        for left, right, item in sorted(legs, key=lambda leg: leg[0]):
            if family == 'occupancy' and merged and (left <= merged[-1][1]):
                old_left, old_right, old = merged[-1]
                if right > old_right:
                    old['end_ts'], old['status'] = (item['end_ts'], item['status'])
                old['duration_seconds'] = int((max(right, old_right) - old_left).total_seconds())
                if item['type'] != 'pass_by':
                    old['type'] = item['type']
                merged[-1] = (old_left, max(right, old_right), old)
            else:
                merged.append((left, right, item))
        result.extend((item for _, _, item in merged))
    return sorted(result, key=lambda row: (-row['duration_seconds'], row['start_ts']))

def occupancy_rollup(rows):
    rooms = defaultdict(lambda: [0, 0])
    for row in rows:
        if row.get('type') in ('room_activity', 'presence', 'pass_by'):
            rooms[row['room']][0] += row['duration_seconds']
            rooms[row['room']][1] += 1
    ordered = sorted(rooms.items(), key=lambda item: (-item[1][0], item[0]))
    return {'note': '可信会话已截到窗口，并按房间合并重叠。分钟为各房间占用，跨房间可同时发生，不是人数或家庭活动总时长；单一占用信号不证明人员身份。', 'room_minutes_total': sum((value[0] for value in rooms.values())) // 60, 'rooms': [{'room': room, 'minutes': value[0] // 60, 'sessions': value[1]} for room, value in ordered[:12]], 'omitted_rooms': max(0, len(ordered) - 12)}

def trusted_session_rows(db, start, end, noise_entities=(), non_occupancy_entities=(), room_label=lambda room: room):
    from .event_preprocessor import detect_incomplete_entities, preprocess
    from .report_context_builder import next_events_after, _session_exclusions, session_inside_systemic_window
    lo, hi = (start.isoformat(), end.isoformat())
    events = db.rows("SELECT * FROM events WHERE datetime(ts)>=datetime(?) AND datetime(ts)<datetime(?) AND category='presence' ORDER BY datetime(ts),id", (lo, hi))
    suspects = detect_incomplete_entities(events + next_events_after(db, hi))
    gated = set(noise_entities) | set(non_occupancy_entities)
    pre = preprocess(events, range_end=hi, suspect_entities=suspects, noise_entities=noise_entities, non_occupancy_entities=non_occupancy_entities)
    excluded = _session_exclusions(db, lo, hi, suspects, gated)
    rows = db.rows('SELECT * FROM sessions WHERE datetime(start_ts)<datetime(?) AND (end_ts IS NULL OR datetime(end_ts)>datetime(?))', (hi, lo))
    rows = [row for row in rows if row['id'] not in excluded and (not session_inside_systemic_window(row, pre.systemic_windows))]
    return clipped_sessions(rows, start, end, room_label)

def window_facts(analyzer, events, sessions, start, end):
    step = timedelta(hours=1 if (end - start).total_seconds() <= 21600 else 6)
    slots, at = ([], start)
    while at < end:
        slots.append({'start': at.isoformat(), 'end': min(at + step, end).isoformat(), 'device_actions': 0, 'actions': [], 'omitted_actions': 0})
        at += step
    candidates = [[] for _ in slots]
    for event in events:
        if classify(event) != 'device_action':
            continue
        stamp = parsed(event.get('ts'))
        if stamp is None or not start <= stamp < end:
            continue
        idx = int((stamp - start).total_seconds() // step.total_seconds())
        slots[idx]['device_actions'] += 1
        candidates[idx].append(event)
    for slot, choices in zip(slots, candidates):
        choices.sort(key=lambda row: (row.get('event_type') != 'automation_correction', row.get('source') != 'manual', row.get('ts') or ''))
        seen = set()
        for row in choices:
            key = (row.get('entity_id'), row.get('new_state'), row.get('event_type'))
            if key in seen or len(slot['actions']) >= 3:
                continue
            seen.add(key)
            label = analyzer._identity(row)
            slot['actions'].append({'entity_id': row.get('entity_id'), 'device': label.get('device') or label.get('name'), 'area': label.get('area'), 'state': row.get('new_state'), 'ts': analyzer._local_time(row.get('ts'))})
        slot['omitted_actions'] = len(choices) - len(slot['actions'])
    block = {'note': '覆盖完整窗口的程序事实；示例有限，omitted_actions非零时不得断言没有其他动作。', 'slots': slots}
    while conservative_token_estimate(compact_json(block)) > 1800:
        populated = [slot for slot in slots if slot['actions']]
        if not populated:
            break
        slot = max(populated, key=lambda s: len(compact_json(s['actions'])))
        slot['actions'].pop()
        slot['omitted_actions'] += 1
    return block

def usable_report(value):
    try:
        body = json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        return False
    return isinstance(body, dict) and body.get('generation_status') == 'succeeded'

def report_coverage(rows, key, expected):

    def normalized(value):
        stamp = parsed(value)
        return stamp if stamp is not None else value
    by_key = {normalized(row[key]): row for row in rows}
    return {'windows': [{'key': value, 'status': 'available' if normalized(value) in by_key and usable_report(by_key[normalized(value)]['summary_json']) else 'unavailable'} for value in expected], 'note': '不可用窗口不提供报告正文；不得把缺失当作无活动。'}
