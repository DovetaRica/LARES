import asyncio
import hashlib
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from app import main as main_module
from app.config import Settings
from app.runtime import build_runtime
from app.web import RegenerationRegistry
REPO_ROOT = Path(__file__).resolve().parents[1]

def isolated_settings(tmp_path, **overrides):
    config = tmp_path / 'config'
    (config / 'prompts').mkdir(parents=True, exist_ok=True)
    (config / 'prompts' / 'system.txt').write_text('system prompt', encoding='utf-8')
    values = {'ha_url': 'http://127.0.0.1:1', 'ha_token': '', 'ollama_url': 'http://127.0.0.1:1', 'ollama_model': 'test', 'timezone': 'Asia/Shanghai', 'learning_only': True, 'database_path': tmp_path / 'data' / 'home_ai.db', 'config_path': config / 'entities.yaml', 'log_path': tmp_path / 'logs' / 'home-ai.log', 'backup_dir': tmp_path / 'backups', 'home_ai_entity_profile_path': config / 'entities.proposed.yaml', 'home_ai_entity_profile_enabled': False, 'third_party_enabled': False, 'lares_enable_network': True, 'lares_enable_schedule': True}
    values.update(overrides)
    return Settings(**values)

async def _hang(events, label):
    events[f'{label}_started'] = True
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        events[f'{label}_cancelled'] = True
        raise

class FakeScheduler:

    def __init__(self):
        self.started = False
        self.shutdown_calls = 0

    def start(self):
        self.started = True

    def shutdown(self, wait=False):
        self.shutdown_calls += 1

def fake_runtime(settings, events):

    async def healthy():
        return True

    async def models():
        return ['fake']

    def flush_pending():
        return None
    return SimpleNamespace(settings=settings, db=SimpleNamespace(scalar=lambda sql, params=(): 1), memory=SimpleNamespace(), ha=SimpleNamespace(connected=True, run=lambda: _hang(events, 'ha')), ollama=SimpleNamespace(healthy=healthy, models=models, model='fake', remote_models=[]), event_filter=SimpleNamespace(), cooccurrence=None, analyzer=SimpleNamespace(pending_period_bounds=lambda: []), provider_settings=SimpleNamespace(), questions=SimpleNamespace(), registry=RegenerationRegistry(), arrival_tracker=SimpleNamespace(flush_pending=flush_pending), scheduler=FakeScheduler())

def test_importing_app_main_writes_no_files(tmp_path):
    env = {key: value for key, value in os.environ.items() if not key.startswith(('HA_', 'OLLAMA_', 'THIRD_PARTY_', 'DATABASE_', 'CONFIG_', 'LEARNING_', 'PROACTIVE_', 'WEB_', 'ADVISOR_', 'MODEL_', 'SEGMENT_', 'QUESTION_', 'ARRIVAL_', 'SLEEP_', 'LOG_', 'BACKUP_'))}
    env['PYTHONPATH'] = str(REPO_ROOT)
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    result = subprocess.run([sys.executable, '-c', 'import app.main'], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert list(tmp_path.iterdir()) == []

def test_build_runtime_wires_the_shared_lock_and_registry(tmp_path, monkeypatch):

    def no_network(*args, **kwargs):
        raise AssertionError('assembly touched the network')
    monkeypatch.setattr('app.ha_client.websockets.connect', no_network)
    settings = isolated_settings(tmp_path)
    runtime = build_runtime(settings)
    assert runtime.segment_memory._model_lock is runtime.analyzer._model_lock
    assert runtime.proactive_scout.registry is runtime.registry
    assert runtime.analyzer.learning_only is settings.learning_only
    assert runtime.analyzer.ollama is runtime.ollama
    prompt = (REPO_ROOT / 'app' / 'resources' / 'prompts' / 'system.txt').read_bytes()
    assert runtime.db.scalar("SELECT value FROM system_state WHERE key='system_prompt_sha256'") == hashlib.sha256(prompt).hexdigest()
    assert runtime.db.scalar("SELECT value FROM system_state WHERE key='collection_started_at'")
    asyncio.run(runtime.analyzer.on_remote_report('period', '2026-09-11T06:00:00+08:00', 'teacher'))
    assert runtime.registry.jobs[0]['scope'] == 'distill'
    assert runtime.registry.jobs[0]['key'] == 'period:2026-09-11T06:00:00+08:00'

def test_factory_apps_keep_isolated_runtimes_and_serve_fakes(tmp_path, caplog):
    settings = isolated_settings(tmp_path)
    runtimes = []
    processed = []

    def factory(received):
        runtime = fake_runtime(received, {})
        runtime.ollama.model = f'fake-{len(runtimes)}'

        async def models():
            return [runtime.ollama.model]
        runtime.ollama.models = models

        async def period(**kwargs):
            processed.append(runtime)
        runtime.analyzer.period = period
        runtimes.append(runtime)
        return runtime
    app = main_module.create_app(settings=settings, runtime_factory=factory)
    other = main_module.create_app(settings=settings, runtime_factory=factory)
    for run in range(2):
        with TestClient(app) as client:
            with pytest.raises(RuntimeError, match='already running'):
                with TestClient(app):
                    pass
            assert client.get('/health').json() == {'status': 'ok', 'ha': True, 'ollama': True, 'database': True}
            if run == 0:
                routes = len(app.routes)
            assert len(app.routes) == routes
            runtime = runtimes[-1]
            assert client.get('/api/models').json()['default'] == runtime.ollama.model
            assert client.portal.call(runtime.registry.add, 'period', 'synthetic', '2026-10-01T12:00:00+08:00')

            async def drained():
                await asyncio.wait_for(runtime.registry.queue.join(), timeout=2)
            client.portal.call(drained)
            assert processed[-1] is runtime
    with TestClient(other) as client:
        assert client.get('/api/models').json()['default'] == runtimes[-1].ollama.model
    assert len(runtimes) == 3
    assert runtimes[0] is not runtimes[1]
    assert runtimes[0].registry is not runtimes[1].registry
    assert all((runtime.scheduler.shutdown_calls == 1 for runtime in runtimes))
    assert len(processed) == 2
    assert not any((record.getMessage() == 'Background task failed' for record in caplog.records))

def test_lifespan_owns_only_its_background_tasks(tmp_path, monkeypatch):
    settings = isolated_settings(tmp_path, ha_token='synthetic-test-token')
    events = {}
    runtime = fake_runtime(settings, events)
    monkeypatch.setattr(main_module, 'backfill_missing_periods', lambda analyzer, registry=None: _hang(events, 'catchup'))
    monkeypatch.setattr(main_module, 'run_regeneration_worker', lambda analyzer, registry: _hang(events, 'worker'))
    app = main_module.create_app(settings=settings, runtime_factory=lambda received: runtime)
    with TestClient(app) as client:
        assert client.get('/health').status_code == 200
        assert runtime.scheduler.started
    assert events['ha_started'] and events['catchup_started'] and events['worker_started']
    assert events['ha_cancelled'] and events['catchup_cancelled'] and events['worker_cancelled']
    assert runtime.scheduler.shutdown_calls == 1

@pytest.mark.parametrize('failure_phase', ['start', 'shutdown'])
def test_lifespan_cancels_owned_tasks_when_scheduler_fails(tmp_path, monkeypatch, failure_phase):
    settings = isolated_settings(tmp_path, ha_token='synthetic-test-token')
    events = {}
    runtime = fake_runtime(settings, events)
    owned = []
    create_task = asyncio.create_task

    def capture(coro, **kwargs):
        task = create_task(coro, **kwargs)
        if coro.cr_code.co_name == '_hang':
            owned.append(task)
        return task
    monkeypatch.setattr(main_module.asyncio, 'create_task', capture)

    def explode(*args, **kwargs):
        raise RuntimeError('scheduler failed')
    setattr(runtime.scheduler, failure_phase, explode)
    app = main_module.create_app(settings=settings, runtime_factory=lambda received: runtime)
    with pytest.raises(RuntimeError):
        with TestClient(app):
            pass
    assert owned and all((task.cancelled() for task in owned))
