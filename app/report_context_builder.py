import json
import logging
from datetime import datetime, timedelta, timezone
logger = logging.getLogger(__name__)
MEMORY_SUMMARY_CHARS = 500
CRITICAL_POOL_LIMIT = 10
CRITICAL_PER_SEGMENT = 3

def _parse_ts(value):
    from .event_preprocessor import _parse_ts as parse
    return parse(value)

def _succeeded_segments(db, start_utc, end_utc):
    return db.rows("SELECT * FROM event_segments WHERE status='succeeded' AND datetime(period_start)>=datetime(?) AND datetime(period_end)<=datetime(?) ORDER BY start_event_id", (start_utc, end_utc))

def _uncovered_ranges(segments, window_first_id, window_last_id):
    ranges, cursor = ([], window_first_id)
    for segment in segments:
        start = max(int(segment['start_event_id']), window_first_id)
        end = min(int(segment['end_event_id']), window_last_id)
        if start > cursor:
            ranges.append((cursor, start - 1))
        cursor = max(cursor, end + 1)
    if cursor <= window_last_id:
        ranges.append((cursor, window_last_id))
    return ranges

def _fetch_range_events(db, start_id, end_id):
    return db.rows('SELECT id,ts,entity_id,domain,category,event_type,old_state,new_state,source,area,attributes_json,metadata_json FROM events WHERE id>=? AND id<=? ORDER BY id', (start_id, end_id))
AUTOMATION_RUNS_LIMIT = 16
OCCUPANCY_PRESENCE_LIMIT = 8
OCCUPANCY_DOOR_LIMIT = 8
OCCUPANCY_NOISE_LIMIT = 6
OCCUPANCY_INCOMPLETE_LIMIT = 8
OCCUPANCY_SYSTEMIC_LIMIT = 4
SYSTEMIC_ENTITIES_LIMIT = 6
OCCUPANCY_BLOCK_TOKEN_LIMIT = 2000

def _parse_dt(value):
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None

def session_inside_systemic_window(session_row, systemic_windows):
    if session_row.get('type') not in ('room_activity', 'pass_by'):
        return False
    start_dt = _parse_dt(session_row.get('start_ts'))
    end_dt = _parse_dt(session_row.get('end_ts'))
    if start_dt is None:
        return False
    for window in systemic_windows or []:
        w_start, w_end = (_parse_dt(window.get('window_start')), _parse_dt(window.get('window_end')))
        if w_start is None or w_end is None:
            continue
        if start_dt >= w_start and (end_dt is not None and end_dt <= w_end):
            return True
    return False

def next_events_after(db, end_utc):
    return db.rows('SELECT e.entity_id, e.old_state, e.new_state FROM events e JOIN (SELECT entity_id, MIN(id) AS nid FROM events WHERE datetime(ts)>=datetime(?)       GROUP BY entity_id) m ON e.id = m.nid', (end_utc,))

def _session_exclusions(db, start_utc, end_utc, suspects, gated_entities):
    rows = db.rows("SELECT id,evidence_json FROM sessions WHERE datetime(start_ts)<datetime(?) AND (end_ts IS NULL OR datetime(end_ts)>=datetime(?)) AND type IN ('room_activity','pass_by')", (end_utc, start_utc))
    excluded = {}
    for row in rows:
        try:
            import json as _json
            evidence = _json.loads(row.get('evidence_json') or '[]')
        except ValueError:
            evidence = []
        entities = [str(item) for item in evidence] if isinstance(evidence, list) else []
        if any((entity in suspects or entity in gated_entities for entity in entities)):
            excluded[row['id']] = True
    return excluded

def _filtered_sessions(session_rows, exclusions):
    return [row for row in session_rows if row.get('id') not in exclusions]

def _compact_item(item, keys):
    return {key: item[key] for key in keys if item.get(key) not in (None, '', [], {})}

def occupancy_summary_block(preprocessed, events):
    from .llm_schema import compact_json, conservative_token_estimate

    def build(scale):
        presence = [item for item in preprocessed.compressed if item.get('kind') == 'interval' and item.get('state') == '有人']
        presence.sort(key=lambda item: -(item.get('minutes') or 0))
        doors = {}
        for item in preprocessed.compressed:
            if item.get('class') != 'presence_raw' or item.get('kind') != 'state':
                continue
            entry = doors.setdefault(item.get('entity'), {'entity': item.get('entity'), 'device': item.get('device'), 'area': item.get('area'), 'count': 0, 'first': item.get('ts'), 'last': item.get('ts')})
            entry['count'] += 1
            entry['last'] = item.get('ts') or entry['last']
        door_list = sorted(doors.values(), key=lambda entry: -entry['count'])
        noise = sorted(preprocessed.sensor_noise, key=lambda item: -(item.get('excluded_flips') or 0))
        incomplete = sorted(preprocessed.incomplete_presence, key=lambda item: item.get('start') or '')
        windows = []
        for window in (preprocessed.systemic_windows or [])[:OCCUPANCY_SYSTEMIC_LIMIT]:
            windows.append({'entities': (window.get('entities') or [])[:SYSTEMIC_ENTITIES_LIMIT], 'count': window.get('count'), 'note': '多房间存在传感器同步锁定，判定为系统性误报，已从占用记录剔除'})
        presence_cap = max(2, OCCUPANCY_PRESENCE_LIMIT // scale)
        block = {'debounced_presence': [_compact_item(item, ('entity', 'device', 'area', 'state', 'start', 'end', 'minutes')) for item in presence[:presence_cap]], 'incomplete_presence': [_compact_item(item, ('entity', 'device', 'area', 'start', 'end', 'minutes')) for item in incomplete[:max(2, OCCUPANCY_INCOMPLETE_LIMIT // scale)]], 'door_contacts': [_compact_item(entry, ('entity', 'device', 'area', 'count', 'first', 'last')) for entry in door_list[:max(2, OCCUPANCY_DOOR_LIMIT // scale)]], 'noise_exclusions': [_compact_item(item, ('entity', 'device', 'area', 'excluded_flips', 'known_noisy')) for item in noise[:max(2, OCCUPANCY_NOISE_LIMIT // scale)]], 'systemic_locks': windows, 'notes': {'occupancy': '占用/进出以门磁开合+灯光点亮时长+去抖存在状态三源印证；单一存在传感器翻转不作为依据', 'incomplete_presence': '该时段采集信号不完整（历史去抖曾丢弃部分跳变），持续时长不可靠，不得据此判断占用'}}
        other_presence = len(presence) - len(block['debounced_presence'])
        other_doors = len(door_list) - len(block['door_contacts'])
        other_noise = sum((item.get('excluded_flips') or 0 for item in noise[len(block['noise_exclusions']):]))
        if other_presence > 0:
            block['other_presence_intervals'] = other_presence
        if other_doors > 0:
            block['other_door_contacts'] = other_doors
        if other_noise > 0:
            block['other_noise_excluded_flips'] = other_noise
        return block
    scale = 1
    block = build(scale)
    while scale < 8 and conservative_token_estimate(compact_json(block)) > OCCUPANCY_BLOCK_TOKEN_LIMIT:
        scale *= 2
        block = build(scale)
    return block

def _segment_block(segment):
    critical = []
    try:
        stored = json.loads(segment.get('critical_events_json') or '[]')
    except ValueError:
        stored = []
    for item in stored[:CRITICAL_PER_SEGMENT]:
        critical.append({'rule': item.get('rule'), 'ts': item.get('ts'), 'entity_id': item.get('entity_id')})
    block = {'period_start': segment['period_start'], 'period_end': segment['period_end'], 'summary': str(segment.get('summary_text') or '')[:MEMORY_SUMMARY_CHARS], 'event_count': segment['event_count'], 'critical': critical, 'model': segment.get('model')}
    try:
        body = json.loads(segment.get('summary_json') or '{}')
    except ValueError:
        body = {}
    if isinstance(body, dict) and body.get('kind') == 'level1_slice':
        occupancy = body.get('occupancy')
        if isinstance(occupancy, list) and occupancy:
            block['occupancy'] = occupancy[:12]
        automation = body.get('automation_runs')
        if isinstance(automation, list) and automation:
            block['automation_runs'] = automation[:8]
        if body.get('sensor_noise_count') is not None:
            block['sensor_noise_count'] = body.get('sensor_noise_count')
        crit = body.get('critical')
        if isinstance(crit, list) and crit:
            block['critical'] = crit[:CRITICAL_PER_SEGMENT]
        if body.get('ai_summary'):
            block['ai_summary'] = str(body.get('ai_summary'))[:MEMORY_SUMMARY_CHARS]
            block['summary'] = block['ai_summary']
    return block

def _recent_detail(events, end_local, minutes, identities=None, suspect_entities=(), noise_entities=(), non_occupancy_entities=()):
    from .event_preprocessor import preprocess
    if not events:
        return []
    cutoff = min(_parse_ts(events[-1]['ts']), (end_local - timedelta(minutes=minutes)).astimezone(timezone.utc))
    recent = [event for event in events if _parse_ts(event['ts']) >= cutoff]
    preprocessed = preprocess(recent, range_end=end_local.astimezone(timezone.utc).isoformat(), identities=identities, suspect_entities=suspect_entities, noise_entities=noise_entities, non_occupancy_entities=non_occupancy_entities)
    preprocessed.compressed.reverse()
    return preprocessed.compressed

async def build_window_payload(analyzer, start_local, end_local, scope, caps=None):
    db, tz = (analyzer.db, analyzer.tz)
    start_utc = start_local.astimezone(timezone.utc).isoformat()
    end_utc = end_local.astimezone(timezone.utc).isoformat()
    segment_memory = getattr(analyzer, 'segment_memory', None)

    async def with_current_snapshot(payload):
        ha = getattr(analyzer, 'ha', None)
        if ha is None:
            return payload
        states = await ha.current_states()
        if states:
            payload['home_state_snapshot'] = {'taken_at': datetime.now(tz).isoformat(), 'note': '以下状态拍摄于报告生成时刻，只代表生成当时，不代表报告时段内一直如此，也不一定等于时段结束瞬间。', 'entities': ha.compact_snapshot(states)}
        return payload
    if segment_memory is None or not segment_memory.enabled:
        return await with_current_snapshot(analyzer._window_payload(start_local, end_local, scope, caps=caps))
    window_meta = db.rows('SELECT id,ts FROM events WHERE datetime(ts)>=datetime(?) AND datetime(ts)<datetime(?) ORDER BY id', (start_utc, end_utc))
    if not window_meta:
        return await with_current_snapshot(analyzer._window_payload(start_local, end_local, scope, caps=caps))
    window_first_id, window_last_id = (window_meta[0]['id'], window_meta[-1]['id'])
    segments = _succeeded_segments(db, start_utc, end_utc)
    if not segments:
        return await with_current_snapshot(analyzer._window_payload(start_local, end_local, scope, caps=caps))
    caps = caps or analyzer.REGULAR_CAPS
    uncovered = _uncovered_ranges(segments, window_first_id, window_last_id)
    uncovered_events = []
    for start_id, end_id in uncovered:
        uncovered_events.extend(_fetch_range_events(db, start_id, end_id))
    all_window_events = _fetch_range_events(db, window_first_id, window_last_id)
    identities = analyzer._identity_map(all_window_events)
    from .event_preprocessor import detect_incomplete_entities
    suspects = detect_incomplete_entities(all_window_events + next_events_after(db, end_utc))
    session_exclusions = _session_exclusions(db, start_utc, end_utc, suspects, getattr(analyzer, 'presence_noise_entities', frozenset()) | getattr(analyzer, 'presence_non_occupancy_entities', frozenset()))
    collection_start = db.scalar("SELECT value FROM system_state WHERE key='collection_started_at'") or start_utc
    sessions = db.rows('SELECT id,room,type,start_ts,end_ts,duration_seconds,status FROM sessions WHERE datetime(start_ts)<datetime(?) AND (end_ts IS NULL OR datetime(end_ts)>=datetime(?)) ORDER BY COALESCE(duration_seconds,0) DESC LIMIT ?', (end_utc, start_utc, caps['sessions']))
    patterns = db.rows('SELECT id,type,description,confidence,sample_count,status FROM patterns ORDER BY confidence DESC LIMIT ?', (caps['patterns'],))
    cooccurrence = analyzer._cooccurrence_payload(end_local)
    from .robot_presence import build_annotations
    robot_presence = build_annotations(all_window_events, cooccurrence['full'], analyzer._identity, caps['robot_presence_annotations'], incomplete_entities=suspects)
    memory_segments = [_segment_block(segment) for segment in segments]
    groups = analyzer._event_groups(uncovered_events, limit=caps['event_groups'])
    evidence_by_id = {group['evidence_id']: {'evidence_id': group['evidence_id'], 'semantic_key': group['semantic_key'], 'status': 'reported', 'sample_count': group['count'], 'local_days': group['local_days'], 'first_ts': group['first_ts'], 'last_ts': group['last_ts'], 'states': group['states']} for group in groups}
    for group in groups:
        group['first_ts'] = analyzer._local_time(group['first_ts'])
        group['last_ts'] = analyzer._local_time(group['last_ts'])
    representative = analyzer._representative_events(uncovered_events, limit=caps['representative_events'])
    important = [{key: analyzer._local_time(event.get(key)) if key == 'ts' else event.get(key) for key in ('category', 'event_type', 'source', 'entity_id', 'area', 'new_state', 'ts')} for event in representative]
    for item, event in zip(important, representative):
        item.update(analyzer._identity(event))
    from .critical_events import detect as detect_critical
    pre_uncovered = None
    pool = detect_critical(uncovered_events)
    for segment in segments:
        try:
            stored = json.loads(segment.get('critical_events_json') or '[]')
        except ValueError:
            stored = []
        for item in stored:
            pool.append(item)
    pool.sort(key=lambda item: (-item.get('score', 0), item.get('ts') or ''))
    seen, critical_pool = (set(), [])
    for item in pool:
        key = (item.get('rule'), item.get('entity_id'), item.get('ts'))
        if key in seen:
            continue
        seen.add(key)
        critical_pool.append(item)
    critical_pool = critical_pool[:CRITICAL_POOL_LIMIT]
    recent_detail = _recent_detail(all_window_events, end_local, segment_memory.recent_detail_minutes, identities=identities, suspect_entities=suspects, noise_entities=analyzer.presence_noise_entities, non_occupancy_entities=getattr(analyzer, 'presence_non_occupancy_entities', frozenset()))
    from .event_preprocessor import preprocess
    pre_uncovered = preprocess(uncovered_events, range_end=end_utc, noise_entities=analyzer.presence_noise_entities, identities=identities, suspect_entities=suspects, non_occupancy_entities=getattr(analyzer, 'presence_non_occupancy_entities', frozenset()))
    occupancy_block = occupancy_summary_block(pre_uncovered, uncovered_events)
    occupancy_block['systemic_locks'] = pre_uncovered.systemic_windows
    occupancy_block['non_occupancy'] = pre_uncovered.non_occupancy[:8]
    occupancy_block.setdefault('notes', {})['excluded_sessions'] = f'另有 {len(session_exclusions)} 个在场会话因来源传感器信号不可靠（噪声/不完整/系统性误报）未纳入本次分析，避免误报停留时长'
    automation_runs = pre_uncovered.automation_runs
    movements = analyzer._movements_in_window(start_local, end_local)
    payload = {'analysis_scope': scope, 'period_start': start_local.isoformat(), 'period_end': end_local.isoformat(), 'total_events': len(all_window_events), 'event_counts': analyzer._counts(all_window_events), 'memory_segments': memory_segments, 'critical_events': critical_pool, 'recent_detail': recent_detail, 'movements': movements, 'occupancy_summary': occupancy_block, 'automation_runs': automation_runs[:AUTOMATION_RUNS_LIMIT], 'robot_presence_annotations': robot_presence, 'event_groups': groups, 'representative_events': important, 'entity_identity_catalog': analyzer._identity_catalog(uncovered_events or all_window_events), 'metric_summaries': analyzer._metric_summaries(uncovered_events, limit=caps['metric_summaries'], identities=identities), 'user_guidance': analyzer._window_guidance(scope, start_local), 'reviewed_pattern_feedback': analyzer._reviewed_feedback(), 'timezone': 'Asia/Shanghai (UTC+08:00, Beijing Time)', 'sessions': [{**row, 'room': analyzer._session_room(row.get('room'))} for row in _filtered_sessions([row for row in analyzer._localize_rows(sessions, ('start_ts', 'end_ts')) if not session_inside_systemic_window(row, pre_uncovered.systemic_windows)], session_exclusions)], 'existing_patterns': patterns, 'deterministic_anomalies': analyzer._localize_rows(analyzer.anomaly_detector.detect(end_local.astimezone(timezone.utc)), ('first_seen', 'last_seen')), 'cooccurrence_findings': cooccurrence['compact'], 'cooccurrence_findings_full': cooccurrence['full'], 'cooccurrence_meta': cooccurrence['meta'], '_evidence_by_id': evidence_by_id}
    return await with_current_snapshot(payload)
