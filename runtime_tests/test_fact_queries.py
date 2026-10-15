import json
import sqlite3
from datetime import datetime, timedelta
import pytest
from app.fact_queries import query_facts
from app.llm_schema import compact_json, conservative_token_estimate
NOW = datetime.fromisoformat('2026-10-08T09:00:00+08:00')
GROUP = {'evidence_id': 'ev-1', 'entity_id': 'light.study', 'device': '书房灯', 'area': '书房', 'appliance_type': '', 'source': 'manual', 'lookback_days': 14}
EVENT_COLUMNS = ('ts', 'entity_id', 'domain', 'category', 'event_type', 'old_state', 'new_state', 'source', 'area', 'attributes_json', 'metadata_json')

def event(ts, entity_id='light.study', domain='light', category='lighting', event_type='state_changed', old_state='off', new_state='on', source='manual', area='书房', attributes=None, metadata=None):
    return (ts, entity_id, domain, category, event_type, old_state, new_state, source, area, json.dumps(attributes or {}), json.dumps(metadata or {}))

def make_db(path, events=(), guidance=()):
    connection = sqlite3.connect(path)
    connection.executescript("CREATE TABLE events (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, entity_id TEXT, domain TEXT, category TEXT NOT NULL, event_type TEXT NOT NULL, old_state TEXT, new_state TEXT, source TEXT NOT NULL, area TEXT, attributes_json TEXT NOT NULL DEFAULT '{}', metadata_json TEXT NOT NULL DEFAULT '{}');CREATE TABLE ai_guidance (id INTEGER PRIMARY KEY, content TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, scope TEXT NOT NULL DEFAULT 'all', source TEXT NOT NULL DEFAULT 'owner', created_at TEXT NOT NULL, archived_at TEXT);")
    connection.executemany(f'INSERT INTO events({','.join(EVENT_COLUMNS)}) VALUES({','.join('?' * len(EVENT_COLUMNS))})', events)
    connection.executemany('INSERT INTO ai_guidance(content,active,scope,source,created_at) VALUES(?,?,?,?,?)', guidance)
    connection.commit()
    connection.close()
    return str(path)

def request(tool, evidence_id='ev-1'):
    return {'tool': tool, 'evidence_id': evidence_id}

def test_strict_request_and_budget_are_rejected_before_db(tmp_path):
    missing = str(tmp_path / 'absent.db')
    bad = [{'tool': None, 'evidence_id': 'ev-1'}, {'tool': 'invented', 'evidence_id': 'ev-1'}, {'tool': 'window_events'}, {'tool': 'window_events', 'evidence_id': 'ev-1', 'extra': 1}, {'tool': 'window_events', 'evidence_id': 'other'}]
    for item in bad:
        with pytest.raises(ValueError):
            query_facts(missing, item, [GROUP], NOW)
    with pytest.raises(ValueError):
        query_facts(missing, request('window_events'), [GROUP, dict(GROUP)], NOW)
    with pytest.raises(ValueError):
        query_facts(missing, request('window_events'), [GROUP], datetime(2026, 10, 8, 9))
    with pytest.raises(ValueError):
        query_facts(missing, request('window_events'), [GROUP], NOW, token_budget=1)
    identity = query_facts(missing, request('entity_identity'), [GROUP], NOW)
    assert identity['status'] == 'ok' and identity['facts']['source'] == 'selected_program_evidence'

def test_lookback_days_requires_a_real_bounded_integer(tmp_path):
    path = make_db(tmp_path / 'lookback.db', events=[event('2026-10-07T08:00:00+08:00')])
    for value in (True, 1.5, '7', 0, 15, None):
        group = dict(GROUP, lookback_days=value)
        result = query_facts(path, request('window_events'), [group], NOW)
        assert result['period_start'] == '2026-09-24T09:00:00+08:00'

def test_similar_names_are_not_mixed(tmp_path):
    other = dict(GROUP, evidence_id='ev-2', entity_id='light.study_2', device='书房灯2')
    result = query_facts(str(tmp_path / 'x.db'), request('entity_identity'), [other, GROUP], NOW)
    facts = result['facts']
    assert facts['entity_id'] == 'light.study' and facts['device'] == '书房灯'
    assert facts['area'] == '书房' and facts['appliance_type'] == 'unknown'
    assert facts['name'] == 'unknown' and result['period_end'].endswith('+08:00')

def test_window_boundaries_and_action_counts_by_date_and_source(tmp_path):
    path = make_db(tmp_path / 'window.db', events=[event('2026-09-24T09:00:00+08:00'), event('2026-10-08T09:00:00+08:00', new_state='off'), event('2026-10-07T23:00:00+08:00', domain='switch', source='manual'), event('2026-10-08T08:00:00+08:00', source='automation'), event('2026-10-08T08:30:00+08:00', domain='sensor', category='environment', new_state='21.5', source='poll', attributes={'unit_of_measurement': '°C'}), event('2026-10-08T08:40:00+08:00', domain='system', category='system', event_type='restart', new_state='started')])
    facts = query_facts(path, request('window_events'), [GROUP], NOW)['facts']
    assert facts['class_counts'] == {'device_action': 3, 'sensor_telemetry': 1, 'system': 1}
    assert facts['counts_are_records_not_uses'] is True
    assert facts['collection_completeness'] == 'unknown'
    counts = {(row['date'], row['source']): row['count'] for row in facts['action_counts_by_date_source']}
    assert counts == {('2026-09-24', 'manual'): 1, ('2026-10-07', 'manual'): 1, ('2026-10-08', 'automation'): 1}
    assert 'metadata_json' not in compact_json(facts)

def test_telemetry_pooling_extrema_units_and_invalid_quality(tmp_path):
    path = make_db(tmp_path / 'telemetry.db', events=[event('2026-10-07T10:00:00+08:00', domain='sensor', category='environment', new_state='10', attributes={'unit_of_measurement': '°C'}), event('2026-10-07T11:00:00+08:00', domain='sensor', category='environment', new_state='20', attributes={'unit_of_measurement': '°C'}), event('2026-10-07T12:00:00+08:00', domain='sensor', category='environment', new_state='3.5', attributes={'unit_of_measurement': '%'}), event('2026-10-07T13:00:00+08:00', domain='sensor', category='appliance', event_type='environment_aggregate', new_state='9', metadata={'count': 0, 'avg': 9, 'min': 9, 'max': 9})])
    facts = query_facts(path, request('window_events'), [GROUP], NOW)['facts']
    metrics = {(row['category'], row['unit']): row for row in facts['metrics']}
    celsius = metrics['environment', '°C']
    assert celsius['sample_count'] == 2 and celsius['mean'] == 15
    assert celsius['min'] == 10 and celsius['max'] == 20 and (celsius['variance'] == 25)
    assert celsius['missing_samples'] is None
    assert metrics['environment', '%']['sample_count'] == 1
    assert metrics['appliance', 'unknown']['sample_count'] == 0
    assert metrics['appliance', 'unknown']['mean'] is None

def test_row_overflow_is_unavailable_without_partial_facts(tmp_path):
    path = make_db(tmp_path / 'overflow.db', events=[event('2026-10-07T08:00:00+08:00'), event('2026-10-07T09:00:00+08:00'), event('2026-10-07T10:00:00+08:00')])
    result = query_facts(path, request('window_events'), [GROUP], NOW, row_limit=2)
    assert result['status'] == 'unavailable' and result['reason'] == 'row_limit'
    assert 'facts' not in result

def test_token_budget_bounds_the_full_envelope(tmp_path):
    group = dict(GROUP, device='灯' * 120, name='名称' * 60)
    result = query_facts(str(tmp_path / 'x.db'), request('entity_identity'), [group], NOW, token_budget=320)
    assert result['status'] == 'unavailable' and result['reason'] == 'result_budget'
    assert 'facts' not in result and conservative_token_estimate(compact_json(result)) <= 320

def test_owner_scope_identity_general_rule_and_masking(tmp_path):
    path = make_db(tmp_path / 'owner.db', guidance=[('书房有人吗？房主回答：有', 1, 'all', 'owner', '2026-10-01T00:00:00+08:00'), ('厨房灯问题？房主回答：验收', 1, 'all', 'owner', '2026-10-02T00:00:00+08:00'), ('书房灯 20:47 至次日 00:00 有人吗？房主回答：不是，那只是传感器刷新。', 1, 'all', 'owner', '2026-10-03T00:00:00+08:00'), ('light.study_2 的规则', 1, 'all', 'owner', '2026-10-03T12:00:00+08:00'), ('书房灯2 的动作', 1, 'all', 'owner', '2026-10-03T13:00:00+08:00'), ('厨房灯不用记录', 1, 'all', 'owner', '2026-10-03T14:00:00+08:00'), ('通用规则：灯光动作不能证明有人在场。', 1, 'all', 'owner', '2026-10-04T00:00:00+08:00')])
    facts = query_facts(path, request('owner_guidance'), [GROUP], NOW)['facts']
    contents = [rule['content'] for rule in facts['rules']]
    assert facts['assertion'] == 'not_sensor_fact'
    assert any(('通用规则' in content for content in contents))
    assert any(('房主回答：不是' in content for content in contents))
    for excluded in ('书房有人吗', '厨房灯', 'light.study_2', '书房灯2'):
        assert not any((excluded in content for content in contents))
    masked = next((content for content in contents if '房主回答：不是' in content))
    assert '20:47' not in masked and '某时刻' in masked

def test_read_only_queries_do_not_mutate_the_database(tmp_path):
    path = make_db(tmp_path / 'ro.db', events=[event('2026-10-07T08:00:00+08:00')], guidance=[('书房灯不要按刷新计', 1, 'all', 'owner', '2026-10-01T00:00:00+08:00')])
    query_facts(path, request('window_events'), [GROUP], NOW)
    query_facts(path, request('owner_guidance'), [GROUP], NOW)
    connection = sqlite3.connect(path)
    try:
        assert connection.execute('SELECT COUNT(*) FROM events').fetchone()[0] == 1
        assert connection.execute('SELECT COUNT(*) FROM ai_guidance').fetchone()[0] == 1
        assert connection.execute('SELECT new_state FROM events').fetchone()[0] == 'on'
    finally:
        connection.close()

def test_oversize_full_rule_is_refused_without_truncation(tmp_path):
    path = make_db(tmp_path / 'big.db', guidance=[('通用规则：' + '甲' * 2000, 1, 'all', 'owner', '2026-10-01T00:00:00+08:00')])
    result = query_facts(path, request('owner_guidance'), [GROUP], NOW)
    assert result['status'] == 'unavailable' and result['reason'] == 'result_budget'
    assert 'facts' not in result and '甲' not in compact_json(result)

def test_empty_scope_is_unknown_and_query_id_is_stable(tmp_path):
    path = make_db(tmp_path / 'empty.db')
    events = query_facts(path, request('window_events'), [GROUP], NOW)
    assert events['status'] == 'unknown' and events['reason'] == 'no_observations'
    guidance = query_facts(path, request('owner_guidance'), [GROUP], NOW)
    assert guidance['status'] == 'unknown' and guidance['reason'] == 'no_scoped_guidance'
    assert 'facts' not in events and 'facts' not in guidance
    again = query_facts(path, request('window_events'), [GROUP], NOW)
    assert again['query_id'] == events['query_id'] and len(again['query_id']) == 64

def test_unidentified_entity_cannot_pool_unidentified_records(tmp_path):
    with pytest.raises(ValueError):
        query_facts(tmp_path / 'missing.db', request('window_events'), [dict(GROUP, entity_id='')], NOW)

def test_fractional_window_edges_do_not_round_down_to_whole_seconds(tmp_path):
    now = NOW.replace(microsecond=500000)
    path = make_db(tmp_path / 'fractional.db', events=[event('2026-09-24T09:00:00.200+08:00'), event('2026-09-24T09:00:00.700+08:00'), event('2026-10-08T09:00:00.200+08:00'), event('2026-10-08T09:00:00.700+08:00')])
    result = query_facts(path, request('window_events'), [GROUP], now)
    assert result['facts']['class_counts']['device_action'] == 2

def test_explicit_period_start_uses_exact_fractional_half_open_window(tmp_path):
    now = NOW.replace(microsecond=500000)
    path = make_db(tmp_path / 'period.db', events=[event('2026-10-08T03:00:00.200+08:00'), event('2026-10-08T03:00:00.700+08:00'), event('2026-10-08T09:00:00.200+08:00', new_state='off'), event('2026-10-08T09:00:00.700+08:00', new_state='off')])
    result = query_facts(path, request('window_events'), [GROUP], now, period_start=now - timedelta(hours=6))
    assert result['period_start'] == '2026-10-08T03:00:00.500000+08:00'
    assert result['period_end'] == '2026-10-08T09:00:00.500000+08:00'
    assert result['facts']['class_counts']['device_action'] == 2

def test_action_state_counts_keep_full_values_and_do_not_truncate(tmp_path):
    path = make_db(tmp_path / 'states.db', events=[event('2026-10-07T08:00:00+08:00', new_state='on', source='manual'), event('2026-10-07T09:00:00+08:00', new_state='off', source='manual'), event('2026-10-07T10:00:00+08:00', new_state='on', source='automation'), event('2026-10-07T11:00:00+08:00', new_state='', source='automation')])
    facts = query_facts(path, request('window_events'), [GROUP], NOW)['facts']
    states = {(row['state'], row['source']): row['count'] for row in facts['action_state_counts']}
    assert states == {('on', 'manual'): 1, ('off', 'manual'): 1, ('on', 'automation'): 1, ('unknown', 'automation'): 1}
    assert facts['class_counts']['device_action'] == 4

def test_action_state_counts_overflow_instead_of_truncating(tmp_path):
    path = make_db(tmp_path / 'states_big.db', events=[event('2026-10-07T08:00:00+08:00', new_state='x' * 1600, source='manual')])
    result = query_facts(path, request('window_events'), [GROUP], NOW)
    assert result['status'] == 'unavailable' and result['reason'] == 'result_budget'
    assert 'facts' not in result and 'x' not in compact_json(result)

def test_guidance_scope_isolates_weekly_and_all_rows(tmp_path):
    path = make_db(tmp_path / 'scope.db', guidance=[('通用规则：全部', 1, 'all', 'owner', '2026-10-01T00:00:00+08:00'), ('通用规则：每日', 1, 'daily', 'owner', '2026-10-01T00:00:00+08:00'), ('通用规则：周期', 1, 'period', 'owner', '2026-10-01T00:00:00+08:00'), ('通用规则：每周', 1, 'weekly', 'owner', '2026-10-01T00:00:00+08:00')])
    daily = query_facts(path, request('owner_guidance'), [GROUP], NOW)
    assert {rule['scope'] for rule in daily['facts']['rules']} == {'all', 'daily'}
    weekly = query_facts(path, request('owner_guidance'), [GROUP], NOW, guidance_scope='weekly')
    assert {rule['scope'] for rule in weekly['facts']['rules']} == {'all', 'weekly'}

def test_invalid_period_start_and_scope_are_rejected_before_db(tmp_path):
    missing = str(tmp_path / 'absent.db')
    for value in (datetime(2026, 10, 8, 3), NOW + timedelta(hours=1), NOW - timedelta(days=15), NOW):
        with pytest.raises(ValueError):
            query_facts(missing, request('window_events'), [GROUP], NOW, period_start=value)
    for scope in ('hourly', 'DAILY', 'period_daily', None):
        with pytest.raises(ValueError):
            query_facts(missing, request('owner_guidance'), [GROUP], NOW, guidance_scope=scope)
