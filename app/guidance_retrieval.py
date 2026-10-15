import re
from .llm_schema import compact_json, conservative_token_estimate
DEFAULT_TOKEN_BUDGET = 1400
REASONS = ('explicit_rule', 'slot_answer', 'related_correction', 'general_correction', 'window_related', 'generic_answer', 'other_window_mention', 'acknowledgement')
_REASON_RANK = {reason: rank for rank, reason in enumerate(REASONS)}
_ENTITY_ID = re.compile('[a-z_]+\\.[a-z0-9_]+')
_ANSWER_MARKER = '房主回答'
_CORRECTION_WORDS = ('不是', '不要', '不能', '不代表', '误报', '纠正')
_ACK_TOKENS = ('知道了', '了解', '明白', '好的', '收到', '是的', '对的', '没问题', '谢谢', '可以', 'ok', 'OK', '嗯', '好', '行', '对')
_ACK_PUNCTUATION = re.compile('[\\s。，、,.！!？?；;：:～~…\\-—]+')

def _labels(value):
    if isinstance(value, str) and len(value) > 1 and (not _ENTITY_ID.fullmatch(value)):
        return {value}
    return set()

def _payload_topics(payload):
    rooms, devices, entities = (set(), set(), set())
    if not isinstance(payload, dict):
        return (set(), set())

    def take(entry):
        if not isinstance(entry, dict):
            return
        rooms.update(_labels(entry.get('room')) | _labels(entry.get('area')))
        devices.update(_labels(entry.get('device')) | _labels(entry.get('name')))
        for key in ('entity_id', 'entity'):
            value = entry.get(key)
            if isinstance(value, str) and value:
                entities.add(value)
    for key in ('event_groups', 'critical_events', 'sessions'):
        for entry in payload.get(key) or []:
            take(entry)
    rollup = payload.get('occupancy_rollup')
    for entry in (rollup.get('rooms') if isinstance(rollup, dict) else None) or []:
        take(entry)
    facts = payload.get('window_facts')
    for slot in (facts.get('slots') if isinstance(facts, dict) else None) or []:
        for action in (slot.get('actions') if isinstance(slot, dict) else None) or []:
            take(action)
    catalog = {}
    for entry in payload.get('entity_identity_catalog') or []:
        if isinstance(entry, dict) and entry.get('entity_id'):
            catalog[str(entry['entity_id'])] = entry
    known = set()
    for entry in catalog.values():
        known |= _labels(entry.get('device')) | _labels(entry.get('name')) | _labels(entry.get('area'))
    for entity_id in entities:
        entry = catalog.get(entity_id)
        if entry is not None:
            devices |= _labels(entry.get('device')) | _labels(entry.get('name'))
            rooms |= _labels(entry.get('area'))
    active = rooms | devices
    return (active, known - active)

def _is_acknowledgement(answer):
    body = _ACK_PUNCTUATION.sub('', str(answer or ''))
    if not body or len(body) > 12:
        return False
    while body:
        for token in _ACK_TOKENS:
            if body.startswith(token):
                body = body[len(token):]
                break
        else:
            return False
    return True

def _reason(item, active, other):
    content = str(item.get('content') or '')
    if item.get('kind') != 'qa_history':
        return 'explicit_rule'
    answer = content.partition(_ANSWER_MARKER)[2]
    if _is_acknowledgement(answer):
        return 'acknowledgement'
    if item.get('id') is None:
        return 'slot_answer'
    related = any((label in content for label in active))
    unrelated = any((label in content for label in other))
    if any((word in answer for word in _CORRECTION_WORDS)):
        if related:
            return 'related_correction'
        if unrelated:
            return 'other_window_mention'
        return 'general_correction'
    if related:
        return 'window_related'
    if unrelated:
        return 'other_window_mention'
    return 'generic_answer'

def _resolved_budget(token_budget):
    try:
        value = int(token_budget)
    except (TypeError, ValueError, OverflowError):
        return 0
    return value if value > 0 else 0

def select_guidance(items, payload, token_budget=DEFAULT_TOKEN_BUDGET):
    entries = items if isinstance(items, list) else []
    active, other = _payload_topics(payload)
    budget = _resolved_budget(token_budget)
    ranked = []
    for index, item in enumerate(entries):
        if not isinstance(item, dict):
            continue
        identifier = item.get('id')
        if not isinstance(identifier, int) or isinstance(identifier, bool):
            identifier = -1
        reason = _reason(item, active, other)
        ranked.append((_REASON_RANK[reason], -identifier, index, item, reason))
    ranked.sort(key=lambda row: row[:3])
    chosen, selected_ids, reasons, seen = ([], [], [], set())
    for _, _, _, item, reason in ranked:
        if budget <= 0:
            break
        key = str(item.get('content') or '').strip()
        if key in seen:
            continue
        try:
            cost = conservative_token_estimate(compact_json(chosen + [item]))
        except (TypeError, ValueError, OverflowError):
            continue
        if cost > budget:
            continue
        seen.add(key)
        chosen.append(item)
        selected_ids.append(item.get('id'))
        reasons.append(reason)
    used = conservative_token_estimate(compact_json(chosen)) if chosen else 0
    diagnostics = {'available': len(entries), 'selected': len(chosen), 'omitted': len(entries) - len(chosen), 'used_tokens': used, 'selected_ids': selected_ids, 'reasons': reasons}
    return (chosen, diagnostics)
