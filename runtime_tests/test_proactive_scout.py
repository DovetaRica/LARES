import asyncio
import copy
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
import pytest
from app.behavior_analyzer import BehaviorAnalyzer
from app.database import Database
from app.model_metrics import ModelCallLock
from app.llm_schema import compact_json, conservative_token_estimate
from app.proactive_contract import apply_guidance_scope, build_scout_payload
from app.guidance_retrieval import select_guidance
from app.ollama_client import OllamaClient
from app.proactive_scout import ProactiveScout, STATE_KEY
from app.web import RegenerationRegistry
NOW = datetime.fromisoformat('2026-10-08T09:35:00+08:00')
GROUP = {'evidence_id': 'known', 'semantic_key': 'light|manual|light.study|on', 'entity_id': 'light.study', 'device': '书房灯', 'area': '书房', 'source': 'manual', 'count': 7, 'local_days': 3, 'state': 'on', 'states': ['on'], 'lookback_days': 14, 'first_ts': '2026-10-05T08:00:00+08:00', 'last_ts': '2026-10-07T09:00:00+08:00'}

def fixture(tmp_path, local=None):
    db = Database(tmp_path / 'scout.db')
    analyzer = SimpleNamespace(_model_lock=ModelCallLock(), segment_memory=None, _json_object=BehaviorAnalyzer._json_object, pending_period_bounds=lambda: [], _pattern_evidence=lambda *args, **kwargs: ([copy.deepcopy(GROUP)], {}), _guidance=lambda scope: [])
    calls = []

    async def select(payload):
        calls.append(payload)
        return [{'evidence_id': 'known', 'focus': 'manual_context'}]
    settings = SimpleNamespace(timezone='Asia/Shanghai', proactive_enabled=True, proactive_max_calls_per_day=3, min_pattern_samples=5)
    scout = ProactiveScout(db, analyzer, local or SimpleNamespace(model='qwen3.5:4b', select_observation_leads=select), RegenerationRegistry(), settings)

    async def plan(payload):
        return {'query': None, 'findings': await scout.local.select_observation_leads(payload)}
    scout.local.plan_observation = plan
    return (scout, calls)

def stored(scout):
    return json.loads(scout.db.scalar('SELECT value FROM system_state WHERE key=?', (STATE_KEY,)))

def test_changed_old_lead_is_checked_once_and_updates_inbox(tmp_path, monkeypatch):
    scout, calls = fixture(tmp_path)
    old = dict(GROUP, focus='manual_context', created_at=(NOW - timedelta(days=2)).isoformat(), status='unconfirmed')
    scout.db.set_state(STATE_KEY, compact_json({'seen_ids': ['known'], 'findings': [old]}))
    changed = dict(GROUP, count=13, local_days=4, last_ts=NOW.isoformat())
    scout.analyzer._pattern_evidence = lambda *args, **kwargs: ([changed], {})
    queries = []

    def facts(path, request, evidence, now):
        queries.append(request)
        return {'tool': 'window_events', 'evidence_id': 'known', 'query_id': 'hash', 'status': 'ok', 'period_start': (now - timedelta(days=14)).isoformat(), 'period_end': now.isoformat(), 'facts': {'action_state_counts': [{'state': 'on', 'source': 'manual', 'count': 13}, {'state': 'off', 'source': 'manual', 'count': 12}]}}
    monkeypatch.setattr('app.proactive_scout.query_facts', facts)
    asyncio.run(scout.run(NOW))
    state = stored(scout)
    assert queries == [{'tool': 'window_events', 'evidence_id': 'known'}]
    assert len(calls) == 2 and 'tool_result' in calls[-1]
    assert len(state['findings']) == 1 and state['findings'][0]['count'] == 13
    assert state['findings_added'] == 0 and state['findings_reviewed'] == 1
    changed.update(count=20, local_days=5, last_ts=(NOW + timedelta(days=2)).isoformat())
    asyncio.run(scout.run(NOW + timedelta(days=2)))
    assert len(calls) == 2 and stored(scout)['status'] == 'no_new_evidence'

def test_old_lead_requires_new_day_new_records_and_two_slots(tmp_path):
    scout, calls = fixture(tmp_path)
    scout.db.set_state(STATE_KEY, compact_json({'seen_ids': ['known'], 'findings': [dict(GROUP, created_at=(NOW - timedelta(days=2)).isoformat())]}))
    asyncio.run(scout.run(NOW))
    assert not calls
    changed = dict(GROUP, count=13, local_days=4, last_ts=NOW.isoformat())
    scout.analyzer._pattern_evidence = lambda *args, **kwargs: ([changed], {})
    scout.settings.proactive_max_calls_per_day = 1
    asyncio.run(scout.run(NOW + timedelta(days=1)))
    assert not calls

def test_scoped_input_over_limit_fits_whole_entries_and_makes_one_call(tmp_path):
    scout, calls = fixture(tmp_path)
    groups = [dict(GROUP, evidence_id=f'{i:064x}', entity_id=f'light.fixture_{i}', semantic_key='s' * 120 + str(i)) for i in range(12)]
    guidance = [{'content': f'规则{i}：灯光动作次数不能证明人员身份或全屋有人。', 'source': 'owner', 'scope': 'all', 'created_at': '2026-10-01T00:00:00+08:00'} for i in range(4)]
    scout.analyzer._pattern_evidence = lambda *args, **kwargs: (groups, {})
    scout.analyzer._guidance = lambda scope: guidance
    before = build_scout_payload(groups, [], [], [])
    selected, _ = select_guidance(guidance, {'event_groups': before['evidence']}, token_budget=600)
    before = apply_guidance_scope(before, selected)
    before['date'] = NOW.date().isoformat()
    assert conservative_token_estimate(compact_json(before)) > 3000

    async def select(payload):
        calls.append(payload)
        return [{'evidence_id': payload['evidence'][0]['evidence_id'], 'focus': 'manual_context'}]
    scout.local.select_observation_leads = select
    asyncio.run(scout.run(NOW))
    assert stored(scout)['status'] == 'succeeded' and len(calls) == 1
    sent = calls[0]
    assert conservative_token_estimate(compact_json(sent)) <= 3000
    assert sent['evidence'] == before['evidence'][:len(sent['evidence'])]
    assert [rule['content'] for rule in sent['user_guidance']] == [rule['content'] for rule in before['user_guidance']]
    assert all((rule['applies_to_evidence_ids'] == [item['evidence_id'] for item in sent['evidence']] for rule in sent['user_guidance']))

def test_unfit_final_scout_core_skips_without_a_model_call(tmp_path, monkeypatch):
    scout, calls = fixture(tmp_path)
    oversized = apply_guidance_scope(build_scout_payload([GROUP], [], [], []), [{'content': '完整业主规则。' * 1000}])
    monkeypatch.setattr('app.proactive_scout.apply_guidance_scope', lambda *args: copy.deepcopy(oversized))
    asyncio.run(scout.run(NOW))
    assert not calls and stored(scout)['status'] == 'input_too_large'
    assert not stored(scout).get('findings')
    assert scout.db.scalar('SELECT COUNT(*) FROM model_calls') == 0

def test_local_leads_are_separate_unconfirmed_program_facts_and_deduplicated(tmp_path):
    scout, calls = fixture(tmp_path)
    asyncio.run(scout.run(NOW))
    state = stored(scout)
    assert state['status'] == 'succeeded' and len(calls) == 1
    assert state['findings'][0]['status'] == 'unconfirmed'
    assert state['findings'][0]['count'] == 7 and state['findings'][0]['local_days'] == 3
    assert '手动' in state['findings'][0]['followup']
    for table in ('events', 'patterns', 'memories', 'daily_summaries', 'period_summaries', 'ai_questions'):
        assert scout.db.scalar(f'SELECT COUNT(*) FROM {table}') == 0
    assert scout.db.rows('SELECT scope,outcome FROM model_calls') == [{'scope': 'proactive', 'outcome': 'succeeded'}]
    asyncio.run(scout.run(NOW + timedelta(hours=4)))
    assert len(calls) == 1 and stored(scout)['status'] == 'no_new_evidence'

@pytest.mark.parametrize('hour,minute,status', [(8, 35, 'outside_hours'), (21, 35, 'outside_hours'), (11, 35, 'report_due'), (17, 35, 'report_due')])
def test_daytime_and_report_reservation(tmp_path, hour, minute, status):
    scout, calls = fixture(tmp_path)
    asyncio.run(scout.run(NOW.replace(hour=hour, minute=minute)))
    assert not calls and stored(scout)['status'] == status

def test_limits_survive_new_runtime_object_and_count_failures(tmp_path):
    scout, calls = fixture(tmp_path)

    async def fail(payload):
        calls.append(payload)
        raise ValueError('only error type may be stored')
    scout.local.select_observation_leads = fail
    for hour in (9, 13, 15, 19):
        restarted = ProactiveScout(scout.db, scout.analyzer, scout.local, scout.registry, scout.settings)
        asyncio.run(restarted.run(NOW.replace(hour=hour)))
    assert len(calls) == 3 and stored(scout)['status'] == 'daily_limit'
    assert stored(scout)['error_type'] == 'ValueError'
    assert 'only error' not in json.dumps(stored(scout))
    asyncio.run(scout.run(NOW + timedelta(days=1)))
    assert len(calls) == 4 and stored(scout)['attempts_today'] == 1

def test_busy_manual_or_missing_report_never_waits_for_model(tmp_path):
    scout, calls = fixture(tmp_path)
    scout.registry.add('daily', 'today', 'today')
    asyncio.run(scout.run(NOW))
    assert not calls and stored(scout)['status'] == 'busy'
    scout.registry.jobs.clear()
    scout.analyzer.pending_period_bounds = lambda: [('missing', 'window')]
    asyncio.run(scout.run(NOW))
    assert not calls and stored(scout)['status'] == 'busy'

def test_disabled_cooldown_and_pending_segments_skip_without_a_call(tmp_path):
    scout, calls = fixture(tmp_path)
    scout.settings.proactive_enabled = False
    asyncio.run(scout.run(NOW))
    assert not calls and stored(scout)['status'] == 'disabled'
    scout.settings.proactive_enabled = True
    scout.db.set_state(STATE_KEY, json.dumps({'last_attempt': NOW.isoformat()}))
    asyncio.run(scout.run(NOW + timedelta(minutes=60)))
    assert not calls and stored(scout)['status'] == 'cooldown'
    scout.analyzer.segment_memory = SimpleNamespace(enabled=True, max_events=400, min_events=40, max_minutes=60, _run_lock=asyncio.Lock(), buffer_state=lambda: {'segments': {'pending': 1}, 'pending_events': 0})
    asyncio.run(scout.run(NOW + timedelta(hours=4)))
    assert not calls and stored(scout)['status'] == 'busy'

def test_regular_lock_waiter_preempts_running_idle_call(tmp_path):
    started, interrupted = (asyncio.Event(), asyncio.Event())

    async def slow(payload):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            interrupted.set()
    scout, _ = fixture(tmp_path, SimpleNamespace(model='local', select_observation_leads=slow))

    async def run():
        idle = asyncio.create_task(scout.run(NOW))
        await started.wait()
        async with scout.analyzer._model_lock:
            assert interrupted.is_set()
        await idle
    asyncio.run(asyncio.wait_for(run(), timeout=2.5))
    assert stored(scout)['status'] == 'yielded'
    assert scout.db.scalar('SELECT outcome FROM model_calls') == 'interrupted'

def test_shutdown_cancels_child_call_and_releases_model(tmp_path):
    started, interrupted = (asyncio.Event(), asyncio.Event())

    async def slow(payload):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            interrupted.set()
    scout, _ = fixture(tmp_path, SimpleNamespace(model='local', select_observation_leads=slow))

    async def run():
        task = asyncio.create_task(scout.run(NOW))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())
    assert interrupted.is_set() and (not scout.analyzer._model_lock.locked())
    assert stored(scout)['status'] == 'interrupted'

def test_known_pattern_ids_and_legacy_rejections_are_not_reproposed(tmp_path):
    scout, calls = fixture(tmp_path)
    with scout.db.connect() as con:
        con.execute("INSERT INTO patterns(type,fingerprint,description,confidence,sample_count,first_seen,last_seen,status,evidence_json) VALUES('habit','old','书房灯无须关注',.5,7,?,?,'rejected','[]')", (NOW.isoformat(), NOW.isoformat()))
    asyncio.run(scout.run(NOW))
    assert not calls and stored(scout)['status'] == 'no_new_evidence'
    with scout.db.connect() as con:
        con.execute('UPDATE patterns SET description=?,evidence_json=?', ('旧名称', json.dumps([{'evidence_id': 'known'}])))
    asyncio.run(scout.run(NOW))
    assert not calls

def test_large_evidence_window_skipped_without_partial_statistics(tmp_path):
    scout, calls = fixture(tmp_path)

    def too_large(*args, **kwargs):
        assert kwargs['max_events'] == 32000
        raise ValueError('dense window')
    scout.analyzer._pattern_evidence = too_large
    asyncio.run(scout.run(NOW))
    assert not calls and stored(scout)['status'] == 'evidence_volume'

def test_owner_answer_scope_is_preserved_between_two_lamps(tmp_path):
    scout, calls = fixture(tmp_path)
    kitchen = dict(GROUP, evidence_id='kitchen', semantic_key='light|kitchen|on', entity_id='light.kitchen', device='厨房灯', area='厨房')
    scout.analyzer._pattern_evidence = lambda *a, **kw: ([copy.deepcopy(GROUP), kitchen], {})
    scout.analyzer._guidance = lambda scope: [{'kind': 'qa_history', 'content': '书房灯为何开关？房主回答：这是验收测试。'}]
    asyncio.run(scout.run(NOW))
    assert calls[0]['user_guidance'][0]['applies_to_evidence_ids'] == ['known']

def test_local_selector_one_call_strict_rejection_and_context_budget():
    client = OllamaClient('http://127.0.0.1:1', 'local', review_reports=True)
    from app.proactive_contract import build_scout_payload
    payload = build_scout_payload([GROUP], [], [], [])
    calls = []

    async def wrong(*args, **kwargs):
        calls.append((args, kwargs))
        return ('{"findings":[{"evidence_id":"invented","focus":"manual_context"}]}', {})
    client._call_model = wrong
    with pytest.raises(ValueError):
        asyncio.run(client.select_observation_leads(payload))
    assert len(calls) == 1 and calls[0][1]['num_predict'] == 512
    assert calls[0][0][4] == 8192
    with pytest.raises(ValueError):
        asyncio.run(client.select_observation_leads(dict(payload, overflow='多' * 15000)))
    assert len(calls) == 1

def test_actual_crossday_query_rejects_dense_window_instead_of_truncating(tmp_path):
    from app.anomaly_detector import AnomalyDetector
    from app.memory_manager import MemoryManager
    db = Database(tmp_path / 'bounded.db')
    for days in (1, 2, 3):
        db.insert_event({'ts': (NOW - timedelta(days=days)).isoformat(), 'domain': 'light', 'entity_id': 'light.study', 'category': 'lighting', 'event_type': 'state_changed', 'source': 'manual', 'old_state': 'off', 'new_state': 'on'})
    analyzer = BehaviorAnalyzer(db, SimpleNamespace(model='local'), MemoryManager(db, min_pattern_samples=3), AnomalyDetector(db), timezone_name='Asia/Shanghai')
    with pytest.raises(ValueError, match='volume'):
        analyzer._pattern_evidence(NOW, max_events=2)
    bounded, _ = analyzer._pattern_evidence(NOW, max_events=3)
    ordinary, _ = analyzer._pattern_evidence(NOW)
    assert bounded == ordinary and bounded[0]['count'] == 3

def planning_fixture(tmp_path, tool='entity_identity'):
    scout, calls = fixture(tmp_path)

    async def plan(payload):
        calls.append(payload)
        if tool is None or not payload['query_allowed']:
            return {'query': None, 'findings': [{'evidence_id': 'known', 'focus': 'manual_context'}]}
        return {'query': {'tool': tool, 'evidence_id': 'known'}, 'findings': []}
    scout.local.plan_observation = plan
    return (scout, calls)

def test_lookup_uses_two_reserved_slots_survives_restart_and_last_slot_cannot_query(tmp_path):
    scout, calls = planning_fixture(tmp_path)
    asyncio.run(scout.run(NOW))
    state = stored(scout)
    assert state['status'] == 'succeeded' and state['attempts_today'] == 2
    assert len(calls) == 2 and calls[1]['tool_result']['facts']['entity_id'] == GROUP['entity_id']
    assert state['last_fact_query']['status'] == 'ok'
    assert 'facts' not in state['last_fact_query']
    for table in ('events', 'patterns', 'memories', 'daily_summaries', 'period_summaries', 'ai_questions'):
        assert scout.db.scalar(f'SELECT COUNT(*) FROM {table}') == 0
    state['seen_ids'] = []
    scout.db.set_state(STATE_KEY, compact_json(state))
    restarted = ProactiveScout(scout.db, scout.analyzer, scout.local, scout.registry, scout.settings)
    asyncio.run(restarted.run(NOW.replace(hour=13)))
    assert len(calls) == 3 and calls[2]['query_allowed'] is False
    assert stored(scout)['attempts_today'] == 3
    asyncio.run(restarted.run(NOW.replace(hour=15)))
    assert len(calls) == 3 and stored(scout)['status'] == 'daily_limit'

def test_planner_can_finish_with_one_request(tmp_path):
    scout, calls = planning_fixture(tmp_path, None)
    asyncio.run(scout.run(NOW))
    assert len(calls) == 1 and stored(scout)['attempts_today'] == 1
    assert stored(scout)['status'] == 'succeeded' and 'last_fact_query' not in stored(scout)

@pytest.mark.parametrize('tool', ['window_events', 'shell'])
def test_failed_or_unobserved_query_is_not_success_and_never_makes_second_request(tmp_path, tool):
    scout, calls = planning_fixture(tmp_path, tool)
    asyncio.run(scout.run(NOW))
    assert len(calls) == 1 and stored(scout)['status'] == 'failed'
    assert not stored(scout).get('seen_ids') and (not stored(scout).get('findings'))

def test_queried_owner_decline_suppresses_only_selected_device(tmp_path):
    scout, calls = planning_fixture(tmp_path, 'owner_guidance')
    other = dict(GROUP, evidence_id='kitchen', entity_id='light.kitchen', device='厨房灯', semantic_key='light|kitchen|on')
    scout.analyzer._pattern_evidence = lambda *a, **kw: ([copy.deepcopy(GROUP), other], {})
    with scout.db.connect() as connection:
        connection.execute('INSERT INTO ai_guidance(content,scope,source,created_at) VALUES(?,?,?,?)', ('书房灯房主回答：不要再问。', 'all', 'owner', NOW.isoformat()))

    async def select(payload):
        calls.append(payload)
        assert [item['evidence_id'] for item in payload['evidence']] == ['kitchen']
        return [{'evidence_id': 'kitchen', 'focus': 'manual_context'}]
    scout.local.select_observation_leads = select
    asyncio.run(scout.run(NOW))
    assert stored(scout)['status'] == 'succeeded'
    assert [note['evidence_id'] for note in stored(scout)['findings']] == ['kitchen']
    assert scout.db.scalar('SELECT COUNT(*) FROM ai_guidance') == 1

def test_priority_arriving_between_requests_yields_without_second_reservation(tmp_path):
    scout, calls = planning_fixture(tmp_path)
    original = scout.local.plan_observation

    async def plan(payload):
        value = await original(payload)
        scout.registry.add('daily', 'today', 'today')
        return value
    scout.local.plan_observation = plan
    asyncio.run(scout.run(NOW))
    assert len(calls) == 1 and stored(scout)['status'] == 'yielded'
    assert stored(scout)['attempts_today'] == 1

def test_priority_cancels_second_request_and_preserves_lock(tmp_path):
    scout, calls = planning_fixture(tmp_path)
    started, cancelled = (asyncio.Event(), asyncio.Event())

    async def slow(payload):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    scout.local.select_observation_leads = slow

    async def run():
        idle = asyncio.create_task(scout.run(NOW))
        await started.wait()
        scout.registry.add('daily', 'today', 'today')
        await idle
    asyncio.run(asyncio.wait_for(run(), timeout=2.5))
    assert cancelled.is_set() and stored(scout)['status'] == 'yielded'
    assert stored(scout)['attempts_today'] == 2 and (not scout.analyzer._model_lock.locked())

def test_omitted_owner_context_requires_query_and_keeps_other_candidates_unseen(tmp_path):
    scout, calls = planning_fixture(tmp_path, 'owner_guidance')
    other = dict(GROUP, evidence_id='kitchen', entity_id='light.kitchen', device='厨房灯', semantic_key='light|kitchen|on')
    scout.analyzer._pattern_evidence = lambda *a, **kw: ([copy.deepcopy(GROUP), other], {})
    scout.analyzer._guidance = lambda scope: [{'content': '无关设备完整规则。' * 1000}]
    with scout.db.connect() as connection:
        connection.execute('INSERT INTO ai_guidance(content,scope,source,created_at) VALUES(?,?,?,?)', ('书房灯房主回答：不要再问。', 'all', 'owner', NOW.isoformat()))
    asyncio.run(scout.run(NOW))
    assert calls[0]['query_required'] is True and len(calls) == 1
    assert stored(scout)['status'] == 'succeeded' and stored(scout)['seen_ids'] == ['known']

def test_insufficient_slots_for_required_query_skip_without_reservation(tmp_path):
    scout, calls = planning_fixture(tmp_path)
    scout.analyzer._guidance = lambda scope: [{'content': '完整规则。' * 1000}]
    scout.db.set_state(STATE_KEY, compact_json({'day': NOW.date().isoformat(), 'attempts_today': 2}))
    asyncio.run(scout.run(NOW))
    assert not calls and stored(scout)['status'] == 'query_budget'
    assert stored(scout)['attempts_today'] == 2
