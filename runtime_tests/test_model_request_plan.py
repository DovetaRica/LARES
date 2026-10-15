import asyncio
import hashlib
import json
import sqlite3
import httpx
import pytest
from app.database import Database, SCHEMA
from app.llm_schema import ollama_input_budget, ollama_request_plan
from app.model_metrics import model_phase, tracked_call
from app.ollama_client import OllamaClient
REPORT = {'summary': '合成灯是验收动作。', 'supports': [], 'new_candidates': [], 'anomalies': [], 'automation_corrections': [], 'noteworthy': []}

def wire(monkeypatch, handler):
    from app.ollama_client import AsyncClient
    original = AsyncClient
    monkeypatch.setattr('app.ollama_client.AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))

def rows(db):
    return [dict(row, stages_json=json.loads(row['stages_json'])) for row in db.rows('SELECT * FROM model_calls ORDER BY id')]

def test_final_request_plan_uses_shared_budget_and_exact_boundary():
    prompt = '系统规则'
    budget = ollama_input_budget(5000, 1024, prompt)
    assert ollama_request_plan(prompt, '证' * budget, 5000, 1024)['budget_exceeded'] is False
    assert ollama_request_plan(prompt, '证' * (budget + 1), 5000, 1024)['budget_exceeded'] is True

def test_budget_rejection_never_opens_transport_and_is_recorded(tmp_path, monkeypatch):

    def forbidden(**kwargs):
        pytest.fail('oversized input must not open a client')
    monkeypatch.setattr('app.ollama_client.AsyncClient', forbidden)
    db = Database(tmp_path / 'test.db')
    client = OllamaClient('http://127.0.0.1:1', 'test', context_tokens=4096)

    async def run():
        await tracked_call(db, 'analyze', {}, 'private-prompt', 'test', lambda: client.analyze('private-prompt', {'secret': '密' * 10000}))
    with pytest.raises(ValueError, match='input budget'):
        asyncio.run(run())
    row = rows(db)[0]
    assert row['outcome'] == 'failed' and row['attempts'] == 0
    assert row['stages_json'][0]['outcome'] == 'input_budget'
    assert row['stages_json'][0]['http_attempted'] is False
    assert 'secret' not in json.dumps(row) and 'private' not in json.dumps(row)

def test_real_wire_retry_critic_repair_are_separate_and_content_free(tmp_path, monkeypatch):
    replies = [{}, REPORT, {'decision': 'revise', 'issues': [{'code': 'missed_guidance', 'detail': '合成测试'}]}, REPORT]
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={'message': {'content': json.dumps(replies[len(bodies) - 1])}, 'prompt_eval_count': 11, 'eval_count': 7, 'done_reason': 'stop'})
    wire(monkeypatch, handler)
    db = Database(tmp_path / 'test.db')
    client = OllamaClient('http://localhost:1', 'test', review_reports=True)
    payload = {'analysis_scope': 'period', 'period_start': 'private-window', 'private': '私密测试'}
    asyncio.run(tracked_call(db, 'analyze', payload, 'private-prompt', 'test', lambda: client.analyze('private-prompt', payload)))
    row = rows(db)[0]
    stages = row['stages_json']
    assert [stage['phase'] for stage in stages] == ['draft', 'draft', 'critic', 'repair']
    assert row['attempts'] == 4 and row['rejections'] == 1
    assert row['prompt_tokens'] == 44 and row['output_tokens'] == 28
    assert [stage['output_limit'] for stage in stages] == [3072, 3072, 512, 3072]
    for stage, body in zip(stages, bodies):
        assert stage['payload_hash'] == hashlib.sha256(body['messages'][1]['content'].encode()).hexdigest()
        assert stage['prompt_hash'] == hashlib.sha256(body['messages'][0]['content'].encode()).hexdigest()
        assert stage['outcome'] == 'received' and stage['done_reason'] == 'stop'
        assert stage['input_tokens_estimate'] <= stage['input_token_budget']
    assert stages[0]['payload_hash'] == stages[1]['payload_hash']
    assert stages[0]['prompt_hash'] != stages[1]['prompt_hash']
    assert 'private' not in json.dumps(row) and '私密' not in json.dumps(row, ensure_ascii=False)

@pytest.mark.parametrize('failure', ['http', 'malformed', 'cancel'])
def test_failed_wire_and_cancellation_are_accounted(tmp_path, monkeypatch, failure):

    async def handler(request):
        if failure == 'cancel':
            raise asyncio.CancelledError()
        return httpx.Response(503 if failure == 'http' else 200, json={})
    wire(monkeypatch, handler)
    db = Database(tmp_path / 'test.db')
    client = OllamaClient('http://localhost:1', 'test')
    expected = {'http': httpx.HTTPStatusError, 'malformed': KeyError, 'cancel': asyncio.CancelledError}[failure]
    with pytest.raises(expected):
        asyncio.run(tracked_call(db, 'analyze', {}, '', 'test', lambda: client.analyze('', {})))
    row = rows(db)[0]
    stage = row['stages_json'][0]
    assert stage['error_type'] == expected.__name__
    assert row['outcome'] == stage['outcome'] == ('interrupted' if failure == 'cancel' else 'failed')

def test_concurrent_request_phases_do_not_leak(tmp_path, monkeypatch):

    async def handler(request):
        await asyncio.sleep(0)
        return httpx.Response(200, json={'message': {'content': '{}'}, 'done_reason': 'untrusted text'})
    wire(monkeypatch, handler)
    db = Database(tmp_path / 'test.db')
    client = OllamaClient('http://localhost:1', 'test')

    async def run_one(phase):
        with model_phase(phase):
            await tracked_call(db, 'analyze', {}, '', 'test', lambda: client._call_model('', '{}', 'test', 1, 4096, 'json', num_predict=512))

    async def run():
        await asyncio.gather(*(run_one(phase) for phase in ('questions', 'segment', 'scout')))
    asyncio.run(run())
    assert {row['stages_json'][0]['phase'] for row in rows(db)} == {'questions', 'segment', 'scout'}
    assert all((len(row['stages_json']) == 1 for row in rows(db)))
    assert all((row['stages_json'][0]['done_reason'] == 'unknown' for row in rows(db)))

def test_review_uses_the_same_evidence_snapshot_as_the_draft(monkeypatch):
    payload = {'analysis_scope': 'period', 'period_start': '2026-10-01T06:00:00+08:00', 'user_guidance': [{'content': '原始完整规则'}], 'window_facts': {'count': 3}}
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            payload['user_guidance'][0]['content'] = '后来的规则'
            payload['window_facts']['count'] = 99
            reply = REPORT
        else:
            reply = {'decision': 'accept', 'issues': []}
        return httpx.Response(200, json={'message': {'content': json.dumps(reply)}})
    wire(monkeypatch, handler)
    client = OllamaClient('http://localhost:1', 'test', review_reports=True)
    asyncio.run(client.analyze('规则', payload))
    draft = json.loads(bodies[0]['messages'][1]['content'])
    evidence = json.loads(bodies[1]['messages'][1]['content'])['evidence']
    assert draft['window_facts'] == evidence['window_facts'] == {'count': 3}
    assert draft['user_guidance'] == evidence['user_guidance'] == [{'content': '原始完整规则'}]

def test_legacy_database_migration_preserves_rows_and_is_idempotent(tmp_path):
    path = tmp_path / 'legacy.db'
    old_schema = SCHEMA.replace(", stages_json TEXT NOT NULL DEFAULT '[]'", '')
    with sqlite3.connect(path) as con:
        con.executescript(old_schema)
        con.execute('INSERT INTO events(ts,category,event_type,source,attributes_json,metadata_json) VALUES(\'2026-10-01\',\'synthetic\',\'test\',\'test\',\'{"raw":1}\',\'{"raw":2}\')')
        con.execute("INSERT INTO model_calls(scope,window_hash,model,started_at,attempts,rejections,queue_ms,call_ms,input_tokens_estimate,outcome,output_items) VALUES('period','hash','test','2026-10-01',1,0,0,1,3,'succeeded',1)")
        before = con.execute('SELECT * FROM events').fetchall()
    for _ in range(2):
        db = Database(path)
    with db.connect() as con:
        assert [tuple(row) for row in con.execute('SELECT * FROM events')] == before
        assert con.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert con.execute('PRAGMA foreign_key_check').fetchall() == []
    assert rows(db)[0]['stages_json'] == [] and rows(db)[0]['attempts'] == 1

@pytest.mark.parametrize('raw', [None, -1, True, 'private response', 1.5, 2 ** 63])
def test_engine_duration_rejects_missing_and_invalid_values(tmp_path, monkeypatch, raw):

    def handler(request):
        return httpx.Response(200, json={'message': {'content': '{}'}, 'total_duration': raw, 'load_duration': raw, 'prompt_eval_duration': raw, 'eval_duration': raw})
    wire(monkeypatch, handler)
    db = Database(tmp_path / 'test.db')
    client = OllamaClient('http://localhost:1', 'test')
    asyncio.run(tracked_call(db, 'analyze', {}, '', 'test', lambda: client._call_model('', '{}', 'test', 1, 4096, 'json', num_predict=512)))
    row = rows(db)[0]
    assert not any((key.startswith('ollama_') for key in row['stages_json'][0]))
    assert 'private response' not in json.dumps(row)

def test_engine_durations_use_nanoseconds_without_changing_wire_or_accounting(tmp_path, monkeypatch):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={'message': {'content': json.dumps(REPORT)}, 'total_duration': 12345678, 'load_duration': 0, 'prompt_eval_duration': 3000000, 'eval_duration': 9000000, 'prompt_eval_count': 10, 'eval_count': 4, 'private_field': 'private response'})
    wire(monkeypatch, handler)
    db = Database(tmp_path / 'test.db')
    client = OllamaClient('http://localhost:1', 'test')
    asyncio.run(tracked_call(db, 'analyze', {}, '', 'test', lambda: client.analyze('', {})))
    row = rows(db)[0]
    timings = {key: value for key, value in row['stages_json'][0].items() if key.startswith('ollama_')}
    assert timings == {'ollama_total_ms': 12.346, 'ollama_load_ms': 0, 'ollama_prompt_eval_ms': 3, 'ollama_eval_ms': 9}
    assert row['attempts'] == len(bodies) == 1
    assert row['prompt_tokens'] == 10 and row['output_tokens'] == 4
    assert not any((key.startswith('ollama_') for key in bodies[0]))
    assert 'private response' not in json.dumps(row)
