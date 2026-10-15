import copy
from app.llm_schema import compact_json, conservative_token_estimate
from app.optional_context import compact_optional_context
REQUIRED_DIAGNOSTICS = {'segments_available', 'segments_compacted', 'segments_omitted', 'segment_tokens_before', 'segment_tokens_after', 'reference_tokens_before', 'reference_tokens_after'}

def tokens(value):
    return conservative_token_estimate(compact_json(value))

def reference_report():
    return {'model': 'big-model', 'period_start': '2026-09-10T00:00:00+08:00', 'summary': '第一句原文。' + '补充细节。' * 300, 'supports': ['支持一。' + '证据' * 200, '支持二'], 'noteworthy': ['注意一。' + '备注' * 200]}

def test_reference_is_bounded_and_keeps_original_prose():
    report = reference_report()
    payload = {'reference_report': report, 'window_facts': {'slots': [1, 2, 3]}}
    original = copy.deepcopy(payload)
    diagnostics = compact_optional_context(payload, reference_token_limit=400, segment_token_limit=600)
    compacted = payload['reference_report']
    assert tokens(compacted) <= 400
    assert diagnostics['reference_tokens_before'] > 400
    assert diagnostics['reference_tokens_after'] <= 400
    assert diagnostics['reference_truncated'] == 1
    assert compacted['model'] == 'big-model'
    assert compacted['period_start'] == report['period_start']
    assert compacted['summary'].startswith('第一句原文。')
    assert report['summary'].startswith(compacted['summary'])
    assert payload['window_facts'] == original['window_facts']

def test_reference_keeps_a_few_original_items():
    report = {'model': 'big', 'period_start': 'p', 'summary': '第一句。' + '内容。' * 300, 'supports': ['证据一', '证据二', '证据三'], 'noteworthy': ['备注一']}
    payload = {'reference_report': report}
    compact_optional_context(payload, reference_token_limit=400)
    compacted = payload['reference_report']
    assert tokens(compacted) <= 400
    assert compacted['summary'].startswith('第一句。')
    assert compacted.get('supports'), 'at least one concrete example item must survive'
    for key in ('supports', 'noteworthy'):
        for item in compacted.get(key, []):
            assert any((original.startswith(item) for original in report[key]))

def test_an_unshrinkable_reference_is_omitted_and_counted():
    payload = {'reference_report': {'model': 'big', 'period_start': 'P' * 4000, 'summary': 'x'}}
    diagnostics = compact_optional_context(payload, reference_token_limit=20)
    assert 'reference_report' not in payload
    assert diagnostics['reference_omitted'] == 1
    assert diagnostics['reference_tokens_after'] == 0

def test_a_huge_segment_is_compressed_but_keeps_its_structure():
    segment = {'period_start': '2026-09-28T06:00:00+08:00', 'period_end': '2026-09-28T12:00:00+08:00', 'model': 'qwen', 'event_count': 42, 'summary': '原文摘要。' + '细节' * 600, 'critical': [{'rule': 'r1', 'ts': 't1', 'entity_id': 'e1'}, {'rule': 'r2', 'ts': 't2', 'entity_id': 'e2'}], 'occupancy': [{'area': '客厅', 'minutes': 30}], 'automation_runs': [{'count': 3}]}
    payload = {'memory_segments': [segment], 'window_facts': {'slots': [{'device_actions': 5}]}}
    original = copy.deepcopy(payload)
    diagnostics = compact_optional_context(payload, segment_token_limit=600)
    kept = payload['memory_segments']
    assert len(kept) == 1
    assert kept[0]['period_start'] == segment['period_start']
    assert kept[0]['period_end'] == segment['period_end']
    assert kept[0]['event_count'] == 42
    assert kept[0]['model'] == 'qwen'
    assert kept[0]['occupancy'] == segment['occupancy']
    assert kept[0]['automation_runs'] == segment['automation_runs']
    assert kept[0]['summary'].startswith('原文摘要。')
    assert len(kept[0]['summary']) < len(segment['summary'])
    assert kept[0]['compacted'] is True
    assert tokens(kept) <= 600
    assert diagnostics['segments_compacted'] == 1
    assert payload['window_facts'] == original['window_facts']

def test_spread_selection_covers_first_last_and_middle_windows():
    segments = [{'period_start': f'win{i:02d}', 'period_end': f'end{i:02d}', 'model': 'm', 'event_count': i, 'summary': f'窗口{i}摘要。'} for i in range(20)]
    unit = tokens([segments[0]])
    payload = {'memory_segments': segments}
    diagnostics = compact_optional_context(payload, segment_token_limit=unit * 4)
    kept = payload['memory_segments']
    indices = {int(item['period_start'][3:]) for item in kept}
    assert 3 <= len(kept) <= 5
    assert {0, 19, 9} <= indices
    assert tokens(kept) <= unit * 4
    assert diagnostics['segments_omitted'] == 20 - len(kept)

def test_long_segments_still_spread_across_many_windows():
    segments = [{'period_start': f'w{i:02d}', 'period_end': f'e{i:02d}', 'model': 'm', 'event_count': i, 'summary': '窗' + f'{i:02d}' + '摘' * 497} for i in range(20)]
    originals = {int(item['period_start'][1:]): item['summary'] for item in segments}
    payload = {'memory_segments': segments}
    compact_optional_context(payload, segment_token_limit=600)
    kept = payload['memory_segments']
    indices = {int(item['period_start'][1:]) for item in kept}
    assert len(kept) >= 3
    assert {0, 19, 9} <= indices
    assert tokens(kept) <= 600
    for item in kept:
        assert len(item.get('summary', '')) < 500
        assert originals[int(item['period_start'][1:])].startswith(item.get('summary', ''))

def test_a_critical_window_is_preferred_over_the_spread():
    segments = [{'period_start': f'w{i:02d}', 'period_end': f'e{i:02d}', 'model': 'm', 'event_count': i, 'summary': '窗' + f'{i:02d}' + '摘' * 497} for i in range(20)]
    segments[10]['critical'] = [{'rule': 'r', 'ts': 't', 'entity_id': 'e'}]
    payload = {'memory_segments': segments}
    compact_optional_context(payload, segment_token_limit=600)
    kept = payload['memory_segments']
    indices = {int(item['period_start'][1:]) for item in kept}
    assert len(kept) >= 3
    assert 10 in indices
    assert {0, 19} <= indices
    assert tokens(kept) <= 600

def test_duplicate_prose_across_distinct_windows_keeps_each_window():
    repeated = '完全相同的正文。'
    segments = [{'period_start': f'w{i}', 'period_end': f'e{i}', 'model': 'm', 'event_count': 10 + i, 'summary': repeated} for i in range(4)]
    payload = {'memory_segments': segments}
    diagnostics = compact_optional_context(payload, segment_token_limit=400)
    kept = payload['memory_segments']
    assert diagnostics['segments_duplicates_collapsed'] == 3
    assert diagnostics['segments_omitted'] == 0
    assert {item['period_start'] for item in kept} == {'w0', 'w1', 'w2', 'w3'}
    assert sum(('summary' in item for item in kept)) == 1
    assert sorted((item['event_count'] for item in kept)) == [10, 11, 12, 13]
    assert tokens(kept) <= 400

def test_final_array_stays_within_tight_limits():
    segments = [{'period_start': f'w{i}', 'period_end': f'e{i}', 'model': 'm', 'event_count': i, 'summary': '摘要。' + '细' * 200, 'critical': [{'rule': 'r', 'ts': 't', 'entity_id': 'e'}]} for i in range(8)]
    for limit in (1, 5, 10, 20, 40, 80, 150, 300, 600):
        payload = {'memory_segments': copy.deepcopy(segments)}
        diagnostics = compact_optional_context(payload, segment_token_limit=limit)
        kept = payload['memory_segments']
        assert tokens(kept) <= limit, f'limit={limit}'
        assert diagnostics['segment_tokens_after'] == tokens(kept)
        for item in kept:
            assert item['period_start'] in {segment['period_start'] for segment in segments}
            assert item['event_count'] == int(item['period_start'][1:])

def test_no_segment_is_fabricated_and_summaries_stay_original_prefixes():
    segments = [{'period_start': 'p1', 'period_end': 'q1', 'model': 'm', 'event_count': 1, 'summary': '第一段。' + '细' * 400}, {'period_start': 'p2', 'period_end': 'q2', 'model': 'm', 'event_count': 2, 'summary': '第二段。' + '节' * 400}]
    payload = {'memory_segments': segments}
    compact_optional_context(payload, segment_token_limit=200)
    originals = {item['period_start']: item['summary'] for item in segments}
    for item in payload['memory_segments']:
        assert item['period_start'] in originals
        assert originals[item['period_start']].startswith(item['summary'])

def test_only_the_two_optional_fields_are_touched():
    payload = {'reference_report': {'model': 'big', 'summary': '范例。' * 500}, 'memory_segments': [{'period_start': 'p', 'period_end': 'q', 'summary': '段。' * 500, 'model': 'm', 'event_count': 3}], 'window_facts': {'slots': [{'device_actions': 1}]}, 'event_counts': {'device_action': 9}, 'critical_events': [{'rule': 'keep'}], 'analysis_scope': 'period'}
    original = copy.deepcopy(payload)
    compact_optional_context(payload, reference_token_limit=100, segment_token_limit=150)
    assert set(payload) == set(original)
    for key in payload:
        if key not in ('reference_report', 'memory_segments'):
            assert payload[key] == original[key]

def test_missing_optional_fields_stay_absent():
    payload = {'window_facts': {'slots': []}}
    diagnostics = compact_optional_context(payload)
    assert payload == {'window_facts': {'slots': []}}
    assert REQUIRED_DIAGNOSTICS <= set(diagnostics)
    assert diagnostics['segments_available'] == 0
    assert diagnostics['reference_tokens_before'] == diagnostics['reference_tokens_after'] == 0

def test_zero_budget_drops_optional_context_safely():
    payload = {'reference_report': {'summary': '范例'}, 'memory_segments': [{'summary': '段落'}], 'facts': 1}
    diagnostics = compact_optional_context(payload, reference_token_limit=0, segment_token_limit=0)
    assert 'reference_report' not in payload and 'memory_segments' not in payload
    assert payload['facts'] == 1
    assert diagnostics['reference_omitted'] == 1
    assert diagnostics['segments_available'] == 1
    assert diagnostics['segments_omitted'] == 1

def test_illegal_optional_content_is_handled_without_raising():
    payload = {'reference_report': 'x' * 1000, 'memory_segments': 'not-a-list', 'facts': 7}
    diagnostics = compact_optional_context(payload, reference_token_limit=10, segment_token_limit=10)
    assert 'reference_report' not in payload
    assert payload['memory_segments'] == 'not-a-list'
    assert payload['facts'] == 7
    assert diagnostics['reference_omitted'] == 1
    assert diagnostics['segment_tokens_before'] == diagnostics['segment_tokens_after']

def test_a_partial_assertion_is_omitted_instead_of_changing_its_meaning():
    segment = {'period_start': 'a', 'period_end': 'b', 'summary': '客厅有信号但不代表有人' * 100}
    payload = {'memory_segments': [segment]}
    compact_optional_context(payload, segment_token_limit=120)
    assert payload['memory_segments']
    assert 'summary' not in payload['memory_segments'][0]
    assert payload['memory_segments'][0]['compacted'] is True
