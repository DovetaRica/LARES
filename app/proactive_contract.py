from copy import deepcopy
import re
from app.llm_schema import RetryableModelFormatError, compact_json, conservative_token_estimate
SCOUT_MAX_ITEMS = 12
SCOUT_MAX_FINDINGS = 3
ROUTINE_CONTEXT = 'routine_context'
MANUAL_CONTEXT = 'manual_context'
AUTOMATION_CONTEXT = 'automation_context'
SCOUT_FOCUS_VALUES = (ROUTINE_CONTEXT, MANUAL_CONTEXT, AUTOMATION_CONTEXT)

def guidance_mentions_identity(content, group):
    mentions = set(re.findall('[a-z_]+\\.[a-z0-9_]+', content))
    if mentions:
        return group.get('entity_id') in mentions
    return any((re.search(re.escape(label) + '(?![A-Za-z0-9_])', content) for label in (group.get('device'), group.get('name')) if isinstance(label, str) and len(label) > 1))
SCOUT_PROMPT = '你是家庭设备行为观察助手。下面 JSON 里的 evidence 是程序统计出的跨天重复设备动作，字段含义和数值由程序负责，不要改动、复述或推断这些统计。\n任务是为已有证据选择下一步核实方向，而不是判断习惯是否成立。请从 evidence 中挑出最多 3 条尚未解释、最值得确认上下文的跨日重复动作，按有用程度排列。有未解释的合格动作时应选择值得了解的条目，不必先证明它是习惯；全部已解释或无价值时返回空数组。\nuser_guidance 是业主已生效的偏好与回答；已解释、已否定、不希望关注的事情不要再选择，不要重提相同问题。applies_to_evidence_ids 标明该回答的适用证据；对某设备的回答不能推广到其他设备，同类设备也不是同一个设备。设备动作记录不等于使用次数，也不能证明习惯或人的动机。\nroutine_context 表示确认不同日期的重复动作是否具有相同情境；manual_context 表示了解手动操作原因；automation_context 表示了解自动化动作是否符合业主预期。每条只能使用该证据 allowed_focus 中列出的 focus，并且只能使用证据里已有的 evidence_id。\n只输出 findings JSON，每项只有 evidence_id 和 focus；不要新增统计、解释、置信度、猜测的房间或人物，也不要控制设备、创建或修改自动化，或断言当时有人在场。所有线索都必须由主人确认后才算成立。'
FACT_TOOLS = ('entity_identity', 'window_events', 'owner_guidance')
SCOUT_FACT_PROMPT = SCOUT_PROMPT + '\n tool_result是程序只读查询结果，只适用于其中evidence_id和时间窗。业主规则是偏好或回答，不是传感器观测；unknown表示未知，不能证明正常。逐日动作包含该实体全部状态和来源，不能当作原单一状态的使用次数。'
SCOUT_PLAN_PROMPT = SCOUT_PROMPT.replace('只输出 findings JSON，每项只有 evidence_id 和 focus；', '本阶段只输出 query 和 findings 两个字段。信息够用时 query=null，findings 按上述要求选择；信息不足且 query_allowed=true 时，query={tool,evidence_id}，findings=[]；') + '\n最多请求一次本地只读查询：entity_identity核对指定设备身份；window_events查指定设备在程序时间窗内的逐日动作分布和指标统计；owner_guidance查该设备相关的已生效业主规则。只可使用输入evidence_id，不可提供SQL、路径、URL或自行指定时间。已有信息足够时不要重复查询；guidance_coverage.omitted>0表示有业主信息未进入本轮输入，不代表没有相关回答；若omitted>0且所选设备没有适用回答，应先用owner_guidance核实是否已解释，避免重复打扰；逐日计数不能回答业主是否已解释，不要用window_events代替owner_guidance。query_allowed=false时必须query=null。查询只是补证据，不保证设备正常或习惯成立。query_required=true时必须请求owner_guidance且findings=[]，不能用空选择代替查询。'

def scout_plan_schema(allow_query=True, require_query=False, evidence_ids=None):
    query = {'type': 'null'}
    if allow_query:
        query = {'anyOf': [query, {'type': 'object', 'properties': {'tool': {'type': 'string', 'enum': list(FACT_TOOLS)}, 'evidence_id': {'type': 'string'}}, 'required': ['tool', 'evidence_id'], 'additionalProperties': False}]}
    findings = scout_schema(evidence_ids)['properties']['findings']
    if require_query:
        query = {'type': 'object', 'properties': {'tool': {'type': 'string', 'enum': ['owner_guidance']}, 'evidence_id': {'type': 'string'}}, 'required': ['tool', 'evidence_id'], 'additionalProperties': False}
        findings = {'type': 'array', 'maxItems': 0, 'items': findings['items']}
    return {'type': 'object', 'properties': {'query': query, 'findings': findings}, 'required': ['query', 'findings'], 'additionalProperties': False}

def validate_scout_plan(value, payload, allow_query=True):
    if not isinstance(value, dict) or set(value) != {'query', 'findings'}:
        raise RetryableModelFormatError('invalid scout plan', code='scout_query')
    query = value['query']
    if payload.get('query_required') and (not isinstance(query, dict) or query.get('tool') != 'owner_guidance'):
        raise RetryableModelFormatError('owner scope lookup required', code='scout_query')
    if query is None:
        return {'query': None, 'findings': validate_scout_response({'findings': value['findings']}, payload)}
    known = {item.get('evidence_id') for item in payload.get('evidence', [])}
    if not allow_query or not isinstance(query, dict) or set(query) != {'tool', 'evidence_id'} or (not isinstance(query['tool'], str)) or (query['tool'] not in FACT_TOOLS) or (not isinstance(query['evidence_id'], str)) or (query['evidence_id'] not in known) or (value['findings'] != []):
        raise RetryableModelFormatError('invalid scout query', code='scout_query')
    return {'query': dict(query), 'findings': []}

def scout_schema(evidence_ids=None):
    schema = {'type': 'object', 'properties': {'findings': {'type': 'array', 'maxItems': SCOUT_MAX_FINDINGS, 'items': {'type': 'object', 'properties': {'evidence_id': {'type': 'string'}, 'focus': {'type': 'string', 'enum': list(SCOUT_FOCUS_VALUES)}}, 'required': ['evidence_id', 'focus'], 'additionalProperties': False}}}, 'required': ['findings'], 'additionalProperties': False}
    if evidence_ids is not None:
        entries = schema['properties']['findings']
        entries['maxItems'] = min(SCOUT_MAX_FINDINGS, len(evidence_ids))
        entries['items']['properties']['evidence_id']['enum'] = list(evidence_ids)
    return schema

def _allowed_focus(group):
    focus = [ROUTINE_CONTEXT]
    source = str(group.get('source') or '')
    if source == 'manual':
        focus.append(MANUAL_CONTEXT)
    elif source == 'automation':
        focus.append(AUTOMATION_CONTEXT)
    return focus

def _eligible(group, blocked_ids, blocked_keys, seen_ids, min_samples):
    evidence_id = str(group.get('evidence_id') or '')
    if not evidence_id or evidence_id in blocked_ids or evidence_id in seen_ids:
        return False
    if str(group.get('semantic_key') or '') in blocked_keys:
        return False
    try:
        count = int(group.get('count') or 0)
        days = int(group.get('local_days') or 0)
    except (TypeError, ValueError):
        return False
    return count >= min_samples and days >= 2

def _scout_order(groups):
    ordered = sorted(groups, key=lambda group: str(group.get('last_ts') or ''), reverse=True)
    ordered.sort(key=lambda group: str(group.get('source') or '') != 'manual')
    head, seen, tail = ([], set(), [])
    for group in ordered:
        entity_id = str(group.get('entity_id') or '')
        if entity_id not in seen:
            seen.add(entity_id)
            head.append(group)
        else:
            tail.append(group)
    return head + tail

def build_scout_payload(groups, blocked_ids, blocked_keys, seen_ids, min_samples=5, token_budget=2200):
    blocked_ids = set(blocked_ids or ())
    blocked_keys = set(blocked_keys or ())
    seen_ids = set(seen_ids or ())
    candidates = [group for group in groups or [] if _eligible(group, blocked_ids, blocked_keys, seen_ids, min_samples)]
    evidence = []
    for group in _scout_order(candidates)[:SCOUT_MAX_ITEMS]:
        item = deepcopy(group)
        item['allowed_focus'] = _allowed_focus(group)
        payload = {'analysis_scope': 'proactive', 'evidence': evidence + [item]}
        if conservative_token_estimate(compact_json(payload)) > int(token_budget):
            break
        evidence.append(item)
    return {'analysis_scope': 'proactive', 'evidence': evidence}

def apply_guidance_scope(payload, guidance):
    result = deepcopy(payload)
    rules, denied = ([], set())
    for raw in guidance:
        rule = deepcopy(raw)
        matching = [item['evidence_id'] for item in result['evidence'] if guidance_mentions_identity(rule.get('content', ''), item)]
        if rule.get('kind') == 'qa_history' and (not matching):
            continue
        rule['applies_to_evidence_ids'] = matching or [item['evidence_id'] for item in result['evidence']]
        owner_text = rule.get('content', '').split('房主回答', 1)[-1]
        decline_terms = ('不要再询问', '不要再问', '不用再问', '不必再问', '无需询问', '不要当作习惯', '不要作为生活习惯')
        if any((term in owner_text for term in decline_terms)):
            if matching or any((term in owner_text for term in ('任何', '所有', '一律', '全部'))) or owner_text.strip('：:. \n') in decline_terms:
                denied.update(rule['applies_to_evidence_ids'])
            else:
                rule['applies_to_evidence_ids'] = []
        rules.append(rule)
    result['evidence'] = [item for item in result['evidence'] if item['evidence_id'] not in denied]
    remaining = {item['evidence_id'] for item in result['evidence']}
    result['user_guidance'] = [rule for rule in rules if remaining.intersection(rule['applies_to_evidence_ids'])]
    return result

def fit_scout_payload(payload, token_budget=3000):
    result = deepcopy(payload)
    while conservative_token_estimate(compact_json(result)) > token_budget:
        if len(result['evidence']) <= 1:
            return None
        result['evidence'].pop()
        remaining = {item['evidence_id'] for item in result['evidence']}
        for rule in result.get('user_guidance', []):
            rule['applies_to_evidence_ids'] = [item for item in rule['applies_to_evidence_ids'] if item in remaining]
        result['user_guidance'] = [rule for rule in result.get('user_guidance', []) if rule['applies_to_evidence_ids']]
    return result

def validate_scout_response(value, payload):
    try:
        if not isinstance(value, dict) or set(value) != {'findings'}:
            raise ValueError('scout response must be an object containing only findings')
        findings = value['findings']
        if not isinstance(findings, list):
            raise ValueError('findings must be a list')
        if len(findings) > SCOUT_MAX_FINDINGS:
            raise ValueError('too many findings')
        allowed = {str(item['evidence_id']): set(item.get('allowed_focus') or ()) for item in (payload or {}).get('evidence') or [] if isinstance(item, dict) and item.get('evidence_id')}
        sanitized, seen = ([], set())
        for finding in findings:
            if not isinstance(finding, dict) or set(finding) != {'evidence_id', 'focus'}:
                raise ValueError('finding must contain only evidence_id and focus')
            evidence_id = finding['evidence_id']
            focus = finding['focus']
            if not isinstance(evidence_id, str) or not isinstance(focus, str):
                raise ValueError('finding evidence_id and focus must be strings')
            if evidence_id not in allowed:
                raise ValueError('finding cites an unknown evidence_id')
            if evidence_id in seen:
                raise ValueError('duplicate evidence_id')
            if focus not in allowed[evidence_id]:
                raise ValueError('focus is not allowed for this evidence source')
            seen.add(evidence_id)
            sanitized.append({'evidence_id': evidence_id, 'focus': focus})
        return sanitized
    except RetryableModelFormatError:
        raise
    except (TypeError, ValueError, OverflowError) as error:
        raise RetryableModelFormatError('scout response failed schema validation', code='scout_schema') from error
