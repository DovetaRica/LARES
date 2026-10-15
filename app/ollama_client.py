import asyncio
import copy
import json
import logging
import time
import httpx
from .network import AsyncClient, check_endpoint
from .llm_schema import MODEL_CONTEXT_TOKENS, MODEL_OUTPUT_RESERVE_TOKENS, MODEL_REQUEST_TIMEOUT_SECONDS, MODEL_CALL_WALL_SECONDS, SEGMENT_ATTEMPTS, SEGMENT_CALL_WALL_SECONDS, SEGMENT_NUM_PREDICT, compact_json, report_response_schema, validate_segment_summary, conservative_token_estimate, ollama_input_budget
from .model_transport import RETRY_INSTRUCTION, validated_model_report
from .model_metrics import note_attempt, model_phase, tracked_request
from .report_review import REVIEW_PROMPT, build_review_payload, review_response_schema, validate_review_response
from .proactive_contract import SCOUT_PROMPT, SCOUT_PLAN_PROMPT, scout_schema, scout_plan_schema, validate_scout_response, validate_scout_plan
logger = logging.getLogger(__name__)

class OllamaClient:
    provider = 'ollama'

    def __init__(self, url, model, timeout_seconds=900, temperature=0.3, context_tokens=None, review_reports=False):
        self.url = url.rstrip('/')
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.temperature = temperature
        self.context_tokens = context_tokens
        self.review_reports = review_reports
        self.network_policy = None

    async def healthy(self):
        try:
            async with AsyncClient(network_policy=self.network_policy, timeout=5) as client:
                r = await client.get(f'{self.url}/api/tags')
                return r.is_success
        except Exception:
            return False

    async def models(self):
        try:
            async with AsyncClient(network_policy=self.network_policy, timeout=2) as client:
                response = await client.get(f'{self.url}/api/tags')
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError, PermissionError):
            return []
        entries = data.get('models') if isinstance(data, dict) else None
        names = {str(item.get('model') or item.get('name') or '').strip() for item in entries or [] if isinstance(item, dict)}
        return sorted((name for name in names if name))

    async def analyze(self, system_prompt, payload, model=None, context_tokens=None):
        started = time.perf_counter()
        payload = copy.deepcopy(payload)
        serialized_payload = compact_json(payload)
        chosen = model or self.model
        num_ctx = int(context_tokens or self.context_tokens or MODEL_CONTEXT_TOKENS)
        timeout = min(self.timeout_seconds, MODEL_REQUEST_TIMEOUT_SECONDS)
        with model_phase('draft'):
            result, metadata = await validated_model_report(lambda prompt: self._call_model(prompt, serialized_payload, chosen, timeout, num_ctx, report_response_schema()), system_prompt, payload)
        if self.review_reports and context_tokens is None and (payload.get('analysis_scope') in ('period', 'daily', 'weekly')):
            result, metadata = await self._review_report(result, metadata, system_prompt, payload, chosen, timeout, num_ctx, MODEL_CALL_WALL_SECONDS - (time.perf_counter() - started))
        return (result, int((time.perf_counter() - started) * 1000), metadata)

    async def _review_report(self, draft, metadata, system_prompt, payload, chosen, timeout, num_ctx, wall_remaining):
        review = {'status': 'skipped', 'issues_found': 0}
        deadline = time.perf_counter() + wall_remaining - 5
        schema = review_response_schema()
        budget = ollama_input_budget(num_ctx, 512, REVIEW_PROMPT)
        request = build_review_payload(draft, payload, budget)
        if request is None:
            review['reason'] = 'review_input_budget'
        if deadline - time.perf_counter() < 30:
            request = None
            review['status'] = 'unavailable'
            review['reason'] = 'time_budget'
        if request is not None:
            phase = 'critic'
            try:
                note_attempt()
                with model_phase('critic'):
                    content, usage = await self._call_model(REVIEW_PROMPT, compact_json(request), chosen, min(timeout, 450, deadline - time.perf_counter()), num_ctx, schema, num_predict=512)
                note_attempt(usage)
                critique = validate_review_response(json.loads(content))
                review['issues_found'] = len(critique['issues'])
                if critique['decision'] == 'accept':
                    review['status'] = 'passed'
                else:
                    repair = {'draft': draft, 'review': critique, 'evidence': request['evidence']}
                    instruction = '根据原始程序证据修订草稿，逐项检查复核意见；复核意见也可能错误，无证据的意见不得采纳。输出完整报告JSON，不复制历史范例事实。'
                    repair_prompt = system_prompt + '\n' + instruction
                    serialized_repair = compact_json(repair)
                    repair_tokens = conservative_token_estimate(serialized_repair)
                    minimum_output = min(MODEL_OUTPUT_RESERVE_TOKENS, max(1024, 2 * conservative_token_estimate(compact_json(draft)) + 512))
                    output_limit = max(0, min(MODEL_OUTPUT_RESERVE_TOKENS, ollama_input_budget(num_ctx, 0, repair_prompt) - repair_tokens))
                    review['repair_output_limit'] = output_limit
                    if deadline - time.perf_counter() < 30:
                        review['status'] = 'unresolved'
                        review['reason'] = 'time_budget'
                    elif output_limit < minimum_output:
                        review['status'] = 'unresolved'
                        review['reason'] = 'repair_output_budget'
                    else:
                        phase = 'repair'
                        with model_phase('repair'):
                            result, metadata = await validated_model_report(lambda prompt: self._call_model(prompt, serialized_repair, chosen, min(timeout, deadline - time.perf_counter()), num_ctx, report_response_schema(), num_predict=output_limit), repair_prompt, payload, attempts=1)
                        if metadata.get('done_reason') == 'length':
                            note_attempt(rejected=True)
                            review.update(status='unresolved', reason='repair_output_limit')
                        else:
                            draft = result
                            review['status'] = 'revised'
            except (httpx.HTTPError, ValueError, TypeError, KeyError):
                if phase == 'critic':
                    note_attempt(rejected=True)
                review['status'] = 'unresolved' if review['issues_found'] else 'unavailable'
                review['reason'] = 'repair_failed' if phase == 'repair' else 'critic_failed'
                logger.warning('report review incomplete status=%s', review['status'])
        draft['model_review'] = review
        metadata = dict(metadata, report_review=review)
        return (draft, metadata)

    async def _call_model(self, prompt, serialized_payload, chosen, timeout, num_ctx, response_schema, num_predict=None):
        body = {'model': chosen, 'stream': False, 'format': response_schema, 'think': False, 'options': {'temperature': self.temperature, 'num_ctx': num_ctx, 'num_predict': int(num_predict or MODEL_OUTPUT_RESERVE_TOKENS)}, 'messages': [{'role': 'system', 'content': prompt}, {'role': 'user', 'content': serialized_payload}]}
        with tracked_request(body) as metadata:
            check_endpoint(f'{self.url}/api/chat', self.network_policy)
            async with AsyncClient(network_policy=self.network_policy, timeout=timeout) as client:
                metadata['_http_attempted'] = True
                response = await client.post(f'{self.url}/api/chat', json=body)
                response.raise_for_status()
                data = response.json()
                content = data['message']['content']
            metadata.update(data)
            metadata['_http_attempted'] = True
        return (content, data)

    async def select_observation_leads(self, payload):
        prompt = SCOUT_PROMPT
        if 'tool_result' in payload:
            from .proactive_contract import SCOUT_FACT_PROMPT
            prompt = SCOUT_FACT_PROMPT
        ids = [item['evidence_id'] for item in payload.get('evidence', [])]
        return await self._observation_response(payload, prompt, scout_schema(ids), 'scout', validate_scout_response)

    async def plan_observation(self, payload):
        allowed = payload.get('query_allowed') is True
        return await self._observation_response(payload, SCOUT_PLAN_PROMPT, scout_plan_schema(allowed, payload.get('query_required') is True, [item['evidence_id'] for item in payload.get('evidence', [])]), 'scout_plan', lambda result, evidence: validate_scout_plan(result, evidence, allowed))

    async def _observation_response(self, payload, prompt, schema, phase, validator):
        payload = copy.deepcopy(payload)
        context = min(int(self.context_tokens or MODEL_CONTEXT_TOKENS), 8192)
        serialized = compact_json(payload)
        if conservative_token_estimate(serialized) > ollama_input_budget(context, 512, prompt):
            raise ValueError('idle selection input exceeds model context')
        note_attempt()
        with model_phase(phase):
            content, usage = await asyncio.wait_for(self._call_model(prompt, serialized, self.model, min(self.timeout_seconds, 180), context, schema, num_predict=512), timeout=180)
        note_attempt(usage)
        try:
            if usage.get('done_reason') == 'length':
                raise ValueError('idle response truncated')
            return validator(json.loads(content), payload)
        except ValueError:
            note_attempt(rejected=True)
            raise

    async def analyze_questions(self, system_prompt, payload, model=None, context_tokens=None):
        payload = copy.deepcopy(payload)
        from .question_grounding import grounded_schema, validate_grounded_response
        num_ctx = int(context_tokens or self.context_tokens or MODEL_CONTEXT_TOKENS)
        serialized = compact_json(payload)
        if conservative_token_estimate(serialized) > ollama_input_budget(num_ctx, MODEL_OUTPUT_RESERVE_TOKENS, system_prompt, retry_instruction=RETRY_INSTRUCTION):
            raise ValueError('question input exceeds model context')
        with model_phase('questions'):
            return await validated_model_report(lambda prompt: self._call_model(prompt, serialized, model or self.model, min(self.timeout_seconds, MODEL_REQUEST_TIMEOUT_SECONDS), num_ctx, grounded_schema(payload)), system_prompt, payload, validator=lambda value: validate_grounded_response(value, payload), check_identity=False)

    async def analyze_segment(self, system_prompt, payload, model=None, context_tokens=None, wall_seconds=None):
        wall = int(wall_seconds or SEGMENT_CALL_WALL_SECONDS)
        payload = copy.deepcopy(payload)
        num_ctx = int(context_tokens or self.context_tokens or MODEL_CONTEXT_TOKENS)
        with model_phase('segment'):
            return await validated_model_report(lambda prompt: self._call_model(prompt, compact_json(payload), model or self.model, min(self.timeout_seconds, wall), num_ctx, 'json', num_predict=SEGMENT_NUM_PREDICT), system_prompt, payload, validator=validate_segment_summary, check_identity=True, attempts=SEGMENT_ATTEMPTS)
