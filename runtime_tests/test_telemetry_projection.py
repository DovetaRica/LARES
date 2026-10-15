import copy
from app.behavior_analyzer import BehaviorAnalyzer
from app.event_preprocessor import preprocess
from app.segment_memory import SegmentMemory
from app.telemetry_projection import NOTE, STABLE_ENTITIES_KEY, project_telemetry, protected_telemetry_entities, stable_telemetry_entities

def telemetry_item(entity, value, count=50, **extra):
    item = {'kind': 'telemetry', 'class': 'sensor_telemetry', 'entity': entity, 'count': count, 'sample_count': count, 'valid_samples': count, 'invalid_samples': 0, 'missing_samples': None, 'min': value, 'max': value, 'avg': value, 'first': str(value), 'last': str(value), 'delta': 0, 'variance': 0.0, 'variance_ddof': 0}
    item.update(extra)
    return item

def event(entity, value, source='system', ts='2026-09-28T00:00:00+00:00'):
    return {'entity_id': entity, 'domain': 'sensor', 'category': 'environment', 'event_type': 'state_changed', 'old_state': None, 'new_state': value, 'source': source, 'ts': ts, 'attributes_json': '{}', 'metadata_json': '{}'}

def report_payload(entity='sensor.fridge', bulk=1):
    return {'analysis_scope': 'period', 'total_events': 100, 'event_counts': {'sensor_telemetry': 100}, 'metric_summaries': [{'entity_id': entity, 'sample_count': 50, 'avg': 4.0, 'min': 4.0, 'max': 4.0, 'note': 'x' * 40 * bulk}], 'recent_detail': [{'kind': 'telemetry', 'class': 'sensor_telemetry', 'entity': entity, 'note': 'y' * 40 * bulk}], 'representative_events': [{'entity_id': entity, 'event_type': 'state_changed'}], 'entity_identity_catalog': [{'entity_id': entity, 'device': '冰箱', 'name': '厨房冰箱', 'area': '厨房'}], 'critical_events': [], 'user_guidance': [], 'long_term_memories': [], STABLE_ENTITIES_KEY: [entity]}

def test_constant_window_becomes_candidate_from_full_preprocess():
    events = [event('sensor.fridge', '4.0'), event('sensor.fridge', '4.0'), event('sensor.humidity', '55.0')]
    compressed = preprocess(events).compressed
    assert len(compressed) == 2
    assert stable_telemetry_entities(compressed) == {'sensor.fridge'}

def test_constant_telemetry_omitted_from_model_copy_only():
    source = report_payload(bulk=20)
    snapshot = copy.deepcopy(source)
    analyzer = BehaviorAnalyzer.__new__(BehaviorAnalyzer)
    model = analyzer._model_payload(source)
    for key in ('metric_summaries', 'recent_detail', 'representative_events', 'entity_identity_catalog', 'critical_events'):
        assert source[key] == snapshot[key], f'source {key} must stay unchanged'
    assert source[STABLE_ENTITIES_KEY] == snapshot[STABLE_ENTITIES_KEY]
    assert 'telemetry_selection' not in source, 'the note belongs to the model copy only'
    assert STABLE_ENTITIES_KEY not in model
    assert model['metric_summaries'] == []
    assert model['recent_detail'] == []
    assert model['representative_events'] == []
    assert model['event_counts'] == {'sensor_telemetry': 100}
    assert model['entity_identity_catalog'][0]['entity_id'] == 'sensor.fridge'
    assert model['telemetry_selection']['note'] == NOTE
    assert model['telemetry_selection']['omitted_entities'] == 1
    assert '非正常判定' in model['telemetry_selection']['note']
    assert '原记录保留' in model['telemetry_selection']['note']
    assert analyzer._payload_tokens(model) < analyzer._payload_tokens(source)

def test_changing_values_and_extrema_spike_are_retained():
    changed = preprocess([event('sensor.fridge', '4.0', ts='2026-09-28T00:00:00+00:00'), event('sensor.fridge', '5.0', ts='2026-09-28T03:00:00+00:00')])
    assert stable_telemetry_entities(changed.compressed) == set()
    spike = telemetry_item('sensor.fridge', 4.2, min=4.0, max=5.0, avg=4.2)
    assert stable_telemetry_entities([spike]) == set()

def test_unknown_invalid_malformed_and_single_sample_are_retained():
    cases = [telemetry_item('sensor.a', 4.0, missing_samples=5), telemetry_item('sensor.a', 4.0, invalid_samples=1), telemetry_item('sensor.a', 4.0, invalid_statistic_rows=2), telemetry_item('sensor.a', 4.0, sample_count=1), telemetry_item('sensor.a', 4.0, sample_count=True, count=True), telemetry_item('sensor.a', 4.0, variance=0.5), telemetry_item('sensor.a', 4.0, min='not-a-number'), telemetry_item('sensor.a', 4.0, delta=0.1)]
    for item in cases:
        assert stable_telemetry_entities([item]) == set(), item
    assert stable_telemetry_entities([telemetry_item('sensor.a', 4.0)]) == {'sensor.a'}
    assert stable_telemetry_entities([telemetry_item('sensor.a', 4.0, invalid_statistic_rows=0)]) == {'sensor.a'}

def test_unknown_missing_cadence_is_allowed_but_keeps_no_coverage_claim():
    item = telemetry_item('sensor.a', 4.0, missing_samples=None)
    assert stable_telemetry_entities([item]) == {'sensor.a'}
    assert NOTE == '省略无变化或已汇总的重复遥测，非正常判定；采样缺失未知，原记录保留。'

def test_manual_and_critical_entities_are_excluded_from_candidates():
    manual = preprocess([event('sensor.manual', '4.0', source='manual'), event('sensor.manual', '4.0', source='manual')])
    assert stable_telemetry_entities(manual.compressed) == {'sensor.manual'}
    assert stable_telemetry_entities(manual.compressed, {'sensor.manual'}) == set()
    critical = [{'entity_id': f'sensor.crit_{index}'} for index in range(12)]
    protected = protected_telemetry_entities([event('sensor.manual', '4.0', source='manual'), event('sensor.other', '3.0')], critical)
    assert 'sensor.manual' in protected and 'sensor.other' not in protected
    assert {f'sensor.crit_{index}' for index in range(12)} <= protected
    payload = {'critical_events': critical, 'metric_summaries': [{'entity_id': item['entity_id']} for item in critical]}
    diagnostics = project_telemetry(payload, {item['entity_id'] for item in critical})
    assert diagnostics['omitted_entities'] == 0
    assert payload['metric_summaries'], 'the full pre-cap critical pool stays in context'

def test_owner_entity_device_or_name_mention_preserves_telemetry():
    for content in ('sensor.fridge', '冰箱', '厨房冰箱'):
        payload = report_payload()
        payload['user_guidance'] = [{'id': 1, 'content': f'关于{content}的规则'}]
        diagnostics = project_telemetry(payload, {payload[STABLE_ENTITIES_KEY].pop()})
        assert diagnostics['omitted_entities'] == 0, content
        assert payload['metric_summaries'], content
    unrelated = report_payload()
    unrelated['user_guidance'] = [{'id': 2, 'content': '客厅灯亮度规则'}]
    diagnostics = project_telemetry(unrelated, {'sensor.fridge'})
    assert diagnostics['omitted_entities'] == 1
    assert unrelated['metric_summaries'] == []
    remembered = report_payload()
    remembered['long_term_memories'] = [{'status': 'confirmed', 'content': '厨房冰箱一直稳定'}]
    diagnostics = project_telemetry(remembered, {'sensor.fridge'})
    assert diagnostics['omitted_entities'] == 0 and remembered['metric_summaries']

def test_segment_model_copy_filters_state_and_events_but_source_stays_full():
    payload = {'compressed_events': [telemetry_item('sensor.fridge', 4.0), telemetry_item('sensor.other', 3.0)], 'start_state': {'sensor.fridge': '4', 'sensor.other': '3'}, 'end_state': {'sensor.fridge': '4'}, 'entity_identity_catalog': {'sensor.fridge': {'device': '冰箱'}}, 'critical_events': [], STABLE_ENTITIES_KEY: ['sensor.fridge']}
    snapshot = copy.deepcopy(payload)
    memory = SegmentMemory.__new__(SegmentMemory)
    bounded = memory._bound_segment_payload(payload, budget_tokens=2000)
    assert payload == snapshot, 'the stored segment payload must stay complete'
    assert STABLE_ENTITIES_KEY not in bounded
    assert [item.get('entity') for item in bounded['compressed_events']] == ['sensor.other']
    assert 'sensor.fridge' not in bounded['start_state']
    assert 'sensor.fridge' not in bounded['end_state']
    assert 'sensor.other' in bounded['start_state']
    assert bounded['telemetry_selection']['note'] == NOTE

def aggregate(entity, value, samples, at, **stats):
    row = event(entity, str(value), ts=at)
    row['event_type'] = 'environment_aggregate'
    row['attributes_json'] = {'unit_of_measurement': '°C'}
    row['metadata_json'] = {'count': samples, 'avg': value, 'min': value, 'max': value, 'first': value, 'delta': 0, 'variance': 0, 'variance_ddof': 0, 'invalid_samples': 0, 'period_start': at, **stats}
    return row

def test_full_window_pool_is_sample_weighted_and_preserves_extrema_before_cap():
    from zoneinfo import ZoneInfo
    analyzer = BehaviorAnalyzer.__new__(BehaviorAnalyzer)
    analyzer.tz = ZoneInfo('Asia/Shanghai')
    rows = [aggregate('sensor.fridge', 4, 2, '2026-09-28T00:00:00Z'), aggregate('sensor.fridge', 6, 6, '2026-09-28T03:00:00Z')]
    snapshot = copy.deepcopy(rows)
    stats = analyzer._metric_summaries(rows, limit=1)[0]
    assert stats['sample_count'] == stats['valid_samples'] == 8
    assert stats['avg'] == 5.5
    assert (stats['min'], stats['max'], stats['first'], stats['last'], stats['delta']) == (4, 6, 4, 6, 2)
    assert stats['variance'] == 0.75 and stats['variance_ddof'] == 0
    assert stats['missing_samples'] is None and stats['invalid_samples'] == 0
    assert stats['period_start'].startswith('2026-09-28T08:')
    assert stats['period_end'].startswith('2026-09-28T11:')
    assert rows == snapshot
    assert stable_telemetry_entities(preprocess(rows).compressed) == set()
    rows.append(aggregate('sensor.fridge', 5, 1, '2026-09-28T04:00:00Z', count=True))
    stats = analyzer._metric_summaries(rows)[0]
    assert stats['invalid_statistic_rows'] == 1 and stats['invalid_samples'] is None

def test_recent_dedup_requires_covered_bounds_extrema_quality_unit_and_no_owner_interest():
    metric = {'entity_id': 'sensor.fridge', 'summary_basis': 'whole_window_samples', 'period_start': '2026-09-28T08:00:00+08:00', 'period_end': '2026-09-28T14:00:00+08:00', 'min': 4, 'max': 4, 'avg': 4, 'first': 4, 'last': 4, 'delta': 0, 'variance': 0, 'variance_ddof': 0, 'invalid_samples': 0, 'missing_samples': None, 'sample_count': 100, 'unit': '°C', 'name': '测试冰箱'}
    recent = {**telemetry_item('sensor.fridge', 4), 'start': '2026-09-28T04:00:00Z', 'end': '2026-09-28T06:00:00Z', 'unit': '°C'}
    payload = {'metric_summaries': [metric], 'recent_detail': [recent], 'event_counts': {'all': 100}}
    baseline = copy.deepcopy(payload)
    view = copy.deepcopy(payload)
    diag = project_telemetry(view, [])
    assert diag['duplicate_items'] == 1 and view['recent_detail'] == []
    assert view['metric_summaries'][0]['min'] == metric['min']
    assert view['metric_summaries'][0]['max'] == metric['max']
    assert view['event_counts'] == {'all': 100}
    assert payload == baseline
    for patch in ({'max': 8}, {'start': '2026-09-27T00:00:00Z'}, {'invalid_samples': 1}, {'missing_samples': 2}, {'unit': '°F'}, {'sample_count': None}, {'sample_count': True}, {'valid_samples': 1}):
        view = copy.deepcopy(payload)
        view['recent_detail'][0].update(patch)
        assert project_telemetry(view, [])['duplicate_items'] == 0
    for key, value in (('critical_events', [{'entity_id': 'sensor.fridge'}]), ('user_guidance', [{'content': '请看测试冰箱的近期温度'}])):
        view = copy.deepcopy(payload)
        view[key] = value
        assert project_telemetry(view, [])['duplicate_items'] == 0

def test_owner_alias_before_catalog_and_guidance_caps_and_malformed_stats_fail_open():
    item = telemetry_item('sensor.fridge', 4)
    assert stable_telemetry_entities([item], context={'user_guidance': [{'content': '关注冰箱温度'}]}, identities={'sensor.fridge': {'device': '冰箱'}}) == set()
    for patch in ({'valid_samples': 1}, {'valid_samples': True}, {'variance_ddof': False}, {'invalid_statistic_rows': None}, {'invalid_samples': None}):
        changed = {**item, **patch}
        assert stable_telemetry_entities([changed]) == set()
    assert stable_telemetry_entities([item, {**item, 'max': 8}]) == set()

def test_local_decline_inside_full_window_extrema_and_unit_change_are_retained():
    local = {**telemetry_item('sensor.fridge', 1), 'first': 10, 'last': 1, 'delta': -9, 'avg': 5, 'min': 1, 'max': 10, 'start': '2026-09-28T05:00:00Z', 'end': '2026-09-28T06:00:00Z', 'unit': '°C'}
    metric = {**local, 'entity_id': 'sensor.fridge', 'first': 1, 'last': 1, 'delta': 0, 'period_start': '2026-09-28T00:00:00Z', 'period_end': '2026-09-28T06:00:00Z', 'summary_basis': 'whole_window_samples'}
    payload = {'recent_detail': [local], 'metric_summaries': [metric]}
    assert project_telemetry(payload, [])['duplicate_items'] == 0
    rows = [aggregate('sensor.fridge', 4, 2, '2026-09-28T00:00:00Z'), aggregate('sensor.fridge', 4, 2, '2026-09-28T01:00:00Z')]
    rows[1]['attributes_json'] = {'unit_of_measurement': '°F'}
    protected = protected_telemetry_entities(rows)
    assert stable_telemetry_entities(preprocess(rows).compressed, protected) == set()

def test_metric_model_compaction_keeps_statistical_facts_and_source_payload():
    source = {'analysis_scope': 'period', 'metric_summaries': [{'entity_id': 'sensor.example', 'summary_basis': 'whole_window_samples', 'sample_count': 8, 'valid_samples': 8, 'invalid_samples': 1, 'missing_samples': None, 'avg': 5.5, 'min': 4, 'max': 6, 'first': 4, 'last': 6, 'delta': 2, 'variance': 0.75, 'variance_ddof': 0, 'average_basis': 'samples', 'period_start': '2026-09-28T08:00:00+08:00', 'period_end': '2026-09-28T14:00:00+08:00'}]}
    before = copy.deepcopy(source['metric_summaries'])
    analyzer = BehaviorAnalyzer.__new__(BehaviorAnalyzer)
    model = analyzer._model_payload(source, budget=10000)
    row = model['metric_summaries'][0]
    assert source['metric_summaries'] == before
    assert all((row[key] == before[0][key] for key in ('avg', 'min', 'max', 'delta', 'sample_count', 'invalid_samples', 'missing_samples', 'variance', 'period_start', 'period_end')))
    assert '有效采样数' in model['telemetry_statistics'] and '缺失采样未知' in model['telemetry_statistics']
    assert source['evidence_coverage']['telemetry_projection']['compacted_metric_items'] == 1
    assert not any((key.startswith('_') for key in model))
