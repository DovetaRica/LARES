import asyncio
import copy
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from app.anomaly_detector import AnomalyDetector
from app.behavior_analyzer import BehaviorAnalyzer
from app.database import Database
from app.llm_schema import conservative_token_estimate
from app.memory_manager import MemoryManager
from app.model_transport import RETRY_INSTRUCTION

def analyzer(tmp_path, provider='ollama'):
    calls = []

    async def analyze(prompt, payload, **kwargs):
        calls.append((prompt, copy.deepcopy(payload), kwargs))
        return ({'summary': '合成测试', 'supports': [], 'new_candidates': [], 'anomalies': [], 'automation_corrections': [], 'noteworthy': []}, 1, {})
    db = Database(tmp_path / 'test.db')
    model = SimpleNamespace(model='test', provider=provider, analyze=analyze)
    return (BehaviorAnalyzer(db, model, MemoryManager(db), AnomalyDetector(db)), calls)

def test_actual_message_text_retry_and_output_share_context(tmp_path):
    a, calls = analyzer(tmp_path)
    a.prompt = '必须遵守证据。' * 280
    payload = {'analysis_scope': 'period', 'event_groups': [{'v': '例子。' * 100}] * 50}
    result, _, _ = asyncio.run(a._safe_model_analysis(payload, report_type='period'))
    prompt, sent, _ = calls[0]
    total = a._payload_tokens(sent) + conservative_token_estimate(prompt + RETRY_INSTRUCTION) + 128 + a.output_reserve_tokens
    assert total <= a.context_tokens
    assert result['evidence_coverage']['input_token_budget'] < a.payload_budget

def test_unfit_prompt_fails_before_any_request(tmp_path):
    a, calls = analyzer(tmp_path)
    a.prompt = '规则' * 15000
    result, duration, _ = asyncio.run(a._safe_model_analysis({'analysis_scope': 'period'}))
    assert calls == [] and duration == 0
    assert result['generation_status'] != 'succeeded'

def test_remote_budget_is_preserved(tmp_path):
    a, calls = analyzer(tmp_path, 'third-party')
    payload = {'analysis_scope': 'daily', 'event_groups': [{'v': '合成。' * 120}] * 45}
    result, _, _ = asyncio.run(a._safe_model_analysis(payload, budget=a.remote_payload_budget))
    assert len(calls[0][1]['event_groups']) == 45
    assert result['evidence_coverage']['input_token_budget'] == a.remote_payload_budget

def test_current_facts_survive_bounded_prose_and_reference(tmp_path):
    a, _ = analyzer(tmp_path)
    payload = {'analysis_scope': 'period', 'window_facts': {'slots': [{'start': str(i), 'end': str(i + 1), 'device_actions': 1, 'actions': [{'device': '合成灯'}], 'omitted_actions': 0} for i in range(6)]}, 'event_groups': [{'device': '合成灯'}], 'memory_segments': [{'period_start': str(i), 'period_end': str(i + 1), 'summary': f'窗口{i}。' + '补充散文。' * 100} for i in range(24)], 'reference_report': {'model': 'teacher', 'summary': '旧日期事实。' * 150}}
    original = copy.deepcopy(payload)
    sent = a._model_payload(payload)
    assert sent['window_facts'] == original['window_facts']
    assert sent['event_groups'] == original['event_groups']
    assert a._payload_tokens(sent['memory_segments']) <= 600
    assert len(sent['memory_segments']) >= 3
    assert a._payload_tokens(sent['reference_report']) <= 400
    assert payload['memory_segments'] == original['memory_segments']
    assert payload['reference_report'] == original['reference_report']
    assert payload['evidence_coverage']['segments_available'] == 24

def test_style_reference_cannot_carry_another_windows_facts(tmp_path):
    a, _ = analyzer(tmp_path)
    payload = {'analysis_scope': 'period', 'event_groups': [], 'reference_report': {'summary': '旧设备在昨天动作999次', 'supports': ['旧日期事实']}}
    sent = a._model_payload(payload)
    assert sent['reference_report']['purpose'] == 'style_only'
    assert '999' not in str(sent) and '旧设备' not in str(sent)
    assert payload['reference_report']['summary'] == '旧设备在昨天动作999次'
    payload['own_report'] = {'summary': '同窗原稿'}
    sent = a._model_payload(payload)
    assert sent['reference_report']['summary'] == payload['reference_report']['summary']

def test_actual_guidance_attachment_uses_window_activity(tmp_path):
    a, _ = analyzer(tmp_path)
    start = datetime(2026, 10, 1, tzinfo=ZoneInfo('Asia/Shanghai'))
    payload = {'event_groups': [{'device': '书房灯', 'area': '书房'}], 'pattern_evidence_groups': [{'device': '车库灯', 'area': '车库'}], 'entity_identity_catalog': [{'entity_id': 'light.garage', 'area': '车库', 'device': '车库灯'}], 'user_guidance': [{'id': 1, 'kind': 'qa_history', 'content': '书房灯房主回答：正在验收灯光。'}] + [{'id': 100 + i, 'kind': 'qa_history', 'content': '车库灯房主回答：不要再按车库灯判断人员。'} for i in range(50)]}
    a._attach_guidance_and_memories(payload, 'period', start, start + timedelta(hours=6))
    assert payload['user_guidance'][0]['id'] == 1
    assert payload['guidance_selection']['protected_count'] == 0
    assert a._payload_floor(payload, 'user_guidance') == 0
    assert payload['guidance_selection']['omitted'] == 51 - len(payload['user_guidance'])

def test_near_floor_window_keeps_owner_text_and_facts_without_retrieval_metadata(tmp_path):
    a, _ = analyzer(tmp_path)
    lamp = {'entity_id': 'light.study', 'device': '书房灯', 'count': 5}
    payload = {'analysis_scope': 'period', 'event_groups': [lamp], 'entity_identity_catalog': [lamp], 'window_facts': {'slots': [{'start': str(i), 'end': str(i + 1), 'device_actions': 5, 'actions': [], 'omitted_actions': 5} for i in range(6)]}, 'user_guidance': [{'id': i, 'content': f'规则{i}：不根据灯光判断人员身份。', 'source': 'owner', 'created_at': '2026-10-08T00:00:00+08:00'} for i in range(11)], 'guidance_selection': {'available': 204, 'selected': 11, 'omitted': 193, 'protected_count': 11, 'note': '有效明确规则优先，其后为相关回答；未入选不等于被否定。'}}
    expected = copy.deepcopy(payload)
    expected.pop('guidance_selection')
    for row in expected['user_guidance']:
        row.pop('id')
    budget = a._payload_tokens(expected) + 2
    assert a._payload_tokens(payload) > budget
    sent = a._model_payload(payload, budget=budget)
    assert sent == expected
    assert len(payload['user_guidance']) == 11 and payload['user_guidance'][0]['id'] == 0
    assert payload['guidance_selection']['protected_count'] == 11
    assert sent['window_facts'] == payload['window_facts']
    assert payload['evidence_coverage']['input_tokens_estimate'] <= budget
