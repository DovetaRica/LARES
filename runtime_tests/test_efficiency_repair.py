import asyncio
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo
import pytest
from app.anomaly_detector import AnomalyDetector
from app.behavior_analyzer import BehaviorAnalyzer
from app.database import Database
from app.memory_manager import MemoryManager
from app.report_context_builder import build_window_payload
from app.report_evidence import clipped_sessions, occupancy_rollup
TZ = ZoneInfo('Asia/Shanghai')
START = datetime(2026, 9, 28, 6, tzinfo=TZ)
END = START + timedelta(hours=6)

def analyzer_at(tmp_path):
    db = Database(tmp_path / 'test.db')
    a = BehaviorAnalyzer(db, SimpleNamespace(model='test'), MemoryManager(db), AnomalyDetector(db))
    return (a, db)

def session(db, start, end, room='测试房间', evidence=(), kind='room_activity'):
    with db.connect() as con:
        con.execute('INSERT INTO sessions(room,type,start_ts,end_ts,duration_seconds,status,evidence_json) VALUES(?,?,?,?,?,?,?)', (room, kind, start.isoformat(), end.isoformat() if end else None, int((end - start).total_seconds()) if end else None, 'closed' if end else 'open', json.dumps(evidence)))

def test_rollup_clips_unions_and_keeps_sleep_separate(tmp_path):
    a, db = analyzer_at(tmp_path)
    session(db, START - timedelta(hours=8), START + timedelta(hours=2))
    session(db, START + timedelta(hours=1), END + timedelta(hours=3))
    session(db, START, END, kind='sleep')
    p = a._window_payload(START, END, 'period')
    assert p['occupancy_rollup']['rooms'][0]['minutes'] == 360
    assert p['occupancy_rollup']['room_minutes_total'] == 360
    assert len(p['sessions']) == 2
    assert all((datetime.fromisoformat(row['end_ts']) <= END for row in p['sessions']))

def test_rejected_sensor_cannot_leak_back_into_rollup(tmp_path):
    a, db = analyzer_at(tmp_path)
    a.presence_noise_entities = frozenset({'binary_sensor.noisy'})
    session(db, START, END, evidence=['binary_sensor.noisy'])
    a.segment_memory = SimpleNamespace(enabled=True, recent_detail_minutes=60)
    p = asyncio.run(build_window_payload(a, START, END, 'period'))
    assert p['sessions'] == []
    assert p['occupancy_rollup']['room_minutes_total'] == 0

def test_open_session_counts_only_to_cutoff(tmp_path):
    a, db = analyzer_at(tmp_path)
    session(db, START, None)
    p = a._window_payload(START, END, 'period')
    assert p['occupancy_rollup']['rooms'][0]['minutes'] == 360
    assert p['sessions'][0]['status'] == 'open'

def test_covered_actions_survive_optional_segment_trimming(tmp_path):
    a, db = analyzer_at(tmp_path)
    for hour in range(6):
        db.insert_event({'ts': (START + timedelta(hours=hour)).isoformat(), 'entity_id': f'light.test_{hour}', 'domain': 'light', 'category': 'device', 'source': 'manual', 'event_type': 'state_changed', 'old_state': 'off', 'new_state': 'on'})
    with db.connect() as con:
        con.execute("INSERT INTO event_segments(period_start,period_end,start_event_id,end_event_id,event_count,summary_text,status,created_at,updated_at) VALUES(?,?,1,6,6,?,'succeeded',?,?)", (START.isoformat(), (END - timedelta(minutes=1)).isoformat(), '段' * 10000, END.isoformat(), END.isoformat()))
    a.segment_memory = SimpleNamespace(enabled=True, recent_detail_minutes=60)
    p = asyncio.run(build_window_payload(a, START, END, 'period'))
    m = a._model_payload(p)
    assert len(m['window_facts']['slots']) == 6
    assert all((slot['device_actions'] == 1 and slot['actions'] for slot in m['window_facts']['slots']))
    assert m['event_groups'], 'successful compaction cannot erase all action evidence'
    assert not a._over_budget(m)

def test_every_daily_window_survives_and_failed_prose_is_excluded(tmp_path):
    a, db = analyzer_at(tmp_path)
    midnight = START.replace(hour=0)
    with db.connect() as con:
        for i in range(4):
            lo = midnight + timedelta(hours=6 * i)
            body = {'summary': '事实' * 300, 'generation_status': 'succeeded'}
            con.execute('INSERT INTO period_summaries VALUES(?,?,?,?,?,?)', (lo.isoformat(), (lo + timedelta(hours=6)).isoformat(), json.dumps(body), END.isoformat(), 'test', 1))
    p = asyncio.run(a._day_payload(date(2026, 9, 28)))
    p['memory_segments'] = [{'summary': '可丢散文' * 500} for _ in range(30)]
    m = a._model_payload(p)
    assert len(m['period_summaries']) == 4
    assert all((x['status'] == 'available' for x in m['report_coverage']['windows']))
    with db.connect() as con:
        con.execute('UPDATE period_summaries SET summary_json=? WHERE period_start=?', (json.dumps({'summary': '不可传播占位', 'generation_status': 'model_output_rejected'}), midnight.isoformat()))
        con.execute('UPDATE period_summaries SET summary_json=? WHERE period_start=?', (json.dumps({'summary': '旧状态未知'}), (midnight + timedelta(hours=6)).isoformat()))
    p = asyncio.run(a._day_payload(date(2026, 9, 28)))
    assert len(p['period_summaries']) == 2
    assert sum((x['status'] == 'unavailable' for x in p['report_coverage']['windows'])) == 2
    assert '不可传播占位' not in json.dumps(p, ensure_ascii=False)

def test_old_explicit_rule_and_new_correction_are_not_tail_trimmed(tmp_path):
    a, db = analyzer_at(tmp_path)
    with db.connect() as con:
        con.execute("INSERT INTO ai_guidance(content,created_at) VALUES('重要纠正规则',?)", (START.isoformat(),))
        for i in range(150):
            con.execute('INSERT INTO ai_guidance(content,created_at) VALUES(?,?)', (f'一般提问{i} 房主回答：了解', START.isoformat()))
        con.execute("INSERT INTO ai_guidance(content,created_at) VALUES('更正提问 房主回答：不能按刷新次数判断活动',?)", (START.isoformat(),))
    p = a._window_payload(START, END, 'period')
    p['memory_segments'] = [{'summary': '散文' * 1000} for _ in range(25)]
    m = a._model_payload(p)
    content = json.dumps(m['user_guidance'], ensure_ascii=False)
    assert '重要纠正规则' in content
    assert '不能按刷新次数判断活动' in content
    assert len(m['user_guidance']) == p['guidance_selection']['selected']
    assert p['guidance_selection']['omitted'] > 0

def test_habits_refresh_expire_and_respect_rejection(tmp_path):
    a, db = analyzer_at(tmp_path)
    for i in range(4):
        lo = START - timedelta(days=i + 1)
        session(db, lo, lo + timedelta(hours=1))
        session(db, lo, lo + timedelta(hours=1))
    created, _ = a.memory.harvest_session_habits(now=END)
    row = db.rows("SELECT * FROM memories WHERE type='room_habit'")[0]
    assert row['status'] == 'observed' and '4.0 小时' in row['content']
    p = a._window_payload(START, END + timedelta(seconds=1), 'period')
    assert p['long_term_memories'][0]['id'] == row['id']
    a.memory.harvest_session_habits(now=END + timedelta(minutes=1))
    assert db.scalar('SELECT COUNT(*) FROM memories') == 1
    assert db.scalar('SELECT updated_at FROM memories') != row['updated_at']
    with db.connect() as con:
        con.execute("UPDATE memories SET status='rejected'")
    a.memory.harvest_session_habits(now=END + timedelta(days=1))
    assert db.scalar('SELECT status FROM memories') == 'rejected'
    assert not a._window_payload(START, END + timedelta(days=1), 'daily')['long_term_memories']

def test_habit_day_is_beijing_day_not_utc_date(tmp_path):
    a, db = analyzer_at(tmp_path)
    for i in range(3):
        lo = START.replace(hour=23) - timedelta(days=i + 1)
        session(db, lo, lo + timedelta(hours=2))
    a.memory.harvest_session_habits(now=END)
    assert db.scalar("SELECT sample_count FROM memories WHERE type='room_habit'") == 4

def test_model_accounting_records_retries_failures_and_no_content(tmp_path):
    from app.model_metrics import tracked_call
    from app.model_transport import validated_model_report
    db = Database(tmp_path / 'calls.db')
    calls = []

    async def provider(prompt):
        calls.append(prompt)
        return ('broken' if len(calls) == 1 else '{"ok":true}', {'prompt_eval_count': 3, 'eval_count': 2})

    async def run():
        return await tracked_call(db, 'analyze', {'analysis_scope': 'period', 'period_start': 'private-window'}, 'private-prompt', 'test', lambda: validated_model_report(provider, 'private-prompt', {}, validator=lambda x: x, check_identity=False))
    asyncio.run(run())
    row = db.rows('SELECT * FROM model_calls')[0]
    assert row['attempts'] == 2 and row['rejections'] == 1
    assert row['prompt_tokens'] == 6 and row['output_tokens'] == 4
    assert 'private' not in json.dumps(row)

    async def fail():
        raise TimeoutError('private household content')
    with pytest.raises(TimeoutError):
        asyncio.run(tracked_call(db, 'analyze_questions', {}, 'private', 'test', fail))
    row = db.rows('SELECT * FROM model_calls ORDER BY id DESC LIMIT 1')[0]
    assert row['outcome'] == 'failed' and row['error_type'] == 'TimeoutError'
    assert 'private' not in json.dumps(row)

def test_automatic_distillation_is_opt_in(tmp_path):
    a, db = analyzer_at(tmp_path)

    async def provider(model):
        return 'third-party'
    queued = []

    async def queue(*args):
        queued.append(args)
    a._serving_provider = provider
    a.on_remote_report = queue
    asyncio.run(a._queue_distillation('period', 'window', 'remote'))
    assert not queued
    a.automatic_distillation = True
    asyncio.run(a._queue_distillation('period', 'window', 'remote'))
    assert queued

def test_budget_keeps_window_coverage_and_reports_omitted_examples(tmp_path):
    a, _ = analyzer_at(tmp_path)
    p = a._window_payload(START, END, 'period')
    p['cooccurrence_meta'] = {'diagnostic': 'debug' * 5000}
    p['occupancy_summary']['debounced_presence'] = [{'entity': f'binary_sensor.test{i}', 'note': '证据' * 300} for i in range(20)]
    before = a._payload_tokens(p['cooccurrence_meta'])
    m = a._model_payload(p)
    assert 'cooccurrence_meta' not in m and before > 800
    assert len(m['window_facts']['slots']) == 6
    assert m['occupancy_summary']['omitted_examples']['debounced_presence'] > 0
    assert m['occupancy_summary']['debounced_presence']
    assert not a._over_budget(m)
    assert a._model_payload(p) == m

def test_coverage_normalizes_offsets_and_fallback_preserves_gap(tmp_path):
    from app.report_evidence import report_coverage
    from datetime import timezone
    a, _ = analyzer_at(tmp_path)
    coverage = report_coverage([{'period_start': START.astimezone(timezone.utc).isoformat(), 'summary_json': json.dumps({'generation_status': 'succeeded'})}], 'period_start', [START.isoformat(), END.isoformat()])
    assert [x['status'] for x in coverage['windows']] == ['available', 'unavailable']
    assert a._fallback_report({'report_coverage': coverage}, TimeoutError())['report_coverage'] == coverage

def test_additive_accounting_schema_preserves_existing_database(tmp_path):
    import sqlite3
    db = Database(tmp_path / 'existing.db')
    db.set_state('keep', 'existing-state')
    with db.connect() as con:
        con.execute('DROP TABLE model_calls')
        with sqlite3.connect(tmp_path / 'backup.db') as backup:
            con.backup(backup)
    db = Database(tmp_path / 'existing.db')
    assert db.scalar("SELECT value FROM system_state WHERE key='keep'") == 'existing-state'
    assert db.scalar('SELECT COUNT(*) FROM model_calls') == 0
    with sqlite3.connect(tmp_path / 'backup.db') as backup:
        assert backup.execute("SELECT value FROM system_state WHERE key='keep'").fetchone()[0] == 'existing-state'

def test_cancelled_model_call_is_accounted_and_lock_is_reusable(tmp_path):
    from app.model_metrics import tracked_call, ModelCallLock
    db = Database(tmp_path / 'cancel.db')

    async def run():
        lock = ModelCallLock()
        entered = asyncio.Event()

        async def hanging():
            entered.set()
            await asyncio.Event().wait()

        async def worker():
            async with lock:
                await tracked_call(db, 'analyze', {}, '', 'test', hanging)
        task = asyncio.create_task(worker())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with lock:
            assert lock.locked()
        assert not lock.locked()
    asyncio.run(run())
    assert db.scalar('SELECT outcome FROM model_calls') == 'interrupted'

def test_weekly_keeps_all_days_and_excludes_unknown_status(tmp_path):
    a, db = analyzer_at(tmp_path)
    first = date(2026, 9, 21)
    with db.connect() as con:
        for i in range(7):
            con.execute("INSERT INTO daily_summaries(day,summary_json,created_at) VALUES(?,?,'2026-09-29T00:00:00+00:00')", ((first + timedelta(days=i)).isoformat(), json.dumps({'summary': '事实' * 300, 'generation_status': 'succeeded'})))
    p = a._week_payload(first)
    p['major_sessions'] = [{'note': '可选' * 1000} for _ in range(20)]
    assert len(a._model_payload(p)['daily_summaries']) == 7
    with db.connect() as con:
        con.execute('UPDATE daily_summaries SET summary_json=? WHERE day=?', (json.dumps({'summary': '未知来源'}), first.isoformat()))
    p = a._week_payload(first)
    assert len(p['daily_summaries']) == 6
    assert p['report_coverage']['windows'][0]['status'] == 'unavailable'

@pytest.mark.parametrize('scope', ['daily', 'weekly'])
def test_retrospective_context_omits_polling_but_keeps_real_actions(tmp_path, scope):
    import copy
    a, _ = analyzer_at(tmp_path)
    payload = {'analysis_scope': scope, 'cooccurrence_meta': {}, 'event_counts': {'device_action': 2, 'automation_run': 14000}, 'automation_runs': [{'entity': 'automation.poll', 'count': 14000}], 'home_state_snapshot': {'entities': [{'entity_id': 'light.current', 'state': 'on'}]}, 'entity_identity_catalog': [{'entity_id': 'automation.poll'}, {'entity_id': 'light.real'}], 'memory_segments': [{'summary': '设备动作', 'automation_runs': [{'count': 300}], 'sensor_noise_count': 400}], 'recent_detail': [{'class': 'automation_run', 'entity': 'automation.poll'}, {'class': 'device_action', 'entity': 'light.real', 'source': 'automation'}], 'representative_events': [{'entity_id': 'automation.poll', 'event_type': 'automation_triggered'}, {'entity_id': 'light.real', 'event_type': 'state_changed', 'source': 'automation'}]}
    original = copy.deepcopy(payload)
    model = a._model_payload(payload)
    assert model['automation_runs'] == []
    assert 'home_state_snapshot' not in model
    assert 'automation_runs' not in model['memory_segments'][0]
    assert model['event_counts'] == original['event_counts']
    assert len(model['recent_detail']) == len(model['representative_events']) == 1
    assert model['recent_detail'][0]['entity'] == 'light.real'
    assert model['entity_identity_catalog'] == [{'entity_id': 'light.real'}]
    for key in original:
        if key != 'cooccurrence_meta':
            assert payload[key] == original[key]
    assert payload['evidence_coverage']['automation_rows_omitted'] == 1
    assert payload['evidence_coverage']['current_snapshot_omitted'] is True

def test_daily_combined_context_shares_budget_without_losing_slots_or_rules(tmp_path):
    import copy
    a, _ = analyzer_at(tmp_path)
    slots = [{'start': str(i), 'end': str(i + 1), 'device_actions': 5, 'actions': [{'device': '测试灯' + '动作' * 80} for _ in range(3)], 'omitted_actions': 2} for i in range(4)]
    rules = [{'content': '明确规则：不能把巡检当成人员活动'}, {'kind': 'qa_history', 'content': '更正提问 房主回答：不要凭单一存在信号判断人在家'}]
    optional = [{'kind': 'qa_history', 'content': f'一般提问{i} 房主回答：了解' + '背景' * 100} for i in range(8)]
    payload = {'analysis_scope': 'daily', 'event_counts': {'device_action': 20, 'automation_run': 14000}, 'window_facts': {'slots': slots}, 'occupancy_rollup': {'room_minutes_total': 30}, 'period_summaries': [{'period_start': str(i), 'summary': '事实' * 50} for i in range(4)], 'report_coverage': {'windows': [{'key': str(i), 'status': 'available'} for i in range(4)]}, 'user_guidance': rules + optional, 'guidance_selection': {'available': 15, 'selected': 10, 'omitted': 5}, 'long_term_memories': [{'status': 'confirmed', 'content': '用户确认事实'}, {'status': 'observed', 'content': '可选程序观察' * 100}], 'event_groups': [{'evidence_id': 'test', 'count': 20}], 'cooccurrence_meta': {}, 'home_state_snapshot': {'entities': [{'state': '当前状态' * 100} for _ in range(8)]}}
    original = copy.deepcopy(payload)
    model = a._model_payload(payload, budget=2200)
    assert not a._over_budget(model, 2200)
    assert len(model['window_facts']['slots']) == len(model['period_summaries']) == 4
    assert model['report_coverage'] == original['report_coverage']
    assert model['user_guidance'][:2] == rules
    assert len(model['user_guidance']) < 10
    assert payload['guidance_selection']['selected'] == len(model['user_guidance'])
    assert 'guidance_selection' not in model
    assert model['long_term_memories'][0]['status'] == 'confirmed'
    for slot in model['window_facts']['slots']:
        assert slot['device_actions'] == 5
        assert len(slot['actions']) + slot['omitted_actions'] == 5
    assert any((slot['omitted_actions'] > 2 for slot in model['window_facts']['slots']))
    assert payload['window_facts'] == original['window_facts']
    assert payload['user_guidance'] == original['user_guidance']
    assert a._model_payload(payload, budget=2200) == model

def test_budget_fallback_explains_model_was_not_called(tmp_path):
    from app.behavior_analyzer import PayloadBudgetError
    a, _ = analyzer_at(tmp_path)
    result = a._fallback_report({'analysis_scope': 'daily'}, PayloadBudgetError())
    assert '尚未调用模型' in result['summary']
    assert result['error_type'] == 'PayloadBudgetError'
