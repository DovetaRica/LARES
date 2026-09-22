import math
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
AGGREGATE_TYPES = frozenset({'environment_aggregate', 'appliance_metric_aggregate'})

def finite_number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None

def timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except (TypeError, ValueError):
        return None

@dataclass(slots=True)
class Moments:
    count: int = 0
    mean: float = 0.0
    m2: float = 0.0
    minimum: float = math.inf
    maximum: float = -math.inf
    variance_known: bool = True

    def merge(self, count, mean, minimum, maximum, variance=0.0):
        total = self.count + count
        delta = mean - self.mean
        new_mean = mean if not self.count else self.mean + delta * (count / total)
        new_m2 = self.m2 + (variance or 0.0) * count
        if self.count:
            new_m2 += delta * delta * (self.count / total) * count
        if not math.isfinite(new_mean) or not math.isfinite(new_m2):
            return False
        self.count, self.mean, self.m2 = (total, new_mean, new_m2)
        self.minimum = min(self.minimum, minimum)
        self.maximum = max(self.maximum, maximum)
        self.variance_known = self.variance_known and variance is not None
        return True

    @property
    def variance(self):
        return max(0.0, self.m2 / self.count) if self.count and self.variance_known else None

@dataclass(slots=True)
class TelemetryWindow:
    moments: Moments = field(default_factory=Moments)
    first_at: datetime | None = None
    last_at: datetime | None = None
    first_ts: str | None = None
    last_ts: str | None = None
    first_value: float | None = None
    last_value: float | None = None
    last_event: dict | None = None
    invalid_samples: int = 0
    out_of_order_samples: int = 0

    def update(self, event):
        value, at = (finite_number(event.get('new_state')), timestamp(event.get('ts')))
        if value is None or at is None or (not self.moments.merge(1, value, value, value)):
            self.invalid_samples += 1
            return False
        if self.last_at is not None and at < self.last_at:
            self.out_of_order_samples += 1
        if self.first_at is None or at < self.first_at:
            self.first_at, self.first_ts, self.first_value = (at, event['ts'], value)
        if self.last_at is None or at >= self.last_at:
            self.last_at, self.last_ts, self.last_value = (at, event['ts'], value)
            self.last_event = deepcopy(event)
        return True

    def aggregate(self):
        if not self.moments.count:
            return None
        base = deepcopy(self.last_event)
        stats = self.moments
        delta = self.last_value - self.first_value
        metadata = dict(base.get('metadata_json') or {})
        metadata.update({'count': stats.count, 'avg': stats.mean, 'min': stats.minimum, 'max': stats.maximum, 'delta': delta if math.isfinite(delta) else None, 'first': self.first_value, 'last': self.last_value, 'period_start': self.first_ts, 'period_end': self.last_ts, 'valid_samples': stats.count, 'invalid_samples': self.invalid_samples, 'missing_samples': None, 'out_of_order_samples': self.out_of_order_samples, 'variance': stats.variance, 'variance_ddof': 0, 'aggregation_version': 'sample-stats-v1', 'average_basis': 'samples', 'metric_class': 'sensor_telemetry'})
        base.update({'ts': self.last_ts, 'old_state': None, 'new_state': str(self.last_value), 'source': 'aggregate', 'metadata_json': metadata, 'event_type': 'environment_aggregate' if base['category'] == 'environment' else 'appliance_metric_aggregate'})
        return base

def aggregate_statistics(stats):
    count = stats.get('count')
    values = {key: finite_number(stats.get(key)) for key in ('avg', 'min', 'max', 'delta')}
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        return None
    if any((value is None for value in values.values())):
        return None
    if not values['min'] <= values['avg'] <= values['max']:
        return None
    return {'count': count, **values}

def summarize_samples(group, metadata):
    moments = Moments()
    first_at = last_at = None
    first_value = last_value = None
    first_ts = last_ts = None
    invalid = 0
    invalid_known = True
    rejected_rows = 0
    for event in group:
        stats = metadata(event)
        start = stats.get('period_start') or event.get('ts')
        end = stats.get('period_end') or event.get('ts')
        at_start, at_end = (timestamp(start), timestamp(end))
        last = finite_number(event.get('new_state'))
        if event.get('event_type') in AGGREGATE_TYPES:
            block = aggregate_statistics(stats)
            if block is None:
                rejected_rows += 1
                invalid_known = False
                continue
            first = finite_number(stats.get('first'))
            if first is None and last is not None:
                first = finite_number(last - block['delta'])
            variance = finite_number(stats.get('variance')) if stats.get('variance_ddof') == 0 else None
            if variance is not None and variance < 0:
                variance = None
            bad = stats.get('invalid_samples')
            if isinstance(bad, int) and (not isinstance(bad, bool)) and (bad >= 0):
                invalid += bad
            else:
                invalid_known = False
        else:
            first = last
            if last is None:
                invalid += 1
                continue
            block = {'count': 1, 'avg': last, 'min': last, 'max': last}
            variance = 0.0
        if not moments.merge(block['count'], block['avg'], block['min'], block['max'], variance):
            rejected_rows += 1
            invalid_known = False
            continue
        if at_start is not None and (first_at is None or at_start < first_at):
            first_at, first_ts, first_value = (at_start, start, first)
        if at_end is not None and (last_at is None or at_end >= last_at):
            last_at, last_ts, last_value = (at_end, end, last)
    result = {'sample_count': moments.count, 'valid_samples': moments.count, 'invalid_samples': invalid if invalid_known else None, 'missing_samples': None, 'average_basis': 'samples'}
    if rejected_rows:
        result['invalid_statistic_rows'] = rejected_rows
    if moments.count:
        result.update({'avg': round(moments.mean, 4), 'min': moments.minimum, 'max': moments.maximum})
        if moments.variance is not None:
            result.update({'variance': moments.variance, 'variance_ddof': 0})
        if first_ts is not None:
            result['start'] = first_ts
        if last_ts is not None:
            result['end'] = last_ts
        if first_value is not None and last_value is not None:
            delta = finite_number(last_value - first_value)
            if delta is not None:
                result.update({'first': first_value, 'last': last_value, 'delta': delta, 'trend': '上升' if delta > 0 else '下降' if delta < 0 else '平稳'})
    return result
