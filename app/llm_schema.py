import json
import math
import re
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
REPORT_LIST_LIMIT = 8
MODEL_CONTEXT_TOKENS = 12288
MODEL_OUTPUT_RESERVE_TOKENS = 3072
MODEL_PROMPT_RESERVE_TOKENS = 1000
DISTILL_CONTEXT_TOKENS = 16384
REMOTE_PAYLOAD_TOKEN_BUDGET = 32768

def payload_token_budget(context_tokens=None, output_reserve_tokens=None, prompt_reserve_tokens=None):
    context = int(context_tokens or MODEL_CONTEXT_TOKENS)
    output = int(output_reserve_tokens or MODEL_OUTPUT_RESERVE_TOKENS)
    prompt = int(prompt_reserve_tokens or MODEL_PROMPT_RESERVE_TOKENS)
    return context - output - prompt
MODEL_PAYLOAD_TOKEN_BUDGET = payload_token_budget()
MODEL_PAYLOAD_LIMIT = MODEL_PAYLOAD_TOKEN_BUDGET * 2
MODEL_REQUEST_TIMEOUT_SECONDS = 900
MODEL_CALL_WALL_SECONDS = 1800
MODEL_ATTEMPTS = 2
SEGMENT_CALL_WALL_SECONDS = 180
SEGMENT_NUM_PREDICT = 1024
SEGMENT_ATTEMPTS = 1
SEGMENT_PROMPT_TOKENS_PER_SECOND = 100
SEGMENT_GEN_TOKENS_PER_SECOND = 4.5
SEGMENT_CALL_OVERHEAD_SECONDS = 30
SEGMENT_CALL_FLOOR_SECONDS = 120
SEGMENT_CALL_CEILING_SECONDS = 600

def segment_call_wall_seconds(prompt_tokens, num_predict=None):
    try:
        output = SEGMENT_NUM_PREDICT if num_predict is None else int(num_predict)
        prompt = int(prompt_tokens or 0)
    except (TypeError, ValueError):
        output, prompt = (SEGMENT_NUM_PREDICT, 0)
    work = SEGMENT_CALL_OVERHEAD_SECONDS + max(0, prompt) / SEGMENT_PROMPT_TOKENS_PER_SECOND + max(0, output) / SEGMENT_GEN_TOKENS_PER_SECOND
    return int(min(SEGMENT_CALL_CEILING_SECONDS, max(SEGMENT_CALL_FLOOR_SECONDS, work)))

class RetryableModelFormatError(ValueError):

    def __init__(self, message, code='invalid_json_shape'):
        super().__init__(message)
        self.code = code
SEGMENT_LIST_LIMIT = 10
SEGMENT_SUMMARY_KEYS = ('occupancy', 'movement', 'device_activity', 'automations', 'manual_actions', 'anomalies', 'uncertainties')

def validate_segment_summary(value):
    try:
        if not isinstance(value, dict):
            raise ValueError('segment summary must be an object')
        summary = value.get('summary')
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError('segment summary text missing')
        normalized = {'summary': summary.strip()[:2000]}
        for key in SEGMENT_SUMMARY_KEYS:
            items = value.get(key, [])
            if items is None:
                items = []
            elif not isinstance(items, list):
                items = [items]
            accepted = []
            for item in items[:SEGMENT_LIST_LIMIT]:
                try:
                    accepted.append(_validate_generic(item))
                except (TypeError, ValueError, OverflowError):
                    continue
            normalized[key] = accepted
        return normalized
    except RetryableModelFormatError:
        raise
    except (TypeError, ValueError, OverflowError) as error:
        raise RetryableModelFormatError('segment summary failed schema validation', code='segment_schema') from error

class CandidateResponse(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    type: str = Field(min_length=1, max_length=64, pattern='^[A-Za-z0-9_-]+$')
    description: str = Field(min_length=1, max_length=500)
    reason: str = Field(max_length=500)
    evidence_ids: list[str] = Field(default_factory=list, max_length=8)
    finding_id: str | None = Field(default=None, min_length=24, max_length=24, pattern='^[0-9a-f]{24}$')

    @field_validator('evidence_ids')
    @classmethod
    def valid_evidence_ids(cls, values):
        if len(values) != len(set(values)):
            raise ValueError('duplicate evidence_ids')
        if any((len(value) != 24 or any((ch not in '0123456789abcdef' for ch in value)) for value in values)):
            raise ValueError('invalid evidence_id')
        return values

class ReportResponse(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    summary: str = Field(max_length=4000)
    supports: list[object] = Field(max_length=REPORT_LIST_LIMIT)
    new_candidates: list[CandidateResponse] = Field(max_length=REPORT_LIST_LIMIT)
    anomalies: list[object] = Field(max_length=REPORT_LIST_LIMIT)
    automation_corrections: list[object] = Field(max_length=REPORT_LIST_LIMIT)
    noteworthy: list[object] = Field(max_length=REPORT_LIST_LIMIT)

class QuestionItem(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    question: str = Field(min_length=1, max_length=300)
    why: str = Field(default='', max_length=300)
    options: list[str] = Field(default_factory=list, max_length=5)

class QuestionResponse(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    questions: list[QuestionItem] = Field(default_factory=list, max_length=3)

def report_response_schema():
    return ReportResponse.model_json_schema()

def question_response_schema():
    return QuestionResponse.model_json_schema()

def segment_response_schema():
    properties = {'summary': {'type': 'string', 'minLength': 1, 'maxLength': 2000}}
    for key in SEGMENT_SUMMARY_KEYS:
        properties[key] = {'type': 'array', 'maxItems': SEGMENT_LIST_LIMIT, 'items': {}}
    return {'type': 'object', 'properties': properties, 'required': ['summary'], 'additionalProperties': False}

def validate_question_response(value):
    try:
        parsed = QuestionResponse.model_validate(value)
    except (ValidationError, TypeError, ValueError, OverflowError) as error:
        raise RetryableModelFormatError('model returned invalid question JSON') from error
    return [item.model_dump() for item in parsed.questions]

def compact_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)

def conservative_token_estimate(text):
    ascii_chars = sum((ord(ch) < 128 for ch in text))
    return len(text) - ascii_chars + math.ceil(ascii_chars / 2)

def ollama_input_budget(context_tokens, output_tokens, prompt, *, prompt_reserve_tokens=0, retry_instruction=''):
    reserve = max(prompt_reserve_tokens, conservative_token_estimate(prompt + retry_instruction) + 128)
    return context_tokens - output_tokens - reserve

def ollama_request_plan(prompt, serialized_payload, context_tokens, output_tokens):
    estimate = conservative_token_estimate(serialized_payload)
    budget = ollama_input_budget(context_tokens, output_tokens, prompt)
    return {'context_tokens': context_tokens, 'output_limit': output_tokens, 'prompt_tokens_estimate': conservative_token_estimate(prompt), 'input_tokens_estimate': estimate, 'input_token_budget': budget, 'budget_exceeded': estimate > budget}

def _validate_generic(value, depth=0):
    if depth > 3:
        raise ValueError('response nesting too deep')
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        if len(value) > 1000:
            raise ValueError('response string too long')
        return value
    if isinstance(value, int):
        if abs(value) > 1000000:
            raise ValueError('response integer out of range')
        return value
    if isinstance(value, float):
        if not math.isfinite(value) or abs(value) > 1000000:
            raise ValueError('response number out of range')
        return value
    if isinstance(value, list):
        if len(value) > 8:
            raise ValueError('nested response list too long')
        return [_validate_generic(item, depth + 1) for item in value]
    if isinstance(value, dict):
        if len(value) > 16 or any((not isinstance(key, str) or len(key) > 64 for key in value)):
            raise ValueError('invalid response object')
        return {key: _validate_generic(item, depth + 1) for key, item in value.items()}
    raise ValueError('unsupported response value')

def validate_report_response(value):
    try:
        if isinstance(value, dict) and set(value) == {'type', 'content'} and (value.get('type') == 'home_assistant'):
            blocks = value.get('content')
            if not isinstance(blocks, list) or len(blocks) > 20:
                raise ValueError('invalid legacy response envelope')
            texts = []
            for block in blocks:
                if not isinstance(block, dict) or set(block) != {'type', 'text'} or block.get('type') != 'text':
                    raise ValueError('invalid legacy response block')
                text = block.get('text')
                if not isinstance(text, str) or len(text) > 4000:
                    raise ValueError('invalid legacy response text')
                texts.append(text)
            value = {'summary': '\n\n'.join(texts)[:4000], 'supports': [], 'new_candidates': [], 'anomalies': [], 'automation_corrections': [], 'noteworthy': []}
        elif isinstance(value, dict):
            summary = value.get('summary')
            if not isinstance(summary, str):
                raise ValueError('summary must be a string')
            normalized = {'summary': summary[:4000]}
            for key in ('supports', 'anomalies', 'automation_corrections', 'noteworthy'):
                items = value.get(key, [])
                if items is None:
                    items = []
                elif not isinstance(items, list):
                    items = [items]
                accepted = []
                for item in items[:REPORT_LIST_LIMIT]:
                    try:
                        accepted.append(_validate_generic(item))
                    except (TypeError, ValueError, OverflowError):
                        continue
                normalized[key] = accepted
            candidates = value.get('new_candidates', [])
            if candidates is None:
                candidates = []
            elif not isinstance(candidates, list):
                candidates = [candidates]
            accepted_candidates = []
            for candidate in candidates[:REPORT_LIST_LIMIT]:
                if not isinstance(candidate, dict):
                    continue
                candidate = {key: candidate[key] for key in ('type', 'description', 'reason', 'evidence_ids', 'finding_id') if key in candidate}
                candidate.setdefault('reason', '')
                try:
                    accepted_candidates.append(CandidateResponse.model_validate(candidate).model_dump(exclude_none=True))
                except (ValidationError, TypeError, ValueError, OverflowError):
                    continue
            normalized['new_candidates'] = accepted_candidates
            value = normalized
        report = ReportResponse.model_validate(value)
        result = report.model_dump(exclude_none=True)
        for key in ('supports', 'anomalies', 'automation_corrections', 'noteworthy'):
            result[key] = [_validate_generic(item) for item in result[key]]
        return result
    except (ValidationError, TypeError, ValueError, OverflowError) as error:
        raise RetryableModelFormatError('Ollama response failed schema validation', code='schema_validation') from error

def _response_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _response_strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _response_strings(item)

def normalize_identity_claims(result, payload):
    if not isinstance(result, dict) or not isinstance(payload, dict):
        return (result, 0)
    findings = {str(item['finding_id']): item for item in payload.get('cooccurrence_findings', []) if isinstance(item, dict) and item.get('finding_id')}

    def payload_items(value):
        if isinstance(value, dict):
            yield value
            for entry in value.values():
                yield from payload_items(entry)
        elif isinstance(value, list):
            for entry in value:
                yield from payload_items(entry)
    has_area_request = any((item.get('event_type') == 'vacuum_area_clean_requested' for item in payload_items(payload)))
    room_terms = ('主卧', '次卧', '客厅', '书房', '厨房', '主卫', '次卫', '客卫', '衣帽间', '阳台', '卫生间', '卧室')
    uncertainty = ('房间未知', '轨迹未知', '没有具体清扫轨迹', '没有明确的清扫轨迹', '无具体清扫轨迹', '无明确清扫轨迹', '无法判断', '无法确认')
    path_verbs = re.compile('(?:覆盖|经过|进入|清扫了|清扫过)')
    room_clean = re.compile('在.{0,40}(?:进行|完成).{0,8}(?:清扫|打扫)')
    changed = 0

    def false_vacuum_path(clause):
        return not has_area_request and '扫地机器人' in clause and any((room in clause for room in room_terms)) and (not any((term in clause for term in uncertainty))) and (path_verbs.search(clause) or room_clean.search(clause))

    def rewrite_text(text):
        nonlocal changed
        pieces = re.split('([。！？!?；;\\n])', text)
        rewritten = []
        for index in range(0, len(pieces), 2):
            clause = pieces[index]
            punctuation = pieces[index + 1] if index + 1 < len(pieces) else ''
            if false_vacuum_path(clause):
                clause = '扫地机器人本时段有运行记录；由于没有区域清扫请求记录，本次清扫房间未知'
                punctuation = punctuation or '。'
                changed += 1
            rewritten.extend((clause, punctuation))
        return ''.join(rewritten)

    def rewrite(value):
        nonlocal changed
        if isinstance(value, str):
            return rewrite_text(value)
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        if not isinstance(value, dict):
            return value
        normalized = {key: rewrite(item) for key, item in value.items()}
        finding = findings.get(str(normalized.get('finding_id') or ''))
        if finding is not None and isinstance(normalized.get('description'), str):
            description = f'{finding.get('actor_label')}与{finding.get('sensor_label')}在历史数据中呈统计共现（相关非因果，仅供观察）。'
            if normalized['description'] != description:
                normalized['description'] = description
                changed += 1
        return normalized
    return (rewrite(result), changed)

def validate_identity_references(result, payload):
    forbidden_by_type = {'water_purifier': ('扫地机器人', '扫地机', 'robot vacuum', 'robotic vacuum'), 'vacuum': ('净水器', 'water purifier')}
    authoritative_by_type = {'water_purifier': ('净水器', 'water purifier'), 'vacuum': ('扫地机器人', '扫地机', 'robot vacuum', 'robotic vacuum')}
    strings = list(_response_strings(result))
    findings = {}
    for item in payload.get('cooccurrence_findings', []):
        if isinstance(item, dict) and item.get('finding_id'):
            findings[str(item['finding_id'])] = item

    def validate_finding_objects(value):
        if isinstance(value, list):
            for entry in value:
                validate_finding_objects(entry)
            return
        if not isinstance(value, dict):
            return
        finding_id = value.get('finding_id')
        if finding_id is not None:
            finding = findings.get(str(finding_id))
            if finding is None:
                raise RetryableModelFormatError('Ollama response cites an unknown co-occurrence finding', code='cooccurrence_identity')
            prose = ' '.join(_response_strings({key: item for key, item in value.items() if key != 'finding_id'}))
            required = [str(finding.get(key) or '').strip() for key in ('actor_label', 'sensor_label')]
            if any((label and label not in prose for label in required)):
                raise RetryableModelFormatError('Ollama response changed a co-occurrence device or room identity', code='cooccurrence_identity')
        for entry in value.values():
            validate_finding_objects(entry)
    validate_finding_objects(result)

    def payload_items(value):
        if isinstance(value, dict):
            yield value
            for entry in value.values():
                yield from payload_items(entry)
        elif isinstance(value, list):
            for entry in value:
                yield from payload_items(entry)
    has_area_request = any((item.get('event_type') == 'vacuum_area_clean_requested' for item in payload_items(payload)))
    if not has_area_request:
        room_terms = ('主卧', '次卧', '客厅', '书房', '厨房', '主卫', '次卫', '客卫', '衣帽间', '阳台', '卫生间', '卧室')
        uncertainty = ('房间未知', '轨迹未知', '没有具体清扫轨迹', '没有明确的清扫轨迹', '无具体清扫轨迹', '无明确清扫轨迹', '无法判断', '无法确认')
        path_verbs = re.compile('(?:覆盖|经过|进入|清扫了|清扫过)')
        room_clean = re.compile('在.{0,40}(?:进行|完成).{0,8}(?:清扫|打扫)')
        for text in strings:
            for clause in re.split('[。！？!?；;\\n]', text):
                if '扫地机器人' not in clause or not any((room in clause for room in room_terms)):
                    continue
                if any((term in clause for term in uncertainty)):
                    continue
                if path_verbs.search(clause) or room_clean.search(clause):
                    raise RetryableModelFormatError('Ollama response invented a vacuum room trajectory', code='vacuum_trajectory')
    identity_pairs = []
    for identity in payload.get('entity_identity_catalog', []):
        if isinstance(identity, dict):
            identity_pairs.append((str(identity.get('device') or '').strip(), str(identity.get('area') or '').strip()))
    for finding in findings.values():
        identity_pairs.extend([(str(finding.get('actor_device') or '').strip(), str(finding.get('actor_area') or '').strip()), (str(finding.get('sensor_device') or '').strip(), str(finding.get('sensor_area') or '').strip())])
    areas = {area for _, area in identity_pairs if area}
    for device, authoritative_area in identity_pairs:
        if len(device) < 2 or not authoritative_area or (not any(('一' <= ch <= '鿿' for ch in device))):
            continue
        for other_area in areas - {authoritative_area}:
            wrong = re.compile(re.escape(other_area) + '(?:的)?' + re.escape(device))
            if any((wrong.search(text) for text in strings)):
                raise RetryableModelFormatError('Ollama response conflicts with authoritative room identity', code='room_identity')
    for identity in payload.get('entity_identity_catalog', []):
        if not isinstance(identity, dict):
            continue
        appliance_type = str(identity.get('appliance_type') or '')
        forbidden = forbidden_by_type.get(appliance_type)
        if not forbidden:
            continue
        entity_id = str(identity.get('entity_id') or '').lower()
        device = str(identity.get('device') or '').lower()
        aliases = {entity_id, entity_id.split('.', 1)[-1], device}
        match = re.search('xiaomi_cn_(\\d+)_([a-z0-9]+)', entity_id)
        if match:
            aliases.update(match.groups())
        aliases = {alias for alias in aliases if len(alias) >= 4}
        for text in strings:
            lowered = text.lower()
            for alias in aliases:
                start = lowered.find(alias)
                while start >= 0:
                    left = max((lowered.rfind(mark, 0, start) for mark in '，,；;。！？!?\n'))
                    right_candidates = [lowered.find(mark, start + len(alias)) for mark in '，,；;。！？!?\n']
                    right_candidates = [position for position in right_candidates if position >= 0]
                    right = min(right_candidates) if right_candidates else len(lowered)
                    nearby = lowered[left + 1:right]
                    correct = authoritative_by_type.get(appliance_type, ())
                    forbidden_present = any((term in nearby for term in forbidden))
                    explicit_negation = any((f'不是{term}' in nearby or f'not a {term}' in nearby or f'not {term}' in nearby for term in forbidden))
                    if forbidden_present and (not (explicit_negation and any((term in nearby for term in correct)))):
                        raise RetryableModelFormatError('Ollama response conflicts with authoritative device identity', code='device_identity')
                    start = lowered.find(alias, start + len(alias))
    return result

def filter_automation_run_claims(result, payload):
    runs = payload.get('automation_runs') or []
    aliases = set()
    for run in runs:
        if not isinstance(run, dict):
            continue
        for key in ('entity', 'name'):
            alias = str(run.get(key) or '').strip().casefold()
            if alias:
                aliases.add(alias)
    if not aliases or not isinstance(result, dict):
        return (result, 0)

    def mentions_run(value):
        return any((alias in text.casefold() for text in _response_strings(value) for alias in aliases))
    filtered = dict(result)
    removed = 0
    for key in ('supports', 'new_candidates', 'anomalies', 'automation_corrections', 'noteworthy'):
        items = filtered.get(key)
        if not isinstance(items, list):
            continue
        kept = [item for item in items if not mentions_run(item)]
        removed += len(items) - len(kept)
        filtered[key] = kept
    summary = filtered.get('summary')
    if isinstance(summary, str) and mentions_run(summary):
        parts = [part for part in re.split('(?<=[。！？!?；;\\n])', summary) if part.strip()]
        kept = [part for part in parts if not mentions_run(part)]
        removed += len(parts) - len(kept)
        filtered['summary'] = ''.join(kept).strip() or '本时段报告已过滤系统自动化运行记录；没有足够的其他证据可作行为判断。'
    return (filtered, removed)

def normalize_device_availability_claims(result, payload):
    if not isinstance(result, dict):
        return (result, 0)
    snapshot = payload.get('home_state_snapshot') if isinstance(payload, dict) else None
    entities = snapshot.get('entities') if isinstance(snapshot, dict) else None
    vacuum_present = any((isinstance(item, dict) and (item.get('appliance_type') == 'vacuum' or str(item.get('entity_id') or '').startswith('vacuum.') or 'robotic_vacuum_cleaner' in str(item.get('entity_id') or '')) for item in entities or []))
    if not vacuum_present:
        return (result, 0)
    replacements = (('无扫地机器人运行状态记录', '本时段没有记录到扫地机器人开始或结束清扫，报告生成时已取得其当前状态'), ('没有扫地机器人运行状态记录', '本时段没有记录到扫地机器人开始或结束清扫，报告生成时已取得其当前状态'), ('输入中无扫地机器人相关实体记录', '本时段没有扫地机器人状态变化，报告生成时已取得其当前状态'))
    changed = 0

    def rewrite(value):
        nonlocal changed
        if isinstance(value, str):
            for old, new in replacements:
                occurrences = value.count(old)
                if occurrences:
                    value = value.replace(old, new)
                    changed += occurrences
            return value
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        if isinstance(value, dict):
            return {key: rewrite(item) for key, item in value.items()}
        return value
    return (rewrite(result), changed)
