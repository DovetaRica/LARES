from copy import deepcopy
from datetime import datetime
import re
from .llm_schema import RetryableModelFormatError, compact_json, conservative_token_estimate, question_response_schema, validate_identity_references, validate_question_response
from .proactive_contract import guidance_mentions_identity
GROUNDING_PROMPT = '\n\n本次以 fact_context 程序证据为准，不从报告文字推导事实。每个问题必须返回一个evidence_id，由程序补充完整设备名称。只询问房主的目的、偏好或人工背景；不要在问题或选项中复述次数、时刻、持续时间或编造已经发生的行为。why由程序生成。记录数不等于使用次数，覆盖未知不等于没有发生。不值得询问时返回空questions。'
_NUMBER = re.compile('\\d|[零一二两三四五六七八九十百]+\\s*(?:次|个|条|回|分钟|小时|秒|点|天|周)')

def _aware(value):
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return result if result.tzinfo is not None else None
    except (ValueError, TypeError):
        return None

def question_context(payload, token_budget=900):
    start, end = (_aware(payload.get('period_start')), _aware(payload.get('period_end')))
    if start is None or end is None or (not 0 < (end - start).total_seconds() <= 14 * 86400):
        return None
    context = {'basis': 'program_event_groups', 'period_start': start.isoformat(), 'period_end': end.isoformat(), 'counts_are_records_not_uses': True, 'collection_completeness': 'unknown', 'evidence': []}
    groups = sorted(payload.get('event_groups') or [], key=lambda item: item.get('source') != 'manual')
    seen = set()
    for group in groups:
        eid, entity = (group.get('evidence_id'), group.get('entity_id'))
        first, last = (_aware(group.get('first_ts')), _aware(group.get('last_ts')))
        count = group.get('count')
        if not eid or not entity or eid in seen or isinstance(count, bool) or (not isinstance(count, int)) or (count < 1) or (first is None) or (last is None) or (not start <= first <= last < end):
            continue
        item = {key: deepcopy(group[key]) for key in ('evidence_id', 'entity_id', 'device', 'name', 'area', 'appliance_type', 'state', 'source', 'count', 'first_ts', 'last_ts') if key in group}
        context['evidence'].append(item)
        if conservative_token_estimate(compact_json(context)) > token_budget:
            context['evidence'].pop()
            continue
        seen.add(eid)
        if len(context['evidence']) == 6:
            break
    return context

def strip_question_context(value):
    if isinstance(value, dict):
        value.pop('fact_context', None)
        for item in value.values():
            strip_question_context(item)
    elif isinstance(value, list):
        for item in value:
            strip_question_context(item)

def grounded_schema(payload):
    schema = question_response_schema()
    context = payload.get('fact_context')
    if context is not None:
        item = schema['$defs']['QuestionItem']
        item['properties']['evidence_id'] = {'type': 'string', 'enum': [row['evidence_id'] for row in context['evidence']]}
        item['required'].append('evidence_id')
        schema['properties']['questions']['maxItems'] = min(3, len(context['evidence']))
    return schema

def validate_grounded_response(value, payload):
    context = payload.get('fact_context')
    if context is None:
        return validate_question_response(value)
    rows = {row['evidence_id']: row for row in context['evidence']}
    if not isinstance(value, dict) or set(value) != {'questions'} or (not isinstance(value['questions'], list)):
        raise RetryableModelFormatError('invalid grounded question shape', code='question_grounding_shape')
    stripped, ids = ([], [])
    for raw in value['questions']:
        if not isinstance(raw, dict) or raw.get('evidence_id') not in rows:
            raise RetryableModelFormatError('unknown question evidence', code='question_evidence')
        ids.append(raw['evidence_id'])
        stripped.append({key: val for key, val in raw.items() if key != 'evidence_id'})
    items = validate_question_response({'questions': stripped})
    for item, eid in zip(items, ids):
        row = rows[eid]
        aliases = [str(row.get(key) or '') for key in ('device', 'name', 'entity_id')]
        aliases = [alias for alias in aliases if len(alias) >= 2]
        if not guidance_mentions_identity(item['question'], row):
            other = [other for other in rows.values() if other['entity_id'] != row['entity_id']]
            wrong_room = any((room in item['question'] and room not in str(row.get('area') or '') for room in ('主卧', '次卧', '客厅', '书房', '厨房', '阳台', '主卫', '次卫', '客卫')))
            if re.search('[a-z_]+\\.[a-z0-9_]+', item['question'], re.I) or wrong_room or any((guidance_mentions_identity(item['question'], entry) for entry in other)):
                raise RetryableModelFormatError('question changed evidence identity', code='question_identity')
            label = row.get('device') or row.get('name') or row['entity_id']
            item['question'] = f'{label}：{item['question']}'
            if len(item['question']) > 300:
                raise RetryableModelFormatError('grounded question too long', code='question_identity')
        text = ' '.join([item['question'], *item['options']])
        for alias in sorted(aliases, key=len, reverse=True):
            text = text.replace(alias, '')
        if _NUMBER.search(text):
            raise RetryableModelFormatError('question contains unsupported numeric premise', code='question_numeric_premise')
        validate_identity_references({'question': item['question'], 'options': item['options']}, {'entity_identity_catalog': list(rows.values())})
        item['evidence_id'] = eid
    return items

def program_question_reasons(items, context):
    rows = {row['evidence_id']: row for row in context['evidence']}
    result = []
    for item in items:
        row = rows[item['evidence_id']]
        label = row.get('device') or row.get('name') or row['entity_id']
        reason = f'程序证据 {row['evidence_id']}：{label}，本报告窗口内同类动作记录{row['count']}条。记录数不等于使用次数；动机和偏好仍需房主确认。'
        result.append({**item, 'why': reason[:300]})
    return result
