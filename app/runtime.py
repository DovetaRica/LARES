import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from .anomaly_detector import AnomalyDetector
from .arrival_tracker import ArrivalTracker
from .behavior_analyzer import BehaviorAnalyzer
from .cooccurrence_analyzer import CooccurrenceAnalyzer
from .config import Settings
from .database import Database
from .event_aggregator import EventAggregator
from .event_filter import EventFilter
from .fact_queries import query_facts
from .ha_client import HAClient
from .home_topology import HomeTopology
from .memory_manager import MemoryManager
from .model_gateway import ModelGateway
from .ollama_client import OllamaClient
from .proactive_scout import ProactiveScout
from .provider_settings import ProviderSettings
from .question_agent import QuestionAgent
from .scheduler import make_scheduler
from .segment_memory import SegmentMemory
from .session_manager import SessionManager
from .sleep_tracker import SleepTracker
from .task_queue import RegenerationRegistry
logger = logging.getLogger(__name__)

@dataclass
class Runtime:
    settings: Settings
    db: Database
    event_filter: EventFilter
    aggregator: EventAggregator
    sessions: SessionManager
    memory: MemoryManager
    anomalies: AnomalyDetector
    cooccurrence: CooccurrenceAnalyzer | None
    questions: QuestionAgent
    analyzer: BehaviorAnalyzer
    ollama: ModelGateway
    provider_settings: ProviderSettings
    ha: HAClient
    sleep_tracker: SleepTracker
    arrival_tracker: ArrivalTracker
    segment_memory: SegmentMemory
    proactive_scout: ProactiveScout
    scheduler: object
    registry: RegenerationRegistry

def configure_logging(settings):
    if logging.getLogger().handlers:
        return
    settings.log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = TimedRotatingFileHandler(settings.log_path, when='midnight', backupCount=30, encoding='utf-8')
    logging.basicConfig(level=logging.INFO, handlers=[handler, logging.StreamHandler()], format='%(asctime)s %(levelname)s %(name)s %(message)s')

def _release_stale_ai_jobs(database):
    for key in ('period_job_status', 'daily_job_status', 'weekly_job_status', 'distill_job_status', 'question_job_status', 'segment_job_status', 'proactive_scout_status'):
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

def build_runtime(settings):
    configure_logging(settings)
    db = Database(settings.database_path)
    if not db.scalar("SELECT value FROM system_state WHERE key='collection_started_at'"):
        db.set_state('collection_started_at', datetime.now(timezone.utc).isoformat())
    event_filter = EventFilter(settings.config_path, settings.presence_debounce_seconds, settings.home_ai_entity_profile_path, settings.home_ai_entity_profile_enabled, settings.opening_debounce_seconds)
    aggregator = EventAggregator(db, settings.correction_window_seconds)
    _noise_entities = [name.strip() for name in settings.presence_noise_entities.split(',') if name.strip()]
    _non_occupancy_entities = [name.strip() for name in settings.presence_non_occupancy_entities.split(',') if name.strip()]
    sessions = SessionManager(db, noise_entities=_noise_entities, non_occupancy_entities=_non_occupancy_entities)
    sessions.close_stale_open_sessions()
    _release_stale_ai_jobs(db)
    local_model = OllamaClient(settings.ollama_url, settings.ollama_model, settings.ollama_timeout_seconds, temperature=settings.model_temperature, context_tokens=settings.model_context_tokens, review_reports=settings.report_review_enabled)
    local_model.network_policy = settings
    provider_settings = ProviderSettings(db, settings.database_path.parent, settings.ollama_timeout_seconds)
    provider_settings.network_policy = settings
    if settings.third_party_enabled and (not provider_settings.read()['enabled']):
        provider_settings.save(True, settings.third_party_base_url, [name.strip() for name in settings.third_party_models.split(',') if name.strip()], settings.third_party_default_model, api_key=settings.third_party_api_key or None)
    ollama = ModelGateway(local_model, provider_settings.client(), provider_settings.automatic_model())
    ollama.database = db
    if ollama.remote is not None:
        ollama.remote.network_policy = settings
    if ollama.remote is not None:
        logger.info('third-party model provider enabled models=%s', len(ollama.remote_models))
    memory = MemoryManager(db, settings.min_pattern_samples, _noise_entities, _non_occupancy_entities)
    anomalies = AnomalyDetector(db)
    cooccurrence = CooccurrenceAnalyzer.from_path(db, settings.config_path, settings.timezone, gated_presence_entities=_noise_entities + _non_occupancy_entities) if settings.cooccurrence_enabled else None
    questions = QuestionAgent(db, ollama, settings=settings, fact_query=query_facts)
    analyzer = BehaviorAnalyzer(db, ollama, memory, anomalies, settings.learning_days, settings.learning_only, settings.timezone, cooccurrence=cooccurrence, questions=questions, context_tokens=settings.model_context_tokens, output_reserve_tokens=settings.model_output_reserve_tokens, prompt_reserve_tokens=settings.model_prompt_reserve_tokens, distill_context_tokens=settings.distill_context_tokens, remote_payload_token_budget=settings.remote_payload_token_budget, automatic_distillation=settings.automatic_distillation)
    prompt_path = Path(__file__).parent / 'resources' / 'prompts' / 'system.txt'
    db.set_state('system_prompt_sha256', hashlib.sha256(prompt_path.read_bytes()).hexdigest())
    sleep_tracker = SleepTracker(db, settings.sleep_curtain_entity, [name.strip() for name in settings.sleep_bed_entities.split(',') if name.strip()], room=settings.sleep_room)
    arrival_tracker = ArrivalTracker(db, settings.arrival_outside_entity, settings.arrival_entry_entity, pair_window_seconds=settings.arrival_pair_window_seconds, entry_door_entity=getattr(settings, 'arrival_entry_door_entity', '') or None, short_trip_seconds=getattr(settings, 'arrival_short_trip_seconds', 60))
    ha = HAClient(settings.ha_url, settings.ha_token, event_filter, aggregator, sessions, db, sleep_tracker=sleep_tracker, arrival_tracker=arrival_tracker)
    ha.network_policy = settings
    analyzer.ha = ha
    analyzer.event_filter = event_filter
    analyzer.home_topology = HomeTopology.from_path(settings.config_path.parent / 'home_topology.json')
    analyzer.presence_noise_entities = frozenset((name.strip() for name in settings.presence_noise_entities.split(',') if name.strip()))
    analyzer.presence_non_occupancy_entities = frozenset((name.strip() for name in settings.presence_non_occupancy_entities.split(',') if name.strip()))
    segment_memory = SegmentMemory(db, analyzer, settings, model_lock=analyzer._model_lock)
    analyzer.segment_memory = segment_memory
    registry = RegenerationRegistry(db)
    proactive_scout = ProactiveScout(db, analyzer, ollama.local, registry, settings)
    scheduler = make_scheduler(settings, analyzer, aggregator, db, segment_memory=segment_memory, arrival_tracker=arrival_tracker, proactive_scout=proactive_scout)

    async def queue_distillation(scope, key, teacher_model):
        queue_key = f'{scope}:{key}'
        added = registry.add('distill', queue_key, queue_key, f'{teacher_model} -> local')
        if added:
            logger.info('distillation queued after a third-party report scope=%s', scope)
    analyzer.on_remote_report = queue_distillation
    return Runtime(settings=settings, db=db, event_filter=event_filter, aggregator=aggregator, sessions=sessions, memory=memory, anomalies=anomalies, cooccurrence=cooccurrence, questions=questions, analyzer=analyzer, ollama=ollama, provider_settings=provider_settings, ha=ha, sleep_tracker=sleep_tracker, arrival_tracker=arrival_tracker, segment_memory=segment_memory, proactive_scout=proactive_scout, scheduler=scheduler, registry=registry)
