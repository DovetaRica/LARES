from copy import deepcopy
from datetime import datetime, timedelta, timezone
from statistics import mean, pvariance
import random
import pytest
from app.event_aggregator import EventAggregator
from app.event_preprocessor import _telemetry_summary
from app.telemetry_stats import TelemetryWindow, summarize_samples

def sample(value, second=0, **overrides):
    row = dict(ts=(datetime(2026, 9, 20, 23, 59, tzinfo=timezone.utc) + timedelta(seconds=second)).isoformat(), entity_id='sensor.test', domain='sensor', category='environment', event_type='state_changed', old_state='0', new_state=str(value), source='system', attributes_json={}, metadata_json={'life_area': 'test'})
    row.update(overrides)
    return row

def aggregate(values, offset=0):
    window = TelemetryWindow()
    for i, value in enumerate(values):
        window.update(sample(value, offset + i))
    return window.aggregate()

class Recorder:

    def __init__(self):
        self.rows = []
        self.fail_entity = None

    def insert_event(self, event):
        if event['entity_id'] == self.fail_entity:
            raise RuntimeError('synthetic write failure')
        self.rows.append(deepcopy(event))
        return len(self.rows)

def test_online_statistics_match_all_samples_and_chronological_endpoints():
    rng = random.Random(19)
    values = [rng.uniform(-30, 120) for _ in range(1000)]
    order = list(range(len(values)))
    rng.shuffle(order)
    window = TelemetryWindow()
    for i in order:
        window.update(sample(values[i], i))
    stats = window.aggregate()['metadata_json']
    assert stats['count'] == len(values)
    assert stats['avg'] == pytest.approx(mean(values))
    assert stats['variance'] == pytest.approx(pvariance(values))
    assert (stats['min'], stats['max']) == (min(values), max(values))
    assert (stats['first'], stats['last']) == (values[0], values[-1])
    assert stats['period_start'] == sample(0)['ts']
    assert stats['period_end'] == sample(0, 999)['ts']
    assert stats['out_of_order_samples'] > 0
    assert stats['life_area'] == 'test'
    assert stats['missing_samples'] is None

def test_invalid_samples_do_not_become_zero_or_infinite_statistics():
    window = TelemetryWindow()
    for value in ['unknown', 'unavailable', 'NaN', 'inf', '-inf', True]:
        row = sample(value)
        row['new_state'] = value
        assert not window.update(row)
    assert window.aggregate() is None
    window.update(sample(25))
    stats = window.aggregate()['metadata_json']
    assert stats['avg'] == 25
    assert stats['invalid_samples'] == 6
    assert stats['count'] == 1

def test_pooling_weights_samples_retains_spikes_and_variance():
    groups = [[10] * 100, [10, 80, 10]]
    rows = [aggregate(groups[0]), aggregate(groups[1], 100)]
    result = _telemetry_summary('sensor.test', rows)
    values = sum(groups, [])
    assert result['count'] == 2
    assert result['sample_count'] == 103
    assert result['avg'] == pytest.approx(mean(values), abs=0.0001)
    assert result['max'] == 80
    assert result['variance'] == pytest.approx(pvariance(values))
    assert result['first'] == result['last'] == '10'

def test_legacy_aggregates_keep_unknown_quality_and_variance():
    row = aggregate([10, 20, 30])
    row['metadata_json'] = dict(count=3, avg=20, min=10, max=30, delta=20)
    result = summarize_samples([row, sample(40, 10)], lambda event: event['metadata_json'])
    assert result['sample_count'] == 4
    assert result['avg'] == 25
    assert result['invalid_samples'] is None
    assert result['missing_samples'] is None
    assert 'variance' not in result

@pytest.mark.parametrize('bad', [{'count': True}, {'avg': 'NaN'}, {'min': 100}, {'count': 0}])
def test_malformed_aggregate_is_not_reinterpreted_as_one_sample(bad):
    row = aggregate([10, 20])
    row['metadata_json'].update(bad)
    result = summarize_samples([row], lambda event: event['metadata_json'])
    assert result['sample_count'] == 0
    assert result['invalid_statistic_rows'] == 1
    assert 'avg' not in result

def test_flush_failure_retries_only_uncommitted_windows():
    db = Recorder()
    aggregator = EventAggregator(db)
    aggregator.process(sample(1, entity_id='sensor.first'))
    aggregator.process(sample(2, entity_id='sensor.second'))
    db.fail_entity = 'sensor.second'
    with pytest.raises(RuntimeError):
        aggregator.flush_environment()
    assert len(db.rows) == 1
    db.fail_entity = None
    assert aggregator.flush_environment() == 1
    assert [row['entity_id'] for row in db.rows] == ['sensor.first', 'sensor.second']
    assert aggregator.flush_environment() == 0

def test_ingestion_during_flush_keeps_the_next_sample():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    entered, release, attempting = (Event(), Event(), Event())

    class BlockingRecorder(Recorder):

        def insert_event(self, row):
            entered.set()
            assert release.wait(3)
            return super().insert_event(row)
    db = BlockingRecorder()
    aggregator = EventAggregator(db)
    aggregator.process(sample(10))

    def ingest():
        attempting.set()
        aggregator.process(sample(20, 1))
    with ThreadPoolExecutor(max_workers=2) as pool:
        flush = pool.submit(aggregator.flush_environment)
        assert entered.wait(3)
        writer = pool.submit(ingest)
        assert attempting.wait(3)
        try:
            assert not writer.done()
        finally:
            release.set()
        assert flush.result(timeout=3) == 1
        writer.result(timeout=3)
    assert aggregator.flush_environment() == 1
    assert [row['metadata_json']['avg'] for row in db.rows] == [10, 20]

def device(state='on', second=0, source='automation', domain='light', attrs=None):
    return sample(state, second, entity_id=domain + '.test', category='device', domain=domain, source=source, attributes_json=attrs or {})

@pytest.mark.parametrize('domain,before,after', [('light', {'brightness': 200}, {'brightness': 100}), ('climate', {'temperature': 25}, {'temperature': 23}), ('fan', {'percentage': 50}, {'percentage': 25}), ('media_player', {'volume_level': 0.5}, {'volume_level': 0.2})])
def test_real_manual_target_change_is_recorded_once_without_mutating_input(domain, before, after):
    db = Recorder()
    aggregator = EventAggregator(db)
    aggregator.process(device(domain=domain, attrs=before))
    manual = device(second=60, source='manual', domain=domain, attrs=after)
    original = deepcopy(manual)
    aggregator.process(manual)
    assert manual == original
    assert db.rows[-1]['event_type'] == 'automation_correction'
    assert db.rows[-1]['metadata_json']['life_area'] == 'test'
    aggregator.process(device('off', 90, 'manual', domain))
    assert db.rows[-1]['event_type'] == 'state_changed'

@pytest.mark.parametrize('domain,before,after', [('light', {'brightness': 200}, {'brightness': 200}), ('light', {'brightness': 200}, {}), ('climate', {'current_temperature': 25}, {'current_temperature': 23}), ('light', {'friendly_name': 'a'}, {'friendly_name': 'b'}), ('light', {'brightness': 200}, {'brightness': float('nan')})])
def test_refreshes_and_missing_attributes_are_not_corrections(domain, before, after):
    db = Recorder()
    aggregator = EventAggregator(db)
    aggregator.process(device(domain=domain, attrs=before))
    aggregator.process(device(second=60, source='manual', domain=domain, attrs=after))
    assert db.rows[-1]['event_type'] == 'state_changed'

@pytest.mark.parametrize('second,state', [(181, 'off'), (-1, 'off'), (60, 'unavailable')])
def test_expired_late_or_unavailable_events_are_not_corrections(second, state):
    db = Recorder()
    aggregator = EventAggregator(db)
    aggregator.process(device())
    aggregator.process(device(state, second, 'manual'))
    assert db.rows[-1]['event_type'] == 'state_changed'

def test_noop_automated_refresh_does_not_extend_correction_window():
    db = Recorder()
    aggregator = EventAggregator(db)
    aggregator.process(device())
    aggregator.process(device(second=170))
    aggregator.process(device('off', 200, 'manual'))
    assert db.rows[-1]['event_type'] == 'state_changed'

def test_intervening_system_change_invalidates_old_automated_target():
    db = Recorder()
    aggregator = EventAggregator(db)
    aggregator.process(device())
    aggregator.process(device('off', 30, 'system'))
    aggregator.process(device('on', 60, 'manual'))
    assert db.rows[-1]['event_type'] == 'state_changed'
