import hashlib
import json
import re
import sqlite3
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from .event_taxonomy import classify
from .proactive_contract import guidance_mentions_identity
from .llm_schema import compact_json, conservative_token_estimate
from .telemetry_stats import summarize_samples
BEIJING = ZoneInfo('Asia/Shanghai')
TOOLS = frozenset({'entity_identity', 'window_events', 'owner_guidance'})
GUIDANCE_SCOPES = frozenset({'daily', 'period', 'weekly'})
LOOKBACK_DAYS, MAX_ROW_LIMIT, MAX_TOKEN_BUDGET, MAX_METRIC_GROUPS = (14, 256, 700, 8)
READ_TIMEOUT_SECONDS, PROGRESS_DEADLINE_SECONDS = (2.0, 1.0)
GENERAL_MARKERS = ('通用规则', '全局规则', '所有设备', '任何设备', '全部设备')
IDENTITY_FIELDS = ('entity_id', 'device', 'name', 'area', 'appliance_type')
MASK_PATTERNS = ((re.compile('\\d{1,2}\\s*[:：]\\s*\\d{2}'), '某时刻'), (re.compile('\\d{1,2}\\s*月\\s*\\d{1,2}\\s*日'), '某日'), (re.compile('\\d+\\s*(?:分钟|小时|秒钟|天|周)'), '若干时间'), (re.compile('\\d+\\s*次'), '若干次'))
WINDOW_SQL = 'SELECT id,domain,category,event_type,source,entity_id,area,old_state,new_state,attributes_json,metadata_json,ts FROM events WHERE entity_id=? AND julianday(ts)>=julianday(?) AND julianday(ts)<julianday(?) ORDER BY julianday(ts),id LIMIT ?'
OWNER_SQL = "SELECT id,content,scope,source,created_at FROM ai_guidance WHERE active=1 AND scope IN ('all',?) AND julianday(created_at)<=julianday(?) ORDER BY id DESC LIMIT ?"

def _bound(value, ceiling):
    if value is None:
        return ceiling
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError('invalid query bound')
    return min(value, ceiling)

def _lookback_days(group):
    days = group.get('lookback_days', LOOKBACK_DAYS)
    valid = not isinstance(days, bool) and isinstance(days, int) and (1 <= days <= LOOKBACK_DAYS)
    return days if valid else LOOKBACK_DAYS

def _beijing(value):
    if not isinstance(value, datetime):
        try:
            value = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        except (TypeError, ValueError):
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(BEIJING).isoformat()

def _json_object(value):
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or '{}')
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}

def _mask_guidance_numbers(content):
    head, marker, tail = str(content).partition('房主回答')
    for pattern, replacement in MASK_PATTERNS:
        head = pattern.sub(replacement, head)
    return f'{head}{marker}{tail}'

def _connect(database_path):
    connection = sqlite3.connect(Path(database_path).expanduser().resolve().as_uri() + '?mode=ro', uri=True, timeout=READ_TIMEOUT_SECONDS)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA query_only=ON')
    deadline = time.monotonic() + PROGRESS_DEADLINE_SECONDS
    connection.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1000)
    return connection

def _envelope(tool, evidence_id, start, end, status, facts=None, reason=None):
    core = {'tool': tool, 'evidence_id': evidence_id, 'status': status, 'period_start': start.isoformat(), 'period_end': end.isoformat()}
    core['facts' if facts is not None else 'reason'] = facts if facts is not None else reason
    core['query_id'] = hashlib.sha256(compact_json(core).encode('utf-8')).hexdigest()
    return core

def _window_events(connection, group, bounds, row_limit):
    params = (str(group.get('entity_id') or ''), *bounds, row_limit + 1)
    rows = [dict(row) for row in connection.execute(WINDOW_SQL, params).fetchall()]
    if len(rows) > row_limit:
        return ('unavailable', None, 'row_limit')
    if not rows:
        return ('unknown', None, 'no_observations')
    counts, actions, states, telemetry = ({}, {}, {}, {})
    for row in rows:
        kind = classify(row)
        counts[kind] = counts.get(kind, 0) + 1
        if kind == 'device_action':
            source = str(row.get('source') or 'unknown')
            key = ((_beijing(row.get('ts')) or '')[:10], source)
            actions[key] = actions.get(key, 0) + 1
            state_key = (str(row.get('new_state') or 'unknown'), source)
            states[state_key] = states.get(state_key, 0) + 1
        elif kind == 'sensor_telemetry':
            metadata = _json_object(row.get('metadata_json'))
            attributes = _json_object(row.get('attributes_json'))
            unit = str(metadata.get('unit') or attributes.get('unit_of_measurement') or 'unknown')[:32]
            key = (str(row.get('category') or 'unknown'), unit)
            telemetry.setdefault(key, []).append(row)
    if len(telemetry) > MAX_METRIC_GROUPS:
        return ('unavailable', None, 'result_budget')
    metrics = []
    for (category, unit), group_rows in telemetry.items():
        stats = summarize_samples(group_rows, lambda event: _json_object(event.get('metadata_json')))
        metrics.append({'category': category, 'unit': unit, 'sample_count': stats.get('sample_count', 0), 'mean': stats.get('avg'), 'min': stats.get('min'), 'max': stats.get('max'), 'delta': stats.get('delta'), 'variance': stats.get('variance'), 'invalid_samples': stats.get('invalid_samples'), 'missing_samples': None, 'start': _beijing(stats.get('start')), 'end': _beijing(stats.get('end'))})
    return ('ok', {'class_counts': counts, 'action_counts_by_date_source': [{'date': date, 'source': source, 'count': count} for (date, source), count in sorted(actions.items())], 'action_state_counts': [{'state': state, 'source': source, 'count': count} for (state, source), count in sorted(states.items())], 'metrics': metrics, 'counts_are_records_not_uses': True, 'collection_completeness': 'unknown'}, None)

def _owner_guidance(connection, group, evidence_id, end_utc, row_limit, guidance_scope):
    rows = [dict(row) for row in connection.execute(OWNER_SQL, (guidance_scope, end_utc, row_limit + 1)).fetchall()]
    if len(rows) > row_limit:
        return ('unavailable', None, 'row_limit')
    rules = []
    for row in rows:
        content = str(row.get('content') or '')
        answered = '房主回答' in content
        matched = guidance_mentions_identity(content, group)
        general = not answered and any((marker in content for marker in GENERAL_MARKERS))
        if not (general or matched):
            continue
        rules.append({'id': row.get('id'), 'evidence_id': evidence_id, 'content': _mask_guidance_numbers(content) if answered else content, 'scope': row.get('scope'), 'source': row.get('source'), 'created_at': _beijing(row.get('created_at'))})
    if not rules:
        return ('unknown', None, 'no_scoped_guidance')
    return ('ok', {'basis': 'owner_guidance', 'assertion': 'not_sensor_fact', 'rules': rules, 'count': len(rules)}, None)

def query_facts(database_path, request, evidence, now, token_budget=700, row_limit=256, *, period_start=None, guidance_scope='daily'):
    if not isinstance(request, dict) or set(request) != {'tool', 'evidence_id'}:
        raise ValueError('invalid request')
    tool, evidence_id = (request.get('tool'), request.get('evidence_id'))
    if not isinstance(tool, str) or tool not in TOOLS or (not isinstance(evidence_id, str)) or (not evidence_id):
        raise ValueError('invalid request')
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('invalid now')
    if not isinstance(guidance_scope, str) or guidance_scope not in GUIDANCE_SCOPES:
        raise ValueError('invalid guidance_scope')
    matches = [item for item in (evidence if isinstance(evidence, list) else []) if isinstance(item, dict) and str(item.get('evidence_id') or '') == evidence_id]
    if len(matches) != 1:
        raise ValueError('invalid evidence_id')
    group = deepcopy(matches[0])
    if tool == 'window_events' and (not str(group.get('entity_id') or '').strip()):
        raise ValueError('missing entity identity')
    row_limit = _bound(row_limit, MAX_ROW_LIMIT)
    token_budget = _bound(token_budget, MAX_TOKEN_BUDGET)
    end = now.astimezone(BEIJING)
    if period_start is not None:
        if not isinstance(period_start, datetime) or period_start.tzinfo is None or period_start.utcoffset() is None:
            raise ValueError('invalid period_start')
        start = period_start.astimezone(BEIJING)
        if not timedelta(0) < end - start <= timedelta(days=LOOKBACK_DAYS):
            raise ValueError('invalid period_start')
    else:
        start = end - timedelta(days=_lookback_days(group))
    floor = _envelope(tool, evidence_id, start, end, 'unavailable', reason='result_budget')
    if token_budget < conservative_token_estimate(compact_json(floor)):
        raise ValueError('token budget below minimum envelope')
    if tool == 'entity_identity':
        facts = {'source': 'selected_program_evidence'}
        for key in IDENTITY_FIELDS:
            text = str(group.get(key) or '').strip()
            facts[key] = text if text else 'unknown'
        status, reason = ('ok', None)
    else:
        connection = None
        try:
            connection = _connect(database_path)
            end_utc = end.astimezone(timezone.utc).isoformat()
            if tool == 'window_events':
                bounds = (start.astimezone(timezone.utc).isoformat(), end_utc)
                status, facts, reason = _window_events(connection, group, bounds, row_limit)
            else:
                status, facts, reason = _owner_guidance(connection, group, evidence_id, end_utc, row_limit, guidance_scope)
        except (sqlite3.Error, OSError, ValueError, TypeError, OverflowError):
            status, facts, reason = ('unavailable', None, 'read_error')
        finally:
            if connection is not None:
                connection.close()
    result = _envelope(tool, evidence_id, start, end, status, facts, reason)
    if conservative_token_estimate(compact_json(result)) > token_budget:
        result = _envelope(tool, evidence_id, start, end, 'unavailable', reason='result_budget')
    return result
