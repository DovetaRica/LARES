import copy
from app.guidance_retrieval import REASONS, select_guidance
from app.llm_schema import compact_json, conservative_token_estimate

def array_cost(entries):
    return conservative_token_estimate(compact_json(entries))

def qa(item_id, question, answer, kind='qa_history', scope='all', source='owner'):
    return {'id': item_id, 'kind': kind, 'scope': scope, 'source': source, 'content': f'{question}\n房主回答：{answer}'}

def test_rules_and_slot_answers_lead_and_scope_is_untouched():
    rule = {'id': 5, 'kind': 'rule', 'scope': 'all', 'source': 'owner', 'content': '明确规则：不得把巡检刷新当成人员活动'}
    slot = {'id': None, 'kind': 'qa_history', 'scope': 'period', 'source': 'owner', 'content': '今晚主卧有人吗？\n房主回答：主卧一直有人'}
    ordinary = qa(900, '阳台浇花', '每周浇两次')
    items = [ordinary, slot, rule]
    before = copy.deepcopy(items)
    payload = {'sessions': [{'room': '主卧', 'type': 'room_activity'}]}
    selected, diagnostics = select_guidance(items, payload)
    assert items == before
    assert selected[0] is rule
    assert selected[1] is slot
    assert slot['scope'] == 'period'
    assert diagnostics['available'] == 3
    assert diagnostics['selected'] == len(selected) == 3
    assert diagnostics['omitted'] == 0
    assert diagnostics['reasons'][:2] == ['explicit_rule', 'slot_answer']
    assert diagnostics['selected_ids'] == [item.get('id') for item in selected]
    assert set(diagnostics['reasons']) <= set(REASONS)
    assert '房主回答' not in compact_json(diagnostics)

def test_budget_bounds_serialized_array_and_invalid_budget_is_empty():
    items = [qa(3, '客厅空调', '客厅空调不要按刷新计'), {'id': 2, 'kind': 'rule', 'scope': 'all', 'source': 'owner', 'content': '规则' + '甲' * 40}, qa(1, '书房灯', '书房灯问题' + '乙' * 60)]
    payload = {'event_groups': [{'device': '客厅空调', 'area': '客厅'}]}
    budget = array_cost(items) - 1
    selected, diagnostics = select_guidance(items, payload, token_budget=budget)
    assert 0 < len(selected) < len(items)
    assert array_cost(selected) <= budget
    assert diagnostics['selected'] == len(selected)
    assert diagnostics['omitted'] == len(items) - len(selected)
    assert diagnostics['used_tokens'] == array_cost(selected)
    for bad in (0, -5, None, 'many'):
        empty, diag = select_guidance(items, payload, token_budget=bad)
        assert empty == []
        assert diag == {'available': len(items), 'selected': 0, 'omitted': len(items), 'used_tokens': 0, 'selected_ids': [], 'reasons': []}
    assert select_guidance(None, payload) == ([], {'available': 0, 'selected': 0, 'omitted': 0, 'used_tokens': 0, 'selected_ids': [], 'reasons': []})

def test_array_punctuation_and_invalid_items_are_counted():
    tiny = {'id': 1, 'kind': 'rule', 'scope': 'all', 'source': 'owner', 'content': '好'}
    invalid = {'id': 2, 'kind': 'rule', 'scope': 'all', 'source': 'owner', 'content': object()}
    another = qa(3, '主卧灯', '主卧灯是感应的')
    payload = {'sessions': [{'room': '主卧'}]}
    budget = array_cost([tiny, another])
    selected, diagnostics = select_guidance([tiny, invalid, another], payload, token_budget=budget)
    assert selected == [tiny, another]
    assert array_cost(selected) <= budget
    assert diagnostics['selected_ids'] == [1, 3]
    assert diagnostics['available'] == 3 and diagnostics['omitted'] == 1
    assert diagnostics['used_tokens'] == array_cost(selected)
    smaller, diagnostics = select_guidance([tiny, another], payload, token_budget=budget - 1)
    assert len(smaller) == 1
    assert array_cost(smaller) <= budget - 1

def test_many_newer_unrelated_answers_do_not_displace_window_history():
    payload = {'event_groups': [{'device': '主卧台灯', 'area': '主卧', 'entity_id': 'light.bed'}], 'sessions': [{'room': '主卧', 'type': 'room_activity'}], 'occupancy_rollup': {'rooms': [{'room': '主卧', 'minutes': 120}]}}
    rule = {'id': 2, 'kind': 'rule', 'scope': 'all', 'source': 'owner', 'content': '明确规则：不要把刷新当活动'}
    older_related = qa(1, '主卧台灯总亮', '主卧台灯是感应的，短暂亮起不代表有人住')
    general_correction = qa(3, '系统噪声', '不要按传感器刷新计人活动')
    unrelated = [qa(1000 + i, f'阳台第{i}盆花', '每两周浇一次' + '浇' * 40) for i in range(40)]
    items = unrelated + [rule, older_related, general_correction]
    keep = [rule, older_related, general_correction]
    budget = array_cost(keep)
    selected, diagnostics = select_guidance(items, payload, token_budget=budget)
    assert selected == keep
    assert array_cost(selected) <= budget
    assert diagnostics['reasons'] == ['explicit_rule', 'related_correction', 'general_correction']
    assert diagnostics['selected'] == 3
    assert diagnostics['omitted'] == len(items) - 3

def test_full_identity_catalog_is_not_a_window_topic_but_resolves_entities():
    catalog = [{'entity_id': 'sensor.purifier', 'device': '净水器', 'area': '书房'}, {'entity_id': 'light.garage', 'device': '车库灯', 'area': '车库'}]
    item = qa(1, '净水器书房', '不要用净水器流量判断书房有人')
    empty_payload = {'entity_identity_catalog': catalog, 'event_groups': [], 'sessions': [], 'critical_events': []}
    _, diagnostics = select_guidance([item], empty_payload)
    assert diagnostics['reasons'] == ['other_window_mention']
    for active_payload in ({'entity_identity_catalog': catalog, 'critical_events': [{'entity_id': 'sensor.purifier', 'score': 80}]}, {'entity_identity_catalog': catalog, 'event_groups': [{'entity': 'sensor.purifier', 'count': 5}]}):
        _, diagnostics = select_guidance([item], active_payload)
        assert diagnostics['reasons'] == ['related_correction']

def test_historic_pattern_evidence_is_not_current_window_activity():
    catalog = [{'entity_id': 'light.garage', 'device': '车库灯', 'area': '车库'}]
    historic = [{'entity_id': 'light.garage', 'device': '车库灯', 'area': '车库', 'count': 50}]
    payload = {'entity_identity_catalog': catalog, 'pattern_evidence_groups': historic, 'event_groups': [{'device': '主卧灯', 'area': '主卧', 'entity_id': 'light.bed'}], 'sessions': [{'room': '主卧', 'type': 'room_activity'}]}
    garage_answer = qa(99, '车库灯', '车库灯不是感应的')
    window_answer = qa(1, '主卧灯', '主卧灯是感应灯')
    selected, diagnostics = select_guidance([garage_answer, window_answer], payload, token_budget=array_cost([window_answer]))
    assert selected == [window_answer]
    assert diagnostics['reasons'] == ['window_related']
    _, only_historic = select_guidance([garage_answer], {'entity_identity_catalog': catalog, 'pattern_evidence_groups': historic})
    assert only_historic['reasons'] == ['other_window_mention']

def test_duplicate_content_keeps_latest_copy_and_counts_as_omitted():
    old = qa(10, '客厅灯', '客厅灯是感应的')
    new = dict(old, id=20)
    selected, diagnostics = select_guidance([old, new], {'sessions': [{'room': '客厅'}]})
    assert selected == [new]
    assert diagnostics['selected_ids'] == [20]
    assert diagnostics['available'] == 2
    assert diagnostics['omitted'] == 1

def test_acknowledgements_do_not_displace_specific_answers():
    specific = qa(1, '主卧有人吗', '主卧今晚一直有人')
    ack = qa(99, '客厅有人吗', '了解')
    slot_ack = {'id': None, 'kind': 'qa_history', 'scope': 'period', 'source': 'owner', 'content': '半夜有人吗\n房主回答：好的'}
    payload = {'sessions': [{'room': '主卧', 'type': 'room_activity'}]}
    tight, diagnostics = select_guidance([ack, slot_ack, specific], payload, token_budget=array_cost([specific]))
    assert tight == [specific]
    assert diagnostics['reasons'] == ['window_related']
    selected, diagnostics = select_guidance([ack, slot_ack, specific], payload)
    assert selected == [specific, ack, slot_ack]
    assert diagnostics['reasons'] == ['window_related', 'acknowledgement', 'acknowledgement']

def test_other_room_correction_does_not_displace_window_specific_answer():
    other_correction = qa(99, '车库灯', '车库灯不是感应的，别按车库判断')
    window_answer = qa(1, '主卧灯', '主卧灯是感应灯，有人经过才亮')
    payload = {'entity_identity_catalog': [{'entity_id': 'light.garage', 'device': '车库灯', 'area': '车库'}], 'sessions': [{'room': '主卧', 'type': 'room_activity'}]}
    selected, diagnostics = select_guidance([other_correction, window_answer], payload, token_budget=array_cost([window_answer]))
    assert selected == [window_answer]
    assert diagnostics['reasons'] == ['window_related']

def test_selection_is_deterministic_and_leaves_inputs_unchanged():
    payload = {'sessions': [{'room': '主卧'}], 'event_groups': [{'device': '主卧空调'}]}
    items = [qa(3, '主卧空调', '主卧空调不要按刷新计'), qa(2, '阳台花', '浇花'), qa(1, '主卧灯', '主卧灯感应')]
    payload_before, items_before = (copy.deepcopy(payload), copy.deepcopy(items))
    first = select_guidance(items, payload)
    second = select_guidance(items, payload)
    assert first[1] == second[1]
    assert [id(item) for item in first[0]] == [id(item) for item in second[0]]
    assert payload == payload_before
    assert items == items_before
