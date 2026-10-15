import copy
from .llm_schema import RetryableModelFormatError, compact_json, conservative_token_estimate
REVIEW_ISSUE_LIMIT = 4
REVIEW_DETAIL_LIMIT = 180
REVIEW_EXCLUDED_FIELDS = frozenset({'memory_segments', 'reference_report', 'period_summaries', 'daily_summaries', 'cooccurrence_meta', 'evidence_coverage'})
REVIEW_CODES = ('unsupported_claim', 'wrong_window', 'wrong_identity', 'missed_guidance', 'missing_key_fact', 'misleading_count', 'unnecessary_alarm')
REVIEW_PROMPT = '你是报告复核员，只核对草稿，不写新报告，也不控制任何设备。\n任务：把草稿与随附的程序证据、已选定的业主偏好逐条对照，找出可核实的问题。\n硬规则：只能使用输入中已有的数字、时间、身份与事实；不得新增统计、换算、推测或补全缺失；不得给出控制、设备操作、自动化或新建议；证据不足时不得下结论。\n问题码只能用：unsupported_claim（无依据的主张）、wrong_window（时间或窗口错误）、wrong_identity（设备或房间身份错误）、missed_guidance（未遵循业主偏好）、missing_key_fact（遗漏关键事实）、misleading_count（计数或统计误导）、unnecessary_alarm（不必要的告警）。\n每条问题给出 code 与 detail：detail 用简体中文，引用草稿原话并说明对应证据，不超过180字；issues 最多4条。\n未发现问题时输出 accept 且 issues 为空；accept 只表示本次未发现问题，不等于草稿正确。发现问题时输出 revise，issues 至少1条。\n只输出 JSON，无 Markdown：{"decision":"accept|revise","issues":[{"code":"...","detail":"..."}]}'

def review_response_schema():
    return {'type': 'object', 'properties': {'decision': {'type': 'string', 'enum': ['accept', 'revise']}, 'issues': {'type': 'array', 'maxItems': REVIEW_ISSUE_LIMIT, 'items': {'type': 'object', 'properties': {'code': {'type': 'string', 'enum': list(REVIEW_CODES)}, 'detail': {'type': 'string', 'minLength': 1, 'maxLength': REVIEW_DETAIL_LIMIT}}, 'required': ['code', 'detail'], 'additionalProperties': False}}}, 'required': ['decision', 'issues'], 'additionalProperties': False}

def validate_review_response(value):
    try:
        if not isinstance(value, dict) or set(value) - {'decision', 'issues'}:
            raise ValueError('review response must contain only decision and issues')
        decision = value.get('decision')
        issues = value.get('issues')
        if not isinstance(decision, str) or decision not in ('accept', 'revise'):
            raise ValueError('decision must be accept or revise')
        if not isinstance(issues, list) or len(issues) > REVIEW_ISSUE_LIMIT:
            raise ValueError('issues must be a list of at most four items')
        normalized = []
        for issue in issues:
            if not isinstance(issue, dict) or set(issue) != {'code', 'detail'}:
                raise ValueError('each issue needs exactly code and detail')
            code, detail = (issue.get('code'), issue.get('detail'))
            if not isinstance(code, str) or code not in REVIEW_CODES:
                raise ValueError('unknown review issue code')
            if not isinstance(detail, str):
                raise ValueError('detail must be a string')
            detail = detail.strip()
            if not detail or len(detail) > REVIEW_DETAIL_LIMIT:
                raise ValueError('detail must be 1-180 characters')
            normalized.append({'code': code, 'detail': detail})
        if decision == 'accept' and normalized:
            raise ValueError('accept cannot carry issues')
        if decision == 'revise' and (not normalized):
            raise ValueError('revise needs at least one issue')
        return {'decision': decision, 'issues': normalized}
    except RetryableModelFormatError:
        raise
    except (TypeError, ValueError, OverflowError) as error:
        raise RetryableModelFormatError('review response failed schema validation', code='review_schema') from error

def build_review_payload(draft, payload, token_budget):
    if isinstance(token_budget, bool) or not isinstance(token_budget, (int, float)):
        return None
    try:
        budget = int(token_budget)
    except (TypeError, ValueError, OverflowError):
        return None
    if budget <= 0 or not isinstance(draft, dict) or (not isinstance(payload, dict)):
        return None
    evidence = {key: copy.deepcopy(item) for key, item in payload.items() if key not in REVIEW_EXCLUDED_FIELDS and (not str(key).startswith('_'))}
    result = {'draft': copy.deepcopy(draft), 'evidence': evidence}
    if conservative_token_estimate(compact_json(result)) > budget:
        return None
    return result
