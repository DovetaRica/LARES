import copy
import asyncio
import hashlib
import json
import re
import logging
import time
import httpx
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from .llm_schema import DISTILL_CONTEXT_TOKENS, MODEL_CALL_WALL_SECONDS, MODEL_CONTEXT_TOKENS, MODEL_OUTPUT_RESERVE_TOKENS, MODEL_PROMPT_RESERVE_TOKENS, REMOTE_PAYLOAD_TOKEN_BUDGET, REPORT_LIST_LIMIT, compact_json, conservative_token_estimate, ollama_input_budget, payload_token_budget
from .llm_schema import RetryableModelFormatError, filter_automation_run_claims, normalize_device_availability_claims, normalize_identity_claims, validate_identity_references
from .guidance_retrieval import select_guidance
from .optional_context import compact_optional_context
from .model_transport import RETRY_INSTRUCTION
from .event_preprocessor import preprocess
from .report_context_builder import AUTOMATION_RUNS_LIMIT, build_window_payload, occupancy_summary_block
from .event_taxonomy import classify as classify_metric
from .robot_presence import block_unreviewed_candidates, build_annotations
from .home_topology import HomeTopology
from .model_metrics import ModelCallLock
from .telemetry_stats import finite_number, summarize_samples
from .telemetry_projection import PROTECTED_ENTITIES_KEY, STABLE_ENTITIES_KEY, project_telemetry, protected_telemetry_entities, stable_telemetry_entities, select_metric_summaries
from .report_evidence import clipped_sessions, occupancy_rollup, window_facts, usable_report, report_coverage
from .question_grounding import question_context, strip_question_context
logger = logging.getLogger(__name__)
PERIOD_NON_TERMINAL = frozenset({'generating', 'interrupted'})
REFERENCE_SUMMARY_CHARS = 800
REFERENCE_ITEM_CHARS = 200
REFERENCE_TOKEN_LIMIT = 2000
DISTILL_LABEL_SUFFIX = '蒸馏版'
DISTILL_STATE_KEY = 'distill_job_status'
DISTILL_REFERENCE_TOKEN_LIMIT = 4096
PATTERN_EVIDENCE_LOOKBACK_DAYS = 30
DISTILL_OWN_SOURCES = {'period': ('period_summaries', 'period_start'), 'daily': ('daily_summaries', 'day'), 'weekly': ('weekly_summaries', 'week_start')}
REPORT_TYPE_INSTRUCTIONS = {'period': '这是六小时时段报告：summary 聚焦这段时间最值得房主知道的事（不超过三件），supports 可以补充更多有证据的细节。不要按事件数量衡量重要性，也不要逐条复述设备的每次状态刷新。', 'daily': '这是日报：综合各时段报告与全天统计，合并跨时段重复的发现，优先保留有证据且反复出现的现象；noteworthy 只写最值得继续观察的问题。总量控制：整份 JSON 不超过约 6000 字，summary 不超过 600 字，每条不超过 120 字。', 'weekly': '这是周报：优先回答哪些现象在多日重复出现、哪些判断仍然缺少证据、哪些问题值得继续观察。跨日结论必须有日期或程序统计支撑，已被房主否定的说法不得换措辞再次出现。'}
PERIOD_AUTO_ATTEMPT_LIMIT = 2
ATTEMPT_STATE_KEY = 'period_attempts'
ATTEMPT_STATE_WINDOWS = 8
PERIOD_UPSERT = 'INSERT OR REPLACE INTO period_summaries(period_start,period_end,summary_json,created_at,llm_model,llm_duration_ms) VALUES(?,?,?,?,?,?)'
PERIOD_PLACEHOLDER = 'INSERT OR IGNORE INTO period_summaries(period_start,period_end,summary_json,created_at,llm_model,llm_duration_ms) VALUES(?,?,?,?,?,?)'
PERIOD_INTERRUPTED = "INSERT INTO period_summaries(period_start,period_end,summary_json,created_at,llm_model,llm_duration_ms) VALUES(?,?,?,?,?,?) ON CONFLICT(period_start) DO UPDATE SET period_end=excluded.period_end,summary_json=excluded.summary_json,created_at=excluded.created_at,llm_model=excluded.llm_model,llm_duration_ms=excluded.llm_duration_ms WHERE CASE WHEN json_valid(period_summaries.summary_json) THEN COALESCE(json_extract(period_summaries.summary_json,'$.generation_status'),'succeeded') ELSE 'unknown' END <> 'succeeded'"

class PayloadBudgetError(RuntimeError):
    pass

class BehaviorAnalyzer:
    PAYLOAD_FLOORS = (('memory_segments', 0), ('metric_summaries', 2), ('representative_events', 4), ('automation_runs', 1), ('existing_patterns', 2), ('patterns', 2), ('sessions', 6), ('major_sessions', 8), ('robot_presence_annotations', 1), ('recent_detail', 8), ('cooccurrence_findings', 1), ('pattern_evidence_groups', 5), ('event_groups', 8), ('entity_identity_catalog', 20), ('reviewed_pattern_feedback', 5), ('critical_events', 3), ('period_summaries', 4), ('daily_summaries', 7), ('automation_corrections', 5), ('spatial_transition_candidates', 0))
    PAYLOAD_HARD_FLOORS = {'event_groups': 1, 'pattern_evidence_groups': 1, 'entity_identity_catalog': 20, 'critical_events': 1}
    HOME_STATE_HARD_FLOOR = 8
    IDENTITY_CATALOG_LIMIT = 60
    IDENTITY_CATALOG_CHARS = 2400
    REGULAR_CAPS = {'event_groups': 100, 'pattern_evidence_groups': 100, 'representative_events': 48, 'sessions': 60, 'metric_summaries': 40, 'patterns': 100, 'weekly_sessions': 100, 'weekly_patterns': 200, 'robot_presence_annotations': 12, 'spatial_transition_candidates': 8}
    REMOTE_CAPS = {'event_groups': 200, 'pattern_evidence_groups': 200, 'representative_events': 100, 'sessions': 200, 'metric_summaries': 100, 'patterns': 200, 'weekly_sessions': 300, 'weekly_patterns': 400, 'robot_presence_annotations': 12, 'spatial_transition_candidates': 16}

    def __init__(self, database, ollama, memory, anomaly_detector, learning_days=7, learning_only=True, timezone_name='Asia/Shanghai', cooccurrence=None, questions=None, context_tokens=None, output_reserve_tokens=None, prompt_reserve_tokens=None, distill_context_tokens=None, remote_payload_token_budget=None, automatic_distillation=False):
        self.db, self.ollama, self.memory, self.learning_days = (database, ollama, memory, learning_days)
        self.anomaly_detector = anomaly_detector
        self.learning_only = learning_only
        self.cooccurrence = cooccurrence
        self.questions = questions
        self.tz = ZoneInfo(timezone_name)
        self.prompt = (Path(__file__).parent / 'resources' / 'prompts/system.txt').read_text(encoding='utf-8')
        self.distill_prompt = (Path(__file__).parent / 'resources' / 'prompts/distill.txt').read_text(encoding='utf-8')
        self.context_tokens = int(context_tokens or MODEL_CONTEXT_TOKENS)
        self.output_reserve_tokens = int(output_reserve_tokens or MODEL_OUTPUT_RESERVE_TOKENS)
        self.prompt_reserve_tokens = int(prompt_reserve_tokens or MODEL_PROMPT_RESERVE_TOKENS)
        self.distill_context_tokens = int(distill_context_tokens or max(DISTILL_CONTEXT_TOKENS, self.context_tokens))
        self.payload_budget = payload_token_budget(self.context_tokens, self.output_reserve_tokens, self.prompt_reserve_tokens)
        self.distill_payload_budget = payload_token_budget(self.distill_context_tokens, self.output_reserve_tokens, self.prompt_reserve_tokens)
        self.remote_payload_budget = int(remote_payload_token_budget or REMOTE_PAYLOAD_TOKEN_BUDGET)
        self._model_lock = ModelCallLock()
        self.automatic_distillation = automatic_distillation
        self.on_remote_report = None
        self.segment_memory = None
        self.ha = None
        self.presence_noise_entities = frozenset()
        self.event_filter = None
        self.home_topology = HomeTopology()

    def _local_time(self, value):
        if not value:
            return value
        try:
            parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(self.tz).isoformat()
        except (TypeError, ValueError):
            return value

    def _localize_rows(self, rows, fields):
        result = []
        for row in rows:
            item = dict(row)
            for field in fields:
                if field in item:
                    item[field] = self._local_time(item[field])
            result.append(item)
        return result

    def _window_guidance(self, scope, start_local):
        guidance = list(self._guidance(scope))
        if scope == 'period' and self.questions is not None:
            for item in self.questions.slot_answers(start_local.isoformat()):
                item['content'] = self._mask_guidance_numbers(str(item.get('content') or ''))
                item['kind'] = 'qa_history'
                guidance.append(item)
        return guidance

    def _guidance(self, scope):
        rows = self.db.rows("SELECT id,content,scope,source,created_at FROM ai_guidance WHERE active=1 AND scope IN ('all',?) ORDER BY id DESC", (scope,))
        for row in rows:
            content = str(row.get('content') or '')
            if '房主回答' in content:
                row['content'] = self._mask_guidance_numbers(content)
                row['kind'] = 'qa_history'
                row['note'] = '问答历史：提问中的数字已隐去（当时统计，不可复述）；只采纳房主回答表达的事实与偏好'
        return rows

    def _attach_guidance_and_memories(self, payload, scope, start, end):
        guidance = payload.get('user_guidance') or []
        chosen, selection = select_guidance(guidance, payload)
        payload['user_guidance'] = chosen
        protected = {'explicit_rule', 'slot_answer', 'related_correction', 'general_correction'}
        payload['guidance_selection'] = {'available': selection['available'], 'selected': selection['selected'], 'omitted': selection['omitted'], 'protected_count': sum((reason in protected for reason in selection['reasons'])), 'note': '有效明确规则优先，其后为相关回答；未入选不等于被否定。'}
        memories = self.db.rows("SELECT id,type,content,confidence,status,updated_at FROM memories WHERE status IN ('confirmed','observed') AND datetime(updated_at)<=datetime(?) ORDER BY CASE WHEN status='confirmed' THEN 0 ELSE 1 END,datetime(updated_at) DESC,id DESC", (end.isoformat(),))
        selected, used = ([], 0)
        for item in memories:
            if item['type'] == 'room_habit':
                from .report_evidence import parsed
                stamp = parsed(item['updated_at'])
                if item['status'] != 'observed' or not stamp or (end - stamp).days > 2:
                    continue
            cost = self._payload_tokens(item)
            if used + cost <= 600:
                selected.append(item)
                used += cost
        payload['long_term_memories'] = selected

    def _payload_floor(self, payload, key):
        if key == 'user_guidance' and 'guidance_selection' in payload:
            selection = payload['guidance_selection']
            if 'protected_count' in selection:
                return min(selection['protected_count'], len(payload[key]))
            protected = [index + 1 for index, item in enumerate(payload[key]) if item.get('kind') != 'qa_history' or any((word in str(item.get('content') or '').partition('房主回答')[2] for word in ('不是', '不要', '不能', '不代表', '误报', '纠正')))]
            return max(protected, default=0)
        if key in ('period_summaries', 'daily_summaries') and 'report_coverage' in payload:
            return len(payload[key])
        if key == 'long_term_memories':
            return sum((item.get('status') == 'confirmed' for item in payload[key]))
        return self.PAYLOAD_HARD_FLOORS.get(key, 0)
    _GUIDANCE_TIME_PATTERNS = ((re.compile('\\d{1,2}\\s*[:：]\\s*\\d{2}'), '某时刻'), (re.compile('\\d{1,2}\\s*月\\s*\\d{1,2}\\s*日'), '某日'), (re.compile('\\d+\\s*(?:分钟|小时|秒钟|天|周)'), '若干时间'), (re.compile('\\d+\\s*次'), '若干次'))

    @classmethod
    def _mask_guidance_numbers(cls, content):
        marker = '房主回答'
        head, _, tail = str(content).partition(marker)
        masked = head
        for pattern, replacement in cls._GUIDANCE_TIME_PATTERNS:
            masked = pattern.sub(replacement, masked)
        return f'{masked}{marker}{tail}'

    @staticmethod
    def _summary_text(value, limit=400):
        if isinstance(value, str):
            return value[:limit]
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'))[:limit]

    @staticmethod
    def _payload_size(payload):
        return len(compact_json(payload))

    @staticmethod
    def _payload_tokens(payload):
        return conservative_token_estimate(compact_json(payload))

    def _default_budget(self):
        return getattr(self, 'payload_budget', None) or payload_token_budget()

    def _over_budget(self, payload, budget=None):
        token_budget = budget or self._default_budget()
        return self._payload_size(payload) > token_budget * 2 or self._payload_tokens(payload) > token_budget

    @staticmethod
    def _trimable_keys(payload):
        return [key for key, value in payload.items() if isinstance(value, list) and key != 'cooccurrence_findings_full']

    def _note_collection_size(self, model_payload, key, size):
        meta = model_payload.get('cooccurrence_meta')
        if isinstance(meta, dict) and isinstance(meta.get('payload_collections'), dict):
            meta['payload_collections'].setdefault('after', {})[key] = size

    def _shrink_to_budget(self, model_payload, budget=None):
        while self._over_budget(model_payload, budget):
            candidates = [(self._payload_size(value), key, value) for key, value in model_payload.items() if isinstance(value, list) and len(value) > self._payload_floor(model_payload, key)]
            snapshot = model_payload.get('home_state_snapshot')
            occupancy = model_payload.get('occupancy_summary')
            if isinstance(occupancy, dict):
                for name, items in occupancy.items():
                    if isinstance(items, list) and len(items) > 1:
                        candidates.append((self._payload_size(items), f'occupancy_summary.{name}', items))
            snapshot_entities = snapshot.get('entities') if isinstance(snapshot, dict) else None
            if isinstance(snapshot_entities, list) and len(snapshot_entities) > self.HOME_STATE_HARD_FLOOR:
                candidates.append((self._payload_size(snapshot_entities), 'home_state_snapshot.entities', snapshot_entities))
            facts = model_payload.get('window_facts')
            slots = facts.get('slots', []) if isinstance(facts, dict) else []
            for index, slot in enumerate(slots):
                actions = slot.get('actions') if isinstance(slot, dict) else None
                if isinstance(actions, list) and len(actions) > 1:
                    candidates.append((self._payload_size(actions), f'window_facts.slots.{index}.actions', actions))
            if not candidates:
                for index, slot in enumerate(slots):
                    actions = slot.get('actions') if isinstance(slot, dict) else None
                    if isinstance(actions, list) and actions:
                        candidates.append((self._payload_size(actions), f'window_facts.slots.{index}.actions', actions))
                if not candidates:
                    return
            _, key, values = max(candidates, key=lambda item: (item[0], item[1]))
            values.pop()
            if key.startswith('occupancy_summary.'):
                omitted = occupancy.setdefault('omitted_examples', {})
                name = key.split('.', 1)[1]
                omitted[name] = omitted.get(name, 0) + 1
            elif key.startswith('window_facts.slots.'):
                slot = slots[int(key.split('.')[2])]
                slot['omitted_actions'] = slot.get('omitted_actions', 0) + 1
            elif key == 'user_guidance':
                selection = model_payload['guidance_selection']
                selection['selected'] = len(values)
                selection['omitted'] = selection.get('available', len(values) + 1) - len(values)
            self._note_collection_size(model_payload, key, len(values))

    def _model_payload(self, payload, budget=None, reference_token_limit=None):
        token_budget = budget or self._default_budget()
        reference_cap = reference_token_limit or REFERENCE_TOKEN_LIMIT
        model_payload = copy.deepcopy(payload)
        strip_question_context(model_payload)
        reference = model_payload.get('reference_report')
        if isinstance(reference, dict) and 'own_report' not in model_payload:
            model_payload['reference_report'] = {'purpose': 'style_only', 'sections': [key for key in ('summary', 'supports', 'anomalies', 'automation_corrections', 'noteworthy') if reference.get(key)], 'instruction': '先描述当前窗口事实，再说明不确定性，最后提出值得房主确认的背景。示例原文的设备、次数和时间不属于当前窗口。'}
        candidates = model_payload.get(STABLE_ENTITIES_KEY)
        protected_telemetry = model_payload.get(PROTECTED_ENTITIES_KEY)
        model_payload.pop('cooccurrence_findings_full', None)
        model_payload.pop('cooccurrence_meta', None)
        model_payload.pop('evidence_coverage', None)
        for key in [key for key in model_payload if str(key).startswith('_')]:
            model_payload.pop(key, None)
        telemetry_diagnostics = project_telemetry(model_payload, candidates, protected_telemetry)
        selection = model_payload.get('guidance_selection')
        if isinstance(selection, dict):
            protected = self._payload_floor(model_payload, 'user_guidance')
            model_payload['guidance_selection'] = {'protected_count': protected}
        for item in model_payload.get('user_guidance') or []:
            if isinstance(item, dict):
                item.pop('id', None)
        for collection in ('event_groups', 'pattern_evidence_groups'):
            for group in model_payload.get(collection) or []:
                if isinstance(group, dict):
                    group.pop('semantic_key', None)
        keys = self._trimable_keys(model_payload)
        before = {key: len(model_payload[key]) for key in keys}
        retrospective = model_payload.get('analysis_scope') in ('daily', 'weekly')
        if retrospective:
            model_payload['automation_runs'] = []
            model_payload.pop('home_state_snapshot', None)
            for segment in model_payload.get('memory_segments') or []:
                if isinstance(segment, dict):
                    segment.pop('automation_runs', None)
                    segment.pop('sensor_noise_count', None)
            for collection in ('recent_detail', 'representative_events'):
                if collection in model_payload:
                    model_payload[collection] = [item for item in model_payload[collection] if item.get('class') != 'automation_run' and item.get('event_type') != 'automation_triggered' and (not str(item.get('entity_id') or item.get('entity') or '').startswith('automation.'))]
            if 'entity_identity_catalog' in model_payload:
                model_payload['entity_identity_catalog'] = [item for item in model_payload['entity_identity_catalog'] if not str(item.get('entity_id') or '').startswith('automation.')]
        if 'cooccurrence_meta' in model_payload:
            model_payload['cooccurrence_meta']['payload_collections'] = {'before': before, 'after': dict(before)}
        optional = {}
        regular = model_payload.get('analysis_scope') in REPORT_TYPE_INSTRUCTIONS and reference_cap < DISTILL_REFERENCE_TOKEN_LIMIT
        if regular:
            if 'reference_report' in model_payload and self._payload_tokens({'reference_report': model_payload['reference_report']}) > reference_cap:
                model_payload.pop('reference_report', None)
            optional = compact_optional_context(model_payload)
        for key, minimum in self.PAYLOAD_FLOORS:
            values = model_payload.get(key)
            if not isinstance(values, list):
                continue
            floor = min(max(minimum, self._payload_floor(model_payload, key)), len(values))
            if regular and key == 'memory_segments':
                floor = min(1, len(values))
            while len(values) > floor and self._over_budget(model_payload, token_budget):
                values.pop()
                self._note_collection_size(model_payload, key, len(values))
        if 'reference_report' in model_payload:
            if self._payload_tokens({'reference_report': model_payload['reference_report']}) > reference_cap:
                model_payload.pop('reference_report', None)
        self._shrink_to_budget(model_payload, token_budget)
        if self._over_budget(model_payload, token_budget) and 'reference_report' in model_payload:
            model_payload.pop('reference_report', None)
            self._shrink_to_budget(model_payload, token_budget)
        after = {key: len(model_payload[key]) for key in keys}
        model_payload.pop('guidance_selection', None)
        if isinstance(selection, dict):
            selection['selected'] = after.get('user_guidance', 0)
            selection['omitted'] = selection.get('available', before.get('user_guidance', 0)) - selection['selected']
            payload['guidance_selection'] = copy.deepcopy(selection)
        if 'cooccurrence_meta' in payload:
            collection_meta = {'before': before, 'after': after}
            if isinstance(payload.get('cooccurrence_meta'), dict):
                payload['cooccurrence_meta']['payload_collections'] = copy.deepcopy(collection_meta)
        coverage = {'telemetry_projection': telemetry_diagnostics, 'input_tokens_estimate': self._payload_tokens(model_payload), 'input_token_budget': token_budget, 'input_tokens_by_field': {key: self._payload_tokens(value) for key, value in model_payload.items()}, 'budget_exceeded': self._over_budget(model_payload, token_budget), 'segments_available': before.get('memory_segments', 0), 'segments_selected': after.get('memory_segments', 0), 'action_groups_available': before.get('event_groups', 0), 'action_groups_selected': after.get('event_groups', 0), 'pattern_groups_available': before.get('pattern_evidence_groups', 0), 'pattern_groups_selected': after.get('pattern_evidence_groups', 0), 'pattern_groups_omitted': before.get('pattern_evidence_groups', 0) - after.get('pattern_evidence_groups', 0), 'guidance_selected': after.get('user_guidance', 0), 'guidance_omitted': (selection or {}).get('omitted', 0), 'memories_selected': after.get('long_term_memories', 0), 'memories_omitted': before.get('long_term_memories', 0) - after.get('long_term_memories', 0), 'automation_rows_omitted': before.get('automation_runs', 0) - after.get('automation_runs', 0), 'spatial_candidates_available': before.get('spatial_transition_candidates', 0), 'spatial_candidates_selected': after.get('spatial_transition_candidates', 0), 'current_snapshot_omitted': retrospective and 'home_state_snapshot' in payload}
        if optional:
            coverage['optional_context'] = optional
        payload['evidence_coverage'] = coverage
        if coverage['budget_exceeded']:
            logger.error('model payload budget exceeded size=%s limit=%s tokens=%s token_budget=%s collections=%s', self._payload_size(model_payload), token_budget * 2, coverage['input_tokens_estimate'], token_budget, after)
            raise PayloadBudgetError('model payload exceeds input budget')
        return model_payload

    def _set_cooccurrence_state(self, value):
        try:
            self.db.set_state('cooccurrence_status', json.dumps(value, ensure_ascii=False, separators=(',', ':')))
        except Exception as error:
            logger.error('cooccurrence state write failed error=%s', type(error).__name__)

    def _cooccurrence_payload(self, end_local):
        if self.cooccurrence is None:
            return {'compact': [], 'full': [], 'meta': {'enabled': False}}
        started = time.perf_counter()
        try:
            result = self.cooccurrence.analyze(end_local)
        except Exception as error:
            config = getattr(self.cooccurrence, 'config', None)
            self._set_cooccurrence_state({'enabled': True, 'algorithm_version': 'cooccurrence-v1', 'config_hash': getattr(config, 'semantic_hash', None), 'last_error': type(error).__name__})
            raise
        self._label_cooccurrence_findings(result)
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        result['meta']['duration_ms'] = duration_ms
        state = dict(result['meta'])
        state.update({'enabled': True, 'last_error': None})
        self._set_cooccurrence_state(state)
        return result

    @staticmethod
    def _human_identity_label(entity_id, identity):
        entity_id = str(entity_id or '')
        device = str((identity or {}).get('device') or '').strip()
        area = str((identity or {}).get('area') or '').strip()
        appliance_type = str((identity or {}).get('appliance_type') or '')
        if appliance_type == 'vacuum' or entity_id.startswith('vacuum.'):
            return '扫地机器人'
        if area and device:
            base = device if device.startswith(area) else f'{area}{device}'
        else:
            base = device or area or '设备'
        if entity_id.startswith('binary_sensor.') and 'occupancy' in entity_id and ('存在传感器' not in base) and (not base.endswith('存在')):
            base += '存在传感器'
        return base[:200]

    def _label_cooccurrence_findings(self, result):
        if not isinstance(result, dict):
            return result
        cache = {}

        def identity(entity_id):
            if entity_id not in cache:
                cache[entity_id] = self._identity({'entity_id': entity_id, 'attributes_json': '{}', 'metadata_json': '{}'})
            return cache[entity_id]
        by_id = {}
        for collection in (result.get('full') or [], result.get('compact') or []):
            for finding in collection:
                if not isinstance(finding, dict):
                    continue
                actor_id = str(finding.get('actor_entity_id') or '')
                sensor_id = str(finding.get('sensor_entity_id') or '')
                actor = identity(actor_id)
                sensor = identity(sensor_id)
                labels = {'actor_device': actor.get('device') or '', 'actor_area': actor.get('area') or '', 'actor_label': self._human_identity_label(actor_id, actor), 'sensor_device': sensor.get('device') or '', 'sensor_area': sensor.get('area') or '', 'sensor_label': self._human_identity_label(sensor_id, sensor)}
                finding.update(labels)
                by_id[finding.get('finding_id')] = labels
                if 'description' in finding:
                    lift = finding.get('lift')
                    lift_text = '基线为零' if lift is None else f'{float(lift):.4f}'
                    finding['description'] = f'{labels['actor_label']}与{labels['sensor_label']}在清扫期间呈统计共现：{finding.get('episodes_with_edge')}/{finding.get('episodes_total')} 个清扫时段命中，提升倍数={lift_text}；这不是因果结论。'
        for collection in (result.get('full') or [], result.get('compact') or []):
            for finding in collection:
                if isinstance(finding, dict) and finding.get('finding_id') in by_id:
                    finding.update(by_id[finding['finding_id']])
        return result

    def _event_groups(self, events, limit=100):
        groups = {}
        for event in events:
            if classify_metric(event) != 'device_action':
                continue
            key = (event.get('category'), event.get('event_type'), event.get('source'), event.get('entity_id'), event.get('new_state'))
            identity = self._identity(event)
            item = groups.setdefault(key, {'category': key[0], 'event_type': key[1], 'source': key[2], 'entity_id': key[3], 'count': 0, 'first_ts': event.get('ts'), 'last_ts': event.get('ts'), 'state': key[4], 'states': [], 'local_days': set(), **identity})
            item['count'] += 1
            item['last_ts'] = event.get('ts')
            localized = self._local_time(event.get('ts'))
            if localized:
                item['local_days'].add(localized[:10])
            state = event.get('new_state')
            if state not in item['states'] and len(item['states']) < 6:
                item['states'].append(state)
        result = []
        for item in groups.values():
            item['local_days'] = len(item['local_days'])
            semantic_key = '|'.join((str(item.get(key) or '') for key in ('category', 'event_type', 'source', 'entity_id', 'state')))
            item['evidence_id'] = hashlib.sha256(f'event-group-v1|{semantic_key}'.encode('utf-8')).hexdigest()[:24]
            item['semantic_key'] = semantic_key
            item['status'] = 'reported'
            result.append(item)
        return sorted(result, key=lambda item: item['count'], reverse=True)[:limit]

    def _pattern_evidence(self, end_local, limit=100, max_events=None):
        end_utc = end_local.astimezone(timezone.utc)
        start_utc = end_utc - timedelta(days=PATTERN_EVIDENCE_LOOKBACK_DAYS)
        collection_start = self.db.scalar("SELECT value FROM system_state WHERE key='collection_started_at'") or start_utc.isoformat()
        events = self.db.rows("SELECT id,domain,category,event_type,source,entity_id,area,old_state,new_state,attributes_json,metadata_json,ts FROM events WHERE datetime(ts)>=MAX(datetime(?),datetime(?)) AND datetime(ts)<datetime(?) AND (domain NOT IN ('sensor','binary_sensor','automation','system','persistent_notification','person','device_tracker') OR (domain='sensor' AND category='appliance')) AND event_type NOT IN ('environment_aggregate','appliance_metric_aggregate','automation_triggered') ORDER BY datetime(ts),id" + (' LIMIT ?' if max_events is not None else ''), (start_utc.isoformat(), collection_start, end_utc.isoformat()) + ((max_events + 1,) if max_events is not None else ()))
        if max_events is not None and len(events) > max_events:
            raise ValueError('cross-day evidence volume exceeds idle task limit')
        groups = self._event_groups(events, limit=limit)
        minimum = getattr(self.memory, 'min_pattern_samples', 5)
        groups = [group for group in groups if group['count'] >= minimum]
        evidence_by_id = {group['evidence_id']: {'evidence_id': group['evidence_id'], 'semantic_key': group['semantic_key'], 'status': 'reported', 'sample_count': group['count'], 'local_days': group['local_days'], 'first_ts': group['first_ts'], 'last_ts': group['last_ts'], 'states': group['states'], 'lookback_days': PATTERN_EVIDENCE_LOOKBACK_DAYS} for group in groups}
        for group in groups:
            group['first_ts'] = self._local_time(group['first_ts'])
            group['last_ts'] = self._local_time(group['last_ts'])
            group['lookback_days'] = PATTERN_EVIDENCE_LOOKBACK_DAYS
        return (groups, evidence_by_id)

    @staticmethod
    def _json_object(value):
        if isinstance(value, dict):
            return value
        try:
            parsed = json.loads(value or '{}')
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, json.JSONDecodeError):
            return {}

    def _identity(self, event):
        metadata = BehaviorAnalyzer._json_object(event.get('metadata_json'))
        attributes = BehaviorAnalyzer._json_object(event.get('attributes_json'))
        entity_id = str(event.get('entity_id') or '')
        event_filter = getattr(self, 'event_filter', None)
        profile = getattr(event_filter, 'profile', None) if event_filter is not None else None
        if profile is not None:
            current = profile.entities.get(entity_id) or {}
            for key, meta_key in (('device', 'life_device'), ('area', 'life_area')):
                if current.get(key):
                    metadata[meta_key] = current[key]
        device = str(metadata.get('life_device') or '')[:120]
        appliance_type = str(metadata.get('appliance_type') or '')
        identity_text = f'{entity_id} {device}'.lower()
        if not appliance_type:
            if '净水器' in identity_text or 'water purifier' in identity_text or 'q1000' in identity_text:
                appliance_type = 'water_purifier'
            elif '扫地' in identity_text or 'robotic vacuum' in identity_text or entity_id.startswith('vacuum.'):
                appliance_type = 'vacuum'
        identity = {'device': device, 'appliance_type': appliance_type[:64], 'area': str(metadata.get('life_area') or event.get('area') or '')[:80]}
        friendly = str(attributes.get('friendly_name') or '').strip()[:120]
        if friendly and friendly != device:
            identity['name'] = friendly
        return identity

    def _identity_catalog(self, events):
        catalog = {}
        for event in events:
            entity_id = str(event.get('entity_id') or '')
            identity = self._identity(event)
            if entity_id and (identity['device'] or identity['appliance_type'] or identity.get('name')):
                catalog[entity_id] = {'entity_id': entity_id, **identity}
        ordered = sorted(catalog.values(), key=lambda item: (not item['appliance_type'], not item['device'], not item.get('name'), item['entity_id']))
        kept, used = ([], 0)
        for item in ordered[:self.IDENTITY_CATALOG_LIMIT]:
            cost = len(json.dumps(item, ensure_ascii=False, separators=(',', ':'))) + 1
            if kept and used + cost > self.IDENTITY_CATALOG_CHARS:
                break
            kept.append(item)
            used += cost
        return kept

    def _identity_map(self, events):
        labels = {}
        for event in events:
            entity_id = event.get('entity_id')
            if not entity_id or entity_id in labels:
                continue
            identity = self._identity(event)
            label = {key: identity.get(key) for key in ('device', 'area', 'name') if identity.get(key)}
            if label:
                labels[entity_id] = label
        return labels

    def _metric_summaries(self, events, limit=40, identities=None, protected=(), context=None):
        groups = {}
        for event in events:
            if event.get('event_type') not in {'environment_aggregate', 'appliance_metric_aggregate'}:
                continue
            attrs = self._json_object(event.get('attributes_json'))
            key = (event.get('entity_id'), event.get('category'), str(attrs.get('unit_of_measurement', ''))[:32])
            groups.setdefault(key, []).append(event)
        summaries = []
        for (entity, category, unit), group in groups.items():
            stats = summarize_samples(group, lambda row: self._json_object(row.get('metadata_json')))
            summaries.append({'entity_id': entity, 'category': category, **{key: value for key, value in (identities or {}).get(entity, {}).items() if key in ('device', 'area', 'name')}, **stats, 'period_start': self._local_time(stats.get('start') or group[0].get('ts')), 'period_end': self._local_time(stats.get('end') or group[-1].get('ts')), 'unit': unit, 'summary_basis': 'whole_window_samples'})
            for key in ('start', 'end', 'trend'):
                summaries[-1].pop(key, None)
        return select_metric_summaries(summaries, limit, protected, context)

    def _reviewed_feedback(self):
        rows = self.db.rows("SELECT p.id,p.type,p.fingerprint,p.description,p.status,f.action,f.note,f.created_at FROM patterns p LEFT JOIN feedback f ON f.id=(SELECT f2.id FROM feedback f2 WHERE f2.pattern_id=p.id ORDER BY f2.id DESC LIMIT 1) WHERE p.status IN ('confirmed','rejected') ORDER BY COALESCE(f.created_at,p.last_seen) DESC LIMIT 20")
        return [{'pattern_id': row['id'], 'type': row['type'], 'fingerprint': row['fingerprint'], 'description': str(row['description'])[:300], 'status': row['status'], 'action': row['action'], 'reason': str(row['note'] or '')[:300], 'reviewed_at': self._local_time(row['created_at'])} for row in rows]

    @staticmethod
    def _representative_events(events, limit=48):
        if len(events) <= limit:
            return events
        priority = [event for event in events if event.get('source') == 'manual' or event.get('event_type') == 'automation_correction' or event.get('category') in {'appliance', 'appliance_metric'}]
        selected = priority[:limit]
        if len(selected) < limit:
            remaining = [event for event in events if event not in selected]
            slots = limit - len(selected)
            step = max(1, len(remaining) // slots)
            selected.extend(remaining[::step][:slots])
        return sorted(selected, key=lambda event: event.get('ts') or '')

    async def _serving_provider(self, model=None):
        provider_for = getattr(self.ollama, 'provider_for', None)
        if provider_for is not None:
            return str(await provider_for(model))
        client_for = getattr(self.ollama, 'client_for', None)
        client = client_for(model) if client_for else self.ollama
        return str(getattr(client, 'provider', ''))

    async def _caps_and_budget(self, model=None):
        if await self._serving_provider(model) == 'third-party':
            return (self.REMOTE_CAPS, self.remote_payload_budget)
        return (self.REGULAR_CAPS, self.payload_budget)

    def _window_payload(self, start_local, end_local, scope, caps=None):
        caps = caps or self.REGULAR_CAPS
        start = start_local.astimezone(timezone.utc)
        end = end_local.astimezone(timezone.utc)
        collection_start = self.db.scalar("SELECT value FROM system_state WHERE key='collection_started_at'") or start.isoformat()
        events = self.db.rows('SELECT id,domain,category,event_type,source,entity_id,area,old_state,new_state,attributes_json,metadata_json,ts FROM events WHERE datetime(ts)>=MAX(datetime(?),datetime(?)) AND datetime(ts)<datetime(?) ORDER BY datetime(ts),id', (start.isoformat(), collection_start, end.isoformat()))
        sessions = self.db.rows('SELECT id,room,type,start_ts,end_ts,duration_seconds,status FROM sessions WHERE datetime(start_ts)<datetime(?) AND (end_ts IS NULL OR datetime(end_ts)>=datetime(?)) ORDER BY COALESCE(duration_seconds,0) DESC', (end.isoformat(), start.isoformat()))
        patterns = self.db.rows('SELECT id,type,description,confidence,sample_count,status FROM patterns ORDER BY confidence DESC LIMIT ?', (caps['patterns'],))
        representative = self._representative_events(events, limit=caps['representative_events'])
        important = [{key: self._local_time(event.get(key)) if key == 'ts' else event.get(key) for key in ('category', 'event_type', 'source', 'entity_id', 'area', 'new_state', 'ts')} for event in representative]
        for item, event in zip(important, representative):
            item.update(self._identity(event))
        groups = self._event_groups(events, limit=caps['event_groups'])
        evidence_by_id = {group['evidence_id']: {'evidence_id': group['evidence_id'], 'semantic_key': group['semantic_key'], 'status': 'reported', 'sample_count': group['count'], 'local_days': group['local_days'], 'first_ts': group['first_ts'], 'last_ts': group['last_ts'], 'states': group['states']} for group in groups}
        for group in groups:
            group['first_ts'] = self._local_time(group['first_ts'])
            group['last_ts'] = self._local_time(group['last_ts'])
        cooccurrence = self._cooccurrence_payload(end_local)
        identities = self._identity_map(events)
        from .event_preprocessor import detect_incomplete_entities
        from .report_context_builder import _filtered_sessions, _session_exclusions, next_events_after, session_inside_systemic_window
        end_utc = end_local.astimezone(timezone.utc).isoformat()
        suspects = detect_incomplete_entities(events + next_events_after(self.db, end_utc))
        gated = self.presence_noise_entities | getattr(self, 'presence_non_occupancy_entities', frozenset())
        session_exclusions = _session_exclusions(self.db, start_local.astimezone(timezone.utc).isoformat(), end_utc, suspects, gated)
        preprocessed = preprocess(events, range_end=end_utc, noise_entities=self.presence_noise_entities, identities=identities, suspect_entities=suspects, non_occupancy_entities=gated)
        occupancy = occupancy_summary_block(preprocessed, events)
        robot_presence = build_annotations(events, cooccurrence['full'], self._identity, caps['robot_presence_annotations'], incomplete_entities=suspects)
        movements = self._movements_in_window(start_local, end_local)
        payload = {'analysis_scope': scope, 'period_start': start_local.isoformat(), 'period_end': end_local.isoformat(), 'total_events': len(events), 'event_counts': self._counts(events), 'event_groups': groups, 'occupancy_summary': occupancy, 'automation_runs': preprocessed.automation_runs[:AUTOMATION_RUNS_LIMIT], 'robot_presence_annotations': robot_presence, 'movements': movements, 'home_topology': self.home_topology.context(), 'spatial_transition_candidates': self._localize_rows(self.home_topology.candidates(preprocessed.compressed, caps['spatial_transition_candidates']), ('time',)), 'representative_events': important, 'entity_identity_catalog': self._identity_catalog(events), 'metric_summaries': [], 'user_guidance': self._window_guidance(scope, start_local), 'reviewed_pattern_feedback': self._reviewed_feedback(), 'timezone': 'Asia/Shanghai (UTC+08:00, Beijing Time)', 'sessions': [{**row, 'room': self._session_room(row.get('room'))} for row in _filtered_sessions([row for row in self._localize_rows(sessions, ('start_ts', 'end_ts')) if not session_inside_systemic_window(row, preprocessed.systemic_windows)], session_exclusions)], 'existing_patterns': patterns, 'deterministic_anomalies': self._localize_rows(self.anomaly_detector.detect(end), ('first_seen', 'last_seen')), 'cooccurrence_findings': cooccurrence['compact'], 'cooccurrence_findings_full': cooccurrence['full'], 'cooccurrence_meta': cooccurrence['meta'], '_evidence_by_id': evidence_by_id}
        trusted = clipped_sessions(payload['sessions'], start_local, end_local, self._session_room)
        payload['sessions'] = trusted[:caps['sessions']]
        payload['occupancy_rollup'] = occupancy_rollup(trusted)
        payload['window_facts'] = window_facts(self, events, trusted, start_local, end_local)
        from .critical_events import detect
        critical_pool = detect(events, preprocessed)
        payload['critical_events'] = sorted(critical_pool, key=lambda item: -item.get('score', 0))[:10]
        protected = protected_telemetry_entities(events, critical_pool)
        payload[PROTECTED_ENTITIES_KEY] = sorted(protected)
        payload['metric_summaries'] = self._metric_summaries(events, limit=caps['metric_summaries'], identities=identities, protected=protected, context=payload)
        payload[STABLE_ENTITIES_KEY] = sorted(stable_telemetry_entities(preprocessed.compressed, protected, context=payload, identities=identities))
        self._attach_guidance_and_memories(payload, scope, start_local, end_local)
        return payload

    async def _day_payload(self, day, caps=None):
        start_local = datetime.combine(day, datetime.min.time(), tzinfo=self.tz)
        end_local = start_local + timedelta(days=1)
        payload = await build_window_payload(self, start_local, end_local, 'daily', caps=caps)
        periods = self.db.rows('SELECT period_start,period_end,summary_json FROM period_summaries WHERE datetime(period_start)>=datetime(?) AND datetime(period_end)<=datetime(?) ORDER BY datetime(period_start)', (start_local.isoformat(), end_local.isoformat()))
        payload['date'] = day.isoformat()
        evidence_groups, evidence_by_id = self._pattern_evidence(end_local, limit=(caps or self.REGULAR_CAPS)['pattern_evidence_groups'])
        payload['pattern_evidence_groups'] = evidence_groups
        payload.setdefault('_evidence_by_id', {}).update(evidence_by_id)
        compact_periods = []
        for row in periods:
            try:
                stored = json.loads(row['summary_json'])
            except (TypeError, json.JSONDecodeError):
                stored = {}
            if not isinstance(stored, dict):
                stored = {}
            if not usable_report(stored):
                continue
            compact_periods.append({'period_start': row['period_start'], 'period_end': row['period_end'], 'summary': self._summary_text(stored.get('summary', ''), 200)})
        payload['period_summaries'] = compact_periods
        expected = [(start_local + timedelta(hours=hour)).isoformat() for hour in (0, 6, 12, 18)]
        payload['report_coverage'] = report_coverage(periods, 'period_start', expected)
        return payload

    @staticmethod
    def _counts(events):
        counts = {'device_action': 0, 'sensor_telemetry': 0, 'presence_raw': 0, 'automation_run': 0, 'system': 0}
        for e in events:
            counts[classify_metric(e)] = counts.get(classify_metric(e), 0) + 1
        return counts

    def _fallback_report(self, payload, error):
        reason = getattr(error, 'code', None)
        if not isinstance(reason, str) or reason not in {'invalid_json_shape', 'schema_validation', 'segment_schema', 'cooccurrence_identity', 'vacuum_trajectory', 'room_identity', 'device_identity'}:
            reason = 'input_budget' if isinstance(error, PayloadBudgetError) else 'transport_or_timeout' if isinstance(error, (httpx.HTTPError, asyncio.TimeoutError)) else 'internal_error'
        event_count = int(payload.get('total_events', 0) or 0)
        summary = f'本时段记录了 {event_count} 条有效事件，但本地模型输出未通过结构或设备身份校验。为避免展示错误设备名称，本次不展示模型推断；原始事件和确定性统计仍已保留。'
        if isinstance(error, PayloadBudgetError):
            summary = '本次分析输入超过模型预算，尚未调用模型，本次不展示模型推断；原始事件、确定性统计和报告覆盖信息仍已保留。'
        return {'summary': summary, 'supports': [], 'new_candidates': [], 'anomalies': payload.get('deterministic_anomalies', [])[:5], 'automation_corrections': [], 'noteworthy': [], 'generation_status': 'model_output_rejected', 'error_type': type(error).__name__, 'error_code': reason, 'analysis_scope': payload.get('analysis_scope', 'period'), 'report_coverage': payload.get('report_coverage', {}), 'evidence_coverage': payload.get('evidence_coverage', {})}

    def resolved_model(self, model=None):
        return str(model or self.ollama.model)

    def local_model(self):
        client = getattr(self.ollama, 'local', self.ollama)
        return str(getattr(client, 'model', '') or '')

    async def _request_model_report(self, model_payload, model, prompt=None, context_tokens=None, report_type=None):
        instruction = prompt or self.prompt
        block = None if prompt else REPORT_TYPE_INSTRUCTIONS.get(report_type or '')
        if block:
            instruction = f'{instruction}\n\n{block}'
        if context_tokens is not None:
            if model:
                return await self.ollama.analyze(instruction, model_payload, model=model, context_tokens=context_tokens)
            return await self.ollama.analyze(instruction, model_payload, context_tokens=context_tokens)
        if model:
            return await self.ollama.analyze(instruction, model_payload, model=model)
        return await self.ollama.analyze(instruction, model_payload)

    async def _safe_model_analysis(self, payload, model=None, prompt=None, context_tokens=None, report_type=None, budget=None):
        reference_cap = REFERENCE_TOKEN_LIMIT
        if context_tokens is not None:
            budget = payload_token_budget(context_tokens, self.output_reserve_tokens, self.prompt_reserve_tokens)
            reference_cap = DISTILL_REFERENCE_TOKEN_LIMIT
        if await self._serving_provider(model) != 'third-party':
            instruction = prompt or self.prompt + '\n\n' + REPORT_TYPE_INSTRUCTIONS.get(report_type or '', '')
            safe_budget = ollama_input_budget(context_tokens or getattr(self, 'context_tokens', MODEL_CONTEXT_TOKENS), getattr(self, 'output_reserve_tokens', MODEL_OUTPUT_RESERVE_TOKENS), instruction, prompt_reserve_tokens=getattr(self, 'prompt_reserve_tokens', MODEL_PROMPT_RESERVE_TOKENS), retry_instruction=RETRY_INSTRUCTION)
            budget = min(budget or self._default_budget(), safe_budget)
            if budget <= 0:
                return (self._fallback_report(payload, PayloadBudgetError('prompt exceeds context')), 0, {})
        try:
            model_payload = self._model_payload(payload, budget=budget, reference_token_limit=reference_cap)
        except PayloadBudgetError as error:
            logger.error('model payload rejected before request error=%s', type(error).__name__)
            return (self._fallback_report(payload, error), 0, {})
        started = time.perf_counter()
        try:
            result, duration, raw = await asyncio.wait_for(self._request_model_report(model_payload, model, prompt, context_tokens=context_tokens, report_type=report_type), timeout=MODEL_CALL_WALL_SECONDS)
        except (RetryableModelFormatError, httpx.HTTPError, asyncio.TimeoutError) as error:
            logger.error('model report rejected; storing deterministic fallback error=%s reason=%s', type(error).__name__, getattr(error, 'code', 'transport_or_timeout'))
            return (self._fallback_report(payload, error), int((time.perf_counter() - started) * 1000), {})
        try:
            result, normalized_count = normalize_identity_claims(result, payload)
            if normalized_count:
                logger.warning('normalized report identity/path claims count=%s', normalized_count)
            validate_identity_references(result, payload)
        except (RetryableModelFormatError, AttributeError, TypeError, ValueError) as error:
            logger.error('model report rejected; storing deterministic fallback error=%s reason=%s', type(error).__name__, getattr(error, 'code', 'identity_or_shape'))
            return (self._fallback_report(payload, error), duration, {})
        result['report_coverage'] = payload.get('report_coverage', {})
        result['evidence_coverage'] = payload.get('evidence_coverage', {})
        result, filtered_count = filter_automation_run_claims(result, payload)
        if filtered_count:
            logger.warning('filtered automation-run-derived report claims count=%s', filtered_count)
        result, corrected_count = normalize_device_availability_claims(result, payload)
        if corrected_count:
            logger.warning('normalized device-availability report claims count=%s', corrected_count)
        result, blocked_count = block_unreviewed_candidates(result, payload.get('robot_presence_annotations') or [])
        if blocked_count:
            logger.warning('blocked unreviewed robot-presence candidates count=%s', blocked_count)
        context = question_context(payload, token_budget=600)
        if context is not None:
            result['fact_context'] = context
        return (result, duration, raw)

    async def _guarded_model_analysis(self, payload, model=None, report_type=None, budget=None):
        try:
            async with self._model_lock:
                return await self._safe_model_analysis(payload, model, report_type=report_type, budget=budget)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error('model analysis failed; storing deterministic fallback error=%s', type(error).__name__)
            return (self._fallback_report(payload, error), 0, {})

    async def daily(self, day=None, model=None):
        day = day or datetime.now(self.tz).date() - timedelta(days=1)
        job = {'day': day.isoformat(), 'status': 'generating', 'source': 'scheduled', 'started_at': datetime.now(timezone.utc).isoformat(), 'model': self.resolved_model(model)}
        self.db.set_state('daily_job_status', json.dumps(job, ensure_ascii=False, separators=(',', ':')))
        try:
            caps, budget = await self._caps_and_budget(model)
            payload = await self._day_payload(day, caps=caps)
            example = await self._attach_reference(payload, 'daily', model)
            result, duration, _ = await self._guarded_model_analysis(payload, model, report_type='daily', budget=budget)
            result['cooccurrence_findings'] = payload['cooccurrence_findings']
            result['cooccurrence_findings_full'] = payload['cooccurrence_findings_full']
            result['cooccurrence_meta'] = payload['cooccurrence_meta']
            result['robot_presence_annotations'] = payload.get('robot_presence_annotations', [])
            first = self.db.scalar('SELECT MIN(ts) FROM events')
            report_end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=self.tz).astimezone(timezone.utc)
            old_enough = first and (report_end - datetime.fromisoformat(first.replace('Z', '+00:00'))).days >= self.learning_days
            mature = bool(old_enough and (not self.learning_only))
            if mature:
                verified = {item['finding_id']: item for item in payload['cooccurrence_findings_full'] if item.get('status') == 'reported'}
                self.memory.upsert_candidates(result.get('new_candidates', []), findings_by_id=verified, evidence_by_id=payload['_evidence_by_id'])
            result['learning_only'] = not bool(mature)
            result.setdefault('generation_status', 'succeeded')
            self._record_reference(result, example)
            with self.db.connect() as con:
                con.execute('INSERT OR REPLACE INTO daily_summaries(day,summary_json,created_at,llm_model,llm_duration_ms) VALUES(?,?,?,?,?)', (day.isoformat(), json.dumps(result, ensure_ascii=False), datetime.now(timezone.utc).isoformat(), self.resolved_model(model), duration))
            self._store_variant('daily', day.isoformat(), result, duration, model)
            await self._propose_questions(result, 'daily', day.isoformat(), model)
            await self._queue_distillation('daily', day.isoformat(), model)
            try:
                self.memory.harvest_session_habits()
            except Exception as error:
                logger.warning('session habit harvest failed error=%s', type(error).__name__)
            job.update({'status': result.get('generation_status') or 'succeeded', 'finished_at': datetime.now(timezone.utc).isoformat(), 'duration_ms': duration})
            self.db.set_state('daily_job_status', json.dumps(job, ensure_ascii=False, separators=(',', ':')))
            return result
        except Exception as error:
            job.update({'status': 'failed', 'error_type': type(error).__name__, 'finished_at': datetime.now(timezone.utc).isoformat()})
            self.db.set_state('daily_job_status', json.dumps(job, ensure_ascii=False, separators=(',', ':')))
            raise

    @staticmethod
    def period_generation_status(summary_json):
        try:
            stored = json.loads(summary_json or '{}')
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(stored, dict):
            return None
        value = stored.get('generation_status')
        return str(value) if value else None

    def period_is_complete(self, start):
        row = self.db.scalar('SELECT summary_json FROM period_summaries WHERE period_start=?', (start.isoformat(),))
        if row is None:
            return False
        return self.period_generation_status(row) not in PERIOD_NON_TERMINAL

    def _movements_in_window(self, start_local, end_local, limit=10):
        start_utc = start_local.astimezone(timezone.utc).isoformat()
        end_utc = end_local.astimezone(timezone.utc).isoformat()
        rows = self.db.rows('SELECT kind,ts,window_seconds,trigger_json FROM movements WHERE datetime(ts)>=datetime(?) AND datetime(ts)<datetime(?) ORDER BY ts ASC LIMIT ?', (start_utc, end_utc, limit))
        kind_label = {'arrival': '回家方向', 'departure': '离家方向'}
        results = []
        for row in rows:
            try:
                trigger = json.loads(row.get('trigger_json') or '[]')
            except (TypeError, ValueError):
                trigger = []
            meta = trigger[0] if isinstance(trigger, list) and trigger and isinstance(trigger[0], dict) else {}
            evidence = {'open': '观测到开门状态', 'unlock_only': '仅有解锁记录，不代表开门', 'other': '门锁状态仅作辅助', 'unavailable': '缺少门状态佐证'}
            results.append({'kind': kind_label.get(row['kind'], row['kind']), 'time': self._local_time(row['ts']), 'window_seconds': row['window_seconds'], 'subject': '双侧信号匹配，身份未确认', 'passage_evidence': '有开门状态佐证' if meta.get('door_evidence') == 'open' else '可能的通行方向，缺少实际开门佐证', 'door_evidence': evidence.get(meta.get('door_evidence'), '旧规则记录，门证据未区分')})
        return results

    def _session_room(self, room):
        event_filter = getattr(self, 'event_filter', None)
        profile = getattr(event_filter, 'profile', None) if event_filter is not None else None
        if profile and room and ('.' in str(room)):
            entry = profile.entities.get(str(room))
            if entry:
                return entry.get('area') or entry.get('device') or room
        return room

    def _collection_floor(self):
        value = self.db.scalar("SELECT value FROM system_state WHERE key='collection_started_at'")
        try:
            parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        truncated = parsed.astimezone(self.tz).replace(minute=0, second=0, microsecond=0)
        boundary = truncated - timedelta(hours=truncated.hour % 6)
        return boundary if boundary == truncated else boundary + timedelta(hours=6)

    def pending_period_bounds(self, limit=4):
        _, latest_end = self.latest_period_bounds()
        floor = self._collection_floor()
        pending, end = ([], latest_end)
        while len(pending) < limit:
            start = end - timedelta(hours=6)
            if floor is not None and start < floor:
                break
            if not self.period_is_complete(start):
                pending.append((start, end))
            end = start
        return sorted(pending)

    def _record_period_job(self, state):
        try:
            self.db.set_state('period_job_status', json.dumps(state, ensure_ascii=False, separators=(',', ':')))
        except Exception as error:
            logger.error('period job state write failed error=%s', type(error).__name__)

    def _begin_period_job(self, start, end, source, attempt):
        job = {'period_start': start.isoformat(), 'period_end': end.isoformat(), 'status': 'generating', 'attempt': max(int(attempt), 1), 'source': source, 'started_at': datetime.now(timezone.utc).isoformat(), 'finished_at': None, 'duration_ms': None, 'error_type': None}
        self._record_period_job(job)
        return job

    def _finish_period_job(self, job, status, duration_ms, error_type, reference_model=None):
        job = dict(job)
        job.update({'status': status, 'finished_at': datetime.now(timezone.utc).isoformat(), 'duration_ms': duration_ms, 'error_type': error_type, 'reference_model': reference_model})
        self._record_period_job(job)

    def _record_distill_job(self, job):
        try:
            self.db.set_state(DISTILL_STATE_KEY, json.dumps(job, ensure_ascii=False, separators=(',', ':')))
        except Exception as error:
            logger.error('distill job state write failed error=%s', type(error).__name__)

    def _begin_distill_job(self, scope, key, teacher):
        job = {'scope': scope, 'period_start': key, 'report_key': key, 'status': 'generating', 'teacher_model': teacher, 'local_model': self.local_model(), 'started_at': datetime.now(timezone.utc).isoformat(), 'finished_at': None, 'duration_ms': None, 'error_type': None}
        self._record_distill_job(job)
        return job

    def _finish_distill_job(self, job, status, started, error_type=None, duration_ms=None):
        job = dict(job)
        job.update({'status': status, 'finished_at': datetime.now(timezone.utc).isoformat(), 'error_type': error_type, 'duration_ms': duration_ms if duration_ms is not None else int((time.perf_counter() - started) * 1000)})
        self._record_distill_job(job)

    async def _queue_distillation(self, scope, key, model):
        if not self.automatic_distillation:
            return
        if await self._serving_provider(model) != 'third-party':
            return
        if self.on_remote_report is None:
            return
        try:
            await self.on_remote_report(scope, key, self.resolved_model(model))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning('distillation enqueue failed error=%s', type(error).__name__)

    async def _distill_payload(self, scope, key):
        if scope == 'period':
            start = datetime.fromisoformat(str(key))
            if start.tzinfo is None:
                start = start.replace(tzinfo=self.tz)
            end = start + timedelta(hours=6)
            return (self._window_payload(start, end, 'period'), {'type': 'period', 'start': start.isoformat(), 'end': end.isoformat()})
        if scope == 'daily':
            return (await self._day_payload(date.fromisoformat(str(key))), 'daily')
        if scope == 'weekly':
            return (self._week_payload(date.fromisoformat(str(key))), 'weekly')
        raise ValueError(f'unknown distill scope {scope}')

    async def distill(self, scope='period', key=None):
        started = time.perf_counter()
        local = self.local_model()
        teacher = self._newest_teacher(scope, key, local)
        if not local or not teacher or scope not in DISTILL_OWN_SOURCES:
            self._finish_distill_job(self._begin_distill_job(scope, key, None), 'no_teacher', started, 'NoTeacherReport')
            logger.info("distillation skipped; no other model's report for this window")
            return None
        job = self._begin_distill_job(scope, key, teacher.get('model'))
        own = self._own_stored_report(scope, key)
        payload = analysis_scope = None
        if not own:
            try:
                baseline_payload, baseline_scope = await self._distill_payload(scope, key)
                baseline, baseline_duration, _ = await self._guarded_model_analysis(baseline_payload, local, report_type=scope, budget=self.payload_budget)
            except asyncio.CancelledError:
                self._finish_distill_job(job, 'interrupted', started, 'CancelledError')
                raise
            except Exception as error:
                self._finish_distill_job(job, 'failed', started, type(error).__name__)
                logger.error('local distillation baseline failed error=%s', type(error).__name__)
                return None
            baseline_status = (baseline or {}).get('generation_status') or 'succeeded'
            if not isinstance(baseline, dict) or baseline_status != 'succeeded':
                error_type = (baseline or {}).get('error_type') or 'LocalBaselineRejected'
                self._finish_distill_job(job, 'model_output_rejected', started, error_type)
                logger.warning('local distillation baseline rejected error_type=%s', error_type)
                return None
            for field in ('cooccurrence_findings', 'cooccurrence_findings_full', 'cooccurrence_meta', 'robot_presence_annotations'):
                if field in baseline_payload:
                    baseline[field] = baseline_payload[field]
            baseline['analysis_scope'] = baseline_scope
            baseline.setdefault('generation_status', 'succeeded')
            self._store_variant(scope, key, baseline, baseline_duration, local)
            own = self._own_stored_report(scope, key)
            if not own:
                self._finish_distill_job(job, 'failed', started, 'LocalBaselineNotStored')
                return None
            payload, analysis_scope = (baseline_payload, baseline_scope)
        if payload is None:
            try:
                payload, analysis_scope = await self._distill_payload(scope, key)
            except (TypeError, ValueError):
                self._finish_distill_job(job, 'failed', started, 'InvalidReportKey')
                return None
        payload['own_report'] = own or {}
        payload['reference_report'] = teacher
        try:
            if self._payload_tokens({'reference_report': payload['reference_report']}) > DISTILL_REFERENCE_TOKEN_LIMIT:
                payload['reference_report'] = None
        except Exception:
            payload['reference_report'] = None
        try:
            async with self._model_lock:
                result, duration, _ = await self._safe_model_analysis(payload, local, self.distill_prompt, context_tokens=self.distill_context_tokens)
        except asyncio.CancelledError:
            self._finish_distill_job(job, 'interrupted', started, 'CancelledError')
            raise
        except Exception as error:
            self._finish_distill_job(job, 'failed', started, type(error).__name__)
            logger.error('distillation failed error=%s', type(error).__name__)
            return None
        status = (result or {}).get('generation_status') or 'succeeded'
        if not isinstance(result, dict) or status != 'succeeded':
            error_type = (result or {}).get('error_type') or 'ModelOutputRejected'
            self._finish_distill_job(job, 'model_output_rejected', started, error_type)
            logger.warning('distillation rejected error_type=%s', error_type)
            return None
        for field in ('cooccurrence_findings', 'cooccurrence_findings_full', 'cooccurrence_meta'):
            if field in payload:
                result[field] = payload[field]
        result['analysis_scope'] = analysis_scope
        result['distilled_from'] = teacher.get('model')
        result['distilled_from_key'] = teacher.get('period_start')
        result.setdefault('generation_status', 'succeeded')
        self._store_variant(scope, key, result, duration, f'{local}{DISTILL_LABEL_SUFFIX}')
        self._finish_distill_job(job, 'succeeded', started, None, duration)
        logger.info('distilled report stored scope=%s key=%s teacher=%s', scope, key, teacher.get('model'))
        return result

    async def _propose_questions(self, report, scope, report_key, model):
        if self.questions is None or not isinstance(report, dict):
            return
        if report.get('generation_status') != 'succeeded':
            return
        if self.questions.offpeak_only and self.questions.peak_now():
            if self.questions.defer(scope, report_key):
                logger.info('question round deferred to off-peak hours scope=%s', scope)
            return
        try:
            async with self._model_lock:
                await self.questions.propose(report, scope, report_key, model)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning('question proposal failed error=%s', type(error).__name__)

    async def drain_question_queue(self, limit=4):
        if self.questions is None:
            return 0
        if self.questions.offpeak_only and self.questions.peak_now():
            return 0
        asked = 0
        for scope, key in self.questions.take_queued(limit):
            report = self.questions.report_for(scope, key)
            if report is None:
                continue
            try:
                async with self._model_lock:
                    await self.questions.propose(report, scope, key, None, source='deferred')
                asked += 1
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning('deferred question round failed error=%s', type(error).__name__)
        if asked:
            logger.info('deferred question rounds asked count=%s', asked)
        return asked

    async def ask_questions(self, scope='period', model=None):
        if self.questions is None:
            return []
        async with self._model_lock:
            return await self.questions.propose_from_latest(scope, model)

    @staticmethod
    def _record_reference(result, example):
        if not example:
            return
        result['reference_model'] = example.get('model')
        result['reference_period'] = example.get('period_start')

    def _store_variant(self, scope, report_key, report, duration_ms, model=None):
        if not isinstance(report, dict) or report.get('generation_status') != 'succeeded':
            return
        with self.db.connect() as con:
            con.execute('INSERT OR REPLACE INTO report_variants(scope,report_key,llm_model,summary_json,created_at,llm_duration_ms) VALUES(?,?,?,?,?,?)', (scope, report_key, self.resolved_model(model), json.dumps(report, ensure_ascii=False), datetime.now(timezone.utc).isoformat(), duration_ms))

    def reference_report(self, scope, exclude_model, key=None, full=False):
        where = 'scope=? AND llm_model<>? AND llm_model NOT LIKE ?' + (' AND report_key=?' if key else '')
        params = (scope, exclude_model, f'%{DISTILL_LABEL_SUFFIX}') + ((key,) if key else ())
        rows = self.db.rows(f'SELECT llm_model,report_key,summary_json,created_at FROM report_variants WHERE {where} ORDER BY created_at DESC, llm_model LIMIT 1', params)
        if not rows:
            return None
        try:
            stored = json.loads(rows[0]['summary_json'] or '{}')
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(stored, dict):
            return None
        summary = str(stored.get('summary') or '').strip()
        if not summary:
            return None

        def items(key, count):
            values = stored.get(key)
            if not isinstance(values, list):
                return []
            return [self._summary_text(item)[:REFERENCE_ITEM_CHARS] for item in values[:count]]
        if full:
            report = {'model': rows[0]['llm_model'], 'period_start': rows[0]['report_key'], 'summary': summary[:4000], 'created_at': rows[0].get('created_at')}
            for name in ('supports', 'new_candidates', 'anomalies', 'automation_corrections', 'noteworthy'):
                values = stored.get(name)
                if isinstance(values, list) and values:
                    report[name] = values[:REPORT_LIST_LIMIT]
            return report
        return {'model': rows[0]['llm_model'], 'period_start': rows[0]['report_key'], 'summary': summary[:REFERENCE_SUMMARY_CHARS], 'supports': items('supports', 4), 'noteworthy': items('noteworthy', 3)}

    def _newest_teacher(self, scope, key, local):
        same_key = self.reference_report(scope, local, key=key, full=True)
        any_key = self.reference_report(scope, local, key=None, full=True)
        if not same_key:
            return any_key
        if not any_key:
            return same_key
        same_at = str(same_key.get('created_at') or '')
        any_at = str(any_key.get('created_at') or '')
        return same_key if same_at >= any_at else any_key

    def _own_stored_report(self, scope, key):
        local = self.local_model()
        source = DISTILL_OWN_SOURCES.get(scope)
        if not source:
            return None
        table, column = source
        candidates = []
        rows = self.db.rows(f'SELECT llm_model,summary_json FROM {table} WHERE {column}=? LIMIT 1', (key,))
        if rows:
            candidates.append(rows[0])
        if local:
            variant = self.db.rows('SELECT llm_model,summary_json FROM report_variants WHERE scope=? AND report_key=? AND llm_model=? ORDER BY created_at DESC LIMIT 1', (scope, key, local))
            if variant:
                candidates.append(variant[0])
        for row in candidates:
            if row['llm_model'] != local:
                continue
            try:
                stored = json.loads(row['summary_json'] or '{}')
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(stored, dict):
                continue
            summary = str(stored.get('summary') or '').strip()
            if not summary:
                continue
            own = {'model': row['llm_model'], 'summary': summary[:4000]}
            for name in ('supports', 'new_candidates', 'anomalies', 'automation_corrections', 'noteworthy'):
                values = stored.get(name)
                if isinstance(values, list) and values:
                    own[name] = values[:REPORT_LIST_LIMIT]
            return own
        return None

    async def _attach_reference(self, payload, scope, model):
        if await self._serving_provider(model) != 'ollama':
            return None
        example = self.reference_report(scope, self.resolved_model(model))
        if example:
            payload['reference_report'] = example
        return example

    def _store_period(self, start, end, report, duration_ms, model=None, placeholder=False, protect_succeeded=False):
        if placeholder:
            statement = PERIOD_PLACEHOLDER
        elif protect_succeeded:
            statement = PERIOD_INTERRUPTED
        else:
            statement = PERIOD_UPSERT
        with self.db.connect() as con:
            con.execute(statement, (start.isoformat(), end.isoformat(), json.dumps(report, ensure_ascii=False), datetime.now(timezone.utc).isoformat(), self.resolved_model(model), duration_ms))

    @staticmethod
    def _pending_report(scope):
        return {'summary': '本时段报告正在由本地模型生成；事件数据已经保存，请稍后刷新。', 'supports': [], 'new_candidates': [], 'anomalies': [], 'automation_corrections': [], 'noteworthy': [], 'generation_status': 'generating', 'analysis_scope': scope}

    @staticmethod
    def _interrupted_report(scope):
        return {'summary': '本时段报告未完成：模型生成过程被进程关闭或重启中断。这不是最终结果，应用下次启动补算时会自动重试该时段。', 'supports': [], 'new_candidates': [], 'anomalies': [], 'automation_corrections': [], 'noteworthy': [], 'generation_status': 'interrupted', 'analysis_scope': scope}

    def _read_period_attempts(self):
        stored = self._json_object(self.db.scalar('SELECT value FROM system_state WHERE key=?', (ATTEMPT_STATE_KEY,)))
        attempts = {}
        for key, value in stored.items():
            if isinstance(value, int) and (not isinstance(value, bool)):
                attempts[str(key)] = value
        return attempts

    def record_period_attempt(self, start, automatic):
        attempts = self._read_period_attempts()
        key = start.isoformat()
        if automatic:
            attempts[key] = attempts.get(key, 0) + 1
        else:
            attempts.pop(key, None)
        self._write_period_attempts(attempts)
        return attempts.get(key, 0)

    def _write_period_attempts(self, attempts):
        recent = sorted(attempts)[-ATTEMPT_STATE_WINDOWS:]
        self.db.set_state(ATTEMPT_STATE_KEY, json.dumps({item: attempts[item] for item in recent}, ensure_ascii=False, separators=(',', ':')))

    def release_period_attempt(self, start):
        attempts = self._read_period_attempts()
        key = start.isoformat()
        if attempts.get(key):
            attempts[key] = attempts[key] - 1
            self._write_period_attempts(attempts)

    def period_auto_attempts(self, start):
        return self._read_period_attempts().get(start.isoformat(), 0)

    @staticmethod
    def _exhausted_report(scope):
        return {'summary': f'本时段已用满 {PERIOD_AUTO_ATTEMPT_LIMIT} 次自动重试，不再自动重跑，以免长时间占用模型。事件数据仍然完整保存在数据库中。如需模型结论，请在报告页面点击「重新总结」手动触发一次。', 'supports': [], 'new_candidates': [], 'anomalies': [], 'automation_corrections': [], 'noteworthy': [], 'generation_status': 'auto_retry_exhausted', 'error_type': 'AutoRetryLimitReached', 'analysis_scope': scope}

    async def period(self, end=None, hours=6, source='scheduled', model=None):
        if end is None:
            _, end = self.latest_period_bounds(hours)
        elif end.tzinfo is None:
            end = end.replace(tzinfo=self.tz)
        else:
            end = end.astimezone(self.tz)
        start = end - timedelta(hours=hours)
        scope = {'type': 'period', 'start': start.isoformat(), 'end': end.isoformat()}
        attempts = self.record_period_attempt(start, automatic=source != 'manual')
        if source != 'manual' and attempts > PERIOD_AUTO_ATTEMPT_LIMIT:
            logger.warning('period automatic retry limit reached period=%s attempts=%s', start.isoformat(), attempts)
            report = self._exhausted_report(scope)
            if not self.period_is_complete(start):
                self._store_period(start, end, report, 0, model)
            job = self._begin_period_job(start, end, source, attempts)
            self._finish_period_job(job, report['generation_status'], 0, report['error_type'])
            return report
        job = self._begin_period_job(start, end, source, attempts)
        started = time.perf_counter()
        self._store_period(start, end, self._pending_report(scope), 0, model, placeholder=True)
        try:
            caps, budget = await self._caps_and_budget(model)
            payload = await build_window_payload(self, start, end, 'period', caps=caps)
            example = await self._attach_reference(payload, 'period', model)
            async with self._model_lock:
                result, duration, _ = await self._safe_model_analysis(payload, model, report_type='period', budget=budget)
        except asyncio.CancelledError:
            elapsed = int((time.perf_counter() - started) * 1000)
            self._store_period(start, end, self._interrupted_report(scope), 0, model, protect_succeeded=True)
            self.release_period_attempt(start)
            self._finish_period_job(job, 'interrupted', elapsed, 'CancelledError')
            raise
        except Exception as error:
            elapsed = int((time.perf_counter() - started) * 1000)
            logger.error('period report failed; storing deterministic fallback error=%s', type(error).__name__)
            fallback = self._fallback_report({'analysis_scope': scope}, error)
            self._store_period(start, end, fallback, elapsed, model)
            self._finish_period_job(job, 'model_output_rejected', elapsed, type(error).__name__)
            return fallback
        result['cooccurrence_findings'] = payload['cooccurrence_findings']
        result['cooccurrence_findings_full'] = payload['cooccurrence_findings_full']
        result['cooccurrence_meta'] = payload['cooccurrence_meta']
        result['robot_presence_annotations'] = payload.get('robot_presence_annotations', [])
        result['analysis_scope'] = scope
        result.setdefault('generation_status', 'succeeded')
        self._record_reference(result, example)
        self._store_period(start, end, result, duration, model)
        self._store_variant('period', start.isoformat(), result, duration, model)
        await self._propose_questions(result, 'period', start.isoformat(), model)
        self._finish_period_job(job, result['generation_status'], int((time.perf_counter() - started) * 1000), result.get('error_type'), (example or {}).get('model'))
        await self._queue_distillation('period', start.isoformat(), model)
        return result

    def latest_period_bounds(self, hours=6):
        now = datetime.now(self.tz)
        boundary_hour = now.hour // hours * hours
        end = now.replace(hour=boundary_hour, minute=0, second=0, microsecond=0)
        return (end - timedelta(hours=hours), end)

    def _systemic_windows_for(self, start_utc, end_utc, suspects, gated):
        from .event_preprocessor import preprocess
        rows = self.db.rows("SELECT id,ts,entity_id,domain,category,event_type,old_state,new_state,area,attributes_json,metadata_json FROM events WHERE datetime(ts)>=datetime(?) AND datetime(ts)<datetime(?) AND category='presence' ORDER BY datetime(ts),id", (start_utc, end_utc))
        if not rows:
            return []
        preprocessed = preprocess(rows, range_end=end_utc, noise_entities=self.presence_noise_entities, identities=self._identity_map(rows), suspect_entities=suspects, non_occupancy_entities=gated)
        return preprocessed.systemic_windows

    def _week_payload(self, week_start, caps=None):
        caps = caps or self.REGULAR_CAPS
        end_day = week_start + timedelta(days=7)
        start_local = datetime.combine(week_start, datetime.min.time(), tzinfo=self.tz)
        end_local = datetime.combine(end_day, datetime.min.time(), tzinfo=self.tz)
        start_utc = start_local.astimezone(timezone.utc).isoformat()
        end_utc = end_local.astimezone(timezone.utc).isoformat()
        daily_rows = self.db.rows('SELECT day,summary_json FROM daily_summaries WHERE day>=? AND day<? ORDER BY day', (week_start.isoformat(), end_day.isoformat()))
        daily = []
        for row in daily_rows:
            try:
                stored = json.loads(row['summary_json'])
            except (TypeError, json.JSONDecodeError):
                stored = {}
            if not usable_report(stored):
                continue
            daily.append({'day': row['day'], 'summary': self._summary_text(stored.get('summary', ''), 200)})
        from .event_preprocessor import detect_incomplete_entities
        from .report_context_builder import _filtered_sessions, _session_exclusions
        edge_rows = self.db.rows('SELECT entity_id,old_state,new_state FROM events WHERE datetime(ts)>=datetime(?) AND datetime(ts)<datetime(?)', (start_utc, end_utc))
        suspects = detect_incomplete_entities(edge_rows)
        gated = self.presence_noise_entities | getattr(self, 'presence_non_occupancy_entities', frozenset())
        session_exclusions = _session_exclusions(self.db, start_utc, end_utc, suspects, gated)
        session_rows = self.db.rows('SELECT id,room,type,start_ts,end_ts,duration_seconds,status FROM sessions WHERE datetime(start_ts)<datetime(?) AND (end_ts IS NULL OR datetime(end_ts)>datetime(?)) ORDER BY duration_seconds DESC', (end_utc, start_utc))
        sessions = _filtered_sessions(session_rows, session_exclusions)
        from .report_context_builder import session_inside_systemic_window
        systemic_windows = self._systemic_windows_for(start_utc, end_utc, suspects, gated)
        if systemic_windows:
            sessions = [row for row in sessions if not session_inside_systemic_window(row, systemic_windows)]
        sessions = clipped_sessions(sessions, start_local, end_local, self._session_room)
        patterns = self.db.rows('SELECT id,type,fingerprint,description,confidence,sample_count,consistency,counter_examples,first_seen,last_seen,status,reason FROM patterns ORDER BY confidence DESC LIMIT ?', (caps['weekly_patterns'],))
        corrections = self.db.rows("SELECT id,entity_id,area,old_state,new_state,source,ts FROM events WHERE event_type='automation_correction' AND datetime(ts)>=datetime(?) AND datetime(ts)<datetime(?) ORDER BY datetime(ts),id", (start_utc, end_utc))
        week_events = self.db.rows('SELECT entity_id,area,attributes_json,metadata_json FROM events WHERE datetime(ts)>=datetime(?) AND datetime(ts)<datetime(?)', (start_utc, end_utc))
        cooccurrence = self._cooccurrence_payload(end_local)
        payload = {'analysis_scope': 'weekly', 'week_start': week_start.isoformat(), 'timezone': 'Asia/Shanghai (UTC+08:00, Beijing Time)', 'home_topology': self.home_topology.context(), 'user_guidance': self._guidance('weekly'), 'reviewed_pattern_feedback': self._reviewed_feedback(), 'daily_summaries': daily, 'major_sessions': self._localize_rows(sessions, ('start_ts', 'end_ts')), 'patterns': patterns, 'automation_corrections': self._localize_rows(corrections, ('ts',)), 'entity_identity_catalog': self._identity_catalog(week_events), 'cooccurrence_findings': cooccurrence['compact'], 'cooccurrence_findings_full': cooccurrence['full'], 'cooccurrence_meta': cooccurrence['meta']}
        payload['major_sessions'] = payload['major_sessions'][:caps['weekly_sessions']]
        payload['occupancy_rollup'] = occupancy_rollup(sessions)
        payload['report_coverage'] = report_coverage(daily_rows, 'day', [(week_start + timedelta(days=i)).isoformat() for i in range(7)])
        self._attach_guidance_and_memories(payload, 'weekly', start_local, end_local)
        return payload

    async def weekly(self, week_start=None, model=None):
        today = datetime.now(self.tz).date()
        week_start = week_start or today - timedelta(days=today.weekday() + 7)
        job = {'week_start': week_start.isoformat(), 'status': 'generating', 'source': 'scheduled', 'started_at': datetime.now(timezone.utc).isoformat(), 'model': self.resolved_model(model)}
        self.db.set_state('weekly_job_status', json.dumps(job, ensure_ascii=False, separators=(',', ':')))
        try:
            caps, budget = await self._caps_and_budget(model)
            payload = self._week_payload(week_start, caps=caps)
            example = await self._attach_reference(payload, 'weekly', model)
            result, duration, _ = await self._guarded_model_analysis(payload, model, report_type='weekly', budget=budget)
            eligible = [p for p in payload['patterns'] if p['status'] in {'candidate', 'confirmed'} and p['confidence'] >= 0.85 and (p['sample_count'] >= 10)]
            result.setdefault('generation_status', 'succeeded')
            self._record_reference(result, example)
            result['automation_suggestions'] = []
            if not self.learning_only:
                result['automation_suggestions'] = [{'pattern_id': p['id'], 'description': p['description'], 'confidence': p['confidence'], 'sample_count': p['sample_count']} for p in eligible]
                with self.db.connect() as con:
                    for item in result['automation_suggestions']:
                        exists = con.execute("SELECT 1 FROM automation_suggestions WHERE pattern_id=? AND status='proposed'", (item['pattern_id'],)).fetchone()
                        if not exists:
                            con.execute("INSERT INTO automation_suggestions(pattern_id,title,description,evidence_json,confidence,sample_count,status,created_at) VALUES(?,?,?,'[]',?,?,'proposed',?)", (item['pattern_id'], '候选自动化建议', item['description'], item['confidence'], item['sample_count'], datetime.now(timezone.utc).isoformat()))
            with self.db.connect() as con:
                con.execute('INSERT OR REPLACE INTO weekly_summaries(week_start,summary_json,created_at,llm_model,llm_duration_ms) VALUES(?,?,?,?,?)', (week_start.isoformat(), json.dumps(result, ensure_ascii=False), datetime.now(timezone.utc).isoformat(), self.resolved_model(model), duration))
            self._store_variant('weekly', week_start.isoformat(), result, duration, model)
            await self._propose_questions(result, 'weekly', week_start.isoformat(), model)
            await self._queue_distillation('weekly', week_start.isoformat(), model)
            job.update({'status': result.get('generation_status') or 'succeeded', 'finished_at': datetime.now(timezone.utc).isoformat(), 'duration_ms': duration})
            self.db.set_state('weekly_job_status', json.dumps(job, ensure_ascii=False, separators=(',', ':')))
            return result
        except Exception as error:
            job.update({'status': 'failed', 'error_type': type(error).__name__, 'finished_at': datetime.now(timezone.utc).isoformat()})
            self.db.set_state('weekly_job_status', json.dumps(job, ensure_ascii=False, separators=(',', ':')))
            raise
