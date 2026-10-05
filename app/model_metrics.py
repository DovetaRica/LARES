import asyncio
import contextvars
import hashlib
import logging
import time
from datetime import datetime, timezone
from .llm_schema import compact_json, conservative_token_estimate
trace = contextvars.ContextVar('model_call_trace', default=None)
queue_wait = contextvars.ContextVar('model_queue_wait', default=0)
log = logging.getLogger(__name__)

class ModelCallLock(asyncio.Lock):

    async def __aenter__(self):
        started = time.perf_counter()
        await self.acquire()
        self._wait_token = queue_wait.set(round((time.perf_counter() - started) * 1000))
        return self

    async def __aexit__(self, *args):
        queue_wait.reset(self._wait_token)
        self.release()

async def tracked_call(db, method, payload, prompt, model, call):
    if db is None:
        return await call()
    scope = payload.get('analysis_scope') or payload.get('scope') or ('weekly' if 'week_start' in payload else 'report')
    if isinstance(scope, dict):
        scope = scope.get('type', 'report')
    if method == 'analyze_questions':
        scope = 'questions'
    elif method == 'analyze_segment':
        scope = 'segment'
    elif 'own_report' in payload:
        scope = 'distill'
    key = str(payload.get('period_start') or payload.get('segment_start') or payload.get('week_start') or payload.get('date') or '')
    data = {'scope': scope if scope in ('period', 'daily', 'weekly', 'segment', 'questions', 'distill') else 'report', 'window_hash': hashlib.sha256(key.encode()).hexdigest()[:24], 'model': str(model)[:120], 'started_at': datetime.now(timezone.utc).isoformat(), 'attempts': 0, 'rejections': 0, 'queue_ms': queue_wait.get(), 'prompt_tokens': None, 'output_tokens': None, 'input_tokens_estimate': conservative_token_estimate(prompt) + conservative_token_estimate(compact_json(payload)), 'outcome': 'failed', 'error_type': None, 'output_items': 0}
    queue_wait.set(0)
    token = trace.set(data)
    started = time.perf_counter()
    try:
        result = await call()
        data['outcome'] = 'succeeded'
        value = result[0] if isinstance(result, tuple) else result
        data['output_items'] = len(value) if isinstance(value, list) else 1
        return result
    except asyncio.CancelledError:
        data['outcome'], data['error_type'] = ('interrupted', 'CancelledError')
        raise
    except Exception as error:
        data['error_type'] = type(error).__name__
        raise
    finally:
        data['call_ms'] = round((time.perf_counter() - started) * 1000)
        trace.reset(token)
        try:
            columns = ','.join(data)
            with db.connect() as con:
                con.execute(f"INSERT INTO model_calls({columns}) VALUES({','.join(('?' for _ in data))})", tuple(data.values()))
        except Exception as error:
            log.warning('model accounting unavailable error=%s', type(error).__name__)

def note_attempt(metadata=None, rejected=False):
    value = trace.get()
    if value is None:
        return
    if rejected:
        value['rejections'] += 1
    elif metadata is None:
        value['attempts'] += 1
    elif isinstance(metadata, dict):
        usage = metadata.get('usage') or {}
        for field, raw in [('prompt_tokens', metadata.get('prompt_eval_count', usage.get('prompt_tokens'))), ('output_tokens', metadata.get('eval_count', usage.get('completion_tokens')))]:
            if isinstance(raw, int) and (not isinstance(raw, bool)) and (raw >= 0):
                value[field] = (value[field] or 0) + raw
