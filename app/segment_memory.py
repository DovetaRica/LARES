import asyncio
import copy
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from .critical_events import detect as detect_critical
from .event_preprocessor import _parse_ts, preprocess
from .event_taxonomy import classify as classify_event
from .llm_schema import RetryableModelFormatError, compact_json, conservative_token_estimate, segment_call_wall_seconds, validate_segment_summary
from .telemetry_projection import PROTECTED_ENTITIES_KEY, STABLE_ENTITIES_KEY, project_telemetry, protected_telemetry_entities, stable_telemetry_entities
from .time_display import chinese_label
logger = logging.getLogger(__name__)

def _program_label(head, segment, tail):
    start = chinese_label(segment['period_start']) or str(segment['period_start'] or '')
    end = chinese_label(segment['period_end']) or str(segment['period_end'] or '')
    return f'{head}{start} 至 {end}，{tail}'
CURSOR_KEY = 'segment_cursor_event_id'
PROMPT_VERSION = 'segment_prompt_v1'
SEGMENT_PROMPT_PATH = str(Path(__file__).parent / 'resources/prompts/segment.txt')
SEGMENT_STATE_MAX_ENTITIES = 24
SEGMENT_CATALOG_MAX_ENTITIES = 24
SEGMENT_EVENT_BUDGET_FRACTION = 0.5

class SegmentRangeTooLarge(RuntimeError):

    def __init__(self, message='segment range exceeds payload hard limit'):
        super().__init__(message)

class SegmentMemory:

    def __init__(self, database, analyzer, settings, model_lock=None):
        self.db = database
        self.analyzer = analyzer
        self.enabled = bool(getattr(settings, 'segments_enabled', True))
        mode = str(getattr(settings, 'segments_ai_mode', '') or '').strip().lower()
        if mode not in ('off', 'hybrid', 'full'):
            mode = 'full' if bool(getattr(settings, 'segments_ai', False)) else 'off'
        self.ai_mode = mode
        self.use_ai = mode != 'off'
        self.max_events = int(settings.segment_max_events)
        self.max_tokens = int(settings.segment_max_tokens)
        self.max_minutes = int(settings.segment_max_minutes)
        self.min_events = int(settings.segment_min_events)
        self.retry_limit = int(getattr(settings, 'segment_retry_limit', 3))
        self.recent_detail_minutes = int(getattr(settings, 'recent_detail_minutes', 60))
        self.segment_payload_hard_limit = int(getattr(settings, 'segment_payload_hard_limit', 60000))
        self.prompt = Path(SEGMENT_PROMPT_PATH).read_text(encoding='utf-8')
        self._model_lock = model_lock or asyncio.Lock()
        self._run_lock = asyncio.Lock()

    def _cursor(self):
        value = self.db.scalar('SELECT value FROM system_state WHERE key=?', (CURSOR_KEY,))
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def buffer_state(self):
        cursor = self._cursor()
        counts = self._counts()
        state = {'cursor': cursor, 'pending_events': 0, 'first_pending_ts': None, 'enabled': self.enabled, 'ai': self.use_ai, 'ai_mode': self.ai_mode, 'thresholds': {'events': self.max_events, 'tokens': self.max_tokens, 'minutes': self.max_minutes, 'min_events': self.min_events}, 'segments': counts}
        if cursor is not None:
            row = self.db.rows('SELECT COUNT(*) n, MIN(ts) first_ts FROM events WHERE id>?', (cursor,))[0]
            state['pending_events'] = int(row['n'] or 0)
            state['first_pending_ts'] = row['first_ts']
        return state

    def _counts(self):
        rows = self.db.rows('SELECT status,COUNT(*) n FROM event_segments GROUP BY status')
        counts = {row['status']: row['n'] for row in rows}
        return {'succeeded': counts.get('succeeded', 0), 'pending': counts.get('pending', 0), 'failed': counts.get('failed', 0)}

    def _uncompacted(self, after_id, limit_id=None):
        upper = ' AND id<=?' if limit_id is not None else ''
        params = (after_id,) + ((limit_id,) if limit_id is not None else ())
        return self.db.rows('SELECT id,ts,entity_id,domain,category,event_type,old_state,new_state,source,area,attributes_json,metadata_json FROM events WHERE id>?' + upper + ' ORDER BY id', params)

    def _state_before(self, start_id, entities):
        if not entities:
            return {}
        marks = ','.join(('?' for _ in entities))
        rows = self.db.rows(f'SELECT entity_id,new_state,MAX(id) FROM events WHERE id<? AND entity_id IN ({marks}) GROUP BY entity_id', (start_id, *entities))
        return {row['entity_id']: row['new_state'] for row in rows if row['new_state'] is not None}

    def _trigger(self, count, tokens, elapsed_minutes):
        if tokens >= self.max_tokens:
            return 'TOKEN_LIMIT'
        if count >= self.max_events:
            return 'EVENT_LIMIT'
        if elapsed_minutes >= self.max_minutes and count >= self.min_events:
            return 'TIME_LIMIT'
        return None

    async def check_and_compact(self, force_end_id=None):
        if not self.enabled or self.analyzer is None:
            return None
        async with self._run_lock:
            await self._retry_due()
            cursor = self._cursor()
            if cursor is None:
                latest = self.db.scalar('SELECT MAX(id) FROM events') or 0
                self.db.set_state(CURSOR_KEY, int(latest))
                logger.info('[SegmentCompact] cursor initialized at %s', latest)
                return None
            events = self._uncompacted(cursor, limit_id=force_end_id)
            if not events:
                return None
            covering = self.db.rows("SELECT id FROM event_segments WHERE status='pending' AND start_event_id<=? AND end_event_id>=? LIMIT 1", (cursor + 1, events[0]['id']))
            if covering:
                return None
            preprocessed = preprocess(events, range_end=events[-1]['ts'])
            tokens = conservative_token_estimate(compact_json(preprocessed.compressed))
            elapsed = (datetime.now(timezone.utc) - _parse_ts(events[0]['ts'])).total_seconds() / 60
            trigger = self._trigger(len(events), tokens, elapsed)
            if not trigger:
                return None
            return await self._compact_range(events, preprocessed, tokens, trigger)

    async def flush_before_report(self, end_id):
        if not self.enabled:
            return None
        async with self._run_lock:
            cursor = self._cursor()
            if cursor is None or cursor >= end_id:
                return None
            events = self._uncompacted(cursor, limit_id=end_id)
            if not events:
                return None
            covering = self.db.rows("SELECT id FROM event_segments WHERE status='pending' AND start_event_id<=? AND end_event_id>=? LIMIT 1", (cursor + 1, events[0]['id']))
            if covering:
                return None
            preprocessed = preprocess(events, range_end=events[-1]['ts'])
            tokens = conservative_token_estimate(compact_json(preprocessed.compressed))
            return await self._compact_range(events, preprocessed, tokens, 'BOUNDARY', summarize=False)

    def _create_segment(self, events, preprocessed, tokens):
        start_id, end_id = (events[0]['id'], events[-1]['id'])
        now = datetime.now(timezone.utc).isoformat()
        with self.db.connect() as con:
            existing = con.execute('SELECT id FROM event_segments WHERE start_event_id=? AND end_event_id=?', (start_id, end_id)).fetchone()
            if existing:
                return (existing[0], False)
            critical = detect_critical(events, preprocessed)
            cur = con.execute("INSERT INTO event_segments(period_start,period_end,start_event_id,end_event_id,event_count,estimated_input_tokens,critical_events_json,start_state_json,end_state_json,prompt_version,summary_level,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,'pending',?,?)", (events[0]['ts'], events[-1]['ts'], start_id, end_id, len(events), tokens, compact_json(critical), compact_json(preprocessed.start_state), compact_json(preprocessed.end_state), PROMPT_VERSION, 'segment', now, now))
            return (cur.lastrowid, True)

    @staticmethod
    def _is_presence_or_device(item):
        if item.get('state') == '有人':
            return True
        cls = str(item.get('class') or '')
        if cls in ('presence_raw', 'device_action'):
            return True
        kind = str(item.get('kind') or '')
        try:
            important = int(item.get('importance') or 0)
        except (TypeError, ValueError):
            important = 0
        if kind in ('state', 'interval') and important >= 20:
            return cls not in ('automation_run', 'sensor_telemetry')
        return False

    def _hybrid_ai_payload(self, segment, events, preprocessed, critical):
        items = [item for item in preprocessed.compressed if self._is_presence_or_device(item)][:80]
        entities = sorted({item.get('entity') for item in items if item.get('entity')})
        identities = {}
        if entities:
            try:
                identities = self.analyzer._identity_map(events)
            except Exception:
                identities = {}
        catalog = {eid: identities.get(eid) for eid in entities if identities.get(eid)}
        return {'note': '本段 AI 只压缩 presence 与 device_action。自动化巡检与传感器遥测不在输入里，不要编造它们。事实必须来自本输入；不确定写入 uncertainties。', 'segment_start': segment['period_start'], 'segment_end': segment['period_end'], 'raw_event_count': int(segment['event_count'] or 0), 'compressed_events': items, 'critical_events': critical[:10], 'entity_identity_catalog': catalog, 'timezone': 'Asia/Shanghai (UTC+08:00, Beijing Time)'}

    def _store_level1_slice(self, segment_id, events, preprocessed, critical=None, extra=None):
        segment = self.db.rows('SELECT * FROM event_segments WHERE id=?', (segment_id,))[0]
        if critical is None:
            critical = detect_critical(events, preprocessed)
        occupancy = [item for item in preprocessed.compressed if item.get('state') == '有人'][:15]
        automation = list(preprocessed.automation_runs or [])[:10]
        summary_text = _program_label('Level-1 切片：', segment, f'共 {int(segment['event_count'] or 0)} 条事件；占用区间 {len(occupancy)}，自动化 {len(automation)}，传感器噪声 {len(preprocessed.sensor_noise)}，关键 {len(critical)}')[:500]
        body = {'kind': 'level1_slice', 'summary': summary_text, 'occupancy': occupancy, 'automation_runs': automation, 'sensor_noise_count': len(preprocessed.sensor_noise), 'critical': critical[:10], 'event_count': int(segment['event_count'] or 0), 'ai_mode': self.ai_mode}
        if extra:
            body.update(extra)
            if extra.get('summary'):
                summary_text = str(extra['summary'])[:500]
                body['summary'] = summary_text
        model = 'level1-deterministic'
        if extra and extra.get('ai_summary'):
            local = None
            local_fn = getattr(self.analyzer, 'local_model', None)
            if callable(local_fn):
                local = local_fn() or 'local'
            model = f'{local}|hybrid'
        now = datetime.now(timezone.utc).isoformat()
        with self.db.connect() as con:
            con.execute("UPDATE event_segments SET summary_json=?, summary_text=?, status='succeeded', model=?, last_error=NULL, updated_at=? WHERE id=?", (compact_json(body), summary_text, model, now, segment_id))
        self._advance_cursor(segment['end_event_id'])
        logger.info('[SegmentCompact] level1 slice id=%s occupancy=%s automation=%s critical=%s ai=%s', segment_id, len(occupancy), len(automation), len(critical), bool(extra and extra.get('ai_summary')))
        return body

    async def _enrich_with_hybrid_ai(self, segment, events, preprocessed, critical):
        payload = self._hybrid_ai_payload(segment, events, preprocessed, critical)
        if not payload.get('compressed_events') and (not payload.get('critical_events')):
            return None
        prompt_tokens = conservative_token_estimate(self.prompt) + conservative_token_estimate(compact_json(payload))
        wall_seconds = segment_call_wall_seconds(prompt_tokens)
        started = time.perf_counter()
        try:
            async with self._model_lock:
                local = None
                local_fn = getattr(self.analyzer, 'local_model', None)
                if callable(local_fn):
                    local = local_fn() or None
                kwargs = {'model': local} if local else {}
                context_tokens = getattr(self.analyzer, 'context_tokens', None)
                if context_tokens:
                    kwargs['context_tokens'] = context_tokens
                call = self.analyzer.ollama.analyze_segment(self.prompt, payload, wall_seconds=wall_seconds, **kwargs)
                result, _meta = await asyncio.wait_for(call, timeout=wall_seconds)
            summary = validate_segment_summary(result)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning('[SegmentCompact] hybrid AI skipped id=%s error=%s duration_ms=%s wall_s=%s', segment['id'], type(error).__name__, int((time.perf_counter() - started) * 1000), wall_seconds)
            return None
        extra = {'ai_summary': summary.get('summary'), 'summary': summary.get('summary')}
        return extra

    async def _compact_range(self, events, preprocessed, tokens, trigger, summarize=True):
        segment_id, created = self._create_segment(events, preprocessed, tokens)
        logger.info('[SegmentCompact] raw=%d compressed=%d ratio=%.1f%% est_tokens=%d trigger=%s id=%s mode=%s', len(events), preprocessed.compressed_count, 100 * preprocessed.compression_ratio, tokens, trigger, segment_id, self.ai_mode)
        if not created:
            return segment_id
        if self.ai_mode == 'off':
            return self._store_level1_slice(segment_id, events, preprocessed)
        if self.ai_mode == 'hybrid':
            critical = detect_critical(events, preprocessed)
            extra = None
            self._store_level1_slice(segment_id, events, preprocessed, critical=critical)
            if summarize:
                segment = self.db.rows('SELECT * FROM event_segments WHERE id=?', (segment_id,))[0]
                extra = await self._enrich_with_hybrid_ai(segment, events, preprocessed, critical)
            return self._store_level1_slice(segment_id, events, preprocessed, critical=critical, extra=extra)
        if not summarize:
            return segment_id
        segment = self.db.rows('SELECT * FROM event_segments WHERE id=?', (segment_id,))[0]
        return await self._summarize_segment(segment)

    async def _retry_due(self):
        if self.ai_mode != 'full':
            rows = self.db.rows("SELECT * FROM event_segments WHERE status='pending' ORDER BY id LIMIT 8")
            for row in rows:
                events = self._uncompacted(row['start_event_id'] - 1, limit_id=row['end_event_id'])
                if not events:
                    with self.db.connect() as con:
                        con.execute("UPDATE event_segments SET status='succeeded', model=?, summary_text=?, updated_at=? WHERE id=?", ('level1-deterministic', 'Level-1 切片：（范围内无事件）', datetime.now(timezone.utc).isoformat(), row['id']))
                    self._advance_cursor(row['end_event_id'])
                    continue
                preprocessed = preprocess(events, range_end=row['period_end'])
                if self.ai_mode == 'hybrid':
                    critical = detect_critical(events, preprocessed)
                    self._store_level1_slice(row['id'], events, preprocessed, critical=critical)
                    extra = await self._enrich_with_hybrid_ai(row, events, preprocessed, critical)
                    self._store_level1_slice(row['id'], events, preprocessed, critical=critical, extra=extra)
                else:
                    self._store_level1_slice(row['id'], events, preprocessed)
            return
        rows = self.db.rows("SELECT id,retry_count,updated_at FROM event_segments WHERE status='pending' ORDER BY id LIMIT 4")
        for row in rows:
            backoff_minutes = min(60, 5 * 2 ** int(row['retry_count']))
            if (datetime.now(timezone.utc) - _parse_ts(row['updated_at'])).total_seconds() < backoff_minutes * 60:
                continue
            segment = self.db.rows('SELECT * FROM event_segments WHERE id=?', (row['id'],))[0]
            await self._summarize_segment(segment)

    def _segment_payload(self, segment):
        events = self._uncompacted(segment['start_event_id'] - 1, limit_id=segment['end_event_id'])
        from .event_preprocessor import detect_incomplete_entities
        preprocessed = preprocess(events, range_end=segment['period_end'], noise_entities=self.analyzer.presence_noise_entities, identities=self.analyzer._identity_map(events), suspect_entities=detect_incomplete_entities(events), non_occupancy_entities=getattr(self.analyzer, 'presence_non_occupancy_entities', frozenset()))
        state_before = self._state_before(segment['start_event_id'], preprocessed.end_state.keys())
        preprocessed.start_state = {entity: state_before[entity] for entity in preprocessed.end_state if entity in state_before}
        critical = detect_critical(events, preprocessed)
        catalog = self.analyzer._identity_catalog(events)
        protected = protected_telemetry_entities(events, critical)
        return {'segment_start': segment['period_start'], 'segment_end': segment['period_end'], 'raw_event_count': segment['event_count'], 'compressed_events': preprocessed.compressed, 'critical_events': critical[:10], 'automation_runs': preprocessed.automation_runs[:10], 'start_state': preprocessed.start_state, 'end_state': preprocessed.end_state, 'entity_identity_catalog': catalog, STABLE_ENTITIES_KEY: sorted(stable_telemetry_entities(preprocessed.compressed, protected)), PROTECTED_ENTITIES_KEY: sorted(protected), 'timezone': 'Asia/Shanghai (UTC+08:00, Beijing Time)'}

    def _bound_segment_payload(self, payload, budget_tokens=None):
        from .llm_schema import payload_token_budget
        budget = budget_tokens or payload_token_budget()
        bounded = copy.deepcopy(payload)
        project_telemetry(bounded, bounded.pop(STABLE_ENTITIES_KEY, None), bounded.pop(PROTECTED_ENTITIES_KEY, None))
        event_budget = max(200, int(budget * SEGMENT_EVENT_BUDGET_FRACTION))
        items = bounded.get('compressed_events') or []
        kept, used = ([], 0)
        for item in items:
            cost = conservative_token_estimate(compact_json(item))
            if used + cost > event_budget and kept:
                break
            kept.append(item)
            used += cost
        bounded['compressed_events'] = kept
        if len(kept) < len(items):
            bounded['compressed_events_truncated'] = {'kept': len(kept), 'dropped': len(items) - len(kept), 'note': '范围过大，摘要仅覆盖前段；未覆盖部分由报告的确定性压缩兜底'}

        def cap_mapping(value, limit):
            if not isinstance(value, dict) or len(value) <= limit:
                return (value, 0)
            keys = list(value)[:limit]
            return ({key: value[key] for key in keys}, len(value) - limit)
        state_dropped = 0
        for key in ('start_state', 'end_state'):
            capped, dropped = cap_mapping(bounded.get(key), SEGMENT_STATE_MAX_ENTITIES)
            bounded[key] = capped
            state_dropped += dropped
        catalog_dropped = 0
        capped_catalog, catalog_dropped = cap_mapping(bounded.get('entity_identity_catalog'), SEGMENT_CATALOG_MAX_ENTITIES)
        bounded['entity_identity_catalog'] = capped_catalog
        if state_dropped or catalog_dropped:
            bounded['dict_blocks_truncated'] = {'state_entities_dropped': state_dropped, 'catalog_entities_dropped': catalog_dropped, 'note': '边界状态与身份目录已按实体数封顶，避免 dict 块占满模型窗口'}
        return bounded

    def _fail(self, segment, error, duration_ms=None, wall_seconds=None, prompt_tokens=None):
        retry = int(segment['retry_count'] or 0) + 1
        now = datetime.now(timezone.utc).isoformat()
        if retry >= self.retry_limit:
            status = 'failed'
            self._advance_cursor(segment['end_event_id'])
        else:
            status = 'pending'
        with self.db.connect() as con:
            con.execute('UPDATE event_segments SET status=?, retry_count=?, last_error=?, updated_at=? WHERE id=?', (status, retry, type(error).__name__, now, segment['id']))
        logger.warning('[SegmentCompact] summary failed id=%s retry=%s/%s error=%s duration_ms=%s wall_s=%s prompt_tokens=%s', segment['id'], retry, self.retry_limit, type(error).__name__, 'n/a' if duration_ms is None else int(duration_ms), 'n/a' if wall_seconds is None else int(wall_seconds), 'n/a' if prompt_tokens is None else int(prompt_tokens))

    def _advance_cursor(self, end_id):
        cursor = self._cursor() or 0
        if end_id > cursor:
            self.db.set_state(CURSOR_KEY, int(end_id))

    def _store_deterministic(self, segment, reason, duration_ms=None, wall_seconds=None, prompt_tokens=None):
        now = datetime.now(timezone.utc).isoformat()
        summary = {'summary': _program_label('确定性压缩段：', segment, f'共 {int(segment['event_count'] or 0)} 条原始事件；（{reason}，未完成 AI 摘要，程序摘要占位；报告以确定性证据为准）')[:500], 'occupancy': [], 'movement': [], 'device_activity': [], 'automations': [], 'manual_actions': [], 'anomalies': [], 'uncertainties': [f'本段因 {reason} 未完成模型压缩，仅保留确定性时间窗与事件量']}
        with self.db.connect() as con:
            con.execute("UPDATE event_segments SET summary_json=?, summary_text=?, status='succeeded', model=?, last_error=NULL, retry_count=?, updated_at=? WHERE id=?", (compact_json(summary), summary['summary'], 'deterministic-fallback', int(segment['retry_count'] or 0) + 1, now, segment['id']))
        self._advance_cursor(segment['end_event_id'])
        logger.warning('[SegmentCompact] deterministic fallback id=%s reason=%s duration_ms=%s wall_s=%s prompt_tokens=%s', segment['id'], reason, 'n/a' if duration_ms is None else int(duration_ms), 'n/a' if wall_seconds is None else int(wall_seconds), 'n/a' if prompt_tokens is None else int(prompt_tokens))
        return summary

    def _set_segment_job(self, state):
        try:
            if state is None:
                self.db.set_state('segment_job_status', json.dumps({'status': 'idle'}, ensure_ascii=False, separators=(',', ':')))
            else:
                self.db.set_state('segment_job_status', json.dumps(state, ensure_ascii=False, separators=(',', ':')))
        except Exception:
            logger.exception('[SegmentCompact] segment job status write failed')

    async def _summarize_segment(self, segment):
        payload = self._segment_payload(segment)
        project_telemetry(payload, payload.pop(STABLE_ENTITIES_KEY, None), payload.pop(PROTECTED_ENTITIES_KEY, None))
        est_tokens = conservative_token_estimate(compact_json(payload))
        if est_tokens > self.segment_payload_hard_limit:
            now = datetime.now(timezone.utc).isoformat()
            with self.db.connect() as con:
                con.execute("UPDATE event_segments SET status='failed', last_error=?, updated_at=? WHERE id=?", ('SegmentRangeTooLarge', now, segment['id']))
            self._advance_cursor(segment['end_event_id'])
            logger.warning('[SegmentCompact] range refused id=%s est_tokens=%s limit=%s', segment['id'], est_tokens, self.segment_payload_hard_limit)
            return None
        payload = self._bound_segment_payload(payload)
        prompt_tokens = conservative_token_estimate(self.prompt) + conservative_token_estimate(compact_json(payload))
        wall_seconds = segment_call_wall_seconds(prompt_tokens)
        started = time.perf_counter()
        self._set_segment_job({'status': 'generating', 'segment_id': segment['id'], 'period_start': segment['period_start'], 'period_end': segment['period_end'], 'started_at': datetime.now(timezone.utc).isoformat(), 'source': 'segment'})
        try:
            async with self._model_lock:
                local = None
                local_fn = getattr(self.analyzer, 'local_model', None)
                if callable(local_fn):
                    local = local_fn() or None
                context_tokens = getattr(self.analyzer, 'context_tokens', None)
                kwargs = {'model': local} if local else {}
                if context_tokens:
                    kwargs['context_tokens'] = context_tokens
                call = self.analyzer.ollama.analyze_segment(self.prompt, payload, wall_seconds=wall_seconds, **kwargs)
                result, _meta = await asyncio.wait_for(call, timeout=wall_seconds)
        except asyncio.CancelledError:
            self._set_segment_job(None)
            raise
        except (TimeoutError, asyncio.TimeoutError) as error:
            self._set_segment_job(None)
            return self._store_deterministic(segment, type(error).__name__, duration_ms=(time.perf_counter() - started) * 1000, wall_seconds=wall_seconds, prompt_tokens=prompt_tokens)
        except Exception as error:
            self._set_segment_job(None)
            self._fail(segment, error, duration_ms=(time.perf_counter() - started) * 1000, wall_seconds=wall_seconds, prompt_tokens=prompt_tokens)
            return None
        try:
            summary = validate_segment_summary(result)
        except RetryableModelFormatError as error:
            self._set_segment_job(None)
            self._fail(segment, error, duration_ms=(time.perf_counter() - started) * 1000, wall_seconds=wall_seconds, prompt_tokens=prompt_tokens)
            return None
        now = datetime.now(timezone.utc).isoformat()
        model = self.analyzer.resolved_model(None)
        with self.db.connect() as con:
            con.execute("UPDATE event_segments SET summary_json=?, summary_text=?, status='succeeded', model=?, updated_at=? WHERE id=?", (compact_json(summary), summary['summary'], model, now, segment['id']))
        self._advance_cursor(segment['end_event_id'])
        self._set_segment_job(None)
        logger.info('[SegmentCompact] summary stored id=%s model=%s duration_ms=%d wall_s=%d prompt_tokens=%d', segment['id'], model, int((time.perf_counter() - started) * 1000), wall_seconds, prompt_tokens)
        return summary
