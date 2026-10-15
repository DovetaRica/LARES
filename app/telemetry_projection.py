import json
from datetime import datetime, timezone
from .telemetry_stats import finite_number as _finite, timestamp
STABLE_ENTITIES_KEY = '_stable_telemetry_entities'
PROTECTED_ENTITIES_KEY = '_protected_telemetry_entities'
VERSION = 'telemetry-projection-v1'
NOTE = '省略无变化或已汇总的重复遥测，非正常判定；采样缺失未知，原记录保留。'
_COLLECTIONS = ('metric_summaries', 'recent_detail', 'representative_events', 'compressed_events')

def _is_zero(value):
    return isinstance(value, int) and (not isinstance(value, bool)) and (value == 0)

def _is_stable_telemetry(item):
    if item.get('kind') != 'telemetry' or item.get('class') != 'sensor_telemetry':
        return False
    count = item.get('sample_count')
    if isinstance(count, bool) or not isinstance(count, int) or count < 2:
        return False
    if isinstance(item.get('valid_samples'), bool) or item.get('valid_samples') != count:
        return False
    if not _is_zero(item.get('invalid_samples')):
        return False
    if 'invalid_statistic_rows' in item and (not _is_zero(item.get('invalid_statistic_rows'))):
        return False
    if item.get('missing_samples') is not None and (not _is_zero(item.get('missing_samples'))):
        return False
    numbers = {key: _finite(item.get(key)) for key in ('min', 'max', 'avg', 'first', 'last')}
    if any((value is None for value in numbers.values())) or len(set(numbers.values())) != 1:
        return False
    if _finite(item.get('delta')) != 0:
        return False
    if 'variance' in item and (_finite(item.get('variance')) != 0 or not _is_zero(item.get('variance_ddof'))):
        return False
    return True

def stable_telemetry_entities(compressed, protected=(), context=None, identities=None):
    protected = {str(entity) for entity in protected if entity}
    entities, blocked = (set(), set())
    for item in compressed or ():
        if not isinstance(item, dict):
            continue
        entity = item.get('entity')
        if not _is_stable_telemetry(item):
            if entity:
                blocked.add(str(entity))
            continue
        if entity and str(entity) not in protected:
            entities.add(str(entity))
    entities -= blocked
    if context:
        entities -= _owner_mentioned(_catalog_labels(identities or context.get('entity_identity_catalog')), entities, _owner_texts(context))
    return entities

def protected_telemetry_entities(events, critical=()):
    manual = {str(event['entity_id']) for event in events or () if event.get('source') == 'manual' and event.get('entity_id')}
    critical_ids = {str(item['entity_id']) for item in critical or () if item.get('entity_id')}
    semantics = {}
    for event in events or ():
        attrs = event.get('attributes_json')
        if isinstance(attrs, str):
            try:
                attrs = json.loads(attrs)
            except ValueError:
                attrs = {}
        unit = (attrs or {}).get('unit_of_measurement') if isinstance(attrs, dict) else None
        semantics.setdefault(event.get('entity_id'), set()).add((event.get('category'), str(unit or '')))
    return manual | critical_ids | {str(entity) for entity, variants in semantics.items() if entity and len(variants) > 1}

def _item_entity(item):
    if not isinstance(item, dict):
        return ''
    return str(item.get('entity_id') or item.get('entity') or '')

def _catalog_labels(catalog):
    if isinstance(catalog, dict):
        entries = [{'entity_id': entity, **label} for entity, label in catalog.items() if isinstance(label, dict)]
    elif isinstance(catalog, list):
        entries = [entry for entry in catalog if isinstance(entry, dict)]
    else:
        entries = []
    labels = {}
    for entry in entries:
        entity = str(entry.get('entity_id') or '')
        if not entity:
            continue
        aliases = {entity}
        for key in ('device', 'name'):
            value = str(entry.get(key) or '').strip()
            if len(value) >= 2:
                aliases.add(value)
        labels[entity] = aliases
    return labels

def _owner_mentioned(labels, candidates, texts):
    content = '\n'.join(texts)
    if not content:
        return set()
    return {entity for entity in candidates if any((alias in content for alias in labels.get(entity, {entity})))}

def _owner_texts(payload):
    return [str(item.get('content') or '') for collection in ('user_guidance', 'long_term_memories') for item in payload.get(collection) or () if isinstance(item, dict)]
_OLDEST = datetime.min.replace(tzinfo=timezone.utc)

def _positive_count(value):
    number = _finite(value)
    return number is not None and number > 0

def _select_tier(item, protected_ids, mentioned):
    entity = _item_entity(item)
    if entity and (entity in protected_ids or entity in mentioned):
        return 0
    if any((_positive_count(item.get(key)) for key in ('invalid_samples', 'invalid_statistic_rows', 'missing_samples'))):
        return 1
    low, high = (_finite(item.get('min')), _finite(item.get('max')))
    if low is not None and high is not None and (low != high):
        return 2
    delta, variance = (_finite(item.get('delta')), _finite(item.get('variance')))
    if delta is not None and delta != 0 or (variance is not None and variance != 0):
        return 2
    return 3

def _mentioned_metric_entities(rows, context):
    if not context:
        return set()
    labels = _catalog_labels(context.get('entity_identity_catalog'))
    for entity, aliases in _catalog_labels(rows).items():
        labels.setdefault(entity, set()).update(aliases)
    candidates = {_item_entity(item) for item in rows if _item_entity(item)}
    return _owner_mentioned(labels, candidates, _owner_texts(context))

def select_metric_summaries(summaries, limit, protected=(), context=None):
    rows = [item for item in summaries or () if isinstance(item, dict)]
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError('invalid metric summary limit')
    if limit == 0 or not rows:
        return []
    protected_ids = {str(entity) for entity in protected or () if entity}
    mentioned = _mentioned_metric_entities(rows, context)
    grouped = {}
    for item in rows:
        tier = _select_tier(item, protected_ids, mentioned)
        grouped.setdefault(tier, []).append((timestamp(item.get('period_end')), item))
    selected = []
    for tier in sorted(grouped):
        entries = grouped[tier]
        entries.sort(key=lambda entry: (entry[0] is not None, entry[0] or _OLDEST), reverse=True)
        for _at, item in entries:
            selected.append(item)
            if len(selected) == limit:
                return selected
    return selected

def _covered_telemetry(item, metrics):
    if not isinstance(item, dict) or item.get('kind') != 'telemetry':
        return False
    metric = metrics.get(_item_entity(item))
    if not metric or not _is_zero(item.get('invalid_samples')):
        return False
    count, full_count = (item.get('sample_count'), metric.get('sample_count'))
    if any((isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in (count, full_count))):
        return False
    if isinstance(item.get('valid_samples'), bool) or item.get('valid_samples') != count or count > full_count:
        return False
    if 'invalid_statistic_rows' in item and (not _is_zero(item['invalid_statistic_rows'])) or (item.get('missing_samples') is not None and (not _is_zero(item['missing_samples']))):
        return False
    if str(item.get('unit') or '') != str(metric.get('unit') or ''):
        return False
    for key in ('avg', 'min', 'max', 'first', 'last', 'delta'):
        value, full = (_finite(item.get(key)), _finite(metric.get(key)))
        if value is None or full is None or value != full:
            return False
    if 'variance' in item or 'variance' in metric:
        if _finite(item.get('variance')) is None or _finite(item.get('variance')) != _finite(metric.get('variance')) or (not _is_zero(item.get('variance_ddof'))) or (not _is_zero(metric.get('variance_ddof'))):
            return False
    bounds = [timestamp(value) for value in (metric.get('period_start'), item.get('start'), item.get('end'), metric.get('period_end'))]
    if any((value is None or value.tzinfo is None for value in bounds)):
        return False
    values = [_finite(value) for value in (metric.get('min'), item.get('min'), item.get('max'), metric.get('max'))]
    return all((value is not None for value in values)) and bounds[0] <= bounds[1] <= bounds[2] <= bounds[3] and (values[0] <= values[1] <= values[2] <= values[3])

def project_telemetry(payload, candidates, protected=()):
    diagnostics = {'version': VERSION, 'omitted_entities': 0, 'omitted_items': 0, 'duplicate_items': 0, 'compacted_metric_items': 0}
    if not isinstance(payload, dict):
        return diagnostics
    candidate_ids = {str(entity) for entity in candidates or () if entity}
    labels = _catalog_labels(payload.get('entity_identity_catalog'))
    for entity, aliases in _catalog_labels(payload.get('metric_summaries')).items():
        labels.setdefault(entity, set()).update(aliases)
    for entity in candidate_ids:
        labels.setdefault(entity, {entity})
    critical = {_item_entity(item) for item in payload.get('critical_events') or ()} | set(protected or ())
    mentioned = _owner_mentioned(labels, candidate_ids, _owner_texts(payload))
    omitted = candidate_ids - critical - mentioned
    omitted_items = 0
    for key in _COLLECTIONS:
        values = payload.get(key)
        if not isinstance(values, list):
            continue
        kept = [item for item in values if _item_entity(item) not in omitted]
        omitted_items += len(values) - len(kept)
        payload[key] = kept
    for key in ('start_state', 'end_state'):
        block = payload.get(key)
        if isinstance(block, dict):
            for entity in omitted:
                block.pop(entity, None)
    diagnostics.update(omitted_entities=len(omitted), omitted_items=omitted_items)
    metric_rows = payload.get('metric_summaries') or ()
    mentioned_metrics = _owner_mentioned(labels, {_item_entity(item) for item in metric_rows}, _owner_texts(payload))
    metrics = {_item_entity(item): item for item in metric_rows if isinstance(item, dict) and item.get('summary_basis') == 'whole_window_samples' and _is_zero(item.get('invalid_samples')) and (_item_entity(item) not in critical | mentioned_metrics) and ('invalid_statistic_rows' not in item or _is_zero(item['invalid_statistic_rows'])) and (item.get('missing_samples') is None or _is_zero(item['missing_samples']))}
    values = payload.get('recent_detail')
    if metrics and isinstance(values, list):
        kept = [item for item in values if not _covered_telemetry(item, metrics)]
        diagnostics['duplicate_items'] = len(values) - len(kept)
        payload['recent_detail'] = kept
    for item in metric_rows:
        if not isinstance(item, dict) or item.get('summary_basis') != 'whole_window_samples':
            continue
        item.pop('summary_basis', None)
        if item.get('valid_samples') == item.get('sample_count'):
            item.pop('valid_samples', None)
        if item.get('average_basis') == 'samples':
            item.pop('average_basis', None)
        item.pop('first', None)
        item.pop('last', None)
        variance = _finite(item.get('variance'))
        if variance is not None and _is_zero(item.get('variance_ddof')):
            item['variance'] = round(variance, 4)
            item.pop('variance_ddof', None)
        diagnostics['compacted_metric_items'] += 1
    if omitted_items or diagnostics['duplicate_items']:
        payload['telemetry_selection'] = dict(diagnostics, note=NOTE)
    if diagnostics['compacted_metric_items']:
        payload['telemetry_statistics'] = 'sample_count为有效采样数；均值按采样加权，方差为总体方差；缺失采样未知，未展示不代表正常。'
    return diagnostics
