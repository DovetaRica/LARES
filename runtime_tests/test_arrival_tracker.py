import asyncio
import json
import pytest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from app.anomaly_detector import AnomalyDetector
from app.arrival_tracker import ArrivalTracker
from app.behavior_analyzer import BehaviorAnalyzer
from app.database import Database
from app.memory_manager import MemoryManager
BEIJING = ZoneInfo('Asia/Shanghai')
BASE = datetime(2026, 9, 13, 11, 0, tzinfo=timezone.utc)
OUTSIDE = 'binary_sensor.example_device_eb49c8'
ENTRY = 'binary_sensor.example_device_3b8146'
DOOR = 'sensor.example_device_378c6b'

class StubModel:
    model = 'local-model'
    provider = 'ollama'

    async def analyze(self, prompt, payload, model=None, context_tokens=None):
        return ({'summary': 'ok', 'supports': [], 'new_candidates': [], 'anomalies': [], 'automation_corrections': [], 'noteworthy': []}, 1, {})

def build(tmp_path, window=120, door=DOOR):
    db = Database(tmp_path / 'test.db')
    db.set_state('collection_started_at', '2026-09-09T22:55:34+08:00')
    db.set_state((CURSOR_KEY := 'segment_cursor_event_id'), 0)
    tracker = ArrivalTracker(db, OUTSIDE, ENTRY, pair_window_seconds=window, entry_door_entity=door)
    analyzer = BehaviorAnalyzer(db, StubModel(), MemoryManager(db), AnomalyDetector(db), timezone_name='Asia/Shanghai')
    analyzer.event_filter = SimpleNamespace(profile=None)
    return (tracker, analyzer, db)

def on(entity, ts):
    return {'ts': ts, 'entity_id': entity, 'domain': 'binary_sensor', 'category': 'presence', 'event_type': 'state_changed', 'old_state': 'off', 'new_state': 'on', 'source': 'system', 'area': None, 'attributes_json': {}, 'metadata_json': {}}

def door_open(ts):
    return {'ts': ts, 'entity_id': DOOR, 'domain': 'sensor', 'category': 'device', 'event_type': 'state_changed', 'old_state': 'closed', 'new_state': 'open', 'source': 'system', 'area': None, 'attributes_json': {}, 'metadata_json': {}}

def off(entity, ts):
    event = on(entity, ts)
    event.update(old_state='on', new_state='off')
    return event

def test_outside_then_entry_is_an_arrival(tmp_path):
    tracker, analyzer, db = build(tmp_path)
    tracker.process(door_open(BASE.isoformat()))
    t0 = (BASE + timedelta(seconds=5)).isoformat()
    t1 = (BASE + timedelta(seconds=45)).isoformat()
    tracker.process(on(OUTSIDE, t0))
    mid = tracker.process(on(ENTRY, t1))
    row = db.rows('SELECT * FROM movements')[0]
    assert row['kind'] == 'arrival' and row['ts'] == t1
    assert mid == row['id']
    trigger = json.loads(row['trigger_json'])
    assert [t['label'] for t in trigger] == ['门外', '门内玄关', '入户门/门锁辅助']
    assert trigger[0]['identity_confirmed'] is False

def test_entry_then_outside_is_a_departure(tmp_path):
    tracker, analyzer, db = build(tmp_path)
    tracker.process(door_open(BASE.isoformat()))
    tracker.process(on(ENTRY, (BASE + timedelta(seconds=5)).isoformat()))
    tracker.process(on(OUTSIDE, (BASE + timedelta(seconds=90)).isoformat()))
    assert db.rows('SELECT * FROM movements') == []
    tracker.flush_pending(BASE + timedelta(seconds=151))
    row = db.rows('SELECT * FROM movements')[0]
    assert row['kind'] == 'departure'

def test_direction_without_door_evidence_remains_identity_unconfirmed(tmp_path):
    tracker, analyzer, db = build(tmp_path)
    tracker.process(on(OUTSIDE, BASE.isoformat()))
    mid = tracker.process(on(ENTRY, (BASE + timedelta(seconds=45)).isoformat()))
    assert mid is not None
    meta = json.loads(db.rows('SELECT trigger_json FROM movements')[0]['trigger_json'])[0]
    assert meta['door_evidence'] == 'unavailable'
    assert meta['identity_confirmed'] is False

def test_stale_on_edge_beyond_the_window_never_pairs(tmp_path):
    tracker, analyzer, db = build(tmp_path, window=120)
    tracker.process(on(OUTSIDE, BASE.isoformat()))
    mid = tracker.process(on(ENTRY, (BASE + timedelta(minutes=10)).isoformat()))
    assert mid is None
    assert db.rows('SELECT COUNT(*) n FROM movements')[0]['n'] == 0

def test_a_restart_between_triggers_still_pairs(tmp_path):
    now = datetime.now(timezone.utc)
    tracker, analyzer, db = build(tmp_path)
    door_ts = (now - timedelta(seconds=40)).isoformat()
    db.insert_event({'ts': door_ts, 'entity_id': DOOR, 'domain': 'sensor', 'category': 'device', 'event_type': 'state_changed', 'old_state': 'closed', 'new_state': 'open', 'source': 'system', 'area': None, 'attributes_json': {}, 'metadata_json': {}})
    db.insert_event({'ts': (now - timedelta(seconds=30)).isoformat(), 'entity_id': OUTSIDE, 'domain': 'binary_sensor', 'category': 'presence', 'event_type': 'state_changed', 'old_state': 'off', 'new_state': 'on', 'source': 'system', 'area': None, 'attributes_json': {}, 'metadata_json': {}})
    tracker.process(on(OUTSIDE, (now - timedelta(seconds=30)).isoformat()))
    resumed = ArrivalTracker(db, OUTSIDE, ENTRY, pair_window_seconds=120, entry_door_entity=DOOR)
    mid = resumed.process(on(ENTRY, now.isoformat()))
    assert mid is not None
    assert db.rows('SELECT kind FROM movements')[0]['kind'] == 'arrival'

def test_short_trip_under_60s_is_suppressed(tmp_path):
    tracker, analyzer, db = build(tmp_path)
    tracker.process(door_open(BASE.isoformat()))
    tracker.process(on(ENTRY, (BASE + timedelta(seconds=5)).isoformat()))
    mid_dep = tracker.process(on(OUTSIDE, (BASE + timedelta(seconds=20)).isoformat()))
    assert mid_dep is None
    assert db.rows('SELECT * FROM movements') == []
    tracker.process(off(ENTRY, (BASE + timedelta(seconds=25)).isoformat()))
    tracker.process(off(OUTSIDE, (BASE + timedelta(seconds=30)).isoformat()))
    tracker.process(on(OUTSIDE, (BASE + timedelta(seconds=35)).isoformat()))
    mid_arr = tracker.process(on(ENTRY, (BASE + timedelta(seconds=50)).isoformat()))
    assert mid_arr is None
    tracker.flush_pending(BASE + timedelta(seconds=200))
    assert db.rows('SELECT * FROM movements') == []
    assert tracker.state['short_trip_count'] == 1

def test_long_trip_over_60s_is_recorded(tmp_path):
    tracker, analyzer, db = build(tmp_path)
    tracker.process(door_open(BASE.isoformat()))
    tracker.process(on(ENTRY, (BASE + timedelta(seconds=5)).isoformat()))
    tracker.process(on(OUTSIDE, (BASE + timedelta(seconds=20)).isoformat()))
    tracker.process(off(ENTRY, (BASE + timedelta(seconds=25)).isoformat()))
    tracker.process(off(OUTSIDE, (BASE + timedelta(seconds=30)).isoformat()))
    tracker.process(door_open((BASE + timedelta(minutes=5)).isoformat()))
    tracker.process(on(OUTSIDE, (BASE + timedelta(minutes=5, seconds=10)).isoformat()))
    tracker.process(on(ENTRY, (BASE + timedelta(minutes=5, seconds=30)).isoformat()))
    rows = db.rows('SELECT kind FROM movements ORDER BY id')
    assert len(rows) == 2
    assert rows[0]['kind'] == 'departure'
    assert rows[1]['kind'] == 'arrival'

def test_report_payload_carries_readable_movements(tmp_path):
    tracker, analyzer, db = build(tmp_path)
    tracker.process(door_open(BASE.isoformat()))
    tracker.process(on(OUTSIDE, (BASE + timedelta(seconds=5)).isoformat()))
    tracker.process(on(ENTRY, (BASE + timedelta(seconds=30)).isoformat()))
    payload = analyzer._window_payload(BASE.astimezone(BEIJING), (BASE + timedelta(hours=6)).astimezone(BEIJING), 'period')
    assert len(payload['movements']) == 1
    movement = payload['movements'][0]
    assert movement['kind'] == '回家方向'
    assert '身份未确认' in movement['subject']

def test_idle_timeout_is_persistent_and_idempotent_across_restart(tmp_path):
    tracker, _, db = build(tmp_path)
    tracker.process(on(ENTRY, BASE.isoformat()))
    tracker.process(on(OUTSIDE, (BASE + timedelta(seconds=20)).isoformat()))
    restarted = ArrivalTracker(db, OUTSIDE, ENTRY, entry_door_entity=DOOR)
    assert restarted.flush_pending(BASE + timedelta(seconds=80)) is None
    assert restarted.flush_pending(BASE + timedelta(seconds=81)) is not None
    assert restarted.flush_pending(BASE + timedelta(seconds=200)) is None
    again = ArrivalTracker(db, OUTSIDE, ENTRY, entry_door_entity=DOOR)
    assert again.flush_pending(BASE + timedelta(seconds=300)) is None
    rows = db.rows('SELECT * FROM movements')
    assert len(rows) == 1 and rows[0]['ts'] == (BASE + timedelta(seconds=20)).isoformat()

def test_pending_short_return_survives_restart_and_suppresses_both_legs(tmp_path):
    tracker, _, db = build(tmp_path)
    tracker.process(on(ENTRY, BASE.isoformat()))
    tracker.process(on(OUTSIDE, (BASE + timedelta(seconds=20)).isoformat()))
    tracker.process(off(ENTRY, (BASE + timedelta(seconds=25)).isoformat()))
    resumed = ArrivalTracker(db, OUTSIDE, ENTRY, entry_door_entity=DOOR)
    resumed.process(on(ENTRY, (BASE + timedelta(seconds=80)).isoformat()))
    resumed.flush_pending(BASE + timedelta(seconds=200))
    assert db.scalar('SELECT count(*) FROM movements') == 0
    assert resumed.state['short_trip_count'] == 1

def test_unlock_is_auxiliary_not_physical_open_or_identity(tmp_path):
    tracker, analyzer, db = build(tmp_path)
    event = door_open(BASE.isoformat())
    event['new_state'] = '已开锁'
    tracker.process(event)
    tracker.process(on(OUTSIDE, (BASE + timedelta(seconds=5)).isoformat()))
    tracker.process(on(ENTRY, (BASE + timedelta(seconds=20)).isoformat()))
    row = db.rows('SELECT trigger_json FROM movements')[0]
    assert json.loads(row['trigger_json'])[0]['door_evidence'] == 'unlock_only'
    result = analyzer._movements_in_window(BASE.astimezone(BEIJING), (BASE + timedelta(hours=1)).astimezone(BEIJING))
    assert '不代表开门' in result[0]['door_evidence']
    assert '可能' in result[0]['passage_evidence']

def test_repeated_on_refresh_does_not_reverse_an_existing_arrival(tmp_path):
    tracker, _, db = build(tmp_path)
    tracker.process(on(OUTSIDE, BASE.isoformat()))
    tracker.process(on(ENTRY, (BASE + timedelta(seconds=10)).isoformat()))
    refresh = on(OUTSIDE, (BASE + timedelta(seconds=20)).isoformat())
    refresh['old_state'] = 'on'
    tracker.process(refresh)
    tracker.flush_pending(BASE + timedelta(seconds=200))
    assert db.rows('SELECT kind FROM movements') == [{'kind': 'arrival'}]

@pytest.mark.parametrize('case', ['simultaneous', 'late', 'unknown', 'incomplete'])
def test_unreliable_edges_cannot_make_a_direction(tmp_path, case):
    tracker, _, db = build(tmp_path)
    tracker.process(on(OUTSIDE, BASE.isoformat()))
    if case == 'simultaneous':
        tracker.process(on(ENTRY, BASE.isoformat()))
    elif case == 'late':
        tracker.process(on(ENTRY, (BASE - timedelta(seconds=1)).isoformat()))
    elif case == 'unknown':
        event = off(OUTSIDE, (BASE + timedelta(seconds=5)).isoformat())
        event['new_state'] = 'unavailable'
        tracker.process(event)
        tracker.process(on(ENTRY, (BASE + timedelta(seconds=10)).isoformat()))
    else:
        event = on(ENTRY, (BASE + timedelta(seconds=5)).isoformat())
        event['old_state'] = None
        tracker.process(event)
    tracker.flush_pending(BASE + timedelta(seconds=200))
    assert db.scalar('SELECT count(*) FROM movements') == 0

def test_snapshot_and_first_install_never_replay_historical_edges(tmp_path):
    tracker, _, db = build(tmp_path)
    db.insert_event(on(OUTSIDE, datetime.now(timezone.utc).isoformat()))
    started = ArrivalTracker(db, OUTSIDE, ENTRY, entry_door_entity=DOOR)
    started.seed_state([{'entity_id': OUTSIDE, 'state': 'on'}, {'entity_id': ENTRY, 'state': 'on'}])
    assert started.state['anchor'] is None
    assert db.scalar('SELECT count(*) FROM movements') == 0

def test_failed_confirmation_rolls_back_row_and_pending_checkpoint(tmp_path, monkeypatch):
    from contextlib import contextmanager
    tracker, _, db = build(tmp_path)
    tracker.process(on(ENTRY, BASE.isoformat()))
    tracker.process(on(OUTSIDE, (BASE + timedelta(seconds=20)).isoformat()))
    connect = db.connect

    @contextmanager
    def failed_commit():
        with connect() as con:
            yield con
            raise RuntimeError('simulated failed transaction')
    with monkeypatch.context() as patch:
        patch.setattr(db, 'connect', failed_commit)
        with pytest.raises(RuntimeError):
            tracker.flush_pending(BASE + timedelta(seconds=81))
    assert tracker.state['pending_departure'] is not None
    assert db.scalar('SELECT count(*) FROM movements') == 0
    assert tracker.flush_pending(BASE + timedelta(seconds=82)) is not None
    assert db.scalar('SELECT count(*) FROM movements') == 1

def test_idle_confirmation_check_does_not_write_database(tmp_path, monkeypatch):
    tracker, _, db = build(tmp_path)

    def unexpected_write():
        raise AssertionError('idle check must not open a write transaction')
    monkeypatch.setattr(db, 'connect', unexpected_write)
    assert tracker.flush_pending(BASE) is None
