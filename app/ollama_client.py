import logging
import time
import httpx
from .network import AsyncClient
from .llm_schema import MODEL_CONTEXT_TOKENS, MODEL_OUTPUT_RESERVE_TOKENS, MODEL_REQUEST_TIMEOUT_SECONDS, SEGMENT_ATTEMPTS, SEGMENT_CALL_WALL_SECONDS, SEGMENT_NUM_PREDICT, compact_json, question_response_schema, report_response_schema, validate_question_response, validate_segment_summary
from .model_transport import validated_model_report
logger = logging.getLogger(__name__)

class OllamaClient:
    provider = 'ollama'

    def __init__(self, url, model, timeout_seconds=900, temperature=0.3, context_tokens=None):
        self.url = url.rstrip('/')
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.temperature = temperature
        self.context_tokens = context_tokens

    async def healthy(self):
        try:
            async with AsyncClient(timeout=5) as client:
                r = await client.get(f'{self.url}/api/tags')
                return r.is_success
        except Exception:
            return False

    async def models(self):
        try:
            async with AsyncClient(timeout=2) as client:
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
        serialized_payload = compact_json(payload)
        chosen = model or self.model
        num_ctx = int(context_tokens or self.context_tokens or MODEL_CONTEXT_TOKENS)
        timeout = min(self.timeout_seconds, MODEL_REQUEST_TIMEOUT_SECONDS)
        result, metadata = await validated_model_report(lambda prompt: self._call_model(prompt, serialized_payload, chosen, timeout, num_ctx, report_response_schema()), system_prompt, payload)
        return (result, int((time.perf_counter() - started) * 1000), metadata)

    async def _call_model(self, prompt, serialized_payload, chosen, timeout, num_ctx, response_schema, num_predict=None):
        body = {'model': chosen, 'stream': False, 'format': response_schema, 'think': False, 'options': {'temperature': self.temperature, 'num_ctx': num_ctx, 'num_predict': int(num_predict or MODEL_OUTPUT_RESERVE_TOKENS)}, 'messages': [{'role': 'system', 'content': prompt}, {'role': 'user', 'content': serialized_payload}]}
        async with AsyncClient(timeout=timeout) as client:
            response = await client.post(f'{self.url}/api/chat', json=body)
            response.raise_for_status()
            data = response.json()
        return (data['message']['content'], data)

    async def analyze_questions(self, system_prompt, payload, model=None, context_tokens=None):
        num_ctx = int(context_tokens or self.context_tokens or MODEL_CONTEXT_TOKENS)
        return await validated_model_report(lambda prompt: self._call_model(prompt, compact_json(payload), model or self.model, min(self.timeout_seconds, MODEL_REQUEST_TIMEOUT_SECONDS), num_ctx, question_response_schema()), system_prompt, payload, validator=validate_question_response, check_identity=False)

    async def analyze_segment(self, system_prompt, payload, model=None, context_tokens=None, wall_seconds=None):
        wall = int(wall_seconds or SEGMENT_CALL_WALL_SECONDS)
        num_ctx = int(context_tokens or self.context_tokens or MODEL_CONTEXT_TOKENS)
        return await validated_model_report(lambda prompt: self._call_model(prompt, compact_json(payload), model or self.model, min(self.timeout_seconds, wall), num_ctx, 'json', num_predict=SEGMENT_NUM_PREDICT), system_prompt, payload, validator=validate_segment_summary, check_identity=True, attempts=SEGMENT_ATTEMPTS)
