"""Deterministic synthetic replay of the extracted application core."""
from datetime import datetime, timedelta, timezone
import json
import tempfile
from pathlib import Path


def seed(database):
    from .event_aggregator import EventAggregator
    aggregator = EventAggregator(database)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    def event(second, entity, domain, category, old, new, source='system'):
        return dict(ts=(start+timedelta(seconds=second)).isoformat(), entity_id=entity,
                    domain=domain, category=category, event_type='state_changed',
                    old_state=old, new_state=new, source=source, area='Example room',
                    attributes_json={}, metadata_json={'life_area': 'Example room'})
    for i, value in enumerate([20, 21, 22, 23]):
        aggregator.process(event(i, 'sensor.example_temperature', 'sensor', 'environment', '20', str(value)))
    aggregator.flush_environment()
    aggregator.process(event(10, 'light.example_lamp', 'light', 'action', 'off', 'on', 'automation'))
    aggregator.process(event(25, 'light.example_lamp', 'light', 'action', 'on', 'off', 'manual'))
    database.set_state('collection_started_at', start.isoformat())
    return database.rows('SELECT * FROM events ORDER BY ts,id')


def main():
    from .database import Database
    from .event_preprocessor import preprocess
    with tempfile.TemporaryDirectory(prefix='lares-replay-') as directory:
        rows = seed(Database(Path(directory)/'demo.db'))
        stats = json.loads(rows[0]['metadata_json'])
        segment = preprocess(rows)
        assert stats['avg'] == 21.5 and stats['count'] == 4
        assert any(row['event_type'] == 'automation_correction' for row in rows)
        print(json.dumps({'synthetic': True, 'model_calls': 0, 'executed_actions': 0,
                          'stored_events': len(rows), 'temperature_sample_count': stats['count'],
                          'temperature_average': stats['avg'], 'compressed_items': len(segment.compressed)},
                         ensure_ascii=False))


if __name__ == '__main__':
    main()
