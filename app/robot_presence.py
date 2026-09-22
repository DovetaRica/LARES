import hashlib
import json
from datetime import datetime
MAX_ANNOTATIONS = 12
ACTIVE_STATES = {'vacuum': {'cleaning'}, 'washing_machine': {'start'}, 'dryer': {'start'}}

def _object(value):
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or '{}')
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}

def _stamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None

def _label(event, identity):
    details = identity(event)
    return (details.get('device') or details.get('name') or str(event.get('entity_id') or '未命名设备'))[:120]

def build_annotations(events, findings, identity, limit=MAX_ANNOTATIONS, incomplete_entities=()):
    blocked = {str(entity) for entity in incomplete_entities or ()}
    ordered = sorted((event for event in events if _stamp(event.get('ts'))), key=lambda event: (_stamp(event['ts']), int(event.get('id') or 0)))
    actors = {}
    for event in ordered:
        metadata = _object(event.get('metadata_json'))
        appliance_type = metadata.get('appliance_type')
        if appliance_type not in ACTIVE_STATES:
            continue
        if appliance_type != 'vacuum' and (not str(event.get('entity_id') or '').endswith('_yun_xing_zhuang_tai')):
            continue
        actors.setdefault((event.get('entity_id'), appliance_type), []).append(event)
    episodes = []
    for (actor, appliance_type), rows in actors.items():
        opened = None
        active_states = ACTIVE_STATES[appliance_type]
        for event in rows:
            old = str(event.get('old_state') or '').lower()
            new = str(event.get('new_state') or '').lower()
            if new in active_states and old not in active_states and (opened is None):
                opened = event
            elif opened is not None and new not in active_states:
                episodes.append((actor, appliance_type, opened, event))
                opened = None
    finding_map = {(item.get('actor_entity_id'), item.get('sensor_entity_id')): item for item in findings or [] if isinstance(item, dict)}
    result = []
    for event in ordered:
        if not (event.get('domain') == 'binary_sensor' and event.get('category') == 'presence' and (str(event.get('old_state') or '').lower() == 'off') and (str(event.get('new_state') or '').lower() == 'on')):
            continue
        if event.get('entity_id') in blocked:
            continue
        at = _stamp(event['ts'])
        for actor, appliance_type, opened, closed in episodes:
            if not _stamp(opened['ts']) <= at < _stamp(closed['ts']):
                continue
            actor_area = str(identity(opened).get('area') or opened.get('area') or '').strip()
            sensor_area = str(identity(event).get('area') or event.get('area') or '').strip()
            if appliance_type != 'vacuum' and (not actor_area or actor_area != sensor_area):
                continue
            sensor = event.get('entity_id')
            finding = finding_map.get((actor, sensor))
            suspicious = bool(finding and finding.get('status') in {'reported', 'needs_observation'})
            actor_name = _label(opened, identity)
            sensor_name = _label(event, identity)
            annotation_id = hashlib.sha256(f"robot-presence-v1|{actor}|{sensor}|{event['ts']}".encode('utf-8')).hexdigest()[:24]
            result.append({'annotation_id': annotation_id, 'classification': 'suspected_device_interference' if suspicious else 'undetermined', 'observed_at': event['ts'], 'actor': actor_name, 'sensor': sensor_name, 'area': (sensor_area or '区域未知')[:80], 'appliance_type': appliance_type, 'actor_state': '正在清扫' if appliance_type == 'vacuum' else '正在运行', 'spatial_evidence': '没有可靠且足够新鲜的扫地机器人实时房间位置' if appliance_type == 'vacuum' else '设备与人体传感器登记在同一区域', 'human_evidence': '尚无房主复核或经验证的独立人员证据', 'basis': '历史统计显示该传感器在清扫期间更常出现信号，仅提示值得复核' if suspicious else '只确认设备运行期间出现了人体信号，现有证据不足以判断来源', 'limitation': '不能据此确认无人、确认设备是原因，自动开灯也不是独立的人体证据', 'review_prompt': '请复核当时该房间是：无人、有人，还是不知道？', 'finding_id': finding.get('finding_id') if suspicious else None})
            if len(result) >= max(0, int(limit)):
                return result
    return result

def block_unreviewed_candidates(report, annotations):
    if not isinstance(report, dict) or not annotations:
        return (report, 0)
    terms = {str(item.get(key) or '').casefold() for item in annotations for key in ('actor', 'sensor') if str(item.get(key) or '').strip()}
    candidates = report.get('new_candidates')
    if not isinstance(candidates, list):
        return (report, 0)
    kept = []
    for candidate in candidates:
        text = json.dumps(candidate, ensure_ascii=False).casefold()
        if any((term in text for term in terms)):
            continue
        kept.append(candidate)
    filtered = dict(report)
    filtered['new_candidates'] = kept
    return (filtered, len(candidates) - len(kept))
