"""Compatibility and isolation checks for the public release, using synthetic stores."""
import asyncio
from contextlib import closing
from pathlib import Path
import sqlite3

import httpx
import pytest

from app.config import Settings, settings
from app.database import Database
from app.network import AsyncClient


def test_v021_rows_survive_additive_upgrade_twice(tmp_path):
    schema = (Path(__file__).parent/'fixtures/v021-schema.sql').read_text(encoding='utf-8')
    path = tmp_path/'legacy.db'
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(schema)
        connection.execute("INSERT INTO events(ts,category,event_type,source) VALUES('2024-01-01','synthetic','test','fixture')")
        connection.execute("INSERT INTO ai_guidance(content,created_at) VALUES('Synthetic rule','2024-01-01')")
        connection.execute("INSERT INTO ai_questions(question,created_at,options_json) VALUES('Synthetic question','2024-01-01','[]')")
        connection.execute("INSERT INTO model_calls(scope,window_hash,model,started_at,attempts,rejections,queue_ms,call_ms,input_tokens_estimate,outcome,output_items) VALUES('period','example-hash','example-model','2024-01-01',1,0,0,1,2,'succeeded',0)")
        connection.commit()
        tables = ('events', 'ai_guidance', 'ai_questions', 'model_calls')
        before = {table: connection.execute(f'SELECT * FROM {table}').fetchall() for table in tables}
    for _ in range(2):
        database = Database(path)
    with database.connect() as connection:
        for table in tables:
            after = [tuple(row) for row in connection.execute(f'SELECT * FROM {table}')]
            if table == 'model_calls':
                assert [row[:-1] for row in after] == before[table]
                assert after[0][-1] == '[]'
            else:
                assert after == before[table]
        assert connection.execute('SELECT COUNT(*) FROM manual_jobs').fetchone()[0] == 0
        assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert connection.execute('PRAGMA foreign_key_check').fetchall() == []


def test_explicit_policies_do_not_share_global_network_authorization(monkeypatch):
    monkeypatch.setattr(settings, 'lares_enable_network', True)
    denied = Settings(_env_file=None, lares_enable_network=False)
    allowed = Settings(_env_file=None, lares_enable_network=True)
    requests = []

    def respond(request):
        requests.append(str(request.url))
        return httpx.Response(200)

    async def run():
        async with AsyncClient(network_policy=denied, transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(PermissionError):
                await client.get('http://localhost:11434/api/tags')
        monkeypatch.setattr(settings, 'lares_enable_network', False)
        async with AsyncClient(network_policy=allowed, transport=httpx.MockTransport(respond)) as client:
            assert (await client.get('http://localhost:11434/api/tags')).status_code == 200

    asyncio.run(run())
    assert requests == ['http://localhost:11434/api/tags']


def test_runtime_and_provider_reconfiguration_keep_instance_policy(tmp_path):
    from app.main import create_app
    from fastapi.testclient import TestClient
    policy = Settings(_env_file=None, database_path=tmp_path/'runtime.db',
                      log_path=tmp_path/'runtime.log', backup_dir=tmp_path/'backups')
    application = create_app(settings=policy)
    with TestClient(application) as client:
        runtime = application.state.runtime
        assert runtime.ha.network_policy is policy
        assert runtime.ollama.local.network_policy is policy
        response = client.post('/settings', headers={'origin': 'http://testserver'},
                               data={'enabled': 'true', 'base_url': 'https://model.example/v1',
                                     'models': 'example-model', 'api_key': 'synthetic-provider-key'},
                               follow_redirects=False)
        assert response.status_code == 303
        assert runtime.ollama.remote.network_policy is policy
        assert runtime.ollama.remote.serves('example-model')


def test_disabled_network_is_not_recorded_as_an_http_attempt(tmp_path):
    import json
    from app.model_metrics import tracked_call
    from app.ollama_client import OllamaClient
    database = Database(tmp_path/'accounting.db')
    client = OllamaClient('http://localhost:11434', 'example-model')
    client.network_policy = Settings(_env_file=None, lares_enable_network=False)
    with pytest.raises(PermissionError):
        asyncio.run(tracked_call(database, 'analyze', {}, '', 'example-model',
                                lambda: client.analyze('', {})))
    row = database.rows('SELECT * FROM model_calls')[0]
    assert row['attempts'] == 0
    assert json.loads(row['stages_json'])[0]['http_attempted'] is False


def test_demo_ignores_ambient_persistent_paths_and_household_config(tmp_path, monkeypatch):
    import app.main as entry
    monkeypatch.setenv('DATABASE_PATH', str(tmp_path/'ambient.db'))
    monkeypatch.setenv('CONFIG_PATH', str(tmp_path/'private.yaml'))
    monkeypatch.setenv('LARES_ENABLE_NETWORK', 'true')
    monkeypatch.setenv('LARES_ENABLE_SCHEDULE', 'true')
    monkeypatch.setenv('HA_TOKEN', 'synthetic-ambient-token')
    monkeypatch.setenv('SLEEP_CURTAIN_ENTITY', 'cover.example_ambient')
    monkeypatch.setattr('sys.argv', ['lares-app', '--demo'])
    captured = []
    application = object()

    def factory(*, settings):
        captured.append(settings)
        return application

    def serve(app, **kwargs):
        assert app is application and kwargs['host'] == '127.0.0.1'
        policy = captured[0]
        assert policy.database_path.is_file()
        assert not policy.lares_enable_network and not policy.lares_enable_schedule
        assert not policy.ha_token and not policy.sleep_curtain_entity
        assert policy.config_path == Path(entry.__file__).parent/'resources/entities.yaml'

    monkeypatch.setattr(entry, 'create_app', factory)
    monkeypatch.setattr('uvicorn.run', serve)
    entry.main()
    assert not (tmp_path/'ambient.db').exists()
    assert not captured[0].database_path.exists()
