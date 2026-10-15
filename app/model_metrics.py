import asyncio
import contextvars
import hashlib
import logging
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from .llm_schema import compact_json, conservative_token_estimate, ollama_request_plan
trace = contextvars.ContextVar('model_call_trace', default=None)
queue_wait = contextvars.ContextVar('model_queue_wait', default=0)
request_phase = contextvars.ContextVar('model_request_phase', default='draft')
log = logging.getLogger(__name__)

class ModelCallLock(asyncio.Lock):

    @property
    def has_waiters(self):
        return any((not waiter.done() for waiter in self._waiters or ()))

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
    data = {'scope': scope if scope in ('period', 'daily', 'weekly', 'segment', 'questions', 'distill', 'proactive') else 'report', 'window_hash': hashlib.sha256(key.encode()).hexdigest()[:24], 'model': str(model)[:120], 'started_at': datetime.now(timezone.utc).isoformat(), 'attempts': 0, 'rejections': 0, 'queue_ms': queue_wait.get(), 'prompt_tokens': None, 'output_tokens': None, 'input_tokens_estimate': conservative_token_estimate(prompt) + conservative_token_estimate(compact_json(payload)), 'outcome': 'failed', 'error_type': None, 'output_items': 0, 'stages_json': []}
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
        if data['stages_json']:
            data['attempts'] = sum((stage['http_attempted'] for stage in data['stages_json']))
        data['stages_json'] = compact_json(data['stages_json'])
        try:
            columns = ','.join(data)
            with db.connect() as con:
                con.execute(f'INSERT INTO model_calls({columns}) VALUES({','.join(('?' for _ in data))})', tuple(data.values()))
        except Exception as error:
            log.warning('model accounting unavailable error=%s', type(error).__name__)

@contextmanager
def model_phase(name):
    token = request_phase.set(name)
    try:
        yield
    finally:
        request_phase.reset(token)

@contextmanager
def tracked_request(body):
    prompt, payload = (message['content'] for message in body['messages'])
    options = body['options']
    record = ollama_request_plan(prompt, payload, options['num_ctx'], options['num_predict'])
    phase = request_phase.get()
    record.update(phase=phase if phase in ('draft', 'critic', 'repair', 'questions', 'segment', 'scout', 'scout_plan') else 'draft', prompt_hash=hashlib.sha256(prompt.encode()).hexdigest(), payload_hash=hashlib.sha256(payload.encode()).hexdigest(), outcome='failed', error_type=None, prompt_tokens=None, output_tokens=None)
    started = time.perf_counter()
    metadata = {}
    try:
        if record['budget_exceeded']:
            record['outcome'] = 'input_budget'
            raise ValueError('final model messages exceed input budget')
        yield metadata
        record['outcome'] = 'received'
        for field, key in [('prompt_tokens', 'prompt_eval_count'), ('output_tokens', 'eval_count')]:
            raw = metadata.get(key)
            if isinstance(raw, int) and (not isinstance(raw, bool)) and (raw >= 0):
                record[field] = raw
        for field, key in [('ollama_total_ms', 'total_duration'), ('ollama_load_ms', 'load_duration'), ('ollama_prompt_eval_ms', 'prompt_eval_duration'), ('ollama_eval_ms', 'eval_duration')]:
            raw = metadata.get(key)
            if isinstance(raw, int) and (not isinstance(raw, bool)) and (0 <= raw <= 2 ** 63 - 1):
                record[field] = round(raw / 1000000, 3)
        reason = metadata.get('done_reason')
        record['done_reason'] = reason if reason in ('stop', 'length', 'load') else 'unknown'
    except asyncio.CancelledError:
        record.update(outcome='interrupted', error_type='CancelledError')
        raise
    except Exception as error:
        record['error_type'] = type(error).__name__
        raise
    finally:
        record['http_attempted'] = metadata.get('_http_attempted') is True
        record['call_ms'] = round((time.perf_counter() - started) * 1000)
        current = trace.get()
        if current is not None and len(current['stages_json']) < 16:
            current['stages_json'].append(record)

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
