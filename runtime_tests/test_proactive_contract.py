from copy import deepcopy
import pytest
from app.llm_schema import RetryableModelFormatError, compact_json, conservative_token_estimate
from app.proactive_contract import AUTOMATION_CONTEXT, MANUAL_CONTEXT, ROUTINE_CONTEXT, SCOUT_PROMPT, apply_guidance_scope, build_scout_payload, fit_scout_payload, scout_schema, validate_scout_response

def make_group(evidence_id, entity_id=None, source='other', count=5, local_days=2, last_ts='2026-01-02T10:00:00', states=None):
    return {'evidence_id': evidence_id, 'semantic_key': f'key|{evidence_id}', 'category': 'action', 'event_type': 'state_change', 'source': source, 'entity_id': entity_id or f'light.{evidence_id}', 'count': count, 'local_days': local_days, 'first_ts': '2026-01-01T09:00:00', 'last_ts': last_ts, 'state': 'on', 'states': states or ['on', 'off'], 'device': '灯', 'area': '客厅', 'lookback_days': 14}

def ids_of(payload):
    return [group['evidence_id'] for group in payload['evidence']]

def test_planner_strict_scope_and_last_request_slot():
    from app.proactive_contract import validate_scout_plan, scout_plan_schema
    payload = build_scout_payload([make_group('known')], [], [], [])
    query = {'tool': 'window_events', 'evidence_id': 'known'}
    assert validate_scout_plan({'query': query, 'findings': []}, payload)['query'] == query
    assert scout_plan_schema(False)['properties']['query'] == {'type': 'null'}
    for bad in ({'query': dict(query, sql='SELECT 1'), 'findings': []}, {'query': dict(query, tool='shell'), 'findings': []}, {'query': dict(query, evidence_id='other'), 'findings': []}, {'query': query, 'findings': [{'evidence_id': 'known', 'focus': 'routine_context'}]}):
        with pytest.raises(ValueError):
            validate_scout_plan(bad, payload)
    with pytest.raises(ValueError):
        validate_scout_plan({'query': query, 'findings': []}, payload, False)

def test_final_budget_includes_scope_ids_and_date_without_broadening_owner_answers():
    groups = [make_group(f'{i:064x}', entity_id=f'light.fixture_{i}') for i in range(8)]
    for i, group in enumerate(groups):
        group['device'] = f'合成设备{i}'
    payload = build_scout_payload(groups, [], [], [], token_budget=10 ** 9)
    guidance = [{'content': '灯光次数不能证明人员身份。', 'kind': 'explicit'}, {'content': '合成设备0房主回答：仅指这台设备。', 'kind': 'qa_history'}, {'content': '合成设备7房主回答：不要推广到其他设备。', 'kind': 'qa_history'}]
    payload = apply_guidance_scope(payload, guidance)
    payload['date'] = '2026-10-08'
    original = deepcopy(payload)
    budget = conservative_token_estimate(compact_json(payload)) - 200
    fitted = fit_scout_payload(payload, budget)
    assert conservative_token_estimate(compact_json(fitted)) <= budget
    assert 0 < len(fitted['evidence']) < len(payload['evidence'])
    assert fitted['evidence'] == payload['evidence'][:len(fitted['evidence'])]
    assert fitted['date'] == payload['date'] and payload == original
    retained = ids_of(fitted)
    for rule in fitted['user_guidance']:
        original_rule = next((row for row in original['user_guidance'] if row['content'] == rule['content']))
        assert {k: v for k, v in rule.items() if k != 'applies_to_evidence_ids'} == {k: v for k, v in original_rule.items() if k != 'applies_to_evidence_ids'}
        assert rule['applies_to_evidence_ids'] == [id for id in original_rule['applies_to_evidence_ids'] if id in retained]
    qa = next((rule for rule in fitted['user_guidance'] if rule.get('kind') == 'qa_history'))
    assert qa['applies_to_evidence_ids'] == [groups[0]['evidence_id']]
    assert all(('合成设备7' not in rule['content'] for rule in fitted['user_guidance']))

def test_unfit_single_entry_never_cuts_owner_text_or_returns_empty_success():
    payload = apply_guidance_scope(build_scout_payload([make_group('single')], [], [], []), [{'content': '完整业主规则。' * 1000, 'kind': 'explicit'}])
    payload['date'] = '2026-10-08'
    original = deepcopy(payload)
    assert fit_scout_payload(payload) is None and payload == original

def test_prompt_and_schema_bound_the_model():
    assert '3' in SCOUT_PROMPT and '确认' in SCOUT_PROMPT
    schema = scout_schema()
    assert schema['additionalProperties'] is False
    assert schema['properties']['findings']['items']['additionalProperties'] is False
    assert schema['properties']['findings']['maxItems'] == 3
    focus_enum = schema['properties']['findings']['items']['properties']['focus']['enum']
    assert set(focus_enum) == {ROUTINE_CONTEXT, MANUAL_CONTEXT, AUTOMATION_CONTEXT}

def test_allowed_focus_follows_source():
    groups = [make_group('m1', source='manual'), make_group('a1', source='automation'), make_group('o1', source='other')]
    payload = build_scout_payload(groups, [], [], [])
    assert payload['analysis_scope'] == 'proactive'
    focus = {item['evidence_id']: item['allowed_focus'] for item in payload['evidence']}
    assert focus['m1'] == [ROUTINE_CONTEXT, MANUAL_CONTEXT]
    assert focus['a1'] == [ROUTINE_CONTEXT, AUTOMATION_CONTEXT]
    assert focus['o1'] == [ROUTINE_CONTEXT]

def test_floors_blocked_and_seen_exclude_groups():
    blocked_key = make_group('blocked_key')
    blocked_key['semantic_key'] = 'block-key'
    groups = [make_group('low_count', count=4), make_group('one_day', local_days=1), make_group('blocked_id'), blocked_key, make_group('seen'), make_group('kept')]
    payload = build_scout_payload(groups, {'blocked_id'}, {'block-key'}, {'seen'})
    assert ids_of(payload) == ['kept']

def test_max_twelve_whole_groups():
    groups = [make_group(f'e{i}', entity_id=f'light.e{i}') for i in range(20)]
    payload = build_scout_payload(groups, [], [], [], token_budget=10 ** 9)
    assert len(payload['evidence']) == 12

def test_budget_measures_full_payload_and_skips_whole_items():
    one = build_scout_payload([make_group('x0')], [], [], [], token_budget=10 ** 9)
    budget = conservative_token_estimate(compact_json(one))
    groups = [make_group(f'x{i}') for i in range(5)]
    built = build_scout_payload(groups, [], [], [], token_budget=budget)
    assert len(built['evidence']) == 1
    assert conservative_token_estimate(compact_json(built)) <= budget
    assert built['evidence'][0]['evidence_id'] == 'x0'
    assert built['evidence'][0]['allowed_focus'] == [ROUTINE_CONTEXT]
    assert build_scout_payload(groups, [], [], [], token_budget=0)['evidence'] == []

def test_punctuation_and_deep_copy_leave_input_unchanged():
    group = make_group('p1', states=['开，关。', '待机?！'])
    before = deepcopy(group)
    payload = build_scout_payload([group], [], [], [])
    item = payload['evidence'][0]
    assert item['states'] == ['开，关。', '待机?！']
    assert group == before
    item['states'].append('mutated')
    item['semantic_key'] = 'changed'
    assert group == before

def test_diverse_entities_precede_further_states_and_manual_first():
    groups = [make_group('a1', entity_id='light.a', last_ts='2026-01-05T10:00:00'), make_group('a2', entity_id='light.a', last_ts='2026-01-04T10:00:00'), make_group('b1', entity_id='light.b', last_ts='2026-01-01T10:00:00')]
    ids = ids_of(build_scout_payload(groups, [], [], []))
    assert ids[0] == 'a1'
    assert ids.index('b1') < ids.index('a2')
    manual = [make_group('o1', source='other', last_ts='2026-01-09T10:00:00'), make_group('m1', source='manual', last_ts='2026-01-01T10:00:00')]
    assert ids_of(build_scout_payload(manual, [], [], []))[0] == 'm1'

def scout_payload():
    groups = [make_group(f'm{i}', source='manual') for i in range(1, 5)]
    groups.append(make_group('a1', source='automation'))
    return build_scout_payload(groups, [], [], [], token_budget=10 ** 9)

def test_validate_accepts_and_returns_sanitized_list():
    payload = scout_payload()
    result = validate_scout_response({'findings': [{'evidence_id': 'm1', 'focus': MANUAL_CONTEXT}]}, payload)
    assert result == [{'evidence_id': 'm1', 'focus': MANUAL_CONTEXT}]
    assert result is not payload
    assert validate_scout_response({'findings': []}, payload) == []

@pytest.mark.parametrize('value', [[], {'other': []}, {'findings': [], 'extra': 1}, {'findings': [{'evidence_id': 'm1', 'focus': MANUAL_CONTEXT, 'why': 'x'}]}, {'findings': [{'evidence_id': 7, 'focus': MANUAL_CONTEXT}]}, {'findings': [{'evidence_id': 'ghost', 'focus': MANUAL_CONTEXT}]}, {'findings': [{'evidence_id': 'm1', 'focus': MANUAL_CONTEXT}, {'evidence_id': 'm1', 'focus': ROUTINE_CONTEXT}]}, {'findings': [{'evidence_id': 'm1', 'focus': MANUAL_CONTEXT}, {'evidence_id': 'm2', 'focus': MANUAL_CONTEXT}, {'evidence_id': 'm3', 'focus': MANUAL_CONTEXT}, {'evidence_id': 'm4', 'focus': MANUAL_CONTEXT}]}, {'findings': [{'evidence_id': 'a1', 'focus': MANUAL_CONTEXT}]}])
def test_validate_rejects_invalid_or_invented_findings(value):
    with pytest.raises(RetryableModelFormatError):
        validate_scout_response(value, scout_payload())

@pytest.mark.parametrize('content,kind,remaining', [('书房灯房主回答：这是测试，不要再询问。', 'qa_history', ['kitchen']), ('书房灯房主回答：这是测试。', 'qa_history', ['study', 'kitchen']), ('客房空调房主回答：不要再询问。', 'qa_history', ['study', 'kitchen']), ('客房空调不要再询问。', 'owner', ['study', 'kitchen']), ('所有设备都不要再询问。', 'owner', []), ('不要再询问', 'owner', []), ('书房灯为何不要再询问？房主回答：请继续观察。', 'qa_history', ['study', 'kitchen'])])
def test_explicit_owner_declines_are_scoped_and_unrelated_answers_cannot_block_all(content, kind, remaining):
    study, kitchen = (make_group('study'), make_group('kitchen'))
    study['device'], kitchen['device'] = ('书房灯', '厨房灯')
    payload = build_scout_payload([study, kitchen], [], [], [])
    guidance = [{'content': content, 'kind': kind}]
    before = deepcopy((payload, guidance))
    result = apply_guidance_scope(payload, guidance)
    assert ids_of(result) == remaining
    assert (payload, guidance) == before
