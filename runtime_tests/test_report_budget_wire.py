import asyncio
import copy
import json
from types import SimpleNamespace
import httpx
from app.network import AsyncClient as GuardedClient
from app.anomaly_detector import AnomalyDetector
from app.behavior_analyzer import BehaviorAnalyzer
from app.database import Database
from app.llm_schema import compact_json, conservative_token_estimate, ollama_input_budget
from app.memory_manager import MemoryManager
from app.ollama_client import OllamaClient

def analyzer(tmp_path):
    calls = []

    async def analyze(prompt, payload, **kwargs):
        calls.append(copy.deepcopy(payload))
        return ({'summary': '合成测试报告', 'supports': [], 'new_candidates': [], 'anomalies': [], 'automation_corrections': [], 'noteworthy': []}, 1, {})
    db = Database(tmp_path / 'test.db')
    model = SimpleNamespace(model='test', provider='ollama', analyze=analyze)
    return (BehaviorAnalyzer(db, model, MemoryManager(db), AnomalyDetector(db)), calls)

def dense_daily():
    group = {'entity_id': 'light.synthetic_study_lamp_state_attribute', 'device': '书房合成灯', 'area': '书房', 'source': 'manual', 'count': 25, 'local_days': 5, 'evidence_id': 'a' * 64, 'first_ts': '2026-10-01T09:00:00+08:00', 'last_ts': '2026-10-07T09:00:00+08:00', 'note': '跨日动作记录不是使用次数，也不证明人员身份。' * 2}
    return {'analysis_scope': 'daily', 'period_start': '2026-10-07T00:00:00+08:00', 'period_end': '2026-10-08T00:00:00+08:00', 'total_events': 3000, 'event_counts': {'device_action': 120, 'presence_raw': 2880}, 'event_groups': [group], 'entity_identity_catalog': [{'entity_id': f'light.synthetic_device_{i}_state_attribute', 'device': f'书房合成灯具{i}', 'area': '书房', 'appliance_type': 'light'} for i in range(20)], 'user_guidance': [{'id': i, 'content': f'规则{i}：' + '灯光动作不能证明房主身份，不要推断房主离家或工作。' * 3, 'source': 'owner', 'scope': 'all', 'created_at': '2026-10-01T00:00:00+08:00'} for i in range(11)], 'guidance_selection': {'available': 211, 'selected': 11, 'omitted': 200, 'protected_count': 11}, 'pattern_evidence_groups': [dict(group, evidence_id=f'{i:064x}') for i in range(12)], 'period_summaries': [{'period_start': f'2026-10-07T{i * 6:02d}:00:00+08:00', 'period_end': f'2026-10-07T{i * 6 + 5:02d}:59:59+08:00', 'summary': '该窗口记录灯光动作；无法据此确定人员身份或全屋是否有人。' * 6} for i in range(4)], 'report_coverage': {'windows': [{'key': str(i), 'status': 'available'} for i in range(4)]}, 'window_facts': {'slots': [{'start': str(i), 'end': str(i + 1), 'device_actions': 30, 'actions': [], 'omitted_actions': 30} for i in range(4)]}, 'occupancy_summary': {'notes': '存在信号不能证明人员身份；缺少信号不能证明房间无人。' * 60}, 'critical_events': [{'kind': 'device_action', 'score': 1}]}

def test_dense_daily_yields_historic_examples_keeps_all_owner_and_lower_sources(tmp_path):
    a, calls = analyzer(tmp_path)
    # Fixed synthetic prompt pressure, independent of the shipped prompt wording.
    a.prompt = '合成预算规则。' * 300
    original = dense_daily()
    floors = a.PAYLOAD_HARD_FLOORS
    a.PAYLOAD_HARD_FLOORS = dict(floors, pattern_evidence_groups=5)
    before, _, _ = asyncio.run(a._safe_model_analysis(copy.deepcopy(original), report_type='daily'))
    assert before['error_type'] == 'PayloadBudgetError' and (not calls)
    a.PAYLOAD_HARD_FLOORS = floors
    payload = copy.deepcopy(original)
    report, _, _ = asyncio.run(a._safe_model_analysis(payload, report_type='daily'))
    assert report.get('generation_status', 'succeeded') == 'succeeded'
    assert len(calls) == 1
    sent = calls[0]
    assert [row['content'] for row in sent['user_guidance']] == [row['content'] for row in original['user_guidance']]
    assert len(sent['entity_identity_catalog']) == 20
    assert sent['window_facts'] == original['window_facts']
    assert sent['event_counts'] == original['event_counts'] and sent['event_groups'] == original['event_groups']
    assert sent['period_summaries'] == original['period_summaries']
    assert sent['report_coverage'] == original['report_coverage']
    assert 1 <= len(sent['pattern_evidence_groups']) < 5
    coverage = report['evidence_coverage']
    assert coverage['input_tokens_estimate'] <= coverage['input_token_budget']
    assert not coverage['budget_exceeded']
    assert coverage['pattern_groups_omitted'] == 12 - len(sent['pattern_evidence_groups'])
    assert 'guidance_selection' not in sent and all(('id' not in row for row in sent['user_guidance']))
    for key in ('user_guidance', 'window_facts', 'pattern_evidence_groups', 'period_summaries'):
        assert payload[key] == original[key]

def test_unfit_protected_core_keeps_text_and_reports_budget_before_any_call(tmp_path):
    a, calls = analyzer(tmp_path)
    payload = {'analysis_scope': 'period', 'user_guidance': [{'id': 1, 'content': '完整业主规则。' * 2000}], 'guidance_selection': {'available': 1, 'selected': 1, 'omitted': 0, 'protected_count': 1}}
    original = copy.deepcopy(payload)
    report, duration, _ = asyncio.run(a._safe_model_analysis(payload, report_type='period'))
    assert not calls and duration == 0 and (report['error_type'] == 'PayloadBudgetError')
    assert payload['user_guidance'] == original['user_guidance']
    coverage = report['evidence_coverage']
    assert coverage['budget_exceeded'] and coverage['input_tokens_estimate'] > coverage['input_token_budget']
    assert coverage['input_tokens_by_field']['user_guidance'] > coverage['input_token_budget']
    assert '完整业主规则' not in compact_json(coverage)

def test_ollama_schema_remains_format_and_never_becomes_message_text(monkeypatch):
    bodies = []

    def respond(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={'message': {'content': '{"summary":"合成测试"}'}})
    client_type = GuardedClient
    monkeypatch.setattr('app.ollama_client.AsyncClient', lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs))
    client = OllamaClient('http://localhost:1', 'test')
    schema = {'type': 'object', 'description': '格式说明。' * 2000}
    asyncio.run(client._call_model('实际提示词', '{"value":1}', 'test', 5, 12288, schema))
    assert bodies[0]['format'] == schema
    assert bodies[0]['messages'] == [{'role': 'system', 'content': '实际提示词'}, {'role': 'user', 'content': '{"value":1}'}]
    assert bodies[0]['options']['num_ctx'] == 12288 and bodies[0]['options']['num_predict'] == 3072

def test_only_message_text_retry_output_and_framing_consume_budget():
    prompt, retry = ('证据规则。' * 200, '失败后重试。')
    expected = 12288 - 3072 - conservative_token_estimate(prompt + retry) - 128
    assert ollama_input_budget(12288, 3072, prompt, retry_instruction=retry) == expected
    assert ollama_input_budget(16384, 3072, prompt, prompt_reserve_tokens=2000) == 16384 - 3072 - 2000
    assert ollama_input_budget(12288, 3072, prompt + '格式说明。' * 100) < expected
