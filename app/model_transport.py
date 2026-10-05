import json
import logging
from .llm_schema import MODEL_ATTEMPTS, RetryableModelFormatError, normalize_identity_claims, validate_identity_references, validate_question_response, validate_report_response
logger = logging.getLogger(__name__)
RETRY_INSTRUCTION = "\nPrevious output failed schema or device-identity validation. Return exactly the required JSON structure and use entity_identity_catalog as authoritative. Each new_candidates entry may contain only type, description, reason, evidence_ids and finding_id. Never add sample_count, consistency, counter_examples, evidence or confidence: the program supplies all statistics and rejects any response that restates them. For every cited finding_id, copy its actor_label and sensor_label verbatim; never invent or combine a room and device name. Without a vacuum_area_clean_requested event, never claim that the robot cleaned, covered, entered or passed through any room; say that this cleaning run's rooms are unknown."
_FORMAT_FAILURES = (json.JSONDecodeError, ValueError, TypeError, KeyError, AttributeError, IndexError)

def strip_code_fence(content):
    text = (content or '').strip()
    if text.startswith('```'):
        text = text.removeprefix('```json').removeprefix('```').removesuffix('```').strip()
    return text

def parse_model_json(content):
    text = strip_code_fence(content).strip()
    if not text:
        raise json.JSONDecodeError('empty model response', '', 0)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find('{')
    end = text.rfind('}')
    if start >= 0 and end > start + 1:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    repaired = repair_truncated_json(text)
    if repaired is not None:
        logger.warning('model reply was cut off by the output cap; repaired the JSON by closing it (chars=%s)', len(text))
        return repaired
    raise json.JSONDecodeError('model response is not a JSON object', text, 0)

def repair_truncated_json(text):
    start = text.find('{')
    if start < 0:
        return None
    body = text[start:]
    stack, in_string, escaped = ([], False, False)
    cuts = {}
    last_index = -1
    for index, char in enumerate(body):
        last_index = index
        if in_string:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in '{[':
            stack.append(char)
        elif char in '}]':
            if not stack:
                return None
            stack.pop()
            if not stack:
                return None
            cuts[len(stack)] = index + 1
        elif char == ',':
            cuts[len(stack)] = index
    if not stack:
        return None
    candidate = body
    if in_string:
        candidate = body + '"'
    else:
        tail = body[:last_index + 1].rstrip()
        if tail.endswith(',') or tail.endswith(':'):
            cut = cuts.get(len(stack))
            if cut is None:
                return None
            candidate = body[:cut]
        elif ':' not in body:
            return None
    closers = ''.join(('}' if opened == '{' else ']' for opened in reversed(stack)))
    try:
        parsed = json.loads(candidate + closers)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None

async def validated_model_report(call_model, system_prompt, payload, retry_instruction=RETRY_INSTRUCTION, validator=None, check_identity=True, attempts=None):
    validate = validator or validate_report_response
    budget = int(attempts or MODEL_ATTEMPTS)
    for attempt in range(max(1, budget)):
        from .model_metrics import note_attempt
        note_attempt()
        content, metadata = await call_model(system_prompt + (retry_instruction if attempt else ''))
        note_attempt(metadata)
        try:
            result = validate(parse_model_json(content))
            if check_identity:
                result, normalized = normalize_identity_claims(result, payload)
                if normalized:
                    logger.warning('normalized model identity/path claims count=%s', normalized)
                validate_identity_references(result, payload)
            return (result, metadata)
        except _FORMAT_FAILURES as error:
            note_attempt(rejected=True)
            reason = getattr(error, 'code', 'invalid_json_shape')
            if attempt + 1 < budget:
                logger.warning('model response rejected; retrying once error=%s reason=%s', type(error).__name__, reason)
                continue
            logger.warning('model response rejected after retry error=%s reason=%s', type(error).__name__, reason)
            raise RetryableModelFormatError('model returned invalid report JSON', code=reason) from error
