import copy
import pytest
from app.telemetry_projection import select_metric_summaries

def summary(entity, period_end, **extra):
    row = {'entity_id': entity, 'category': 'environment', 'unit': '°C', 'min': 4.0, 'max': 4.0, 'delta': 0.0, 'variance': 0.0, 'invalid_samples': 0, 'invalid_statistic_rows': 0, 'missing_samples': None, 'period_end': period_end}
    row.update(extra)
    return row

def test_cap_keeps_owner_critical_quality_and_change_over_newer_routine():
    rows = [summary('sensor.owner', '2026-09-28T01:00:00+00:00'), summary('sensor.flat_a', '2026-09-28T09:00:00+00:00'), summary('sensor.critical', '2026-09-28T02:00:00+00:00'), summary('sensor.bad', '2026-09-28T03:00:00+00:00', invalid_samples=2), summary('sensor.change', '2026-09-28T04:00:00+00:00', min=1.0, max=6.0), summary('sensor.flat_b', '2026-09-28T10:00:00+00:00')]
    selected = select_metric_summaries(rows, 4, protected=('sensor.critical',), context={'entity_identity_catalog': [{'entity_id': 'sensor.owner', 'device': '旧设备'}], 'user_guidance': [{'content': '请关注旧设备'}]})
    assert {row['entity_id'] for row in selected} == {'sensor.owner', 'sensor.critical', 'sensor.bad', 'sensor.change'}
    assert all((row['entity_id'] not in ('sensor.flat_a', 'sensor.flat_b') for row in selected))

def test_selection_is_pure_and_returns_original_whole_entries():
    rows = [summary('sensor.a', '2026-09-28T05:00:00+00:00'), summary('sensor.b', '2026-09-28T06:00:00+00:00', delta=0.5)]
    snapshot = copy.deepcopy(rows)
    selected = select_metric_summaries(rows, 1)
    assert rows == snapshot
    assert selected == [rows[1]] and selected[0] is rows[1]
    assert selected[0]['delta'] == 0.5 and selected[0]['category'] == 'environment'

def test_same_entity_multiple_units_stays_whole_and_separate():
    cold = summary('sensor.fridge', '2026-09-28T04:00:00+00:00', unit='°C', min=1.0, max=9.0)
    warm = summary('sensor.fridge', '2026-09-28T06:00:00+00:00', unit='°F')
    assert select_metric_summaries([cold, warm], 1) == [cold]
    flat = summary('sensor.fridge', '2026-09-28T05:00:00+00:00', unit='°C')
    assert select_metric_summaries([flat, warm], 1) == [warm]
    assert select_metric_summaries([flat, warm], 2) == [warm, flat]

def test_unknown_missing_is_routine_but_known_positive_is_quality():
    unknown = summary('sensor.unknown', '2026-09-28T09:00:00+00:00', missing_samples=None)
    known = summary('sensor.known', '2026-09-28T01:00:00+00:00', missing_samples=3)
    assert select_metric_summaries([unknown, known], 1) == [known]
    assert select_metric_summaries([unknown], 1) == [unknown]
    zero = summary('sensor.zero', '2026-09-28T08:00:00+00:00', missing_samples=0)
    assert select_metric_summaries([unknown, zero], 1) == [unknown]

def test_invalid_limit_rejected_and_zero_is_empty():
    rows = [summary('sensor.a', '2026-09-28T05:00:00+00:00'), summary('sensor.b', '2026-09-28T06:00:00+00:00')]
    assert select_metric_summaries(rows, 0) == []
    assert select_metric_summaries(rows, 1) == [rows[1]]
    assert select_metric_summaries([], 3) == []
    for bad in (True, False, -1, 1.5, '2', None):
        with pytest.raises(ValueError):
            select_metric_summaries(rows, bad)

def test_recency_newer_first_and_original_order_breaks_ties():
    unknown = summary('sensor.unknown', None)
    older = summary('sensor.old', '2026-09-28T05:00:00+00:00')
    first_tie = summary('sensor.tie_a', '2026-09-28T07:00:00+00:00')
    second_tie = summary('sensor.tie_b', '2026-09-28T07:00:00+00:00')
    newer = summary('sensor.new', '2026-09-28T08:00:00+00:00')
    selected = select_metric_summaries([unknown, older, first_tie, second_tie, newer], 5)
    assert [row['entity_id'] for row in selected] == ['sensor.new', 'sensor.tie_a', 'sensor.tie_b', 'sensor.old', 'sensor.unknown']

def test_owner_long_term_memory_mention_protects_older_routine_entry():
    owner = summary('sensor.fridge', '2026-09-28T01:00:00+00:00')
    flat = summary('sensor.other', '2026-09-28T09:00:00+00:00')
    selected = select_metric_summaries([owner, flat], 1, context={'long_term_memories': [{'content': '厨房冰箱长期稳定'}], 'entity_identity_catalog': {'sensor.fridge': {'name': '厨房冰箱'}}})
    assert selected == [owner]
    assert select_metric_summaries([owner, flat], 1) == [flat]
