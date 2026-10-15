import asyncio
import copy
import json
from app.database import Database
from app.model_metrics import tracked_call
from app.ollama_client import OllamaClient
from app.llm_schema import compact_json, conservative_token_estimate, ollama_input_budget
REPORT = {'summary': '书房灯由人工打开。', 'supports': [], 'new_candidates': [], 'anomalies': [], 'automation_corrections': [], 'noteworthy': []}
PAYLOAD = {'analysis_scope': 'period', 'event_groups': [{'entity_id': 'light.study', 'device': '书房灯', 'area': '书房', 'source': 'manual', 'count': 1}]}
REVISE = {'decision': 'revise', 'issues': [{'code': 'missed_guidance', 'detail': '应说明这是验收测试。'}]}

def client_for(replies):
    calls = []
    client = OllamaClient('http://127.0.0.1:1', 'test', review_reports=True)

    async def call(prompt, serialized, chosen, timeout, context, schema, **options):
        calls.append({'prompt': prompt, 'payload': json.loads(serialized), 'schema': schema, 'options': options})
        reply = replies[len(calls) - 1]
        return (reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False), {'prompt_eval_count': 10, 'eval_count': 5})
    client._call_model = call
    return (client, calls)

def test_accept_uses_two_calls_and_no_repair():
    client, calls = client_for([REPORT, {'decision': 'accept', 'issues': []}])
    result, _, raw = asyncio.run(client.analyze('只读分析', copy.deepcopy(PAYLOAD)))
    assert len(calls) == 2
    assert calls[1]['payload']['draft'] == REPORT
    assert calls[1]['options']['num_predict'] == 512
    assert result['summary'] == REPORT['summary']
    assert raw['report_review']['status'] == 'passed'

def test_format_retry_then_critic_and_repair_stops_at_four_calls():
    repaired = dict(REPORT, summary='书房灯是验收测试。')
    client, calls = client_for(['{}', REPORT, REVISE, repaired])
    result, _, raw = asyncio.run(client.analyze('只读分析', copy.deepcopy(PAYLOAD)))
    assert len(calls) == 4
    assert result['summary'] == repaired['summary']
    assert raw['report_review']['status'] == 'revised'

def test_revision_has_one_repair_and_all_calls_are_accounted(tmp_path):
    repaired = dict(REPORT, summary='书房灯由人工打开，这是验收测试。')
    client, calls = client_for([REPORT, REVISE, repaired])
    db = Database(tmp_path / 'metrics.db')

    async def run():
        return await tracked_call(db, 'analyze', PAYLOAD, '只读分析', 'test', lambda: client.analyze('只读分析', copy.deepcopy(PAYLOAD)))
    result, _, raw = asyncio.run(run())
    assert len(calls) == 3
    assert result['summary'] == repaired['summary']
    assert calls[2]['payload']['evidence']['event_groups'] == PAYLOAD['event_groups']
    assert raw['report_review'] == {'status': 'revised', 'issues_found': 1, 'repair_output_limit': 3072}
    row = db.rows('SELECT attempts,prompt_tokens,output_tokens FROM model_calls')[0]
    assert row == {'attempts': 3, 'prompt_tokens': 30, 'output_tokens': 15}

def test_malformed_critic_preserves_valid_draft_and_reports_incomplete():
    client, calls = client_for([REPORT, '{broken'])
    result, _, raw = asyncio.run(client.analyze('只读分析', copy.deepcopy(PAYLOAD)))
    assert len(calls) == 2 and result['summary'] == REPORT['summary']
    assert raw['report_review']['status'] == 'unavailable'

def test_failed_repair_is_not_retried_or_labeled_passed():
    client, calls = client_for([REPORT, REVISE, '{}'])
    result, _, raw = asyncio.run(client.analyze('只读分析', copy.deepcopy(PAYLOAD)))
    assert len(calls) == 3 and result['summary'] == REPORT['summary']
    assert raw['report_review']['status'] == 'unresolved'

def test_oversized_review_skips_without_cutting_facts_or_extra_call():
    client, calls = client_for([REPORT])
    payload = dict(PAYLOAD, window_facts={'important': '证据' * 20000})
    original = copy.deepcopy(payload)
    result, _, raw = asyncio.run(client.analyze('只读分析', payload))
    assert len(calls) == 1 and payload == original
    assert raw['report_review']['status'] == 'skipped'

def test_distillation_keeps_existing_single_pass_path():
    client, calls = client_for([REPORT])
    _, _, raw = asyncio.run(client.analyze('蒸馏提示', copy.deepcopy(PAYLOAD), context_tokens=16384))
    assert len(calls) == 1 and 'report_review' not in raw

def test_short_draft_repair_shares_context_without_cutting_input():
    repaired = dict(REPORT, summary='书房灯是验收测试。')
    client, calls = client_for([REPORT, REVISE, repaired])
    payload = dict(PAYLOAD, window_facts={'whole_fact': '完整程序证据。' * 1000}, user_guidance=[{'content': '完整业主规则，不能删改。'}])
    original = copy.deepcopy(payload)
    prompt = '只读分析'
    request = {'draft': REPORT, 'review': REVISE, 'evidence': payload}
    instruction = '根据原始程序证据修订草稿，逐项检查复核意见；复核意见也可能错误，无证据的意见不得采纳。输出完整报告JSON，不复制历史范例事实。'
    repair_prompt = prompt + '\n' + instruction
    client.context_tokens = conservative_token_estimate(compact_json(request)) + conservative_token_estimate(repair_prompt) + 128 + 1600
    result, _, raw = asyncio.run(client.analyze(prompt, payload))
    assert len(calls) == 3 and raw['report_review']['status'] == 'revised'
    assert calls[2]['payload'] == request and payload == original
    assert calls[2]['options']['num_predict'] == 1600
    assert raw['report_review']['repair_output_limit'] == 1600
    assert result['summary'] == repaired['summary']
    assert conservative_token_estimate(compact_json(request)) <= ollama_input_budget(client.context_tokens, 1600, calls[2]['prompt'])

def test_repair_with_insufficient_output_room_keeps_whole_draft_and_reason():
    client, calls = client_for([REPORT, REVISE])
    payload = dict(PAYLOAD, window_facts={'whole_fact': '完整程序证据。' * 1000})
    original = copy.deepcopy(payload)
    request = {'draft': REPORT, 'review': REVISE, 'evidence': payload}
    instruction = '根据原始程序证据修订草稿，逐项检查复核意见；复核意见也可能错误，无证据的意见不得采纳。输出完整报告JSON，不复制历史范例事实。'
    client.context_tokens = conservative_token_estimate(compact_json(request)) + conservative_token_estimate('只读分析\n' + instruction) + 128 + 1023
    result, _, raw = asyncio.run(client.analyze('只读分析', payload))
    assert len(calls) == 2 and payload == original and (result['summary'] == REPORT['summary'])
    assert raw['report_review']['status'] == 'unresolved'
    assert raw['report_review']['reason'] == 'repair_output_budget'
    assert raw['report_review']['repair_output_limit'] == 1023

def test_truncated_repair_is_not_accepted_even_when_json_is_valid():
    client, calls = client_for([REPORT, REVISE, dict(REPORT, summary='达到输出上限的草稿。')])
    original_call = client._call_model

    async def call(*args, **kwargs):
        content, usage = await original_call(*args, **kwargs)
        if len(calls) == 3:
            usage['done_reason'] = 'length'
        return (content, usage)
    client._call_model = call
    result, _, raw = asyncio.run(client.analyze('只读分析', copy.deepcopy(PAYLOAD)))
    assert len(calls) == 3 and result['summary'] == REPORT['summary']
    assert raw['report_review']['status'] == 'unresolved'
    assert raw['report_review']['reason'] == 'repair_output_limit'
