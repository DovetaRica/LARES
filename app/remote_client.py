import logging
import time
import httpx
from .network import AsyncClient
from .llm_schema import MODEL_REQUEST_TIMEOUT_SECONDS, compact_json, validate_question_response, validate_segment_summary
from .model_transport import validated_model_report
logger = logging.getLogger(__name__)
REMOTE_OUTPUT_TOKENS = 16384

class OpenAICompatibleClient:
    provider = 'third-party'

    def __init__(self, base_url, api_key, models, model=None, timeout_seconds=900, transport=None):
        self.base_url = (base_url or '').rstrip('/')
        self.api_key = api_key or ''
        self.installed = [str(name).strip() for name in models if str(name).strip()]
        self.model = model or (self.installed[0] if self.installed else '')
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    @property
    def configured(self):
        return bool(self.base_url and self.api_key and self.installed)

    def serves(self, model):
        return bool(model) and model in self.installed

    async def models(self):
        return list(self.installed)

    async def healthy(self):
        if not self.configured:
            return False
        try:
            async with AsyncClient(timeout=5, transport=self.transport) as client:
                response = await client.get(f'{self.base_url}/models', headers={'Authorization': f'Bearer {self.api_key}'})
                return response.is_success
        except Exception:
            return False

    async def analyze(self, system_prompt, payload, model=None, context_tokens=None):
        started = time.perf_counter()
        chosen = model or self.model
        serialized = compact_json(payload)
        result, metadata = await validated_model_report(lambda prompt: self._call_model(prompt, serialized, chosen), system_prompt, payload)
        return (result, int((time.perf_counter() - started) * 1000), metadata)

    async def analyze_questions(self, system_prompt, payload, model=None):
        return await validated_model_report(lambda prompt: self._call_model(prompt, compact_json(payload), model or self.model), system_prompt, payload, validator=validate_question_response, check_identity=False)

    async def analyze_segment(self, system_prompt, payload, model=None, context_tokens=None):
        return await validated_model_report(lambda prompt: self._call_model(prompt, compact_json(payload), model or self.model), system_prompt, payload, validator=validate_segment_summary, check_identity=True)

    async def _call_model(self, prompt, serialized, chosen):
        body = {'model': chosen, 'stream': False, 'temperature': 0.2, 'max_tokens': REMOTE_OUTPUT_TOKENS, 'response_format': {'type': 'json_object'}, 'messages': [{'role': 'system', 'content': prompt}, {'role': 'user', 'content': serialized}]}
        async with AsyncClient(timeout=min(self.timeout_seconds, MODEL_REQUEST_TIMEOUT_SECONDS), transport=self.transport) as client:
            response = await client.post(f'{self.base_url}/chat/completions', json=body, headers={'Authorization': f'Bearer {self.api_key}'})
            response.raise_for_status()
            data = response.json()
        choice = (data.get('choices') or [{}])[0]
        if choice.get('finish_reason') == 'length':
            logger.warning('third-party answer truncated at max_tokens=%s', REMOTE_OUTPUT_TOKENS)
        return ((choice.get('message') or {}).get('content') or '', {'provider': self.provider, 'model': chosen, 'usage': data.get('usage') or {}})
