import asyncio
import json
import pytest
from app.database import Database
from app.task_queue import RegenerationRegistry, run_regeneration_worker

class Recorder:

    def __init__(self):
        self.calls = []

    async def period(self, end=None, hours=6, source='scheduled', model=None):
        self.calls.append(('period', end.isoformat(), source, model))

    async def daily(self, day=None, model=None):
        self.calls.append(('daily', day.isoformat(), model))

    async def weekly(self, week_start=None, model=None):
        self.calls.append(('weekly', week_start.isoformat(), model))

    async def ask_questions(self, scope, model=None):
        self.calls.append(('questions', scope, model))

    async def distill(self, scope, key):
        self.calls.append(('distill', scope, key))

def test_schema_is_additive_and_idempotent(tmp_path):
    path = tmp_path / 'legacy.db'
    db = Database(path)
    attributes = '{"synthetic": "值\\nline"}'
    report = json.dumps({'summary': '已有报告', 'note': 'synthetic\n'}, ensure_ascii=False)
    with db.connect() as con:
        con.execute('INSERT INTO events(ts,category,event_type,source,attributes_json) VALUES(?,?,?,?,?)', ('2026-09-11T00:00:00+00:00', 'synthetic', 'test_event', 'test', attributes))
        con.execute('INSERT INTO period_summaries(period_start,period_end,summary_json,created_at,llm_model,llm_duration_ms) VALUES(?,?,?,?,?,?)', ('2026-09-11T06:00:00+08:00', '2026-09-11T12:00:00+08:00', report, '2026-09-11T12:05:00+08:00', 'test', 1))
    before_events = db.rows('SELECT * FROM events')
    before_reports = db.rows('SELECT * FROM period_summaries')
    with db.connect() as con:
        con.execute('DROP TABLE manual_jobs')
    Database(path)
    Database(path)
    assert db.rows('SELECT attributes_json FROM events')[0]['attributes_json'] == attributes
    assert db.rows('SELECT summary_json FROM period_summaries')[0]['summary_json'] == report
    assert db.rows('SELECT * FROM events') == before_events
    assert db.rows('SELECT * FROM period_summaries') == before_reports
    assert db.scalar('PRAGMA integrity_check') == 'ok'
    assert db.rows('PRAGMA foreign_key_check') == []
    assert db.scalar("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='manual_jobs'") == 1
    assert db.scalar("SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='idx_manual_jobs_active'") == 1

def test_queued_jobs_restore_in_fifo_with_models_and_guard(tmp_path):
    db = Database(tmp_path / 'q.db')
    first = RegenerationRegistry(db)
    assert first.add('period', 'k1', 'v1', 'm1')
    assert first.add('daily', 'k2', 'v2', 'm2')
    assert first.add('period', 'k1', 'v1', 'm1') is False
    rows = db.rows('SELECT scope,key,value,model,status FROM manual_jobs ORDER BY id')
    assert [(r['key'], r['status']) for r in rows] == [('k1', 'queued'), ('k2', 'queued')]
    restored = RegenerationRegistry(db)
    assert [j['key'] for j in restored.jobs] == ['k1', 'k2']
    assert [j['model'] for j in restored.jobs] == ['m1', 'm2']
    assert restored.queue.qsize() == 2
    assert restored.in_flight == {('period', 'k1'), ('daily', 'k2')}

def test_previously_running_job_becomes_interrupted_and_is_not_replayed(tmp_path):
    db = Database(tmp_path / 'q.db')
    first = RegenerationRegistry(db)
    assert first.add('period', 'k', '2026-09-11T12:00:00+08:00', 'm')
    job = first.queue.get_nowait()
    first.mark_running(job)
    restarted = RegenerationRegistry(db)
    assert restarted.jobs == []
    assert restarted.queue.qsize() == 0
    row = db.rows('SELECT status,error_type,finished_at FROM manual_jobs')[0]
    assert row['status'] == 'interrupted'
    assert row['error_type'] == 'AppRestart'
    assert row['finished_at']

def test_cancellation_marks_running_interrupted_and_keeps_waiting(tmp_path):
    db = Database(tmp_path / 'q.db')
    registry = RegenerationRegistry(db)
    assert registry.add('period', 'a', '2026-09-11T12:00:00+08:00')
    assert registry.add('period', 'b', '2026-09-11T18:00:00+08:00')

    class Hanger:

        async def period(self, **kwargs):
            started.set()
            await asyncio.Event().wait()

    async def scenario():
        worker = asyncio.create_task(run_regeneration_worker(Hanger(), registry))
        await asyncio.wait_for(started.wait(), timeout=2)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
    started = asyncio.Event()
    asyncio.run(scenario())
    rows = db.rows('SELECT key,status FROM manual_jobs ORDER BY id')
    assert rows == [{'key': 'a', 'status': 'interrupted'}, {'key': 'b', 'status': 'queued'}]
    restored = RegenerationRegistry(db)
    assert [j['key'] for j in restored.jobs] == ['b']
    assert restored.running is None

def test_handler_failure_records_failed_and_worker_continues(tmp_path):
    db = Database(tmp_path / 'q.db')
    registry = RegenerationRegistry(db)
    assert registry.add('period', 'a', '2026-09-11T12:00:00+08:00')
    assert registry.add('period', 'b', '2026-09-11T18:00:00+08:00')
    recorder = Recorder()

    class Flaky(Recorder):

        async def period(self, end=None, source='scheduled', model=None):
            recorder.calls.append(('period', end.isoformat(), source, model))
            if len(recorder.calls) == 1:
                raise RuntimeError('boom')

    async def scenario():
        worker = asyncio.create_task(run_regeneration_worker(Flaky(), registry))
        await asyncio.wait_for(registry.queue.join(), timeout=2)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
    asyncio.run(scenario())
    rows = db.rows('SELECT key,status,error_type FROM manual_jobs ORDER BY id')
    assert rows[0] == {'key': 'a', 'status': 'failed', 'error_type': 'RuntimeError'}
    assert rows[1] == {'key': 'b', 'status': 'completed', 'error_type': None}
    assert [call[0] for call in recorder.calls] == ['period', 'period']

def test_explicit_retry_after_a_terminal_state_is_allowed(tmp_path):
    db = Database(tmp_path / 'q.db')
    registry = RegenerationRegistry(db)
    assert registry.add('period', 'k', 'v', 'm')
    job = registry.queue.get_nowait()
    registry.mark_running(job)
    registry.jobs.clear()
    assert registry.finish(job, 'completed') is True
    assert registry.add('period', 'k', 'v', 'm') is True
    rows = db.rows('SELECT status FROM manual_jobs ORDER BY id')
    assert [row['status'] for row in rows] == ['completed', 'queued']

def test_supported_tasks_dispatch_the_right_arguments(tmp_path):
    db = Database(tmp_path / 'q.db')
    registry = RegenerationRegistry(db)
    assert registry.add('period', 'k', '2026-09-11T12:00:00+08:00', 'm1')
    assert registry.add('daily', '2026-09-10', '2026-09-10', 'm2')
    assert registry.add('weekly', '2026-09-08', '2026-09-08', 'm3')
    assert registry.add('questions', 'latest', None)
    assert registry.add('distill', 'period:2026-09-11T06:00:00+08:00', 'period:2026-09-11T06:00:00+08:00', 'local')
    recorder = Recorder()

    async def scenario():
        worker = asyncio.create_task(run_regeneration_worker(recorder, registry))
        await asyncio.wait_for(registry.queue.join(), timeout=2)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
    asyncio.run(scenario())
    assert recorder.calls == [('period', '2026-09-11T12:00:00+08:00', 'manual', 'm1'), ('daily', '2026-09-10', 'm2'), ('weekly', '2026-09-08', 'm3'), ('questions', 'period', None), ('distill', 'period', '2026-09-11T06:00:00+08:00')]

def test_unknown_scope_fails_without_falling_through_to_weekly(tmp_path):
    db = Database(tmp_path / 'q.db')
    registry = RegenerationRegistry(db)
    assert registry.add('mystery', 'k', 'v', 'm')
    recorder = Recorder()

    async def scenario():
        worker = asyncio.create_task(run_regeneration_worker(recorder, registry))
        await asyncio.wait_for(registry.queue.join(), timeout=2)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
    asyncio.run(scenario())
    assert recorder.calls == []
    row = db.rows('SELECT status,error_type FROM manual_jobs')[0]
    assert row['status'] == 'failed'
    assert row['error_type'] == 'ValueError'

def test_database_duplicate_is_refused_without_enqueuing(tmp_path):
    db = Database(tmp_path / 'q.db')
    registry = RegenerationRegistry(db)
    with db.connect() as con:
        con.execute("INSERT INTO manual_jobs(scope,key,status,queued_at) VALUES('questions','latest','queued','now')")
    assert registry.add('questions', 'latest', None) is False
    assert registry.jobs == [] and registry.queue.empty()

def test_failed_acceptance_write_never_enqueues(tmp_path, monkeypatch):
    import sqlite3
    db = Database(tmp_path / 'q.db')
    registry = RegenerationRegistry(db)

    def fail():
        raise sqlite3.OperationalError('synthetic write failure')
    monkeypatch.setattr(db, 'connect', fail)
    with pytest.raises(sqlite3.OperationalError):
        registry.add('questions', 'latest', None)
    assert registry.jobs == [] and registry.queue.empty()

def test_terminal_commit_failure_stops_further_model_work(tmp_path, monkeypatch):
    import sqlite3
    registry = RegenerationRegistry(Database(tmp_path / 'q.db'))
    registry.add('daily', 'first', '2026-10-01')
    registry.add('daily', 'second', '2026-10-02')
    real_connect = registry.db.connect

    def fail_write():
        raise sqlite3.OperationalError('private database error')

    class FailAfterHandler(Recorder):

        async def daily(self, **kwargs):
            await super().daily(**kwargs)
            monkeypatch.setattr(registry.db, 'connect', fail_write)
    recorder = FailAfterHandler()

    async def scenario():
        with pytest.raises(RuntimeError, match='terminal state'):
            await asyncio.wait_for(run_regeneration_worker(recorder, registry), 2)
    asyncio.run(scenario())
    monkeypatch.setattr(registry.db, 'connect', real_connect)
    assert len(recorder.calls) == 1
    assert registry.queue.qsize() == 1 and len(registry.in_flight) == 2
    assert registry.db.rows('SELECT status FROM manual_jobs ORDER BY id') == [{'status': 'running'}, {'status': 'queued'}]
    assert registry.snapshot()['error'] == {'code': 'terminal_save_failed', 'error_type': 'OperationalError'}
