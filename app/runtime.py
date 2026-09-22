import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from .behavior_analyzer import BehaviorAnalyzer
from .anomaly_detector import AnomalyDetector
from .config import settings
from .cooccurrence_analyzer import CooccurrenceAnalyzer
from .database import Database
from .event_aggregator import EventAggregator
from .event_filter import EventFilter
from .ha_client import HAClient
from .memory_manager import MemoryManager
from .model_gateway import ModelGateway
from .ollama_client import OllamaClient
from .provider_settings import ProviderSettings
from .question_agent import QuestionAgent
from .scheduler import make_scheduler
from .segment_memory import SegmentMemory
from .sleep_tracker import SleepTracker
from .arrival_tracker import ArrivalTracker
from .session_manager import SessionManager
from .web import REGENERATION_REGISTRY, create_web_router, run_regeneration_worker

def configure_logging():
    settings.log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = TimedRotatingFileHandler(settings.log_path, when='midnight', backupCount=30, encoding='utf-8')
    logging.basicConfig(level=logging.INFO, handlers=[handler, logging.StreamHandler()], format='%(asctime)s %(levelname)s %(name)s %(message)s')
configure_logging()
logger = logging.getLogger(__name__)
db = Database(settings.database_path)
if not db.scalar("SELECT value FROM system_state WHERE key='collection_started_at'"):
    db.set_state('collection_started_at', datetime.now(timezone.utc).isoformat())
event_filter = EventFilter(settings.config_path, settings.presence_debounce_seconds, settings.home_ai_entity_profile_path, settings.home_ai_entity_profile_enabled, settings.opening_debounce_seconds)
aggregator = EventAggregator(db, settings.correction_window_seconds)
_noise_entities = [name.strip() for name in settings.presence_noise_entities.split(',') if name.strip()]
_non_occupancy_entities = [name.strip() for name in settings.presence_non_occupancy_entities.split(',') if name.strip()]
sessions = SessionManager(db, noise_entities=_noise_entities, non_occupancy_entities=_non_occupancy_entities)
sessions.close_stale_open_sessions()

def _release_stale_ai_jobs(database):
    for key in ('period_job_status', 'daily_job_status', 'weekly_job_status', 'distill_job_status', 'question_job_status', 'segment_job_status'):
        raw = database.scalar('SELECT value FROM system_state WHERE key=?', (key,))
        if not raw:
            continue
        try:
            job = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(job, dict) or job.get('status') != 'generating':
            continue
        job['status'] = 'interrupted'
        job['error_type'] = 'AppRestart'
        job['finished_at'] = datetime.now(timezone.utc).isoformat()
        database.set_state(key, json.dumps(job, ensure_ascii=False, separators=(',', ':')))
        logger.info('released stale generating job key=%s', key)
_release_stale_ai_jobs(db)
local_model = OllamaClient(settings.ollama_url, settings.ollama_model, settings.ollama_timeout_seconds, temperature=settings.model_temperature, context_tokens=settings.model_context_tokens)
provider_settings = ProviderSettings(db, settings.database_path.parent, settings.ollama_timeout_seconds)
if settings.third_party_enabled and (not provider_settings.read()['enabled']):
    provider_settings.save(True, settings.third_party_base_url, [name.strip() for name in settings.third_party_models.split(',') if name.strip()], settings.third_party_default_model, api_key=settings.third_party_api_key or None)
ollama = ModelGateway(local_model, provider_settings.client(), provider_settings.automatic_model())
if ollama.remote is not None:
    logging.getLogger(__name__).info('third-party model provider enabled models=%s', len(ollama.remote_models))
memory = MemoryManager(db, settings.min_pattern_samples)
anomalies = AnomalyDetector(db)
cooccurrence = CooccurrenceAnalyzer.from_path(db, settings.config_path, settings.timezone, gated_presence_entities=_noise_entities + _non_occupancy_entities) if settings.cooccurrence_enabled else None
questions = QuestionAgent(db, ollama, settings=settings)
analyzer = BehaviorAnalyzer(db, ollama, memory, anomalies, settings.learning_days, settings.learning_only, settings.timezone, cooccurrence=cooccurrence, questions=questions, context_tokens=settings.model_context_tokens, output_reserve_tokens=settings.model_output_reserve_tokens, prompt_reserve_tokens=settings.model_prompt_reserve_tokens, distill_context_tokens=settings.distill_context_tokens, remote_payload_token_budget=settings.remote_payload_token_budget)
prompt_path = settings.config_path.parent / 'prompts' / 'system.txt'
db.set_state('system_prompt_sha256', hashlib.sha256(prompt_path.read_bytes()).hexdigest())
sleep_tracker = SleepTracker(db, settings.sleep_curtain_entity, [name.strip() for name in settings.sleep_bed_entities.split(',') if name.strip()], room=settings.sleep_room)
arrival_tracker = ArrivalTracker(db, settings.arrival_outside_entity, settings.arrival_entry_entity, pair_window_seconds=settings.arrival_pair_window_seconds, entry_door_entity=getattr(settings, 'arrival_entry_door_entity', '') or None, short_trip_seconds=getattr(settings, 'arrival_short_trip_seconds', 60))
ha = HAClient(settings.ha_url, settings.ha_token, event_filter, aggregator, sessions, db, sleep_tracker=sleep_tracker, arrival_tracker=arrival_tracker)
analyzer.ha = ha
analyzer.event_filter = event_filter
analyzer.presence_noise_entities = frozenset((name.strip() for name in settings.presence_noise_entities.split(',') if name.strip()))
analyzer.presence_non_occupancy_entities = frozenset((name.strip() for name in settings.presence_non_occupancy_entities.split(',') if name.strip()))
segment_memory = SegmentMemory(db, analyzer, settings, model_lock=analyzer._model_lock)
analyzer.segment_memory = segment_memory
scheduler = make_scheduler(settings, analyzer, aggregator, db, segment_memory=segment_memory)

async def queue_distillation(scope, key, teacher_model):
    queue_key = f'{scope}:{key}'
    added = REGENERATION_REGISTRY.add('distill', queue_key, queue_key, f'{teacher_model} -> local')
    if added:
        logger.info('distillation queued after a third-party report scope=%s', scope)
analyzer.on_remote_report = queue_distillation

def log_background_failure(task):
    if task.cancelled():
        return
    try:
        task.result()
    except Exception:
        logger.exception('Background period catch-up failed')

async def backfill_missing_periods(analyzer):
    pending = analyzer.pending_period_bounds()
    if not pending:
        return
    logger.info('period catch-up pending_windows=%s', len(pending))
    for _, end in pending:
        try:
            await analyzer.period(end=end, source='catchup')
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error('period catch-up failed error=%s', type(error).__name__)

@asynccontextmanager
async def lifespan(app):
    if not settings.lares_enable_network or not settings.ha_token or settings.ha_token.startswith('replace_'):
        logging.getLogger(__name__).error('HA_TOKEN is missing; collector will not start')
        task = None
    else:
        task = asyncio.create_task(ha.run())
    if settings.lares_enable_network and settings.lares_enable_schedule:
        scheduler.start()
    catchup_task = asyncio.create_task(backfill_missing_periods(analyzer)) if settings.lares_enable_network and settings.lares_enable_schedule else None
    if catchup_task:
        catchup_task.add_done_callback(log_background_failure)
    regeneration_worker = asyncio.create_task(run_regeneration_worker(analyzer, REGENERATION_REGISTRY))
    regeneration_worker.add_done_callback(log_background_failure)
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)
    if task:
        task.cancel()
    if catchup_task and (not catchup_task.done()):
        catchup_task.cancel()
    if not regeneration_worker.done():
        regeneration_worker.cancel()
app = FastAPI(title='LARES local preview', lifespan=lifespan)
app.mount('/static', StaticFiles(directory=Path(__file__).parent / 'static'), name='static')

@app.get('/health')
async def health():
    database_ok = True
    try:
        db.scalar('SELECT 1')
    except Exception:
        database_ok = False
    ollama_ok = await ollama.healthy()
    status = 'ok' if ha.connected and ollama_ok and database_ok else 'degraded'
    return {'status': status, 'ha': ha.connected, 'ollama': ollama_ok, 'database': database_ok}
app.include_router(create_web_router(db, memory, ha, ollama, event_filter, settings, cooccurrence=cooccurrence, analyzer=analyzer, provider_settings=provider_settings, questions=questions))

from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse
from urllib.parse import urlsplit
app.add_middleware(TrustedHostMiddleware, allowed_hosts=['localhost', '127.0.0.1', '[::1]', 'testserver'])

@app.middleware('http')
async def local_request_boundary(request, call_next):
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        origin = request.headers.get('origin')
        if origin:
            parsed = urlsplit(origin)
            if parsed.scheme not in ('http', 'https') or parsed.netloc != request.headers.get('host'):
                return JSONResponse({'detail': 'Cross-origin write blocked'}, status_code=403)
        elif request.headers.get('x-lares-client') != 'local':
            return JSONResponse({'detail': 'Same-origin request or X-LARES-Client: local required'}, status_code=403)
    return await call_next(request)
