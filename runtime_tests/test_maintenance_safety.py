import asyncio
import threading
from types import SimpleNamespace
import httpx
from app.network import AsyncClient as GuardedClient
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.database import Database
from app.memory_manager import MemoryManager
from app.provider_settings import ProviderSettings
from app.web import create_web_router

@pytest.fixture(autouse=True)
def _no_unmocked_network(monkeypatch):
    real_client = GuardedClient

    def guarded(*args, **kwargs):
        assert kwargs.get('transport') is not None, 'test attempted real HTTP'
        return real_client(*args, **kwargs)
    monkeypatch.setattr('app.ollama_client.AsyncClient', guarded)

def build_provider(tmp_path):
    return ProviderSettings(Database(tmp_path / 'home_ai.db'), tmp_path / 'data')

class FakeGateway:
    model = 'local-model'
    installed = ['local-model']

    def __init__(self):
        self.models_calls = 0

    async def models(self):
        self.models_calls += 1
        return list(self.installed)

    async def healthy(self):
        return True

def build_web(tmp_path, gateway=None):
    gateway = gateway or FakeGateway()
    db = Database(tmp_path / 'web.db')
    memory = MemoryManager(db)
    settings = SimpleNamespace(timezone='Asia/Shanghai', web_auth_enabled=False, web_username='', web_password='', third_party_default_model='')
    provider = ProviderSettings(db, tmp_path / 'data')
    app = FastAPI()
    app.include_router(create_web_router(db, memory, SimpleNamespace(connected=True), gateway, SimpleNamespace(domains=set(), include=set()), settings, provider_settings=provider))
    return (TestClient(app), db, provider, gateway, memory)

def seed_candidate(db, fingerprint='fp-1'):
    now = '2026-10-07T00:00:00+00:00'
    with db.connect() as con:
        cursor = con.execute("INSERT INTO patterns(type,fingerprint,description,confidence,sample_count,consistency,counter_examples,first_seen,last_seen,status,evidence_json,reason) VALUES('habit',?,'desc',0.8,12,0.9,1,?,?,'candidate','[]','')", (fingerprint, now, now))
        return cursor.lastrowid

def test_changing_the_endpoint_without_a_key_is_refused_before_any_mutation(tmp_path):
    provider = build_provider(tmp_path)
    provider.save(True, 'https://old.example/v1', ['m'], 'm', 'sk-old')
    before = provider.read()
    with pytest.raises(ValueError):
        provider.save(False, 'https://new.example/v1', ['n'], 'n', None)
    assert provider.read() == before
    assert provider.load_key() == 'sk-old'

@pytest.mark.parametrize('url', ['http://old.example/v1', 'ftp://old.example/v1', 'https://user:pass@old.example/v1', 'https://@old.example/v1', 'https://old.example/v1?token=1', 'https://old.example/v1#frag', 'https:///v1', 'https://:443/v1', 'https://old.example:notaport/v1', 'https://old.example:99999/v1', 'https://old.example\\evil/v1', 'https://old.example /v1', 'https://old.example/\x00'])
def test_invalid_remote_urls_are_refused_without_mutation(tmp_path, url):
    provider = build_provider(tmp_path)
    before = provider.read()
    with pytest.raises(ValueError):
        provider.save(True, url, ['m'], '', 'sk-new')
    assert provider.read() == before
    assert provider.load_key() == ''

def test_an_empty_endpoint_stays_supported(tmp_path):
    provider = build_provider(tmp_path)
    provider.save(True, '', [], '', None)
    assert provider.client() is None

def test_a_trailing_slash_is_not_a_new_endpoint(tmp_path):
    provider = build_provider(tmp_path)
    provider.save(True, 'https://old.example/v1', ['m'], 'm', 'sk-old')
    provider.save(True, 'https://old.example/v1/', ['m'], 'm', None)
    assert provider.load_key() == 'sk-old'

def test_explicit_rotation_and_clear_may_move_the_endpoint(tmp_path):
    provider = build_provider(tmp_path)
    provider.save(True, 'https://old.example/v1', ['m'], 'm', 'sk-old')
    provider.save(True, 'https://new.example/v1', ['m'], 'm', 'sk-new')
    assert provider.load_key() == 'sk-new'
    assert provider.read()['base_url'] == 'https://new.example/v1'
    provider.save(True, 'https://third.example/v1', ['m'], 'm', '')
    assert provider.load_key() == ''
    assert provider.read()['base_url'] == 'https://third.example/v1'

def test_a_rejected_rebind_never_sends_the_old_key_to_the_new_destination(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, 'lares_allow_remote', True)
    provider = build_provider(tmp_path)
    provider.save(True, 'https://old.example/v1', ['m'], 'm', 'sk-old')
    with pytest.raises(ValueError):
        provider.save(True, 'https://evil.example/v1', ['m'], 'm', None)
    seen = []

    def handler(request):
        seen.append((request.url.host, request.headers.get('authorization')))
        return httpx.Response(200)
    client = provider.client()
    client.transport = httpx.MockTransport(handler)
    assert asyncio.run(client.healthy()) is True
    assert seen == [('old.example', 'Bearer sk-old')]

def test_settings_rejects_a_mismatching_origin_before_any_provider_work(tmp_path):
    gateway = FakeGateway()
    client, _, provider, gateway, _ = build_web(tmp_path, gateway)
    provider.save(True, 'https://old.example/v1', ['m'], 'm', 'sk-old')
    gateway.models_calls = 0
    response = client.post('/settings', headers={'Origin': 'http://evil.example'}, data={'enabled': 'true', 'base_url': 'https://evil.example/v1', 'api_key': '', 'models': 'm'})
    assert response.status_code == 403
    assert gateway.models_calls == 0
    assert provider.read()['base_url'] == 'https://old.example/v1'
    assert provider.load_key() == 'sk-old'

@pytest.mark.parametrize('origin', ['ftp://testserver', 'http://testserver:8443', 'http://user:pass@testserver', 'http://testserver/settings', 'http://testserver/?x=1', 'http://testserver#frag', 'not-a-url'])
def test_settings_rejects_a_malformed_origin_before_any_provider_work(tmp_path, origin):
    gateway = FakeGateway()
    client, _, provider, gateway, _ = build_web(tmp_path, gateway)
    provider.save(True, 'https://old.example/v1', ['m'], 'm', 'sk-old')
    gateway.models_calls = 0
    response = client.post('/settings', headers={'Origin': origin}, data={'enabled': 'true', 'base_url': 'https://evil.example/v1', 'api_key': '', 'models': 'm'})
    assert response.status_code == 403
    assert gateway.models_calls == 0
    assert provider.load_key() == 'sk-old'

def test_settings_accepts_a_matching_or_absent_origin(tmp_path):
    client, _, provider, _, _ = build_web(tmp_path)
    provider.save(True, 'https://old.example/v1', [], '', 'sk-old')
    payload = {'enabled': 'true', 'base_url': 'https://old.example/v1', 'api_key': '', 'models': ''}
    for origin in (None, 'http://testserver', 'https://testserver'):
        headers = {'Origin': origin} if origin else {}
        response = client.post('/settings', headers=headers, data=payload, follow_redirects=False)
        assert response.status_code == 303, origin
        assert provider.load_key() == 'sk-old'

def test_settings_returns_a_safe_400_for_a_rebinding_attempt(tmp_path):
    gateway = FakeGateway()
    client, _, provider, gateway, _ = build_web(tmp_path, gateway)
    provider.save(True, 'https://old.example/v1', ['m'], 'm', 'sk-old')
    gateway.models_calls = 0
    response = client.post('/settings', headers={'Origin': 'http://testserver'}, data={'enabled': 'true', 'base_url': 'https://new.example/v1', 'api_key': '', 'models': 'm'})
    assert response.status_code == 400
    assert '接口地址' in response.json()['detail']
    assert 'sk-old' not in response.text
    assert 'old.example' not in response.text
    assert 'new.example' not in response.text
    assert gateway.models_calls == 0
    assert provider.read()['base_url'] == 'https://old.example/v1'
    assert provider.load_key() == 'sk-old'

def test_competing_feedback_attempts_have_exactly_one_winner(tmp_path):
    db = Database(tmp_path / 'mem.db')
    memory = MemoryManager(db, min_pattern_samples=1)
    pattern_id = seed_candidate(db)
    barrier = threading.Barrier(2)
    results = []

    def attempt(action):
        barrier.wait()
        results.append((action, memory.feedback(pattern_id, action, None)))
    threads = [threading.Thread(target=attempt, args=(action,)) for action in ('confirm', 'reject')]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted((result for _, result in results)) == [False, True]
    assert db.scalar('SELECT COUNT(*) FROM feedback WHERE pattern_id=?', (pattern_id,)) == 1
    assert db.scalar('SELECT COUNT(*) FROM memories WHERE pattern_id=?', (pattern_id,)) == 1
    assert db.scalar("SELECT COUNT(*) FROM patterns WHERE id=? AND status='candidate'", (pattern_id,)) == 0
    assert memory.feedback(pattern_id, 'confirm', None) is False
    assert db.scalar('SELECT COUNT(*) FROM feedback WHERE pattern_id=?', (pattern_id,)) == 1
    assert db.scalar('SELECT COUNT(*) FROM memories WHERE pattern_id=?', (pattern_id,)) == 1

def test_web_reports_conflict_and_missing_after_a_failed_feedback(tmp_path):
    client, db, _, _, _ = build_web(tmp_path)
    pattern_id = seed_candidate(db)
    assert client.post(f'/api/patterns/{pattern_id}/confirm', json={}).status_code == 200
    assert client.post(f'/api/patterns/{pattern_id}/reject', json={}).status_code == 409
    assert client.post('/api/patterns/999999/confirm', json={}).status_code == 404
    assert db.scalar('SELECT COUNT(*) FROM feedback WHERE pattern_id=?', (pattern_id,)) == 1
    assert db.scalar('SELECT COUNT(*) FROM memories WHERE pattern_id=?', (pattern_id,)) == 1

def test_web_reports_conflict_when_feedback_loses_the_candidate_race(tmp_path, monkeypatch):
    client, db, _, _, memory = build_web(tmp_path)
    pattern_id = seed_candidate(db)
    real_feedback = memory.feedback

    def lost_race(pid, action, note=None):
        assert real_feedback(pid, 'reject', None) is True
        return False
    monkeypatch.setattr(memory, 'feedback', lost_race)
    response = client.post(f'/api/patterns/{pattern_id}/confirm', json={})
    assert response.status_code == 409
    assert db.scalar('SELECT status FROM patterns WHERE id=?', (pattern_id,)) == 'rejected'
    assert db.scalar('SELECT COUNT(*) FROM feedback WHERE pattern_id=?', (pattern_id,)) == 1
    assert db.scalar('SELECT COUNT(*) FROM memories WHERE pattern_id=?', (pattern_id,)) == 1
