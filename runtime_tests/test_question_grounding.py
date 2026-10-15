import asyncio
import copy
import json
import pytest
from app.database import Database
from app.fact_queries import query_facts
from app.llm_schema import RetryableModelFormatError, compact_json, conservative_token_estimate
from app.question_agent import QuestionAgent
from app.question_grounding import question_context, grounded_schema, validate_grounded_response, program_question_reasons, strip_question_context
GROUP = {'evidence_id': 'a' * 24, 'entity_id': 'light.study', 'device': '书房灯', 'area': '书房', 'source': 'manual', 'state': 'on', 'count': 7, 'first_ts': '2026-10-08T01:00:00+08:00', 'last_ts': '2026-10-08T04:00:00+08:00'}
WINDOW = {'period_start': '2026-10-08T00:00:00+08:00', 'period_end': '2026-10-08T06:00:00+08:00', 'event_groups': [GROUP]}
ITEM = {'evidence_id': 'a' * 24, 'question': '书房灯的手动操作通常出于什么目的？', 'why': '模型编造的说明不能入库', 'options': ['阅读', '临时照明']}

def test_context_keeps_whole_groups_in_exact_window_with_a_size_cap():
    payload = copy.deepcopy(WINDOW)
    payload['event_groups'] += [dict(GROUP, evidence_id='outside', first_ts='2026-10-07T23:00:00+08:00')]
    payload['event_groups'] += [dict(GROUP, evidence_id='huge', device='设备' * 1000)]
    before = copy.deepcopy(payload)
    context = question_context(payload, token_budget=600)
    assert context['evidence'] == [GROUP] and payload == before
    assert conservative_token_estimate(compact_json(context)) <= 600
    assert context['counts_are_records_not_uses'] and context['collection_completeness'] == 'unknown'
    assert question_context({'period_start': 'bad'}) is None

def test_local_contract_binds_identity_and_rejects_invented_numbers():
    payload = {'fact_context': question_context(WINDOW)}
    schema = grounded_schema(payload)
    assert schema['$defs']['QuestionItem']['properties']['evidence_id']['enum'] == ['a' * 24]
    for change in ({'evidence_id': 'unknown'}, {'question': '厨房灯出于什么目的？'}, {'question': '书房灯在03:00开了七次，是为什么？'}, {'question': '书房灯为什么有七个操作？'}, {'options': ['每天3次']}):
        with pytest.raises(RetryableModelFormatError):
            validate_grounded_response({'questions': [{**ITEM, **change}]}, payload)
    valid = validate_grounded_response({'questions': [ITEM]}, payload)
    stored = program_question_reasons(valid, payload['fact_context'])[0]
    assert '7条' in stored['why'] and '记录数不等于使用次数' in stored['why']
    assert ITEM['why'] not in stored['why']

def test_device_names_with_numbers_are_not_rejected_as_numeric_premises():
    window = {**WINDOW, 'event_groups': [{**GROUP, 'device': '书房灯2'}]}
    payload = {'fact_context': question_context(window)}
    item = {**ITEM, 'question': '书房灯2通常用于什么？'}
    assert validate_grounded_response({'questions': [item]}, payload)

def test_nested_saved_context_is_removed_from_model_report_copies():
    payload = {'daily_summaries': [{'report': {'summary': '测试', 'fact_context': {'private': 1}}}], 'own_report': {'fact_context': {'private': 2}, 'summary': '测试'}}
    snapshot = copy.deepcopy(payload)
    stripped = copy.deepcopy(payload)
    strip_question_context(stripped)
    assert payload == snapshot and 'fact_context' not in compact_json(stripped)
    assert stripped['own_report']['summary'] == '测试'

class Model:

    def __init__(self, provider):
        self.provider, self.calls = (provider, [])

    async def analyze_questions(self, prompt, payload, model=None):
        self.calls.append(copy.deepcopy(payload))
        item = dict(ITEM)
        if self.provider != 'ollama':
            item.pop('evidence_id')
        return {'questions': [item]}

@pytest.mark.parametrize('provider', ['ollama', 'third-party'])
def test_local_questions_use_facts_but_remote_data_boundary_is_unchanged(tmp_path, provider):
    db = Database(tmp_path / 'questions.db')
    model = Model(provider)
    agent = QuestionAgent(db, model, policy=None, fact_query=query_facts)
    report = {'summary': '不可信模型摘要', 'supports': ['摘要主张'], 'fact_context': question_context(WINDOW, token_budget=600)}
    assert asyncio.run(agent.propose(report, 'period', WINDOW['period_start']))
    sent = model.calls[0]
    if provider == 'ollama':
        assert 'report_summary' not in sent and 'fact_context' in sent
        context = sent['fact_context']
        assert conservative_token_estimate(compact_json(context)) <= 900
        assert context['checked_fact']['period_start'] == WINDOW['period_start']
        assert context['checked_fact']['period_end'] == WINDOW['period_end']
        assert '程序证据' in agent.pending()[0]['why']
    else:
        assert 'fact_context' not in sent and sent['report_summary'] == report['summary']

def test_failed_proposal_is_not_reported_as_success(tmp_path, monkeypatch):
    import app.question_agent as module

    class Slow(Model):

        async def analyze_questions(self, *args, **kwargs):
            await asyncio.sleep(1)
    monkeypatch.setattr(module, 'MODEL_CALL_WALL_SECONDS', 0.01)
    db = Database(tmp_path / 'timeout.db')
    agent = QuestionAgent(db, Slow('third-party'), policy=None)
    assert asyncio.run(agent.propose({'summary': '测试'}, 'period', WINDOW['period_start'])) == []
    state = json.loads(db.scalar("SELECT value FROM system_state WHERE key='question_job_status'"))
    assert state['status'] == 'failed' and state['stored'] == 0
