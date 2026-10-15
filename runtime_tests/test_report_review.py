import copy
import pytest
from app.llm_schema import RetryableModelFormatError, compact_json, conservative_token_estimate
from app.report_review import REVIEW_CODES, REVIEW_DETAIL_LIMIT, REVIEW_EXCLUDED_FIELDS, REVIEW_ISSUE_LIMIT, REVIEW_PROMPT, build_review_payload, review_response_schema, validate_review_response

def test_prompt_is_concise_and_schema_is_strict():
    assert len(REVIEW_PROMPT) < 650
    assert 'accept' in REVIEW_PROMPT and 'revise' in REVIEW_PROMPT
    schema = review_response_schema()
    assert schema['required'] == ['decision', 'issues']
    assert schema['additionalProperties'] is False
    assert schema['properties']['decision']['enum'] == ['accept', 'revise']
    issue_schema = schema['properties']['issues']
    assert issue_schema['maxItems'] == REVIEW_ISSUE_LIMIT
    assert issue_schema['items']['additionalProperties'] is False
    assert issue_schema['items']['properties']['code']['enum'] == list(REVIEW_CODES)
    assert issue_schema['items']['properties']['detail']['maxLength'] == REVIEW_DETAIL_LIMIT

def test_accept_and_revise_contract_normalizes_detail():
    assert validate_review_response({'decision': 'accept', 'issues': []}) == {'decision': 'accept', 'issues': []}
    result = validate_review_response({'decision': 'revise', 'issues': [{'code': 'unsupported_claim', 'detail': '  草稿称有人做饭，证据只有灯光。  '}]})
    assert result == {'decision': 'revise', 'issues': [{'code': 'unsupported_claim', 'detail': '草稿称有人做饭，证据只有灯光。'}]}
    for code in REVIEW_CODES:
        normalized = validate_review_response({'decision': 'revise', 'issues': [{'code': code, 'detail': '证据不符'}]})
        assert normalized['issues'][0]['code'] == code

@pytest.mark.parametrize('value', [None, 'accept', [], {'decision': 'accept'}, {'decision': 'Accept', 'issues': []}, {'decision': True, 'issues': []}, {'decision': 'accept', 'issues': {}, 'note': 'x'}, {'decision': 'accept', 'issues': 'none'}, {'decision': 'accept', 'issues': [1]}, {'decision': 'accept', 'issues': [{'code': 'unsupported_claim', 'detail': 'x'}]}, {'decision': 'revise', 'issues': []}, {'decision': 'revise', 'issues': [{'code': 'unsupported_claim', 'detail': True}]}, {'decision': 'revise', 'issues': [{'code': True, 'detail': 'x'}]}, {'decision': 'revise', 'issues': [{'code': 'made_up', 'detail': 'x'}]}, {'decision': 'revise', 'issues': [{'code': 'unsupported_claim', 'detail': 'x', 'extra': 1}]}, {'decision': 'revise', 'issues': [{'code': 'unsupported_claim', 'detail': '   '}]}, {'decision': 'revise', 'issues': [{'code': 'unsupported_claim', 'detail': '长' * (REVIEW_DETAIL_LIMIT + 1)}]}, {'decision': 'revise', 'issues': [{'code': 'unsupported_claim', 'detail': 'x'}] * (REVIEW_ISSUE_LIMIT + 1)}])
def test_review_response_rejects_anything_outside_the_contract(value):
    with pytest.raises(RetryableModelFormatError) as error:
        validate_review_response(value)
    assert error.value.code == 'review_schema'

def _payload():
    return {'analysis_scope': 'daily', 'window': {'start': '2026-10-08T00:00:00+08:00', 'end': '2026-10-08T06:00:00+08:00'}, 'guidance': [{'key': 'quiet_night', 'text': '夜间安静不是异常'}], 'major_sessions': [{'room': '主卧', 'minutes': 312, 'sessions': 4}], 'candidate_evidence': [{'evidence_id': 'a' * 24, 'count': 17}], 'report_coverage': {'windows': [{'key': '2026-10-08', 'status': 'available'}]}, 'memory_segments': [{'summary': '可丢散文' * 200}], 'reference_report': {'summary': '范例' * 200}, 'period_summaries': [{'summary': '时段散文'}], 'daily_summaries': [{'summary': '日报散文'}], 'cooccurrence_meta': {'diagnostic': 'debug' * 500}, 'evidence_coverage': {'windows': []}, '_private_note': '内部备注'}

def test_payload_keeps_every_authoritative_fact_and_full_draft():
    draft = {'summary': '夜间安静', 'supports': [{'note': '主卧占用312分钟'}]}
    result = build_review_payload(draft, _payload(), 100000)
    assert result is not None
    assert result['draft'] == draft
    evidence = result['evidence']
    assert evidence['guidance'] == [{'key': 'quiet_night', 'text': '夜间安静不是异常'}]
    assert evidence['major_sessions'] == [{'room': '主卧', 'minutes': 312, 'sessions': 4}]
    assert evidence['report_coverage'] == {'windows': [{'key': '2026-10-08', 'status': 'available'}]}
    assert evidence['candidate_evidence'] == [{'evidence_id': 'a' * 24, 'count': 17}]
    assert evidence['analysis_scope'] == 'daily'
    assert not REVIEW_EXCLUDED_FIELDS & set(evidence)
    assert not any((key.startswith('_') for key in evidence))

def test_payload_is_a_deep_copy_and_never_mutates_inputs():
    draft = {'summary': '夜间安静', 'supports': [{'note': '主卧占用312分钟'}]}
    payload = _payload()
    draft_before, payload_before = (copy.deepcopy(draft), copy.deepcopy(payload))
    result = build_review_payload(draft, payload, 100000)
    assert draft == draft_before and payload == payload_before
    result['evidence']['major_sessions'][0]['minutes'] = 0
    result['draft']['supports'].append({'note': '注入'})
    assert payload['major_sessions'][0]['minutes'] == 312
    assert draft['supports'] == [{'note': '主卧占用312分钟'}]

def test_budget_covers_the_whole_request_and_oversize_skips_honestly():
    draft = {'summary': '夜间安静' * 40}
    payload = {'major_sessions': [{'room': '主卧', 'minutes': 312}]}
    result = build_review_payload(draft, payload, 10 ** 9)
    whole = conservative_token_estimate(compact_json(result))
    assert build_review_payload(draft, payload, whole) is not None
    assert build_review_payload(draft, payload, whole - 1) is None
    evidence_only = conservative_token_estimate(compact_json({'evidence': payload}))
    assert build_review_payload(draft, payload, evidence_only) is None

@pytest.mark.parametrize('budget', [None, 0, -5, True, False, '1500', 1.5])
def test_invalid_or_too_small_budget_skips(budget):
    assert build_review_payload({'summary': 'x'}, {'a': 1}, budget) is None

@pytest.mark.parametrize('draft,payload', [({'summary': 'x'}, 5), ({'summary': 'x'}, [1, 2]), ('散文草稿', {'a': 1}), ({'summary': 'x'}, None)])
def test_non_object_inputs_skip(draft, payload):
    assert build_review_payload(draft, payload, 10000) is None

def test_minimal_request_is_allowed():
    result = build_review_payload({'summary': '无活动'}, {}, 10000)
    assert result == {'draft': {'summary': '无活动'}, 'evidence': {}}
