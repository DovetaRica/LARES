import asyncio
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from .guidance_retrieval import select_guidance
from .fact_queries import query_facts
from .llm_schema import compact_json
from .model_metrics import tracked_call
from .proactive_contract import SCOUT_PLAN_PROMPT, SCOUT_FACT_PROMPT, apply_guidance_scope, build_scout_payload, fit_scout_payload, validate_scout_plan
STATE_KEY = 'proactive_scout_status'
IDLE_EVIDENCE_ROW_LIMIT = 32000
FOLLOWUPS = {'routine_context': '这些跨日重复动作是否属于固定习惯，还是不同情境下的独立操作？', 'manual_context': '这些手动操作的原因是什么，是否与现有自动化或个人偏好有关？', 'automation_context': '这些自动化产生的设备动作是否符合预期，是否需要补充解释？'}

class FactQueryUnavailable(ValueError):
    pass

class PriorityWorkArrived(Exception):
    pass

class ProactiveScout:

    def __init__(self, database, analyzer, local_client, registry, settings):
        self.db, self.analyzer, self.local = (database, analyzer, local_client)
        self.registry, self.settings = (registry, settings)
        self.tz = ZoneInfo(settings.timezone)

    def _state(self):
        return self.analyzer._json_object(self.db.scalar('SELECT value FROM system_state WHERE key=?', (STATE_KEY,)))

    def _save(self, state, status, **fields):
        state.update(status=status, **fields)
        self.db.set_state(STATE_KEY, compact_json(state))

    def _priority_work(self):
        segment = self.analyzer.segment_memory
        return bool(self.registry.jobs) or self.analyzer._model_lock.has_waiters or bool(segment and segment._run_lock.locked())

    def _segment_due(self, now):
        segment = self.analyzer.segment_memory
        if segment is None or not segment.enabled:
            return False
        buffer = segment.buffer_state()
        if buffer['segments']['pending'] or buffer['pending_events'] >= segment.max_events:
            return True
        first = buffer.get('first_pending_ts')
        return bool(first and buffer['pending_events'] >= segment.min_events and (now - datetime.fromisoformat(first) >= timedelta(minutes=segment.max_minutes)))

    def _blocked(self, groups):
        blocked_ids, blocked_keys = (set(), set())
        for row in self.db.rows("SELECT evidence_json,description FROM patterns WHERE status!='obsolete'"):
            try:
                evidence = json.loads(row['evidence_json'])
            except (TypeError, ValueError):
                evidence = []
            if isinstance(evidence, list):
                for item in evidence:
                    if isinstance(item, dict):
                        blocked_ids.add(item.get('evidence_id'))
                        blocked_keys.add(item.get('semantic_key'))
            for group in groups:
                labels = (group.get('device'), group.get('name'), group.get('entity_id'))
                if any((label and len(label) > 1 and (label in row['description']) for label in labels)):
                    blocked_ids.add(group['evidence_id'])
        return (blocked_ids, blocked_keys)

    def _reviewable(self, groups, state, now):
        previous_reviews = state.get('evidence_reviews')
        reviews = {key: value for key, value in (previous_reviews.items() if isinstance(previous_reviews, dict) else ()) if key in state.get('seen_ids', []) and isinstance(value, dict)}
        state['evidence_reviews'] = reviews
        notes = {item.get('evidence_id'): item for item in state.get('findings', [])}
        ready = set()
        for group in groups:
            eid = group['evidence_id']
            if eid not in state.get('seen_ids', []):
                continue
            previous = reviews.get(eid)
            if previous is None:
                old = notes.get(eid, group)
                previous = {key: old.get(key) for key in ('count', 'local_days', 'last_ts')}
                previous.update(reviews=1, reviewed_at=old.get('created_at') or now.isoformat())
                reviews[eid] = previous
            try:
                elapsed = now - datetime.fromisoformat(previous['reviewed_at'])
                newer = datetime.fromisoformat(group['last_ts']) > datetime.fromisoformat(previous['last_ts'])
                enough = group['count'] >= previous['count'] + self.settings.min_pattern_samples and group['local_days'] > previous['local_days']
                if previous['reviews'] < 2 and elapsed >= timedelta(days=1) and newer and enough:
                    ready.add(eid)
            except (KeyError, TypeError, ValueError):
                continue
        return ready

    async def _observe(self, payload, state, now):
        planning = deepcopy(payload)
        planning['query_allowed'] = state['attempts_today'] < self.settings.proactive_max_calls_per_day
        planning['query_required'] = bool(payload.get('guidance_coverage', {}).get('omitted'))
        if planning['query_required'] and (not planning['query_allowed']):
            raise FactQueryUnavailable()
        plan = await tracked_call(self.db, 'plan_observation', planning, SCOUT_PLAN_PROMPT, self.local.model, lambda: self.local.plan_observation(planning))
        plan = validate_scout_plan(plan, planning, planning['query_allowed'])
        if plan['query'] is None:
            revisited = [item['evidence_id'] for item in plan['findings'] if item['evidence_id'] in payload.get('review_evidence_ids', [])]
            if not revisited:
                return plan['findings']
            if not planning['query_allowed']:
                raise FactQueryUnavailable()
            plan = {'query': {'tool': 'window_events', 'evidence_id': revisited[0]}, 'findings': []}
        if self._priority_work():
            raise PriorityWorkArrived()
        result = await asyncio.to_thread(query_facts, self.db.path, plan['query'], payload['evidence'], now)
        state['last_fact_query'] = {key: result[key] for key in ('tool', 'evidence_id', 'query_id', 'status', 'period_start', 'period_end')}
        self._save(state, 'generating')
        if result['status'] == 'unavailable' or (result['tool'] == 'window_events' and result['status'] != 'ok'):
            raise FactQueryUnavailable()
        followup = deepcopy(payload)
        if payload.get('review_evidence_ids') and result['tool'] == 'window_events':
            followup['evidence'] = [item for item in followup['evidence'] if item['evidence_id'] not in payload['review_evidence_ids'] or item['evidence_id'] == result['evidence_id']]
            state['considered_ids'] = [item['evidence_id'] for item in followup['evidence']]
        if planning['query_required']:
            followup['evidence'] = [item for item in followup['evidence'] if item['evidence_id'] == result['evidence_id']]
            state['considered_ids'] = [result['evidence_id']]
        if result['tool'] == 'owner_guidance' and result['status'] == 'ok':
            selected = [item for item in payload['evidence'] if item['evidence_id'] == result['evidence_id']]
            scoped = apply_guidance_scope({'evidence': selected}, result['facts']['rules'])
            if not scoped['evidence']:
                followup['evidence'] = [item for item in followup['evidence'] if item['evidence_id'] != result['evidence_id']]
        if not followup['evidence']:
            return []
        remaining = {item['evidence_id'] for item in followup['evidence']}
        for rule in followup.get('user_guidance', []):
            rule['applies_to_evidence_ids'] = [key for key in rule['applies_to_evidence_ids'] if key in remaining]
        followup['user_guidance'] = [rule for rule in followup.get('user_guidance', []) if rule['applies_to_evidence_ids']]
        followup['tool_result'] = result
        if self._priority_work():
            raise PriorityWorkArrived()
        self._save(state, 'generating', attempts_today=state['attempts_today'] + 1)
        return await tracked_call(self.db, 'select_observation_leads', followup, SCOUT_FACT_PROMPT, self.local.model, lambda: self.local.select_observation_leads(followup))

    async def run(self, now=None):
        now = (now or datetime.now(timezone.utc)).astimezone(self.tz)
        state = self._state()
        if not self.settings.proactive_enabled:
            self._save(state, 'disabled')
            return
        if not 9 <= now.hour < 21:
            self._save(state, 'outside_hours')
            return
        if state.get('day') != now.date().isoformat():
            state.update(day=now.date().isoformat(), attempts_today=0)
        if state.get('attempts_today', 0) >= self.settings.proactive_max_calls_per_day:
            self._save(state, 'daily_limit')
            return
        last = state.get('last_attempt')
        if last and now - datetime.fromisoformat(last) < timedelta(hours=2):
            self._save(state, 'cooldown')
            return
        next_period = now.replace(hour=now.hour // 6 * 6, minute=5, second=0, microsecond=0)
        if next_period <= now:
            next_period += timedelta(hours=6)
        if next_period - now <= timedelta(minutes=30):
            self._save(state, 'report_due')
            return
        lock = self.analyzer._model_lock
        if lock.locked() or self._priority_work() or self._segment_due(now) or self.analyzer.pending_period_bounds():
            self._save(state, 'busy')
            return
        try:
            groups, _ = self.analyzer._pattern_evidence(now, limit=100, max_events=IDLE_EVIDENCE_ROW_LIMIT)
        except ValueError:
            self._save(state, 'evidence_volume')
            return
        blocked_ids, blocked_keys = self._blocked(groups)
        reviewable = self._reviewable(groups, state, now)
        if self.settings.proactive_max_calls_per_day - state.get('attempts_today', 0) < 2:
            reviewable = set()
        payload = build_scout_payload(groups, blocked_ids, blocked_keys, set(state.get('seen_ids', [])) - reviewable, self.settings.min_pattern_samples)
        if not payload['evidence']:
            self._save(state, 'no_new_evidence')
            return
        guidance, coverage = select_guidance(self.analyzer._guidance('daily'), {'event_groups': payload['evidence']}, token_budget=600)
        if coverage['omitted'] and reviewable:
            payload['evidence'] = [item for item in payload['evidence'] if item['evidence_id'] not in reviewable]
            reviewable = set()
        payload = apply_guidance_scope(payload, guidance)
        payload['guidance_coverage'] = {key: coverage[key] for key in ('available', 'selected', 'omitted')}
        if not payload['evidence']:
            self._save(state, 'no_new_evidence')
            return
        payload['date'] = now.date().isoformat()
        payload = fit_scout_payload(payload)
        if payload is None:
            self._save(state, 'input_too_large')
            return
        payload['review_evidence_ids'] = [item['evidence_id'] for item in payload['evidence'] if item['evidence_id'] in reviewable]
        if payload.get('guidance_coverage', {}).get('omitted') and self.settings.proactive_max_calls_per_day - state.get('attempts_today', 0) < 2:
            self._save(state, 'query_budget')
            return
        if lock.locked() or self._priority_work():
            self._save(state, 'busy')
            return
        async with lock:
            state.update(attempts_today=state.get('attempts_today', 0) + 1, last_attempt=now.isoformat(), started_at=now.isoformat(), model=self.local.model, source='proactive', error_type=None)
            state.pop('last_fact_query', None)
            state.pop('considered_ids', None)
            self._save(state, 'generating')
            call = asyncio.create_task(asyncio.wait_for(self._observe(payload, state, now), timeout=180))
            try:
                while not call.done():
                    await asyncio.wait({call}, timeout=1)
                    if self._priority_work():
                        call.cancel()
                        try:
                            await call
                        except asyncio.CancelledError:
                            pass
                        self._save(state, 'yielded')
                        return
                findings = await call
            except asyncio.CancelledError:
                self._save(state, 'interrupted')
                raise
            except PriorityWorkArrived:
                self._save(state, 'yielded')
                return
            except Exception as error:
                self._save(state, 'failed', error_type=type(error).__name__)
                return
            finally:
                if not call.done():
                    call.cancel()
                    try:
                        await call
                    except asyncio.CancelledError:
                        pass
            by_id = {item['evidence_id']: item for item in payload['evidence']}
            blocked_ids, blocked_keys = self._blocked(payload['evidence'])
            notes = []
            for item in findings:
                evidence = by_id[item['evidence_id']]
                if evidence['evidence_id'] in blocked_ids or evidence['semantic_key'] in blocked_keys:
                    continue
                notes.append({key: evidence.get(key) for key in ('evidence_id', 'device', 'name', 'entity_id', 'area', 'source', 'state', 'count', 'local_days', 'first_ts', 'last_ts', 'lookback_days')})
                notes[-1].update(focus=item['focus'], followup=FOLLOWUPS[item['focus']], created_at=now.isoformat(), status='unconfirmed')
            seen = list(dict.fromkeys([*state.get('seen_ids', []), *state.get('considered_ids', by_id)]))[-128:]
            reviews = state.setdefault('evidence_reviews', {})
            for eid in state.get('considered_ids', by_id):
                if eid not in by_id:
                    continue
                previous = reviews.get(eid, {})
                reviews[eid] = {key: by_id[eid].get(key) for key in ('count', 'local_days', 'last_ts')}
                reviews[eid].update(reviews=min(2, previous.get('reviews', 0) + 1), reviewed_at=now.isoformat())
            state['evidence_reviews'] = {eid: reviews[eid] for eid in seen if eid in reviews}
            old_notes = state.get('findings', [])
            updated = {item['evidence_id'] for item in notes}
            self._save(state, 'succeeded', last_completed=datetime.now(timezone.utc).isoformat(), seen_ids=seen, findings=(notes + [item for item in old_notes if item.get('evidence_id') not in updated])[:12], findings_added=sum((item['evidence_id'] not in {old.get('evidence_id') for old in old_notes} for item in notes)), findings_reviewed=len(updated & reviewable))
